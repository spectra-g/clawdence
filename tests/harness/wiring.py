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

S12 and S17 replace the remaining two with real handlers. When they do, the fake
*ports* stay — they become those handlers' test doubles, which is why the port
fakes are in ``clawdence.ports`` and only the wiring is here.

**S6 already did that to the runner.** What was a sketch here is now
``clawdence.runners.RunnerHandler``, and this module imports it rather than
keeping a second version: two handlers that both claim to know what a runner
step does is how the harness starts testing something the product does not
contain. What stays here is the part that is genuinely a test's business — which
repository, worktree and branch — because the real answer comes from triage
(S11) and inventing one in the product would be S6 deciding a later step's
question.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from clawdence.domain import (
    ContractKind,
    RepoProfile,
    StepType,
    VerificationContract,
)
from clawdence.engine import (
    HandlerOutcome,
    HandlerRegistry,
    ScriptHandler,
    StepContext,
    StepFailure,
)
from clawdence.ports import Ports
from clawdence.runners import Dispatch, RunnerHandler
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
            StepType.RUNNER: RunnerHandler(
                runner=ports.runner,
                dispatch=Dispatch(
                    profile=profile,
                    work_item_id=work_item_id,
                    branch=branch,
                    base_commit=base_commit,
                    worktree_path=worktree_path,
                    contract=VerificationContract(kind=ContractKind.TEST_AFTER),
                ),
                # A literal rather than a reference, because the workflows this
                # harness runs have no agent stage producing one. A real
                # pipeline passes `${plan.json.text}`.
                plan_template=f"do the work for {work_item_id}",
            ),
            StepType.APPROVAL: CannedApprovalHandler(dict(decisions or {})),
        }
    )
