"""Handlers backed by fake ports, so a whole workflow can run under test.

**This is harness scaffolding, not the product.** The engine's
``default_registry`` still refuses ``agent``, ``runner`` and ``approval`` steps
by name, and it must keep doing so — a stub that returns success makes a
workflow look like it ran, which is the most expensive possible way to be wrong
about an orchestrator. These handlers live in ``tests/`` for exactly that
reason, and must be registered deliberately, which is the same rule
``engine.StubHandler`` follows.

What they buy is S5's verification criterion: a workflow with all four step
types executes end to end, on fakes, with no network and no spend. That is a
statement about whether the ports *compose*, which is not answerable from unit
tests of each one.

S6, S12 and S17 replace each of these with a real handler. When they do, the
fake *ports* stay — they become those handlers' test doubles, which is why the
port fakes are in ``clawdence.ports`` and only the wiring is here. The runner
handler below is deliberately the thinnest possible request/dispatch/unwrap: the
real I/O contract (plan augmentation, verdict files, token parsing, stub
carry-over) is §3.9 and belongs to S6, and sketching it here would mean S6
inherits guesses made by a test fixture.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import JsonValue

from clawdence.domain import (
    Budget,
    ContractKind,
    RepoProfile,
    RunnerOutcome,
    RunnerRequest,
    RunnerStage,
    StepType,
    VerificationContract,
)
from clawdence.engine import (
    HandlerOutcome,
    HandlerRegistry,
    ScriptHandler,
    StepContext,
    StepFailure,
    idempotency_key,
)
from clawdence.ports import PortError, Ports, RunnerPort
from tests.harness.cassette import Cassette


@dataclass(slots=True)
class CassetteAgentHandler:
    """An ``agent`` step answered from a recording.

    The request digest is built from the fields that actually decide what a
    model would say — role, model, prompt version, turn budget — so a workflow
    that changes any of them is a cassette miss rather than a silently reused
    answer. Which is the whole point of keying on the request.
    """

    cassette: Cassette
    calls: list[str] = field(default_factory=list)

    async def __call__(self, ctx: StepContext) -> HandlerOutcome:
        stage = ctx.stage
        if stage.type is not StepType.AGENT:  # pragma: no cover - the registry routes by type
            raise StepFailure("wrong-handler", f"{stage.id} is not an agent stage")

        self.calls.append(stage.id)
        request: JsonValue = {
            "stage": stage.id,
            "role": getattr(stage, "role", None),
            "model": getattr(getattr(stage, "model", None), "model", None),
            "prompt_version": getattr(stage, "prompt_version", None),
            "max_turns": getattr(stage, "max_turns", None),
        }
        return HandlerOutcome(output=await self.cassette.play(request))


@dataclass(slots=True)
class PortRunnerHandler:
    """A ``runner`` step dispatched through ``RunnerPort``.

    The failure taxonomy is honoured rather than collapsed: only ``SUCCEEDED``
    is a success, ``TESTS_FAILED`` and ``TIMED_OUT`` are retryable failures, and
    an empty diff is not. That distinction is the reason ``RunnerOutcome`` has
    eleven values, and a handler that mapped all of them to "failed" would make
    the enum decorative.
    """

    runner: RunnerPort
    profile: RepoProfile
    work_item_id: str
    branch: str
    base_commit: str
    worktree_path: str
    contract: VerificationContract = field(
        default_factory=lambda: VerificationContract(kind=ContractKind.TEST_AFTER)
    )
    budget: Budget = field(default_factory=Budget)

    #: Outcomes a second attempt could plausibly change. An OOM kill or a
    #: budget cap will not, so retrying those spends the budget more slowly
    #: rather than differently.
    retryable: frozenset[RunnerOutcome] = frozenset(
        {
            RunnerOutcome.TESTS_FAILED,
            RunnerOutcome.TIMED_OUT,
            RunnerOutcome.STARTUP_FAILED,
            RunnerOutcome.NETWORK_DENIED,
        }
    )

    async def __call__(self, ctx: StepContext) -> HandlerOutcome:
        stage = ctx.stage
        if not isinstance(stage, RunnerStage):  # pragma: no cover - the registry routes by type
            raise StepFailure("wrong-handler", f"{stage.id} is not a runner stage")

        request = RunnerRequest(
            run_id=ctx.run_id,
            stage_id=stage.id,
            work_item_id=self.work_item_id,
            worktree_path=self.worktree_path,
            branch=self.branch,
            base_commit=self.base_commit,
            profile=self.profile,
            contract=self.contract,
            budget=stage.budget or self.budget,
            plan=f"execute {stage.id}",
            # The same derivation the ledger uses, so a redelivered dispatch
            # collides with the row the previous incarnation wrote instead of
            # running the work twice.
            idempotency_key=idempotency_key(ctx.run_id, stage.id, ctx.attempt),
            created_at=datetime.now(UTC),
        )

        try:
            result = await self.runner.dispatch(request)
        except PortError as exc:
            # A failure to *dispatch* — no image, no daemon — never reached the
            # data plane, so it is a step failure carrying the adapter's own
            # verdict on whether repeating it could help.
            raise StepFailure(exc.kind, exc.message, retryable=exc.retryable) from exc

        output: JsonValue = {
            "outcome": result.outcome.value,
            "tree_hash": result.tree_hash,
            "files_changed": result.diff.files_changed if result.diff else 0,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        }
        if result.outcome is not RunnerOutcome.SUCCEEDED:
            raise StepFailure(
                f"runner-{result.outcome.value}",
                result.message or f"the runner reported {result.outcome.value}",
                retryable=result.outcome in self.retryable,
            )
        return HandlerOutcome(output=output)


@dataclass(slots=True)
class CannedApprovalHandler:
    """An ``approval`` step answered without a human.

    The decision lands in ``response`` rather than ``output``, which is the
    distinction S2 built into ``HandlerOutcome``: later stages read
    ``$gate.response.decision``, and "a person decided" stays separable from
    "the model decided" in the audit trail. A harness that put it in ``output``
    would make that separation untestable.
    """

    decisions: Mapping[str, JsonValue]
    calls: list[str] = field(default_factory=list)

    async def __call__(self, ctx: StepContext) -> HandlerOutcome:
        self.calls.append(ctx.stage.id)
        decision = self.decisions.get(ctx.stage.id)
        if decision is None:
            raise StepFailure(
                "no-canned-decision",
                f"this harness has no decision for approval stage {ctx.stage.id!r}",
                retryable=False,
            )
        return HandlerOutcome(response=decision)


def fake_registry(
    ports: Ports,
    *,
    cassette: Cassette,
    profile: RepoProfile,
    work_item_id: str,
    branch: str,
    base_commit: str,
    worktree_path: str,
    decisions: Mapping[str, JsonValue] | None = None,
    environ: Mapping[str, str] | None = None,
) -> HandlerRegistry:
    """A registry where every step type runs against a fake.

    Never a default anywhere. It takes the whole world explicitly — the profile,
    the branch, the base commit — because the real versions of those come from
    triage and the repo registry (S9, S11), and a harness that invented them
    would be quietly asserting decisions those steps have not made yet.
    """
    return HandlerRegistry(
        {
            StepType.SCRIPT: ScriptHandler(environ),
            StepType.AGENT: CassetteAgentHandler(cassette),
            StepType.RUNNER: PortRunnerHandler(
                runner=ports.runner,
                profile=profile,
                work_item_id=work_item_id,
                branch=branch,
                base_commit=base_commit,
                worktree_path=worktree_path,
            ),
            StepType.APPROVAL: CannedApprovalHandler(dict(decisions or {})),
        }
    )
