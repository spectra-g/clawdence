"""Workflow definitions — the declarative process, versioned.

v1's process was implicit in a call graph: 23 handlers in a 5,107-line
orchestrator, each hardcoding its successor. Nothing could vary per work item.
Here the process is data.

The shape is adopted from Lobster (ADR-0003) — step id, ``when``, ``retry``,
``timeout``, ``on_error``, ``$stage.json.field`` refs, and the
``$stage.response.field`` distinction for human input — because the shape is
well-judged and familiar. Schema *compatibility* is a stated non-goal:
workflows are not portable between the two engines and this does not pretend
otherwise.

Two divergences are visible in these types:

``ScriptStage.command`` is argv, not a shell string.
    Lobster's ``${arg}`` is a raw string replace into command text, which is
    command injection by construction the moment untrusted issue text is an
    argument. Making the field a list removes the shell from the path
    entirely — there is no string for an argument to break out of.

Rejection is a branch, never a cancel.
    Lobster's ``approval:`` terminates the run when rejected, so
    "reject → re-plan with feedback" cannot use it. ``ApprovalStage`` carries a
    ``response_schema`` and later stages branch on the decision, which keeps
    the approver-identity checks and the rejection path in the same step type.

Naming: a ``Stage`` is the static declaration in the YAML; a ``StepResult``
(see ``run``) is what one execution of it produced. One stage can yield several
step results — that is what ``attempt`` counts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from clawdence.domain._base import DomainModel
from clawdence.domain.budget import Budget
from clawdence.domain.ids import Condition, SemVer, Slug, StageId

#: Bumped when the workflow file format changes incompatibly. A workflow
#: declaring an unsupported version is rejected with a migration hint rather
#: than half-interpreted.
WORKFLOW_SCHEMA_VERSION = 1


class StepType(StrEnum):
    SCRIPT = "script"
    AGENT = "agent"
    RUNNER = "runner"
    APPROVAL = "approval"


class OnError(StrEnum):
    """What a stage failure does to the run."""

    FAIL = "fail"
    CONTINUE = "continue"
    SKIP_REST = "skip_rest"


class ContextOverflowPolicy(StrEnum):
    """What happens when an agent step exceeds its context budget.

    Silently dropping context is not an option. v1's whole Kimi failure class —
    tool calls emitted inside think blocks, sessions ending with no reply — was
    context growth across turns with no declared policy.
    """

    COMPACT = "compact"
    TRUNCATE = "truncate"
    FAIL = "fail"


class ModelCapability(StrEnum):
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"
    LONG_CONTEXT = "long_context"
    VISION = "vision"


class RetryPolicy(DomainModel):
    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_seconds: float = Field(default=0.0, ge=0)


class ModelSelector(DomainModel):
    """Which model runs a step, and what it must be able to do.

    Model is per *step*, not per agent — v1 pinned it per agent in
    ``openclaw.json``, so changing one role's model changed it everywhere that
    role appeared.

    ``requires`` exists so a model swap fails validation rather than failing
    mysteriously at run time. ``temperature`` and ``seed`` are pinned so the
    eval harness measures prompt changes rather than sampling noise.
    """

    model: str
    #: Tried in order on quota exhaustion — which is distinct from rate
    #: limiting, a distinction v1 had to learn the hard way.
    fallbacks: tuple[str, ...] = ()
    requires: tuple[ModelCapability, ...] = ()
    temperature: float | None = Field(default=None, ge=0, le=2)
    seed: int | None = None


class StageBase(DomainModel):
    """Fields every stage has, whatever its type."""

    id: StageId
    name: str | None = None

    #: Guard expression. Absent means "always run".
    when: Condition | None = None

    on_error: OnError = OnError.FAIL
    retry: RetryPolicy = RetryPolicy()

    #: Declared here, enforced by the engine, and consumed by S4's watchdog —
    #: which is why it lives in the definition rather than in the executor.
    timeout_seconds: float | None = Field(default=None, gt=0)


class ScriptStage(StageBase):
    """Run a command. No shell, no interpolation into command text."""

    type: Literal[StepType.SCRIPT] = StepType.SCRIPT

    #: argv. The first element is the executable; nothing is word-split, and
    #: no value interpolated into a later element can become a new argument.
    command: tuple[str, ...] = Field(min_length=1)

    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    stdin: str | None = None


class AgentStage(StageBase):
    """Invoke an LLM in a declared role.

    Agent steps are stateless: every execution starts from a clean context and
    all state lives in the store. v1 reset ``sessions.json`` before every spawn
    for exactly this reason, and it is the single biggest reason its later
    pipeline was more reliable than its earlier one.
    """

    type: Literal[StepType.AGENT] = StepType.AGENT

    #: Key into the prompt registry. Prompts are versioned data, overridable by
    #: users without forking — everyone will want to tune the BA.
    role: str
    prompt_version: str | None = None

    #: What this step is asked to do, as a template — ``${intake.json.text}``,
    #: ``${ba.json.result.summary}``. Required, and in the workflow rather than in
    #: the handler, because "what is this agent being asked" is the process, and
    #: the process is data (S12, added rev 13). The role prompt says *who* the
    #: model is; this says what it is looking at. It is delivered framed as
    #: quoted material, never concatenated into the role prompt, because the
    #: values it expands are request text, memory and discovery notes — all of
    #: which an attacker may have influenced (§3.9, and S10b at the other end).
    task: str

    model: ModelSelector

    #: Explicit, not discovered by trial. v1's budgets were BA 1, Tech Lead 1,
    #: Phase A 2, Architect 2, Consensus 3 — all found by trial and error.
    max_turns: int = Field(default=1, ge=1, le=32)

    context_budget_tokens: int | None = Field(default=None, gt=0)
    on_context_overflow: ContextOverflowPolicy = ContextOverflowPolicy.FAIL

    #: Name of the schema the response is validated against. Malformed JSON is
    #: common enough that repair is load-bearing, not a nicety.
    response_schema: str | None = None

    #: Tool surface for this step. Empty means no tools.
    tools: tuple[str, ...] = ()

    #: Aborted sessions retain usable partial content; whether that is salvaged
    #: or discarded is a per-step decision, not a global one.
    salvage_partial_output: bool = False

    budget: Budget | None = None


class RunnerStage(StageBase):
    """Execute repo code in the data plane."""

    type: Literal[StepType.RUNNER] = StepType.RUNNER

    #: What the agent in the data plane is told to build, as a template —
    #: ``${plan.json.result}`` after a planning stage, ``${request.json.text}``
    #: in a workflow that has none. The runner half of ``AgentStage.task``, and
    #: it is here for the same reason (S11, added rev 14): the plan a step is
    #: given is *the process*, and the process is data. Left on the handler it
    #: would be one template per composition root, so a workflow that plans and
    #: one that goes straight to code could not both be wired by the same
    #: pipeline — which is exactly what triage does with ``sprint`` and
    #: ``quick-fix``.
    #:
    #: Absent means the handler's own default, which is what an ad-hoc
    #: ``clawdence run`` gets.
    plan: str | None = None

    #: Overrides the repo profile's tier when set. Narrowing only — the engine
    #: refuses a widening override for untrusted work.
    isolation_tier_override: str | None = None

    budget: Budget | None = None


class ApprovalStage(StageBase):
    """Ask a human, and carry what they said into what happens next."""

    type: Literal[StepType.APPROVAL] = StepType.APPROVAL

    prompt: str

    #: Identity constraints, adopted wholesale — they answer S17's
    #: authorization gap, where v1 let anyone in the channel approve a merge.
    required_approver: str | None = None
    require_different_approver: bool = False

    #: Name of the schema the decision is validated against. The response is
    #: readable by later stages as ``$stage.response.<field>``, which is how
    #: free-text feedback reaches the stage that retries.
    response_schema: str | None = None

    #: A gate that waits forever is v1's behaviour and it stalled runs
    #: silently. ``None`` means wait indefinitely and is not the default.
    timeout_seconds_override: float | None = Field(default=None, gt=0)


Stage = Annotated[
    ScriptStage | AgentStage | RunnerStage | ApprovalStage,
    Field(discriminator="type"),
]


class Workflow(DomainModel):
    """An ordered process, versioned and pinned per run."""

    schema_version: int = Field(default=WORKFLOW_SCHEMA_VERSION, ge=1)
    name: Slug
    version: SemVer
    description: str | None = None

    stages: tuple[Stage, ...] = Field(min_length=1)

    #: Defaults applied to every stage that does not override them.
    default_budget: Budget | None = None

    @model_validator(mode="after")
    def _stage_ids_are_unique(self) -> Workflow:
        """Duplicate ids make ``$stage.json`` references ambiguous.

        Caught at load time rather than when execution reaches the second
        stage — which, for a workflow whose earlier steps call an LLM, is
        after the run has already cost money.
        """
        seen: set[str] = set()
        for stage in self.stages:
            if stage.id in seen:
                raise ValueError(f"duplicate stage id: {stage.id!r}")
            seen.add(stage.id)
        return self
