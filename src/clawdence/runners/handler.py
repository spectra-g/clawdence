"""The ``runner`` step type, at last: a stage that dispatches through the port.

The engine owns control flow and knows nothing about step types; this is the
other half of that split, and it is the piece S3 left a hole for. What it adds
over "call the port and unwrap the answer" is the mapping from
``RunnerOutcome`` to what the engine does next, and that mapping is the whole
reason the taxonomy has fifteen values.

**Which failures are worth a second attempt.** Failing tests are — that is the
loop the whole system is built around. A timeout might be. A budget cap is not:
retrying it spends the rest of the budget more slowly rather than differently.
An OOM kill is not, because nothing about the second attempt is smaller. An
empty diff is not, because the agent read the plan and concluded there was
nothing to do, and asking again gets the same conclusion at the same price.
``BLOCKED`` is not, and it is the reason that value exists: an agent stopped by a
missing dependency, retried three times, is v1's budget being spent to learn that
the dependency is still missing.

A handler that mapped every outcome to "failed" would make the enum decorative,
and that is precisely what v1 did.

**What is deliberately not here.** Where the worktree comes from, which repo the
work belongs to, and which branch it is on. Those are triage's and the pipeline's
(S11, S15): this takes a ``Dispatch`` describing them, because a handler that
invented them would be quietly making decisions that belong to steps that have
not been built.

S15 has since built three of the four. ``Dispatch.for_worktree`` turns a checkout
``clawdence.vcs`` handed out into the record this handler wants, so the worktree
path, the branch and the base commit now have a real source. What is still
missing — and why ``default_registry`` still refuses ``runner`` steps out of the
box — is *which repository* a work item belongs to, which is S11's.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import JsonValue

from clawdence.domain import (
    Budget,
    RepoProfile,
    RunnerOutcome,
    RunnerRequest,
    RunnerStage,
    VerificationContract,
)
from clawdence.engine import HandlerOutcome, StepContext, StepFailure, idempotency_key
from clawdence.engine.errors import InterpolationError
from clawdence.engine.interpolation import expand
from clawdence.ports import PortError, RunnerPort
from clawdence.vcs.worktrees import Worktree

#: Outcomes a second attempt could plausibly change. Everything absent from this
#: set halts the run to a human, which is what ``on_exhausted`` means one step
#: earlier: nothing here force-proceeds, and there is no value that expresses
#: "give up and merge anyway".
#:
#: S6b's three — ``PROVIDER_ERROR``, ``DROPPED_COMMIT``, ``NO_MODEL_RESPONSE`` —
#: are **deliberately absent**, and that is a decision rather than an oversight.
#: What is worth another attempt is S13's question, and the safe answer until it
#: is asked properly is the one that stops: a rejected credential and an exhausted
#: balance are both things a retry re-discovers at full price, which is the exact
#: failure mode ``BLOCKED`` was added for. ``DROPPED_COMMIT`` is the arguable one
#: — a second attempt might well commit — and it is left out for consistency
#: rather than conviction.
RETRYABLE: frozenset[RunnerOutcome] = frozenset(
    {
        RunnerOutcome.TESTS_FAILED,
        RunnerOutcome.TIMED_OUT,
        RunnerOutcome.STARTUP_FAILED,
        RunnerOutcome.NETWORK_DENIED,
    }
)


@dataclass(frozen=True, slots=True)
class Dispatch:
    """Where the work happens — everything the stage does not say itself.

    Taken whole rather than assembled here, and required rather than defaulted,
    for the reason the harness gave when it did the same thing: the real values
    come from triage and the repo registry, and a default would be this step
    asserting a decision another step has not made yet.
    """

    profile: RepoProfile
    work_item_id: str
    branch: str
    base_commit: str
    worktree_path: str
    contract: VerificationContract
    budget: Budget = field(default_factory=Budget)

    #: Unresolved stubs from earlier stories in the same epic (§3.9). Empty at
    #: M1, because epics fan out at S15b.
    carried_stubs: tuple[str, ...] = ()

    #: Whether the work came from a trusted submitter (``Submitter.trusted``).
    #: Deny by default, and carried rather than derived here for the same reason
    #: everything else on this record is: triage knows who asked, and a default
    #: computed at this layer would be this step deciding a question ingestion
    #: owns. Only the socket tier reads it, and only to refuse.
    trusted_provenance: bool = False

    @classmethod
    def for_worktree(
        cls,
        worktree: Worktree,
        profile: RepoProfile,
        *,
        work_item_id: str,
        contract: VerificationContract,
        budget: Budget | None = None,
        carried_stubs: tuple[str, ...] = (),
        trusted_provenance: bool = False,
    ) -> Dispatch:
        """Build a dispatch from a checkout ``clawdence.vcs`` handed out.

        Three of this record's fields — the worktree path, the branch and the
        base commit — were taken as data at S6 precisely because inventing them
        would have been the runner deciding a question a later step owned. S15 is
        that step, and this is the one line where its answer meets S6's. What is
        still not derived here is *which repository*, which is S11's: the profile
        arrives as an argument for the same reason the other three used to.

        The direction of the import is worth noting. ``runners`` depends on
        ``vcs`` and not the reverse — ``vcs.git`` is the shared plumbing, and
        ``vcs`` knowing what a runner dispatch is would put a cycle between two
        packages that currently read top to bottom.
        """
        return cls(
            profile=profile,
            work_item_id=work_item_id,
            branch=worktree.branch,
            base_commit=worktree.base_commit,
            worktree_path=str(worktree.path),
            contract=contract,
            budget=budget or Budget(),
            carried_stubs=carried_stubs,
            trusted_provenance=trusted_provenance,
        )


@dataclass(slots=True)
class RunnerHandler:
    """Runs a ``runner`` stage through a ``RunnerPort``.

    ``plan_template`` is expanded with the engine's own interpolation, so a
    workflow feeds a prior agent stage's output in as ``${plan.json.text}``. A
    template rather than a field on ``RunnerStage``, because what the plan *is*
    is S12's question — an agent step's output shape — and adding a domain field
    now would pin an answer to it from the outside.
    """

    runner: RunnerPort
    dispatch: Dispatch
    plan_template: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    retryable: frozenset[RunnerOutcome] = RETRYABLE

    #: Stage ids dispatched, in order. What tests assert on.
    calls: list[str] = field(default_factory=list)

    async def __call__(self, ctx: StepContext) -> HandlerOutcome:
        stage = ctx.stage
        if not isinstance(stage, RunnerStage):  # pragma: no cover - the registry routes by type
            raise StepFailure("wrong-handler", f"{stage.id} is not a runner stage")
        self.calls.append(stage.id)

        request = self._request(ctx, stage)
        try:
            result = await self.runner.dispatch(request)
        except PortError as exc:
            # A failure to *dispatch* — no binary, no daemon, a request that
            # cannot honestly be run — never reached the data plane, so it is a
            # step failure carrying the adapter's own verdict on whether
            # repeating it could help.
            raise StepFailure(exc.kind, exc.message, retryable=exc.retryable) from exc

        output: JsonValue = {
            "outcome": result.outcome.value,
            "tree_hash": result.tree_hash,
            "exit_code": result.exit_code,
            "files_changed": result.diff.files_changed if result.diff else 0,
            "insertions": result.diff.insertions if result.diff else 0,
            "deletions": result.diff.deletions if result.diff else 0,
            # §3.10's artifacts, forwarded rather than re-derived. A later stage
            # asking "did the agent actually commit anything" reads this instead
            # of running git against a worktree it may not be able to reach.
            "commits_ahead": result.commits_ahead,
            "dirty": result.dirty,
            "tests_failed": result.test_evidence.failed if result.test_evidence else None,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "usd": str(result.cost.usd) if result.cost is not None else None,
        }

        if result.outcome is not RunnerOutcome.SUCCEEDED:
            raise StepFailure(
                f"runner-{result.outcome.value}",
                result.message or f"the runner reported {result.outcome.value}",
                retryable=result.outcome in self.retryable,
            )
        return HandlerOutcome(output=output)

    def _request(self, ctx: StepContext, stage: RunnerStage) -> RunnerRequest:
        target = self.dispatch
        return RunnerRequest(
            run_id=ctx.run_id,
            stage_id=stage.id,
            work_item_id=target.work_item_id,
            worktree_path=target.worktree_path,
            branch=target.branch,
            base_commit=target.base_commit,
            profile=target.profile,
            contract=target.contract,
            budget=stage.budget or target.budget,
            plan=self._plan(ctx),
            carried_stubs=target.carried_stubs,
            trusted_provenance=target.trusted_provenance,
            # The derivation the ledger uses, so a redelivered dispatch collides
            # with the row the previous incarnation wrote rather than running the
            # work — and charging for it — twice.
            idempotency_key=idempotency_key(ctx.run_id, stage.id, ctx.attempt),
            created_at=self.clock(),
        )

    def _plan(self, ctx: StepContext) -> str:
        try:
            return expand(self.plan_template, ctx.resolver)
        except InterpolationError as exc:
            # Permanent: the template names a stage that did not produce what it
            # promised, and running the agent with a half-expanded plan would
            # spend a coding budget on a prompt containing `${plan.json.text}`.
            raise StepFailure("plan-unresolved", f"plan: {exc}", retryable=False) from exc
