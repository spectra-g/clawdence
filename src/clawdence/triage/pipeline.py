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

**Publication is a durable external effect.** The immutable command is recorded
before the first forge write. Delivery is claimed separately from workflow
execution, so a crash resumes Git work without dispatching the coding agent.
"""

from __future__ import annotations

import secrets as _secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.errors import PortError
from clawdence.ports.runner import RunnerPort
from clawdence.ports.vcs import PullRequest, VcsPort
from clawdence.runners import Dispatch, RunnerHandler
from clawdence.store import (
    DEFAULT_LEASE_SECONDS,
    EffectKind,
    EffectState,
    ExternalEffect,
    ExternalEffects,
    Intake,
    Publication,
    Publications,
    SqliteLedger,
    StateStore,
    new_effect_id,
)
from clawdence.triage.config import ConfigError, Deployment
from clawdence.triage.effects import PublicationEffectHandler, PublishPullRequestCommand
from clawdence.triage.routing import Decision, Routed, route
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

    #: Delivery is independent of the terminal workflow status.
    effect_id: str | None = None
    delivery_state: EffectState | None = None

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

    #: Stable for this drainer process; claims from a dead process expire.
    effect_owner: str = field(default_factory=lambda: f"work.{_secrets.token_hex(6)}")
    effect_lease_seconds: float = DEFAULT_LEASE_SECONDS
    effect_clock: Clock = utc_now

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

    async def resume_publications(
        self, *, ref: str | None = None, limit: int | None = None
    ) -> tuple[Outcome, ...]:
        """Drain due publication effects without dispatching the agent again.

        An intent is written before the first remote side effect. Branch creation,
        pushing the same hash and opening-or-finding a pull request are all
        idempotent, so a process may die after any one of them and this method can
        safely continue on the next ``work`` invocation.
        """
        outcomes: list[Outcome] = []
        outcomes.extend(await self._resume_legacy_publications(ref=ref, limit=limit))
        if limit is not None and len(outcomes) >= limit:
            return tuple(outcomes)
        queue = self._effects()
        remaining = None if limit is None else limit - len(outcomes)
        due = queue.due(limit=None if ref is not None else remaining)
        intake = Intake(self.store)
        for effect in due:
            if effect.kind != EffectKind.PUBLISH_PULL_REQUEST:
                claimed = queue.claim(
                    effect.id,
                    owner=self.effect_owner,
                    lease_seconds=self.effect_lease_seconds,
                )
                if claimed is not None:
                    queue.park(
                        claimed.id,
                        owner=self.effect_owner,
                        error_kind="unsupported-effect-kind",
                        error_detail=f"no handler is installed for {claimed.kind!r}",
                    )
                continue
            try:
                command = PublishPullRequestCommand.model_validate(effect.command)
            except ValueError:
                command = None
            admission = None if command is None else intake.for_work_item(command.work_item_id)
            if admission is None:
                claimed = queue.claim(
                    effect.id,
                    owner=self.effect_owner,
                    lease_seconds=self.effect_lease_seconds,
                )
                if claimed is not None:
                    queue.park(
                        claimed.id,
                        owner=self.effect_owner,
                        error_kind=(
                            "invalid-effect-command" if command is None else "work-item-not-found"
                        ),
                        error_detail=(
                            "the publication command is invalid"
                            if command is None
                            else f"no intake row exists for {command.work_item_id!r}"
                        ),
                    )
                continue
            item = admission.item
            if ref is not None and item.source_ref.external_id != ref:
                continue
            if limit is not None and len(outcomes) >= limit:
                break
            claimed = queue.claim(
                effect.id,
                owner=self.effect_owner,
                lease_seconds=self.effect_lease_seconds,
            )
            if claimed is None:
                continue
            report = self._stored_report(claimed)
            routed = self._stored_routing(item, claimed, report)
            outcomes.append(
                await self._deliver_publication(
                    claimed,
                    item=item,
                    routed=routed,
                    report=report,
                )
            )
        return tuple(outcomes)

    async def _resume_legacy_publications(
        self, *, ref: str | None, limit: int | None
    ) -> tuple[Outcome, ...]:
        """Drain migration 4 rows written before generic effects existed.

        New code never enqueues here. Keeping this narrow reader is what makes
        upgrading with a commit waiting to publish safe instead of silently
        abandoning the obligation in an old table.
        """
        outcomes: list[Outcome] = []
        legacy = Publications(self.store)
        for publication in legacy.pending(limit=None if ref is not None else limit):
            item = publication.work_item
            if ref is not None and item.source_ref.external_id != ref:
                continue
            if limit is not None and len(outcomes) >= limit:
                break
            report = self._legacy_report(publication)
            routed = self._legacy_routing(item, publication, report)
            base = Outcome(
                item_id=item.id,
                routed=routed,
                run_id=publication.run_id,
                report=report,
            )
            try:
                profile = self.deployment.profile(publication.repo_id)
                inspect_path = self.repos.mirror(profile)
                legacy.attempting(publication.run_id)
                findings = await audit(
                    inspect_path,
                    publication.base_commit,
                    publication.head_commit,
                )
                if findings:
                    refusal = (
                        "this legacy branch is not reviewable as it stands, so nothing was "
                        "published:\n  " + "\n  ".join(finding.describe() for finding in findings)
                    )
                    legacy.refused(publication.run_id, refusal)
                    outcomes.append(replace(base, findings=findings, refusal=refusal))
                    continue
                await self.vcs.create_branch(
                    profile.id,
                    publication.branch,
                    from_commit=publication.head_commit,
                )
                await self.vcs.push(
                    profile.id,
                    publication.branch,
                    worktree_path=str(inspect_path),
                    expect_commit=publication.head_commit,
                )
                pull = await self.vcs.open_pull_request(
                    profile.id,
                    title=item.title,
                    body=render_body(
                        _summary(item, routed, report),
                        template=await read_template(
                            self.repos,
                            profile,
                            publication.base_commit,
                        ),
                    ),
                    head_branch=publication.branch,
                    base_branch=profile.default_branch,
                    work_item_id=item.id,
                    policy=profile.pull_request,
                )
            except (ConfigError, GitError, OSError, PortError) as exc:
                refusal = f"legacy publication is still queued for retry: {exc}"
                legacy.failed(publication.run_id, refusal)
                outcomes.append(replace(base, refusal=refusal))
                continue
            legacy.published(publication.run_id)
            outcomes.append(replace(base, pull_request=pull))
        return tuple(outcomes)

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

        try:
            findings = await audit(lease.path, lease.base_commit, head)
        except (GitError, OSError) as exc:
            return replace(
                base,
                refusal=(
                    f"the completed run could not publish because its diff could not be read: {exc}"
                ),
            )
        if findings:
            refusal = (
                "this branch is not reviewable as it stands, so nothing was "
                "published:\n  " + "\n  ".join(finding.describe() for finding in findings)
            )
            return replace(base, findings=findings, refusal=refusal)

        body = render_body(
            _summary(item, routed, report),
            template=await read_template(self.repos, profile, lease.base_commit),
        )
        command = PublishPullRequestCommand(
            repository_id=profile.id,
            work_item_id=item.id,
            branch=lease.branch,
            base_commit=lease.base_commit,
            head_commit=head,
            title=item.title,
            body=body,
            base_branch=profile.default_branch,
            policy=profile.pull_request,
        )
        effect = self._effects().enqueue(
            effect_id=new_effect_id(),
            idempotency_key=(f"publish-pull-request:{lease.run_id}:{lease.branch}:{head}"),
            run_id=lease.run_id,
            kind=EffectKind.PUBLISH_PULL_REQUEST,
            command=command.model_dump(mode="json"),
        )
        claimed = self._effects().claim(
            effect.id,
            owner=self.effect_owner,
            lease_seconds=self.effect_lease_seconds,
        )
        if claimed is None:
            current = self._effects().require(effect.id)
            return replace(
                base,
                effect_id=current.id,
                delivery_state=current.state,
                refusal=self._delivery_refusal(current),
            )
        return await self._deliver_publication(
            claimed,
            item=item,
            routed=routed,
            report=report,
            inspect_path=lease.path,
        )

    async def _deliver_publication(
        self,
        effect: ExternalEffect,
        *,
        item: WorkItem,
        routed: Routed,
        report: RunReport | None,
        inspect_path: Path | None = None,
    ) -> Outcome:
        """One claimed, idempotent attempt at a recorded publication."""
        base = Outcome(
            item_id=item.id,
            routed=routed,
            run_id=effect.run_id,
            report=report,
            effect_id=effect.id,
            delivery_state=EffectState.DELIVERING,
        )
        pull = await PublicationEffectHandler(
            deployment=self.deployment,
            effects=self._effects(),
            repos=self.repos,
            vcs=self.vcs,
            owner=self.effect_owner,
        ).deliver(effect, inspect_path=inspect_path)
        settled = self._effects().require(effect.id)
        if pull is not None:
            return replace(
                base,
                pull_request=pull,
                delivery_state=EffectState.DELIVERED,
            )
        return replace(
            base,
            delivery_state=settled.state,
            refusal=self._delivery_refusal(settled),
        )

    @staticmethod
    def _delivery_refusal(effect: ExternalEffect) -> str:
        command = PublishPullRequestCommand.model_validate(effect.command)
        head = str(command.head_commit)[:12]
        if effect.state is EffectState.PARKED:
            return (
                f"the run produced {head}, but publication is parked after "
                f"{effect.attempts} attempt(s): {effect.error_kind}: {effect.error_detail}"
            )
        if effect.state is EffectState.DELIVERING:
            return f"the run produced {head} and publication is claimed by another drainer"
        return (
            f"the run produced {head} and publication is queued for retry at "
            f"{effect.next_attempt_at.isoformat(timespec='seconds')}: "
            f"{effect.error_kind}: {effect.error_detail}"
        )

    # --------------------------------------------------------------- plumbing

    def _effects(self) -> ExternalEffects:
        return ExternalEffects(self.store, clock=self.effect_clock)

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

    def _stored_report(self, effect: ExternalEffect) -> RunReport | None:
        run = self.store.require_run(effect.run_id)
        try:
            workflow = self._workflow(run.workflow)
        except WorkflowLoadError:
            return None
        attempts = self.store.steps_for(effect.run_id)
        final = {result.stage_id: result for result in attempts}
        return RunReport(
            run=run,
            workflow=workflow,
            attempts=attempts,
            final=final,
        )

    def _stored_routing(
        self, item: WorkItem, effect: ExternalEffect, report: RunReport | None
    ) -> Routed:
        command = PublishPullRequestCommand.model_validate(effect.command)
        run = self.store.require_run(effect.run_id)
        current = route(
            item,
            profiles=self.deployment.profiles,
            policy=self.deployment.config.routing,
        )
        return replace(
            current,
            workflow=Decision(
                report.workflow.name if report is not None else run.workflow,
                "pinned by the completed run",
            ),
            repo=Decision(command.repository_id, "pinned by the publication command"),
        )

    def _legacy_report(self, publication: Publication) -> RunReport:
        run = self.store.require_run(publication.run_id)
        attempts = self.store.steps_for(publication.run_id)
        return RunReport(
            run=run,
            workflow=publication.workflow,
            attempts=attempts,
            final={result.stage_id: result for result in attempts},
        )

    def _legacy_routing(
        self, item: WorkItem, publication: Publication, report: RunReport
    ) -> Routed:
        current = route(
            item,
            profiles=self.deployment.profiles,
            policy=self.deployment.config.routing,
        )
        return replace(
            current,
            workflow=Decision(report.workflow.name, "pinned by the completed legacy run"),
            repo=Decision(publication.repo_id, "pinned by the legacy publication intent"),
        )


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
