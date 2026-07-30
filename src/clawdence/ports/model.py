"""Talking to a model provider.

The eighth port, and the one the control plane spends money through. Everything
above it names a model as a string and knows nothing else: which provider serves
``claude-opus-5``, over what protocol, with which authentication, is decided once
in an adapter. v1 pinned the model per *agent* in ``openclaw.json`` and reached
the provider through a CLI it did not control, which is why "change the reviewer's
model" was a config edit in one place and a surprise in three others.

Four properties are stated here rather than left to each adapter, because each of
them decides how a *caller* has to behave:

**Quota exhaustion and rate limiting are different failures.** v1 hit
``insufficient_quota`` and treated it as a rate limit, so a dead billing account
looked like a busy provider for three retries and a delay. ``RateLimited`` is
transient — waiting changes the answer. ``QuotaExhausted`` is permanent for *this
model*, and permanent-but-fall-through is why ``ModelSelector.fallbacks`` exists:
retryability and fallback-ability are two questions, and collapsing them is what
made v1's backoff spin against an account that would never answer.

**A completion is deliberately not idempotent, and that is not an oversight.**
Every other write in this package collides on a caller-derived key. A completion
must not: the retry that matters is "the response failed its schema, ask again",
and a port that returned the recorded answer for a repeated request would make
that loop return the same malformed answer forever. Double-charging is prevented
one layer up instead, where it already is — the executor re-runs a stage only if
it did *not* previously succeed, so a resumed run does not re-ask a question it
has an answer to. Test-time determinism is the cassette's job
(``tests/harness/cassette.py``), which is a fixture, not a cache.

**The request carries no run identity.** No ``run_id``, no ``stage_id``, no
attempt number — and the omission is load-bearing twice over. A cassette keys on
a digest of the request, so a request carrying a run id would produce a cassette
that replays for exactly one run and misses for every other. And cost
attribution needs the stage, which the *handler* knows: putting it in the request
would push identity through a provider that has no use for it.

**Capabilities and prices are askable before anything is spent.** ``describe`` is
synchronous and total, so a workflow naming a model that cannot do structured
output fails validation rather than failing mysteriously three stages in — the
plan's requirement in §5 S12, and the reason ``ModelCapability`` was in the
domain model from S2. It is also where prices come from, so a dollar cap is
evaluated against the model that actually answered rather than the one the
workflow asked for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

from pydantic import Field, JsonValue

from clawdence.domain import DomainModel, ModelCapability, TokenUsage
from clawdence.ports.errors import PermanentError, TransientError

#: Characters per token, for the estimate used when nothing better is available.
#: Deliberately low: the estimator's job is to refuse a request that will not fit
#: *before* it is billed, and an estimate that runs under the true count is a
#: refusal that never happens. See ``estimate_tokens``.
CHARS_PER_TOKEN: Final = 3.5


class MessageRole(StrEnum):
    """Who said a message.

    Two values, not three. The role prompt is ``ModelRequest.system`` rather than
    a message with a ``system`` role, because every provider treats it as a
    separate channel with separate caching and separate precedence, and a
    conversation that can *contain* a system message is one where a tool result
    carrying attacker-influenced repo text can claim to be one.
    """

    USER = "user"
    ASSISTANT = "assistant"


class Message(DomainModel):
    """One turn of a conversation."""

    role: MessageRole
    text: str


class ToolSpec(DomainModel):
    """A tool offered to a model.

    ``input_schema`` is JSON Schema and travels as data. Nothing in this package
    executes a tool call — what a tool *is* belongs to ``agent.tools``, which
    ships empty and refuses an undeclared name, because a tool surface assembled
    by accident is an agent with a capability nobody granted it.
    """

    name: str
    description: str
    input_schema: JsonValue = None


class ToolCall(DomainModel):
    """A model asking for a tool to be run."""

    id: str
    name: str
    arguments: JsonValue = None


class StopReason(StrEnum):
    """Why a response ended.

    ``ABORTED`` is v1's ``stopReason: "aborted"``, which arrived after
    mid-stream aborts on large structured output and — this is the part worth
    keeping — *retained usable partial content*. Whether that partial content is
    salvaged is a per-step decision (``AgentStage.salvage_partial_output``), and
    it can only be a decision if the reason it stopped is a distinct value rather
    than an exception.
    """

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    TOOL_USE = "tool_use"
    STOP_SEQUENCE = "stop_sequence"
    ABORTED = "aborted"


#: Stop reasons that mean the model did not finish saying what it was saying, so
#: whatever text arrived is a fragment. Both of them can still carry usable
#: content, which is exactly why they are not exceptions.
INCOMPLETE: Final = frozenset({StopReason.MAX_TOKENS, StopReason.ABORTED})


class TokenPrice(DomainModel):
    """What tokens cost, per million.

    Shared by the model port and the runner (``runners.agent``), which is the
    whole reason it lives here rather than beside either caller: two cost
    formulas is two answers to "what did this run cost", and the one that is
    wrong is always the one nobody reads.
    """

    input_usd: Decimal = Field(ge=0)
    output_usd: Decimal = Field(ge=0)
    cached_input_usd: Decimal = Field(default=Decimal("0"), ge=0)

    def usd(self, usage: TokenUsage, *, unattributed: int = 0) -> Decimal:
        """Cost of this usage, plus tokens reported without a breakdown.

        Unattributed tokens are priced at the output rate — the more expensive of
        the two. A cap that errs towards firing early is still a cap; one that
        errs the other way is decoration.
        """
        return (
            self.input_usd * usage.input_tokens
            + self.output_usd * (usage.output_tokens + usage.reasoning_tokens + unattributed)
            + self.cached_input_usd * usage.cached_input_tokens
        ) / Decimal(1_000_000)


class ModelDescriptor(DomainModel):
    """What one model can do, and what it charges.

    Data rather than discovery. No provider exposes context windows and
    capabilities over an API in a form worth trusting, so this is a table an
    adapter maintains — and a table that is wrong fails a workflow at validation
    time with a name in the message, which is a far better failure than the
    silent truncation it replaces.
    """

    model: str
    capabilities: tuple[ModelCapability, ...] = ()

    #: Total input the model will accept, in tokens. What
    #: ``AgentStage.context_budget_tokens`` is checked against when it is absent,
    #: and the ceiling it may not exceed when it is present.
    context_window_tokens: int = Field(gt=0)

    #: Most the model will emit in one response.
    max_output_tokens: int = Field(gt=0)

    prices: TokenPrice


class ModelRequest(DomainModel):
    """One completion, fully specified.

    Contains everything that decides the answer and nothing that does not — see
    the module docstring on why run identity is absent. ``temperature`` and
    ``seed`` are here rather than defaulted by the adapter so that S21b's evals
    measure prompt changes instead of sampling noise, and so that two adapters
    cannot disagree about what "default temperature" means.
    """

    model: str

    #: The role prompt, from the registry. Its own channel, not a message.
    system: str

    messages: tuple[Message, ...] = Field(min_length=1)
    max_output_tokens: int = Field(gt=0)

    temperature: float | None = Field(default=None, ge=0, le=2)
    seed: int | None = None
    tools: tuple[ToolSpec, ...] = ()


class ModelResponse(DomainModel):
    """What came back.

    ``model`` is the model that *answered*, which is not always the one asked
    for: a quota fallback is invisible in the run record otherwise, and "why did
    this review read differently on Tuesday" is unanswerable without it.
    """

    model: str
    text: str
    stop_reason: StopReason
    usage: TokenUsage = TokenUsage()
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def incomplete(self) -> bool:
        return self.stop_reason in INCOMPLETE


def estimate_tokens(text: str) -> int:
    """A tokenizer-free upper-ish estimate of a string's token count.

    Every real tokenizer is a dependency with a vocabulary file per model
    family, downloaded at run time or vendored into the supply chain, and it
    would buy accuracy for a number this system only ever compares against a
    budget. So: characters over ``CHARS_PER_TOKEN``, rounded up, with the ratio
    set low enough to over-count.

    Over-counting is the whole design. This decides whether a request is *sent*,
    and the failure it prevents is a prompt that overruns the context window and
    is silently truncated by the provider — which surfaces as an agent that
    ignored half its instructions, three stages later, having been paid for.
    An estimate that runs high refuses a request that would have fitted, says so
    by name, and costs nothing.
    """
    return -(-len(text) // int(CHARS_PER_TOKEN)) if text else 0


class UnknownModelError(PermanentError):
    """A model name no adapter recognises.

    Permanent, and raised by ``describe`` rather than by ``complete``, so a typo
    in a workflow surfaces before the run starts instead of as a 404 from a
    provider halfway through a sprint.
    """

    def __init__(self, model: str) -> None:
        super().__init__("unknown-model", f"no configured provider serves a model named {model!r}")
        self.model = model


class CapabilityError(PermanentError):
    """A model was asked for something it cannot do.

    The failure ``ModelSelector.requires`` exists to produce. Swapping a model
    for a cheaper one that cannot emit structured output is a config change that
    would otherwise surface as unparseable responses and a repair loop burning
    turns on a model that was never going to comply.
    """

    def __init__(self, model: str, missing: Sequence[ModelCapability]) -> None:
        names = ", ".join(sorted(capability.value for capability in missing))
        super().__init__(
            "model-capability",
            f"{model!r} does not provide the required capability: {names}",
        )
        self.model = model
        self.missing = tuple(missing)


class QuotaExhaustedError(PermanentError):
    """The account cannot pay for this call.

    Permanent, because no amount of waiting adds credit — and this is the
    distinction v1 got wrong. It is nonetheless the failure that
    ``ModelSelector.fallbacks`` reacts to: routing tries the next model, which is
    a different question from whether *this* call is worth repeating.
    """

    def __init__(self, model: str, detail: str = "") -> None:
        super().__init__(
            "model-quota-exhausted",
            f"the account has no remaining quota for {model!r}" + (f": {detail}" if detail else ""),
        )
        self.model = model


class RateLimitedError(TransientError):
    """The provider is asking for less traffic.

    ``retry_after`` is the provider's own number when it supplied one. Kept
    rather than folded into the message because a caller that backs off for a
    guessed interval either waits too long or gets limited again.
    """

    def __init__(self, model: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(
            "model-rate-limited",
            f"the provider is rate limiting {model!r}"
            + (f"; it asked for {retry_after_seconds}s" if retry_after_seconds is not None else ""),
        )
        self.model = model
        self.retry_after_seconds = retry_after_seconds


class ContextWindowExceededError(PermanentError):
    """The request is larger than the model will accept.

    Permanent: the same request will not fit next time either. What to do about
    it is ``ContextOverflowPolicy``'s decision, made before the call by the
    handler — this exists for the case where the estimate said it would fit and
    the provider disagreed, which is the honest limit of a tokenizer-free
    estimate and is why the message carries both numbers when they are known.
    """

    def __init__(
        self, model: str, *, requested: int | None = None, limit: int | None = None
    ) -> None:
        detail = ""
        if requested is not None and limit is not None:
            detail = f": {requested} tokens against a window of {limit}"
        super().__init__("model-context-window", f"the request is too large for {model!r}{detail}")
        self.model = model
        self.requested = requested
        self.limit = limit


#: The one model ``Ports.fakes()`` knows about. Named so that it is obviously
#: not real — the ``NULL_PREFIX`` instinct applied to a model name, because a
#: fixture that says ``claude-opus-5`` is one somebody eventually points at a
#: provider. Priced at zero, and capable of everything, so a test asserting the
#: *capability* check writes a descriptor of its own rather than fighting this one.
FAKE_MODEL: Final = ModelDescriptor(
    model="fake-model",
    capabilities=tuple(ModelCapability),
    context_window_tokens=200_000,
    max_output_tokens=8_192,
    prices=TokenPrice(input_usd=Decimal("0"), output_usd=Decimal("0")),
)


class ModelPort(Protocol):
    """A provider of completions."""

    def describe(self, model: str) -> ModelDescriptor:
        """Capabilities, context window and prices for a model name.

        Synchronous, because every caller needs it during validation — before a
        run starts, and in the loader, where nothing may await. Raises
        ``UnknownModelError`` for a name this adapter does not serve, so an
        unrecognised model is a configuration error rather than a request.
        """
        ...

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Ask for a completion and return what came back.

        Raises ``QuotaExhaustedError``, ``RateLimitedError``,
        ``ContextWindowExceededError`` or another ``PortError`` for a call that
        did not produce a response. A model that answered *badly* — malformed
        JSON, a refusal, a truncated document — is a ``ModelResponse``, not an
        exception: repair and schema validation are the caller's, and a port that
        raised on unparseable text would make ``salvage_partial_output``
        unimplementable.
        """
        ...


class RefusingModel:
    """The default. Refuses, naming what to wire.

    Same reasoning as ``RefusingRunner`` and ``engine.UnimplementedHandler``,
    spelled out a third time because it keeps being the right answer: a model
    port that returned canned text would make an agent step look like it
    consulted a model, and a workflow that appears to have run is the most
    expensive way for an orchestrator to be wrong.
    """

    __slots__ = ()

    def describe(self, model: str) -> ModelDescriptor:
        raise UnknownModelError(model)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise PermanentError(
            "no-model-provider",
            f"no model provider is configured, so {request.model!r} cannot be reached — "
            f"wire clawdence.agent.AnthropicModels, or a ScriptedModel for tests",
        )


class ScriptedModel:
    """Canned replies, keyed by a fragment of the system prompt. The fake.

    Keyed on the *role prompt* rather than consumed in order, for the reason the
    cassette module gives about sequential fixtures: a test whose two agent
    stages are reordered, or whose engine interleaves them at S3b, would
    otherwise silently receive each other's answers and still pass. A fragment of
    the system prompt is the readable, order-free key — ``{"business analyst":
    ...}`` says which role is being answered.

    Every request is kept in ``requests``, which is what the tests asserting
    statelessness read: two attempts of one stage must arrive as two
    single-message conversations, not as one that grew.
    """

    __slots__ = ("_catalogue", "_default", "_fail_with", "_replies", "_requests")

    def __init__(
        self,
        replies: Mapping[str, str | ModelResponse] | None = None,
        *,
        default: str | ModelResponse | None = None,
        catalogue: Mapping[str, ModelDescriptor] | None = None,
    ) -> None:
        self._replies = dict(replies or {})
        self._default = default
        self._catalogue = dict(catalogue or {})
        self._requests: list[ModelRequest] = []
        self._fail_with: dict[str, BaseException] = {}

    # ------------------------------------------------------------- scripting

    def returns(self, fragment: str, reply: str | ModelResponse) -> None:
        self._replies[fragment] = reply

    def fail_with(self, model: str, error: BaseException | None) -> None:
        """Make one model's completions raise. How quota fallback is tested."""
        if error is None:
            self._fail_with.pop(model, None)
        else:
            self._fail_with[model] = error

    def knows(self, descriptor: ModelDescriptor) -> None:
        self._catalogue[descriptor.model] = descriptor

    # ------------------------------------------------------------------ port

    def describe(self, model: str) -> ModelDescriptor:
        known = self._catalogue.get(model)
        if known is None:
            raise UnknownModelError(model)
        return known

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._requests.append(request)

        failure = self._fail_with.get(request.model)
        if failure is not None:
            raise failure

        reply = self._match(request.system)
        if reply is None:
            raise PermanentError(
                "no-scripted-reply",
                f"this fake has no reply for a system prompt starting {request.system[:60]!r}",
            )
        if isinstance(reply, ModelResponse):
            # The script is written by a test that does not know which model
            # routing chose, so identity comes from the request.
            return reply.model_copy(update={"model": request.model})
        return ModelResponse(
            model=request.model,
            text=reply,
            stop_reason=StopReason.END_TURN,
            usage=TokenUsage(
                input_tokens=estimate_tokens(request.system)
                + sum(estimate_tokens(message.text) for message in request.messages),
                output_tokens=estimate_tokens(reply),
            ),
        )

    def _match(self, system: str) -> str | ModelResponse | None:
        folded = system.casefold()
        for fragment, reply in self._replies.items():
            if fragment.casefold() in folded:
                return reply
        return self._default

    @property
    def requests(self) -> Sequence[ModelRequest]:
        return tuple(self._requests)
