"""A real provider, over the standard library.

No SDK, and that is a decision rather than an accident. Every runtime dependency
in this project is pinned exactly and reviewed as supply chain (S1, §3.8), and a
provider SDK brings a transitive tree — an HTTP client, an async compatibility
shim, a JSON accelerator — into the process that holds every credential in the
system. What it would buy is streaming, typed models and retry/backoff. M1's agent
steps are single-shot requests for a structured document, so streaming buys
nothing; the types are ``ports.model``'s, which are the ones the rest of the system
speaks; and retry belongs to the engine's declared ``RetryPolicy``, not to a second
policy inside a library (``agent.routing`` says why two retry loops multiply).

What it costs is this file: a JSON POST, an error mapping, and a table.

**The transport is a seam, and that is what makes the tests free.** ``complete``
turns a ``ModelRequest`` into a payload, hands the payload to a callable, and turns
the answer back into a ``ModelResponse``. The default callable posts it; the tests
pass ``Cassette.play``, which is the "S12 plugs its real transport into ``play``"
that ``tests/harness/cassette.py`` was written for. Nothing about record/replay
appears in this file, and the suite reaches no socket.

**The blocking call runs in a thread.** ``urllib`` is synchronous and the control
plane is an event loop; a 600-second POST on the loop thread would stall every
other run, the watchdog included. ``asyncio.to_thread`` is the whole of the fix
and it is honest about what it is — one OS thread per in-flight completion, which
is fine at the concurrency an orchestrator of coding agents runs at.

**Error messages carry the provider's error *type*, never its text.** A provider's
message quotes the request back, and the request contains the prompt, and a prompt
is where a pasted credential turns up (threat model T11). Redacting it would work
and is what the cassette does on the way in; not carrying it is simpler and cannot
be got wrong. The status code and the ``error.type`` slug are enough to act on,
which is all ``kind`` is for.

**Substring matching on the provider's error type is deliberate here and nowhere
else.** ``ports.errors`` says a caller must never guess retryability from a
message — and this is not a caller. An adapter is the only code entitled to know
that this provider signals an exhausted balance as a 400 rather than a 402, and
translating that into ``QuotaExhaustedError`` once is precisely the point of having
an adapter at all.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any, Final

from pydantic import JsonValue

from clawdence.domain import ModelCapability, TokenUsage
from clawdence.ports import (
    ContextWindowExceededError,
    ModelDescriptor,
    ModelRequest,
    ModelResponse,
    PermanentError,
    QuotaExhaustedError,
    RateLimitedError,
    SecretProvider,
    StopReason,
    TokenPrice,
    TransientError,
    UnknownModelError,
)

DEFAULT_BASE_URL: Final = "https://api.anthropic.com"

#: The API version header. Pinned, because it is the mechanism the provider offers
#: for not having the response shape change underneath a deployed system.
API_VERSION: Final = "2023-06-01"

#: The secret name resolved through ``SecretProvider``. A name, not a value, and
#: never held on this object — ``ports.secrets`` exists so that "where does a
#: credential become an ordinary string" is one grep, and the answer here is one
#: line inside one method.
DEFAULT_SECRET_NAME: Final = "anthropic-api-key"  # noqa: S105 - a name to look up, not a value

#: v1 needed 600s after mid-stream aborts on large structured output. Kept, and
#: it is a *transport* timeout — the step's own deadline is
#: ``StageBase.timeout_seconds``, enforced by the executor, which cancels this.
DEFAULT_TIMEOUT_SECONDS: Final = 600.0

_ALL_TEXT: Final = (
    ModelCapability.TOOL_CALLING,
    ModelCapability.STRUCTURED_OUTPUT,
    ModelCapability.LONG_CONTEXT,
    ModelCapability.VISION,
)


def _descriptor(
    model: str,
    *,
    input_usd: str,
    output_usd: str,
    cached_input_usd: str,
    context_window: int = 200_000,
    max_output: int = 64_000,
    capabilities: tuple[ModelCapability, ...] = _ALL_TEXT,
) -> ModelDescriptor:
    return ModelDescriptor(
        model=model,
        capabilities=capabilities,
        context_window_tokens=context_window,
        max_output_tokens=max_output,
        prices=TokenPrice(
            input_usd=Decimal(input_usd),
            output_usd=Decimal(output_usd),
            cached_input_usd=Decimal(cached_input_usd),
        ),
    )


#: The table. Data an adapter maintains, because no provider exposes context
#: windows, capabilities and prices over an API in a form worth trusting — and a
#: table that has gone stale fails a workflow at validation time, naming the model,
#: which is a far better failure than the silent truncation it replaces.
#:
#: Prices are USD per million tokens. Verify them against the provider's pricing
#: page before relying on a dollar cap; a cap evaluated against a stale price is a
#: cap that fires at the wrong number.
CATALOGUE: Final[Mapping[str, ModelDescriptor]] = {
    descriptor.model: descriptor
    for descriptor in (
        _descriptor("claude-opus-5", input_usd="5", output_usd="25", cached_input_usd="0.5"),
        _descriptor("claude-sonnet-5", input_usd="3", output_usd="15", cached_input_usd="0.3"),
        _descriptor(
            "claude-haiku-4-5-20251001",
            input_usd="1",
            output_usd="5",
            cached_input_usd="0.1",
            max_output=32_000,
        ),
    )
}

#: ``error.type`` values, and fragments of them, that mean the account cannot pay.
#: The credit-balance case arrives as an ``invalid_request_error``, which is why a
#: fragment list exists at all rather than a set of types.
_QUOTA_MARKERS: Final = ("credit balance", "insufficient_quota", "billing", "payment")

#: Status codes that mean "later". 529 is the provider's overloaded signal and is
#: not in the 5xx range every client already retries.
_TRANSIENT_STATUS: Final = frozenset({408, 409, 500, 502, 503, 504, 529})

#: ``stop_reason`` as the API spells it, mapped to the port's vocabulary. An
#: unrecognised value becomes ``ABORTED`` rather than raising, because a provider
#: adding a reason must not turn every completion into a failure — and ``ABORTED``
#: is the honest reading of "it stopped for a reason we do not understand".
_STOP_REASONS: Final[Mapping[str, StopReason]] = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "tool_use": StopReason.TOOL_USE,
    "pause_turn": StopReason.ABORTED,
    "refusal": StopReason.END_TURN,
}

Transport = Callable[[JsonValue], Awaitable[JsonValue]]

#: The only hosts a credential may be sent to in plaintext. A closed set of *host
#: names* rather than a flag, because "allow insecure transport" is a setting
#: somebody eventually turns on in production to get past a proxy, and a local stub
#: is the only case that legitimately needs it.
_LOOPBACK: Final = frozenset({"localhost", "127.0.0.1", "::1"})


def require_secure(url: str) -> None:
    """Refuse to put a credential on a plaintext connection.

    A free function so it is testable without a socket — which matters, because the
    suite blocks TCP outright (``tests/conftest``) and a test that proved this by
    *attempting* a connection would be asserting on the guard rather than on the
    rule. Called from the method that actually sends the request rather than from
    the constructor, so there is no window in which a reconfigured base URL is
    trusted.

    The host is compared after parsing rather than by prefix, and that is not
    fastidiousness: ``http://127.0.0.1.evil.example/`` starts with
    ``http://127.0.0.1`` and is a different machine entirely. A prefix check here
    was written first and a test caught it, which is the argument for the test more
    than for the fix.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (parsed.hostname or "").casefold() in _LOOPBACK:
        return
    raise PermanentError(
        "insecure-provider-url",
        f"refusing to send credentials to {url!r} over plaintext",
    )


class ProviderHttpError(Exception):
    """Raised by the transport and translated by ``complete``.

    Its own type so that the transport seam stays a plain
    ``JsonValue -> JsonValue`` callable: a cassette wrapping it does not have to
    know the provider's error taxonomy, and a test can raise this to exercise the
    mapping without a socket.

    The provider's free-text message is held in ``_detail`` and reachable only
    through ``markers``, which is lowercased and used for matching. It is not in
    ``str(self)`` and no translation puts it in an outgoing message — because that
    text quotes the request back, and the request is the prompt.
    """

    def __init__(
        self,
        status: int,
        error_type: str = "",
        detail: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"HTTP {status} ({error_type or 'no error type'})")
        self.status = status
        self.error_type = error_type
        self.retry_after = retry_after
        self._detail = detail

    @property
    def markers(self) -> str:
        """Type and message, folded, for classification only."""
        return f"{self.error_type} {self._detail}".casefold()


class AnthropicModels:
    """``ModelPort`` over the Messages API."""

    __slots__ = ("_base_url", "_catalogue", "_secret_name", "_secrets", "_timeout", "_transport")

    def __init__(
        self,
        secrets: SecretProvider,
        *,
        secret_name: str = DEFAULT_SECRET_NAME,
        base_url: str = DEFAULT_BASE_URL,
        catalogue: Mapping[str, ModelDescriptor] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Transport | None = None,
    ) -> None:
        self._secrets = secrets
        self._secret_name = secret_name
        self._base_url = base_url.rstrip("/")
        self._catalogue = dict(CATALOGUE if catalogue is None else catalogue)
        self._timeout = timeout_seconds
        self._transport = transport if transport is not None else self.post

    # ------------------------------------------------------------------ port

    def describe(self, model: str) -> ModelDescriptor:
        descriptor = self._catalogue.get(model)
        if descriptor is None:
            raise UnknownModelError(model)
        return descriptor

    async def complete(self, request: ModelRequest) -> ModelResponse:
        descriptor = self.describe(request.model)
        payload = to_payload(request, descriptor)
        try:
            answer = await self._transport(payload)
        except ProviderHttpError as exc:
            raise _translate(exc, request.model) from None
        return from_payload(answer, requested=request.model)

    # ------------------------------------------------------------- transport

    def headers(self) -> dict[str, str]:
        """The request headers, including the resolved credential.

        A method rather than four lines inside the POST so that the one place a
        secret becomes an ordinary string is testable — the POST itself opens a
        socket and this suite has none. ``reveal()`` is called here and the result
        is not stored, so the process holds no long-lived copy of the key outside
        the ``SecretProvider``'s own keeping.
        """
        return {
            "content-type": "application/json",
            "accept": "application/json",
            "anthropic-version": API_VERSION,
            "x-api-key": self._secrets.resolve(self._secret_name).reveal(),
        }

    async def post(self, payload: JsonValue) -> JsonValue:
        """POST the payload to ``/v1/messages``, off the event loop."""
        return await asyncio.to_thread(self._post_blocking, payload)

    def _post_blocking(self, payload: JsonValue) -> JsonValue:
        url = f"{self._base_url}/v1/messages"
        require_secure(url)

        appeal = urllib.request.Request(  # noqa: S310 - scheme checked immediately above
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=self.headers(),
        )

        try:
            with urllib.request.urlopen(appeal, timeout=self._timeout) as response:  # noqa: S310
                decoded: JsonValue = json.loads(response.read().decode("utf-8"))
                return decoded
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            # A connection that never completed. Transient by nature: DNS, a reset,
            # a timeout. Nothing about the request is known to be wrong.
            raise TransientError(
                "model-unreachable", f"could not reach {self._base_url}: {exc.__class__.__name__}"
            ) from None
        except ValueError as exc:
            raise PermanentError(
                "model-response-unreadable", f"the provider's reply was not JSON: {exc}"
            ) from None


# --------------------------------------------------------------- translation


def to_payload(request: ModelRequest, descriptor: ModelDescriptor) -> JsonValue:
    """A ``ModelRequest`` as the Messages API wants it.

    A free function so that a cassette key is a pure function of the request: the
    digest that identifies a recorded interaction must not depend on which
    instance of the adapter produced it, or on anything held on that instance.
    """
    payload: dict[str, JsonValue] = {
        "model": request.model,
        "system": request.system,
        "max_tokens": min(request.max_output_tokens, descriptor.max_output_tokens),
        "messages": [
            {"role": message.role.value, "content": message.text} for message in request.messages
        ],
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.tools:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema
                if tool.input_schema is not None
                else {"type": "object"},
            }
            for tool in request.tools
        ]
    # ``seed`` is deliberately not sent: this API has no such parameter, and
    # inventing a field the provider ignores would make a workflow that pinned a
    # seed look reproducible while being nothing of the kind. It stays in
    # ``ModelRequest`` because it is part of the *declaration* an eval reads, and
    # ``ModelSelector.requires`` is where a step says it needs determinism.
    return payload


def from_payload(answer: JsonValue, *, requested: str) -> ModelResponse:
    """A Messages API reply as a ``ModelResponse``.

    Text is every ``text`` block joined; tool calls are every ``tool_use`` block.
    Unknown block types are ignored rather than rejected — a provider adding a
    block type must not fail a completion whose text arrived intact.
    """
    if not isinstance(answer, dict):
        raise PermanentError(
            "model-response-unreadable", "the provider's reply was not a JSON object"
        )

    content = answer.get("content")
    blocks: list[Any] = content if isinstance(content, list) else []

    text = "".join(
        str(block["text"])
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text" and "text" in block
    )
    calls = tuple(
        {
            "id": str(block.get("id", "")),
            "name": str(block.get("name", "")),
            "arguments": block.get("input"),
        }
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "tool_use"
    )

    raw_usage = answer.get("usage")
    usage_fields: Mapping[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}

    return ModelResponse.model_validate(
        {
            # The model the provider says answered, not the one asked for. They
            # differ when an alias resolves to a dated snapshot, and the run record
            # wants the resolved one.
            "model": str(answer.get("model") or requested),
            "text": text,
            "stop_reason": _STOP_REASONS.get(
                str(answer.get("stop_reason") or ""), StopReason.ABORTED
            ),
            "usage": TokenUsage(
                input_tokens=_count(usage_fields.get("input_tokens")),
                output_tokens=_count(usage_fields.get("output_tokens")),
                cached_input_tokens=_count(usage_fields.get("cache_read_input_tokens"))
                + _count(usage_fields.get("cache_creation_input_tokens")),
            ),
            "tool_calls": calls,
        }
    )


def _count(value: Any) -> int:
    """A usage number, or zero. Never a crash: usage is metadata, and a completion
    that arrived is not worth discarding because the token count was absent."""
    return value if isinstance(value, int) and value >= 0 else 0


def _http_error(exc: urllib.error.HTTPError) -> ProviderHttpError:
    """Pull the three things worth keeping out of an ``HTTPError``."""
    try:
        body = exc.read()
    except OSError:  # pragma: no cover - a body that will not read
        body = b""
    return read_http_error(exc.code, body, exc.headers.get("retry-after"))


def read_http_error(status: int, body: bytes, retry_after: str | None) -> ProviderHttpError:
    """Read the status, the error type and ``retry-after``, and nothing else.

    Split from ``_http_error`` so it is testable from plain values rather than from
    a hand-built ``HTTPError`` — the parsing is where the bugs are, and mocking a
    stdlib exception's ``read`` to reach it is how a test ends up asserting on the
    mock. The provider's free-text message is confined to ``detail`` here rather
    than later, so there is no code path on which it can be logged by mistake.
    """
    error_type = ""
    detail = ""
    try:
        document = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        document = None
    if isinstance(document, dict):
        described = document.get("error")
        if isinstance(described, dict):
            error_type = str(described.get("type", ""))
            detail = str(described.get("message", ""))

    parsed_retry: float | None = None
    if retry_after is not None:
        try:
            parsed_retry = float(retry_after)
        except ValueError:  # pragma: no cover - a date-formatted retry-after
            parsed_retry = None

    return ProviderHttpError(status, error_type=error_type, detail=detail, retry_after=parsed_retry)


def _translate(exc: ProviderHttpError, model: str) -> Exception:
    """One HTTP failure, as the port's taxonomy.

    Order matters: quota is checked before the generic 400 handling, because the
    exhausted-balance case *is* a 400 on this provider and reading it as a
    malformed request would send somebody looking for a bug in the payload.
    """
    marker = exc.markers

    if any(fragment in marker for fragment in _QUOTA_MARKERS):
        return QuotaExhaustedError(model, exc.error_type)
    if exc.status == 429:
        return RateLimitedError(model, exc.retry_after)
    if exc.status in _TRANSIENT_STATUS:
        return TransientError(
            "model-provider-unavailable", f"the provider returned HTTP {exc.status} for {model!r}"
        )
    if exc.status in (401, 403):
        return PermanentError(
            "model-auth-rejected",
            f"the provider rejected the credentials for {model!r} (HTTP {exc.status})",
        )
    if exc.status == 404:
        return UnknownModelError(model)
    if "prompt is too long" in marker or "max_tokens" in marker:
        return ContextWindowExceededError(model)
    return PermanentError(
        "model-request-rejected",
        f"the provider rejected the request for {model!r} with HTTP {exc.status}"
        + (f" ({exc.error_type})" if exc.error_type else ""),
    )
