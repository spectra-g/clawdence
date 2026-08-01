"""Handlers backed by fake ports, so a whole workflow can run under test.

**This is harness scaffolding, not the product.** The engine's
``default_registry`` still refuses ``agent``, ``runner`` and ``approval`` steps by
name unless one is handed in, and it must keep doing so — a stub that returns
success makes a workflow look like it ran, which is the most expensive possible way
to be wrong about an orchestrator. What lives in ``tests/`` is the *wiring*, and it
has to be asked for, which is the same rule ``engine.StubHandler`` follows.

What they buy is S5's verification criterion: a workflow with all four step
types executes end to end, on fakes, with no network and no spend. That is a
statement about whether the ports *compose*, which is not answerable from unit
tests of each one.

S17 replaces the remaining one with a real handler. When it does, the fake
*ports* stay — they become that handler's test doubles, which is why the port
fakes are in ``clawdence.ports`` and only the wiring is here.

**S6 and S12 already did that to two of them.** What were sketches here are now
``clawdence.runners.RunnerHandler`` and ``clawdence.agent.AgentHandler``, and this
module imports them rather than keeping second versions: two handlers that both
claim to know what an agent step does is how the harness starts testing something
the product does not contain. The cassette-backed agent handler that used to live
here is gone entirely — a recording is a *transport* concern, and the real handler
reaches its provider through a seam a cassette can wrap, so the harness no longer
has to model what an agent step is in order to record one.

**S15 took two of the three remaining invented values away.** A worktree path, a
branch and a base commit now come from ``clawdence.vcs.WorktreeManager`` and turn
into a ``Dispatch`` through ``Dispatch.for_worktree`` — ``tests/vcs/test_pipeline``
is the sequence written out. They are still passed in here as plain values,
because a workflow test about branching or retry has no business building a bare
mirror and a checkout to get one; what a fixture may reasonably fake is a value
some other tested component would have computed.

What stays genuinely invented is *which repository* — the real answer comes from
triage (S11), and inventing one in the product would be S6 or S15 deciding a
later step's question.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue

from clawdence.agent import AgentHandler, PromptRegistry
from clawdence.domain import ContractKind, RepoProfile, VerificationContract
from clawdence.engine import (
    HandlerOutcome,
    HandlerRegistry,
    StepContext,
    StepFailure,
    default_registry,
)
from clawdence.ports import Ports
from clawdence.runners import Dispatch, RunnerHandler


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
    profile: RepoProfile,
    work_item_id: str,
    branch: str,
    base_commit: str,
    worktree_path: str,
    decisions: Mapping[str, JsonValue] | None = None,
    prompts: PromptRegistry | None = None,
    environ: Mapping[str, str] | None = None,
) -> HandlerRegistry:
    """A registry where every step type runs against a fake.

    Never a default anywhere. It takes the whole world explicitly — the profile,
    the branch, the base commit — because the real versions of those come from
    triage and the repo registry (S9, S11), and a harness that invented them
    would be quietly asserting decisions those steps have not made yet.
    """
    return default_registry(
        environ,
        agent=AgentHandler(model=ports.model, prompts=prompts or PromptRegistry()),
        runner=RunnerHandler(
            runner=ports.runner,
            dispatch=Dispatch(
                profile=profile,
                work_item_id=work_item_id,
                branch=branch,
                base_commit=base_commit,
                worktree_path=worktree_path,
                contract=VerificationContract(kind=ContractKind.TEST_AFTER),
            ),
            # A literal rather than a reference, because not every workflow this
            # harness runs has an agent stage producing one. A real pipeline
            # passes `${plan.json.result}`.
            plan_template=f"do the work for {work_item_id}",
        ),
        approval=CannedApprovalHandler(dict(decisions or {})),
    )
