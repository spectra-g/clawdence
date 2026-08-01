"""The ``agent`` step type: a role, a task, a model, a bounded conversation.

The hole S3 left, and the one v1 never filled cleanly. What is here beyond "call
a model and keep the text" is the execution model — five properties v1 discovered
by trial, each of which is now a declared field on ``AgentStage`` and enforced
here.

**Turns are declared, not discovered.** v1's budgets (BA 1, Tech Lead 1, Phase A
2, Architect 2, Consensus 3) were found by watching things fail. ``max_turns``
defaults to 1, and a second turn happens for exactly one reason: the response
failed its schema and there is budget left to say so. That is a narrow use of a
multi-turn loop and it is the useful one — the alternative, a loop that continues
while the model keeps talking, is how v1's context grew until the Kimi failure
class appeared (tool calls emitted inside ``<think>`` blocks, sessions ending with
no reply at all).

**Context growth is bounded and never silently dropped.** The budget is checked
before every call, against an estimate that deliberately runs high, and against
the model's own window. Exceeding it does what the stage declared —
``COMPACT``/``TRUNCATE``/``FAIL`` — and *records which*, because the failure this
prevents is a prompt quietly cut by the provider, which surfaces as a model that
ignored half its instructions long after it was paid for.

**Steps are stateless.** Every attempt builds a fresh conversation from the
prompt registry and the resolver. Nothing is carried between attempts and nothing
between stages except through the run record. v1 reset ``sessions.json`` before
every spawn for exactly this reason, and it is the single biggest reason its later
pipeline was more reliable than its earlier one — so it is an invariant here
rather than a habit: this class holds no per-run state at all, which is what makes
it safe for S3b to run two of these at once.

**Partial output is a decision, not a default.** A response that stopped at the
output limit or was aborted mid-stream still carries text, and v1 observed that
text is often usable. ``salvage_partial_output`` decides whether to try, and the
repair that closes a truncated document is recorded as a repair.

**The output is a proposal (§1.3).** This handler is constructed with a model
port, a prompt registry, a schema registry and a tool surface — and nothing else.
It cannot reach the state store, the workflow definitions, the verification
contracts or the trigger configuration, because it was never given them. That is
the enforcement: there is no second path by which an agent's output arrives
already applied, which is what makes S17's approval rule a boundary rather than a
convention. It also leaves ``HandlerOutcome.response`` alone — that field means "a
human said this", and an agent step filling it in would erase the one distinction
the audit trail cannot reconstruct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from pydantic import JsonValue

from clawdence.agent import prompts as prompt_registry
from clawdence.agent import repair as repair_module
from clawdence.agent.response import ResponseInvalidError, ResponseSchemas, SchemaNotFoundError
from clawdence.agent.routing import Route
from clawdence.agent.routing import complete as route_complete
from clawdence.agent.routing import validate as validate_route
from clawdence.agent.tools import ToolSurface, UnknownToolError
from clawdence.domain import AgentStage, ContextOverflowPolicy, TokenUsage
from clawdence.engine import HandlerOutcome, StepContext, StepFailure
from clawdence.engine.errors import InterpolationError
from clawdence.engine.interpolation import expand
from clawdence.ports import (
    CHARS_PER_TOKEN,
    Message,
    MessageRole,
    ModelDescriptor,
    ModelPort,
    ModelRequest,
    PortError,
    ToolSpec,
    estimate_tokens,
)
from clawdence.ports._common import Clock, utc_now

#: Marker left in place of anything a policy removed. Never an empty string: a
#: prompt that silently lost a section is one where the model's confusion has no
#: visible cause, and this is what turns "why did it ignore the criteria" into a
#: line in the prompt saying they were cut.
ELIDED = "[…elided to fit the context budget…]"

#: Floor on the room left for a reply after the input budget is worked out. The
#: window bounds input *plus* output on every provider worth supporting, so a
#: request sized to fill the window entirely leaves nowhere for an answer.
_MIN_INPUT_BUDGET = 1

#: What this asks a model to emit when the stage says nothing, capped per call by
#: the descriptor. A stage-level knob was considered and left out: the numbers a
#: workflow author cares about are the context budget and the money budget, both
#: of which exist, and a third limit in output tokens is one more thing to get
#: wrong for no decision it enables.
DEFAULT_MAX_OUTPUT_TOKENS = 8_192


@dataclass(frozen=True, slots=True)
class ContextReport:
    """What one turn's budget check concluded. Part of the step output."""

    estimated_input_tokens: int
    budget_tokens: int
    policy_applied: ContextOverflowPolicy | None = None

    def as_json(self) -> JsonValue:
        return {
            "estimated_input_tokens": self.estimated_input_tokens,
            "budget_tokens": self.budget_tokens,
            "overflow": None if self.policy_applied is None else self.policy_applied.value,
        }


@dataclass(slots=True)
class AgentHandler:
    """Runs an ``agent`` stage through a ``ModelPort``."""

    model: ModelPort
    prompts: prompt_registry.PromptRegistry
    schemas: ResponseSchemas = field(default_factory=ResponseSchemas)
    tools: ToolSurface = field(default_factory=ToolSurface)
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    clock: Clock = utc_now

    #: Stage ids run, in order. What tests assert on.
    calls: list[str] = field(default_factory=list)

    async def __call__(self, ctx: StepContext) -> HandlerOutcome:
        stage = ctx.stage
        if not isinstance(stage, AgentStage):  # pragma: no cover - the registry routes by type
            raise StepFailure("wrong-handler", f"{stage.id} is not an agent stage")
        self.calls.append(stage.id)

        prompt = self._prompt(stage)
        tools = self._tools(stage)
        system = self._system(prompt.text, stage)
        conversation = [Message(role=MessageRole.USER, text=self._task(ctx, stage))]

        started = self.clock()
        usage = TokenUsage()
        route: Route | None = None
        context = ContextReport(estimated_input_tokens=0, budget_tokens=0)
        repairs: tuple[str, ...] = ()
        result: JsonValue = None
        invalid: ResponseInvalidError | None = None
        turns = 0

        while turns < stage.max_turns:
            turns += 1
            conversation, context = self._fit(stage, system, conversation)

            route = await self._ask(stage, system, conversation, tools)
            usage = _add(usage, route.response.usage)
            self._charge(stage, route, usage, started)

            if route.response.incomplete and not stage.salvage_partial_output:
                raise StepFailure(
                    "agent-response-incomplete",
                    f"the model stopped with {route.response.stop_reason.value!r} and this step "
                    "does not salvage partial output",
                    retryable=False,
                )

            if stage.response_schema is None:
                # No schema means the text *is* the product. One turn, always:
                # there is nothing a second turn could be told to fix.
                invalid = None
                break

            result, repairs, invalid = self._structured(stage, route.response.text)
            if invalid is None:
                break

            if turns < stage.max_turns:
                # The correction is a turn of the same conversation rather than a
                # fresh call, because the model needs to see what it said in order
                # to fix it — and it is bounded by the same declared budget as any
                # other turn, so a step permitting one turn gets one attempt.
                conversation = [
                    *conversation,
                    Message(role=MessageRole.ASSISTANT, text=route.response.text),
                    Message(role=MessageRole.USER, text=_correction(invalid)),
                ]

        if route is None:  # pragma: no cover - max_turns is >= 1 by the domain model
            raise StepFailure("agent-no-turns", "the step ran no turns", retryable=False)

        if invalid is not None:
            # Retryable: a fresh attempt is a fresh conversation, and this is the
            # failure v1 retried successfully more often than not. A workflow that
            # pinned temperature and seed has made that retry deterministic and so
            # pointless — which is the author's decision to have made, and shows
            # up as a stage that failed twice identically.
            raise StepFailure("agent-response-invalid", str(invalid), retryable=True)

        return HandlerOutcome(
            output=self._output(
                stage=stage,
                prompt=prompt,
                route=route,
                usage=usage,
                context=context,
                repairs=repairs,
                result=result,
                turns=turns,
            )
        )

    # ------------------------------------------------------------ assembling

    def _prompt(self, stage: AgentStage) -> prompt_registry.Prompt:
        try:
            return self.prompts.get(stage.role, stage.prompt_version)
        except prompt_registry.PromptNotFoundError as exc:
            raise StepFailure("prompt-not-found", str(exc), retryable=False) from None

    def _tools(self, stage: AgentStage) -> tuple[ToolSpec, ...]:
        try:
            return self.tools.resolve(stage.tools)
        except UnknownToolError as exc:
            raise StepFailure("tool-not-available", str(exc), retryable=False) from None

    def _task(self, ctx: StepContext, stage: AgentStage) -> str:
        try:
            expanded = expand(stage.task, ctx.resolver)
        except InterpolationError as exc:
            # Permanent, for the reason ``RunnerHandler`` gives about plans: a
            # half-expanded task spends a model call on a prompt containing
            # ``${ba.json.result.summary}``, and the model answers it.
            raise StepFailure("task-unresolved", f"task: {exc}", retryable=False) from exc
        return prompt_registry.frame("task", expanded)

    def _system(self, text: str, stage: AgentStage) -> str:
        if stage.response_schema is None:
            return text
        try:
            instruction = self.schemas.instruction(stage.response_schema)
        except SchemaNotFoundError as exc:
            raise StepFailure("response-schema-not-found", str(exc), retryable=False) from None
        return f"{text.rstrip()}\n\n{instruction}\n"

    async def _ask(
        self,
        stage: AgentStage,
        system: str,
        conversation: list[Message],
        tools: tuple[ToolSpec, ...],
    ) -> Route:
        def build(descriptor: ModelDescriptor) -> ModelRequest:
            return ModelRequest(
                model=descriptor.model,
                system=system,
                messages=tuple(conversation),
                max_output_tokens=min(self.max_output_tokens, descriptor.max_output_tokens),
                temperature=stage.model.temperature,
                seed=stage.model.seed,
                tools=tools,
            )

        try:
            route = await route_complete(self.model, stage.model, build)
        except PortError as exc:
            raise StepFailure(exc.kind, exc.message, retryable=exc.retryable) from exc

        if route.response.tool_calls and not tools:
            # Nothing was offered, so nothing may be called. A model that invented
            # a tool call has not done the work, and treating its text as an answer
            # would report the invention as a result.
            raise StepFailure(
                "agent-unrequested-tool-call",
                f"the model asked to call {route.response.tool_calls[0].name!r}, "
                "but this step offered no tools",
                retryable=True,
            )
        return route

    # ------------------------------------------------------------- structured

    def _structured(
        self, stage: AgentStage, text: str
    ) -> tuple[JsonValue, tuple[str, ...], ResponseInvalidError | None]:
        """Parse, repair and validate. Returns ``(result, repairs, failure)``."""
        schema = stage.response_schema
        assert schema is not None  # noqa: S101 - callers check; narrowing for mypy
        try:
            parsed = repair_module.extract_json(text, close_truncated=stage.salvage_partial_output)
        except repair_module.RepairFailed as exc:
            return None, (), ResponseInvalidError(schema, str(exc))

        try:
            return self.schemas.validate(schema, parsed.value), parsed.repairs, None
        except ResponseInvalidError as exc:
            return None, parsed.repairs, exc

    # --------------------------------------------------------- context budget

    def _fit(
        self, stage: AgentStage, system: str, conversation: list[Message]
    ) -> tuple[list[Message], ContextReport]:
        """Measure the prompt and apply the declared policy if it does not fit.

        The window comes from the *primary* model rather than whichever one
        routing ends up asking, because the check happens before the call. A
        fallback with a smaller window would therefore be measured generously —
        which is why ``ContextWindowExceededError`` exists on the port as the
        backstop for exactly that case, rather than being treated as unreachable.
        """
        descriptor = self.model.describe(stage.model.model)
        reply_room = min(self.max_output_tokens, descriptor.max_output_tokens)
        window = max(descriptor.context_window_tokens - reply_room, _MIN_INPUT_BUDGET)

        # The declared budget narrows, never widens: a workflow asking for more
        # context than the model has is asking for silent truncation by the
        # provider, which is the thing this check exists to prevent.
        budget = min(stage.context_budget_tokens or window, window)
        estimated = estimate_tokens(system) + sum(
            estimate_tokens(message.text) for message in conversation
        )

        if estimated <= budget:
            return conversation, ContextReport(
                estimated_input_tokens=estimated, budget_tokens=budget
            )

        if stage.on_context_overflow is ContextOverflowPolicy.FAIL:
            raise StepFailure(
                "context-budget-exceeded",
                f"the prompt is about {estimated} tokens against a budget of {budget}, "
                "and this step fails rather than cutting it",
                retryable=False,
            )

        if stage.on_context_overflow is ContextOverflowPolicy.COMPACT:
            kept = _compact(conversation)
        else:
            kept = _truncate(conversation, budget - estimate_tokens(system))

        return kept, ContextReport(
            estimated_input_tokens=estimate_tokens(system)
            + sum(estimate_tokens(message.text) for message in kept),
            budget_tokens=budget,
            policy_applied=stage.on_context_overflow,
        )

    # ------------------------------------------------------------------ money

    def _charge(
        self, stage: AgentStage, route: Route, usage: TokenUsage, started: datetime
    ) -> None:
        """Enforce the step budget. ``Budget.on_exceeded`` is ``abort``, always.

        Checked *after* the call, because what a completion costs is not knowable
        before it — the output length is the model's decision. That bounds the
        overrun at one response, which is also why ``max_output_tokens`` is capped
        by the descriptor rather than left open: the worst case is one reply's
        worth of overspend, not an unbounded one.
        """
        budget = stage.budget
        if budget is None:
            return

        if budget.max_tokens is not None and _total(usage) > budget.max_tokens:
            raise StepFailure(
                "budget-exceeded",
                f"the step used {_total(usage)} tokens against a cap of {budget.max_tokens}",
                retryable=False,
            )
        if budget.max_usd is not None:
            spent = route.descriptor.prices.usd(usage)
            if spent > budget.max_usd:
                raise StepFailure(
                    "budget-exceeded",
                    f"the step spent ${spent:.4f} against a cap of ${budget.max_usd}",
                    retryable=False,
                )
        if budget.max_wall_clock_seconds is not None:
            elapsed = (self.clock() - started).total_seconds()
            if elapsed > budget.max_wall_clock_seconds:
                raise StepFailure(
                    "budget-exceeded",
                    f"the step ran for {elapsed:.1f}s against a cap of "
                    f"{budget.max_wall_clock_seconds}s",
                    retryable=False,
                )

    # ----------------------------------------------------------------- output

    def _output(
        self,
        *,
        stage: AgentStage,
        prompt: prompt_registry.Prompt,
        route: Route,
        usage: TokenUsage,
        context: ContextReport,
        repairs: tuple[str, ...],
        result: JsonValue,
        turns: int,
    ) -> JsonValue:
        """One shape, always — the rule ``ScriptHandler`` states and keeps.

        ``result`` is the validated structured value, or null for a step with no
        schema; ``text`` is what the model actually said, whether or not it
        parsed. Both, because ``$ba.json.result.confidence`` and "show me what it
        wrote" are different questions, and a workflow reading the first should not
        depend on anyone having kept the second.
        """
        cost = route.descriptor.prices.usd(usage)
        return {
            "role": stage.role,
            "prompt_version": prompt.version,
            "prompt_origin": prompt.origin.value,
            # What answered, as the *provider* named it — an alias resolves to a
            # dated snapshot, and "claude-sonnet-5" in a run record six months old
            # does not identify the weights that wrote it. ``model_requested`` is
            # what the workflow asked for, and ``models_exhausted`` is the trail
            # between them, so a quota fallback is legible from the record alone.
            "model": route.response.model,
            "model_requested": stage.model.model,
            "models_exhausted": list(route.attempted),
            "turns": turns,
            "stop_reason": route.response.stop_reason.value,
            "text": route.response.text,
            "result": result,
            "repairs": list(repairs),
            "context": context.as_json(),
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "output_tokens": usage.output_tokens,
            # A string, as ``RunnerHandler`` does it: a Decimal has no JSON form
            # that survives a round trip, and a float cost is a budget that drifts
            # (``domain.budget``).
            "usd": str(cost.quantize(Decimal("0.000001"))),
        }


def validate_stage(
    stage: AgentStage,
    *,
    model: ModelPort,
    prompts: prompt_registry.PromptRegistry,
    schemas: ResponseSchemas | None = None,
    tools: ToolSurface | None = None,
) -> None:
    """Check everything about an agent stage that is knowable before it runs.

    Separate from ``__call__`` so it can be run over a whole workflow at load
    time. A stage naming a role with no prompt, a schema nobody registered, a tool
    nothing provides, or a model that cannot do what the step requires is a
    configuration error — and discovering it three stages into a sprint means the
    two agent steps before it have already been paid for.

    Raises the underlying ``PromptNotFoundError`` / ``SchemaNotFoundError`` /
    ``UnknownToolError`` / ``PortError`` rather than a ``StepFailure``: nothing is
    executing yet, so there is no step for it to be a failure of.
    """
    prompts.get(stage.role, stage.prompt_version)
    if stage.response_schema is not None:
        (schemas or ResponseSchemas()).model_for(stage.response_schema)
    (tools or ToolSurface()).resolve(stage.tools)
    validate_route(model, stage.model)


def _correction(invalid: ResponseInvalidError) -> str:
    """The feedback a second turn carries.

    States the failure and repeats the requirement, and says nothing about what
    the model *should* have answered — a correction that supplies content is one
    that gets echoed back as if the model had concluded it.
    """
    return (
        f"Your previous reply could not be used: {invalid.explanation}.\n\n"
        "Reply again with a single JSON object satisfying the schema, and nothing else. "
        "Do not apologise or explain; emit only the object."
    )


def _compact(conversation: list[Message]) -> list[Message]:
    """Keep the first and last messages; replace the middle with a marker.

    The first message is the task and the last is what is being answered;
    everything between them is superseded working. Marked rather than removed —
    ``ContextOverflowPolicy`` exists because v1 dropped context silently, and the
    consequence was a model that appeared to ignore its instructions.
    """
    if len(conversation) <= 2:
        return conversation
    return [conversation[0], Message(role=MessageRole.USER, text=ELIDED), conversation[-1]]


def _truncate(conversation: list[Message], allowance: int) -> list[Message]:
    """Cut message text to fit an allowance, and mark every cut.

    Cuts the *end* of a message rather than the start. Which end is right is not
    obvious and this is the defensible choice: the beginning of a task states what
    is being asked, and a prompt that lost its opening is one where the model is
    answering a question nobody put.
    """
    if allowance <= 0:
        return [Message(role=conversation[-1].role, text=ELIDED)]

    kept: list[Message] = []
    remaining = allowance
    for message in conversation:
        cost = estimate_tokens(message.text)
        if cost <= remaining:
            kept.append(message)
            remaining -= cost
            continue
        # Characters rather than tokens, because the estimate is in characters
        # anyway and a cut expressed in tokens would need the tokenizer this
        # deliberately does not have.
        room = max(int(remaining * CHARS_PER_TOKEN) - len(ELIDED), 0)
        kept.append(Message(role=message.role, text=message.text[:room] + ELIDED))
        remaining = 0
    return kept


def _add(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    """Accumulate usage across turns. Every field, so none is quietly dropped."""
    return TokenUsage(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
        reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
    )


def _total(usage: TokenUsage) -> int:
    return (
        usage.input_tokens
        + usage.output_tokens
        + usage.cached_input_tokens
        + usage.reasoning_tokens
    )
