"""A request in, a pull request out. The last vertebra of the walking skeleton.

Every piece of this was built and tested before S11 and nothing joined them, so
what is new here is the *order*, and the order is the content. Read
``tests/vcs/test_pipeline`` next to this: S15 wrote the sequence out by hand
because nothing in the product performed it, and this is that sequence with the
two decisions S15 could not make — which repository, which workflow — filled in.

    route → check the repository's policy → acquire a worktree → wire the
    registry → execute → audit the diff → push → open the pull request → release

Six of those steps are somebody else's code called once. The interesting content
is entirely in what happens when one of them says no, so that is what the rest of
this docstring is about.

**Refusals are ordered by what they cost.** Routing refuses before a file is
opened, a missing runner refuses before the policy check spends a call to the
forge, the policy check refuses before a checkout, and the diff audit refuses
before anything is published — so a repository that requires signed commits is
turned away before an agent step, a container or a test suite has been paid for,
which is what "fail at configuration time, not at merge time" meant. Reversing
any two of those would still be correct and would cost money to discover.

**Nothing is published unless the branch moved.** A workflow with no runner step
— ``spike`` — never touches the data plane, so it is never given a checkout at
all: there is no branch, and therefore no empty pull request. A workflow that did
run the data plane and produced nothing is the same answer arrived at
differently, and both are reported as a run with no pull request rather than as a
failure, because "there was nothing to do" is a legitimate outcome that S6's
``EMPTY_DIFF`` already names.

**The worktree is released in a ``finally`` and the release is conservative.**
``WorktreeManager.release`` deletes the branch only when it can prove nothing was
committed on it; between the agent committing and the push succeeding the local
branch is the only copy of the run's work. Nothing here overrides that.

**A halted run still publishes.** If the runner produced a commit and a *later*
stage failed — a review step, a verification — the branch is pushed and the pull
request is opened anyway, marked with what went wrong. The alternative is
throwing away work that exists because the opinion about it was unfavourable,
and the whole premise (§1.3) is that an agent's product is a proposal entering
the normal review path. A human reads the pull request; the run record says the
review failed.
"""

from __future__ import annotations

import secrets as _secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from clawdence.domain import (
    Actor,
    ActorKind,
    ContractKind,
    EventKind,
    RepoProfile,
    StepType,
    VerificationContract,
    Workflow,
    WorkItem,
)
from clawdence.engine import (
    HandlerRegistry,
    RunReport,
    StepHandler,
    WorkflowLoadError,
    default_registry,
    execute,
    load_workflow,
)
from clawdence.ports.errors import PortError
from clawdence.ports.runner import RunnerPort
from clawdence.ports.vcs import PullRequest, VcsPort
from clawdence.runners import Dispatch, RunnerHandler
from clawdence.store import Intake, SqliteLedger, StateStore
from clawdence.triage.config import ConfigError, Deployment
from clawdence.triage.routing import Routed, route
from clawdence.vcs import (
    Finding,
    PolicyRefused,
    RepoStore,
    Violation,
    Worktree,
    WorktreeManager,
    audit,
    read_template,
    refuse_if_blocking,
    render_body,
)
from clawdence.vcs.git import GitError


@runtime_checkable
class PolicyChecking(Protocol):
    """A ``VcsPort`` that can also be asked about the repository's settings.

    Structural rather than nominal so that a test double opts in by having the
    method, and a fake that does not is simply not asked — which is the right
    answer for ``InMemoryVcs``, whose repositories have no settings to report.
    """

    async def check_policy(self, profile: RepoProfile) -> tuple[Violation, ...]: ...


#: The plan a runner stage is given when neither it nor a workflow says. The
#: request itself, which is the honest default: a stage that declared no plan is
#: one whose workflow has no planning step, and the thing that was asked for is
#: the only description of the work that exists.
DEFAULT_PLAN_TEMPLATE = "${request.json.text}"

#: What a run's verification contract is at M1. ``TEST_AFTER`` rather than
#: ``OUTSIDE_IN_TDD`` because choosing per work item is S13's, and this is the
#: weakest contract that still requires evidence — a run whose tests did not pass
#: does not report success.
DEFAULT_CONTRACT = VerificationContract(kind=ContractKind.TEST_AFTER)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the pipeline did with one work item.

    ``refusal`` and ``report`` are not alternatives. A run can execute, halt, and
    still be refused publication by the diff audit — three facts, and collapsing
    them into a status would lose the one an operator needs.
    """

    item_id: str
    routed: Routed

    run_id: str | None = None
    report: RunReport | None = None
    pull_request: PullRequest | None = None

    #: Why this went no further. ``None`` when nothing stopped it.
    refusal: str | None = None

    #: What the diff audit found, when it is the reason nothing was published.
    findings: tuple[Finding, ...] = ()

    @property
    def published(self) -> bool:
        return self.pull_request is not None

    @property
    def succeeded(self) -> bool:
        """Did the process complete as written?

        Deliberately not "is there a pull request". A ``spike`` opens none and
        succeeds; a run whose review stage failed opens one and did not.
        """
        return self.refusal is None and self.report is not None and self.report.succeeded


@dataclass(slots=True)
class Pipeline:
    """Runs work items to completion against a configured deployment.

    Everything is injected. The pipeline never reads the configuration file, opens
    a socket or constructs an adapter — ``wiring`` does that — which is what lets
    the tests below it drive the whole sequence against a bare git repository and
    a fake forge without a config file existing.
    """

    deployment: Deployment
    store: StateStore
    repos: RepoStore
    worktrees: WorktreeManager
    vcs: VcsPort

    #: Absent means ``runner`` steps refuse, naming the configuration key. See
    #: ``wiring.runner`` for why substituting a default would be the wrong
    #: kindness.
    runner: RunnerPort | None = None

    #: Extra handlers merged into every run's registry — the agent handler, which
    #: needs a model provider the CLI resolves from the environment. Passed as a
    #: mapping rather than a keyword per type so that S17's approval handler
    #: arrives without reopening this signature.
    handlers: Mapping[StepType, StepHandler] = field(default_factory=dict)

    environ: Mapping[str, str] | None = None

    async def start(self, item: WorkItem, *, run_id: str | None = None) -> Outcome:
        """Route one work item and carry it as far as it goes.

        Never raises for an ordinary refusal — a request that cannot be routed, a
        repository that cannot be worked on, a workflow file with a typo in it are
        all *results*, and a caller draining an inbox has to be able to record one
        and move to the next item. Genuine faults (a database that will not open)
        still propagate.
        """
        routed = route(
            item,
            profiles=self.deployment.profiles,
            policy=self.deployment.config.routing,
        )
        self._record(item, routed)

        if not routed.repo.resolved:
            return Outcome(item_id=item.id, routed=routed, refusal=routed.repo.reason)
        if not routed.workflow.resolved:  # pragma: no cover - always resolves today
            return Outcome(item_id=item.id, routed=routed, refusal=routed.workflow.reason)

        try:
            workflow = self._workflow(routed.workflow.value or "")
            profile = self.deployment.profile(routed.repo.value or "")
        except (ConfigError, WorkflowLoadError) as exc:
            return Outcome(item_id=item.id, routed=routed, refusal=str(exc))

        return await self._execute(item, routed, workflow, profile, run_id=run_id)

    # ------------------------------------------------------------------ steps

    async def _execute(
        self,
        item: WorkItem,
        routed: Routed,
        workflow: Workflow,
        profile: RepoProfile,
        *,
        run_id: str | None,
    ) -> Outcome:
        needs_worktree = any(stage.type is StepType.RUNNER for stage in workflow.stages)

        if needs_worktree and self.runner is None:
            # Cheaper than the policy check below, and checked first: no I/O at
            # all, versus a call to the forge. A workflow with a runner step and
            # no runner configured is refused here, before a worktree is
            # acquired for it — not discovered several steps later as a
            # step-type-not-implemented failure once the checkout already
            # happened for nothing.
            return Outcome(
                item_id=item.id,
                routed=routed,
                refusal=(
                    f"{routed.workflow.value!r} has a runner step, but this deployment has no "
                    "`runner:` section configured — refusing before a worktree is acquired. "
                    "Add one to config.yaml (see USER-GUIDE.md §3), or route this to a "
                    "workflow with no runner step."
                ),
            )

        if needs_worktree:
            # Before the checkout, because a repository that cannot be worked on
            # as configured should cost nothing to discover. ``check_policy``
            # degrades to "unknown" rather than failing when the token cannot
            # read a setting, so this refuses on what it *knows*.
            try:
                refuse_if_blocking(profile, await self._policy(profile))
            except (PolicyRefused, PortError) as exc:
                return Outcome(item_id=item.id, routed=routed, refusal=str(exc))

        identifier = run_id or f"run.{_secrets.token_hex(6)}"
        lease: Worktree | None = None
        try:
            if needs_worktree:
                try:
                    lease = await self.worktrees.acquire(
                        profile,
                        run_id=identifier,
                        work_item_id=item.id,
                        title=item.title,
                    )
                except PortError as exc:
                    return Outcome(item_id=item.id, routed=routed, refusal=str(exc))

            report = await execute(
                workflow,
                run_id=identifier,
                work_item_id=item.id,
                registry=self._registry(item, profile, lease),
                ledger=SqliteLedger(self.store, run_id=identifier),
                request=_request(item, routed),
            )
            self._attach_repo(identifier, routed)

            if lease is None:
                return Outcome(item_id=item.id, routed=routed, run_id=identifier, report=report)
            return await self._publish(item, routed, profile, lease, report)
        finally:
            if lease is not None:
                await self.worktrees.release(lease)

    async def _publish(
        self,
        item: WorkItem,
        routed: Routed,
        profile: RepoProfile,
        lease: Worktree,
        report: RunReport,
    ) -> Outcome:
        """Push what the run committed and open the pull request for it.

        The head is read from the *mirror*, not from the runner's report. A
        runner's ``tree_hash`` is output from the data plane and
        ``domain.runner`` says the control plane does not act on one without
        checking; here the check is free, because the branch is a local ref and
        reading it is one ``rev-parse``.
        """
        head = await self._head(lease)
        base = Outcome(item_id=item.id, routed=routed, run_id=lease.run_id, report=report)
        if head is None or head == lease.base_commit:
            return base

        findings = await audit(lease.path, lease.base_commit, head)
        if findings:
            return Outcome(
                item_id=item.id,
                routed=routed,
                run_id=lease.run_id,
                report=report,
                findings=findings,
                refusal=(
                    "this branch is not reviewable as it stands, so nothing was "
                    "published:\n  " + "\n  ".join(finding.describe() for finding in findings)
                ),
            )

        try:
            await self.vcs.create_branch(profile.id, lease.branch, from_commit=lease.base_commit)
            await self.vcs.push(
                profile.id,
                lease.branch,
                worktree_path=str(lease.path),
                expect_commit=head,
            )
            pull = await self.vcs.open_pull_request(
                profile.id,
                title=item.title,
                body=render_body(
                    _summary(item, routed, report),
                    template=await read_template(self.repos, profile, lease.base_commit),
                ),
                head_branch=lease.branch,
                base_branch=profile.default_branch,
                work_item_id=item.id,
                policy=profile.pull_request,
            )
        except PortError as exc:
            # The work is committed on a local branch that ``release`` will not
            # delete, because it moved. Reported rather than raised for the same
            # reason: a failed push is a thing to look at, not a thing to lose.
            return Outcome(
                item_id=item.id,
                routed=routed,
                run_id=lease.run_id,
                report=report,
                refusal=f"the run produced {head[:12]} and it could not be published: {exc}",
            )

        return Outcome(
            item_id=item.id,
            routed=routed,
            run_id=lease.run_id,
            report=report,
            pull_request=pull,
        )

    # --------------------------------------------------------------- plumbing

    def _workflow(self, name: str) -> Workflow:
        return load_workflow(self.deployment.workflow_path(name))

    async def _policy(self, profile: RepoProfile) -> tuple[Violation, ...]:
        """What the forge says about this repository, or nothing.

        ``check_policy`` is not on ``VcsPort`` and should not be: it is the one
        operation that asks the *forge* about its own settings rather than about
        refs, and a port method every adapter had to implement would make the
        in-memory fake answer a question it has no way to know. So it is an
        optional capability, tested for structurally.
        """
        if not isinstance(self.vcs, PolicyChecking):
            return ()
        return await self.vcs.check_policy(profile)

    def _registry(
        self, item: WorkItem, profile: RepoProfile, lease: Worktree | None
    ) -> HandlerRegistry:
        """Script always, agent if one was supplied, runner if there is a checkout.

        The runner handler is built per run because a ``Dispatch`` names *this*
        run's worktree. That is the same reason S6 took one as data instead of
        assembling it: everything on it is a decision some other step owns, and
        this is the step that owns the last of them.
        """
        extra = dict(self.handlers)
        runner_handler = None
        if lease is not None and self.runner is not None:
            runner_handler = RunnerHandler(
                runner=self.runner,
                dispatch=Dispatch.for_worktree(
                    lease,
                    profile,
                    work_item_id=item.id,
                    contract=DEFAULT_CONTRACT,
                    trusted_provenance=item.submitter.trusted,
                ),
                plan_template=DEFAULT_PLAN_TEMPLATE,
            )
        return default_registry(
            self.environ,
            agent=extra.get(StepType.AGENT),
            runner=runner_handler,
            approval=extra.get(StepType.APPROVAL),
        )

    async def _head(self, lease: Worktree) -> str | None:
        """What the run's branch points at now, or ``None`` if it never existed.

        A branch that does not exist is the ordinary outcome of a run that
        reached no runner step, so it is an answer rather than an error.
        """
        try:
            return await self.repos.git(
                lease.mirror, "rev-parse", "--verify", f"refs/heads/{lease.branch}"
            )
        except (GitError, OSError):
            return None

    def _record(self, item: WorkItem, routed: Routed) -> None:
        """The ``WORK_ITEM_ROUTED`` event, which is why that kind exists.

        Written whether or not the routing succeeded. An unrouted request is the
        case somebody comes back to ask about, and a log that only recorded the
        successes would have nothing to say about it.
        """
        self.store.audit.record(
            EventKind.WORK_ITEM_ROUTED,
            at=_now(),
            work_item_id=item.id,
            actor=Actor(kind=ActorKind.SYSTEM, id="triage"),
            payload=routed.payload(),
        )

    def _attach_repo(self, run_id: str, routed: Routed) -> None:
        """Write the routed repository onto the run row.

        ``Run.repo_id`` has been on the record since S2 and nothing has ever set
        it, because nothing chose a repository. This is the line that was
        missing, and it is what lets a run answer "which repository was that
        against" months later.

        The *status* is left alone. The ledger closed the run when ``execute``
        returned, and a second writer restating it would be two answers to one
        question — the ordering bug that ``update_run``'s version column exists
        to make loud rather than silent.
        """
        repo_id = routed.repo.value
        self.store.update_run(run_id, lambda run: run.model_copy(update={"repo_id": repo_id}))


def acknowledge(intake: Intake, outcome: Outcome) -> None:
    """Mark a request handled — but only if something actually took it on.

    An item the pipeline refused stays ``pending``, which is the whole point of
    the state: acknowledging a request nobody could route would take it out of
    the queue and leave a person waiting on work that will never start. That is
    v1's ``sessions.json`` bug, which ``Intake.unacknowledge`` exists to undo.
    """
    if outcome.run_id is not None:
        intake.acknowledge(outcome.item_id)


def _request(item: WorkItem, routed: Routed) -> JsonValue:
    """The work item as a workflow sees it: ``${request.json.…}``.

    A projection, not the model. ``raw_text`` is here because the process needs
    what was asked for; the submitter's identity is reduced to a display name and
    a trust flag because a workflow branching on who asked is a thing to design
    (S17's approver identity), not a thing to leave open by handing over the whole
    record.
    """
    return {
        "id": item.id,
        "type": routed.item_type.value,
        "title": item.title,
        "text": item.raw_text,
        "labels": list(item.labels),
        "repo": routed.repo.value,
        "workflow": routed.workflow.value,
        "source": item.source_ref.source.value,
        "url": item.source_ref.url,
        "submitter": item.submitter.display_name or item.submitter.external_id,
        "trusted": item.submitter.trusted,
    }


def _summary(item: WorkItem, routed: Routed, report: RunReport) -> str:
    """The part of the pull request body this system writes.

    Deliberately short and factual. Everything in it is a fact about the run —
    what was asked, which process ran, what failed — and none of it is a claim
    about the change, because a description of a diff written by the thing that
    produced the diff is not evidence a reviewer can use.
    """
    lines = [
        f"Requested by **{item.submitter.display_name or item.submitter.external_id}** "
        f"via {item.source_ref.source.value}.",
        "",
        f"- work item: `{item.id}`",
        f"- workflow: `{report.workflow.name}@{report.workflow.version}` "
        f"({routed.workflow.reason})",
        f"- run: `{report.run.id}`",
    ]
    if item.source_ref.url:
        lines.append(f"- source: {item.source_ref.url}")
    failed = report.failed_stages
    if failed:
        lines += [
            "",
            f"⚠️ This run did not complete cleanly — {', '.join(failed)} failed. "
            f"The work is here for review rather than because it was approved.",
        ]
    lines += ["", "> Opened by clawdence. Review it as you would any other proposal."]
    return "\n".join(lines)


def _now() -> datetime:
    return datetime.now(UTC)


def workflow_names(deployment: Deployment) -> tuple[str, ...]:
    """Workflow files this deployment can route to, by name.

    A read for the CLI, and it exists so an unroutable ``workflow_override`` can
    be answered with the list rather than with a missing file. ``routing`` does
    not use it — see ``routing._workflow`` on why that module does not list a
    directory.
    """
    directory: Path = deployment.config.paths.workflows
    if not directory.is_dir():
        return ()
    return tuple(sorted(path.stem for path in directory.glob("*.yaml")))
