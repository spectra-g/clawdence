"""The provider adapter: the payload, the reply, and the error mapping.

No socket is opened here and none is needed — ``complete`` hands its payload to an
injected transport, which is the seam a cassette wraps. The one test that touches
``urllib`` at all is the URL-scheme refusal, and it refuses before it opens
anything.
"""

from __future__ import annotations

import io
import json
import urllib.error
from decimal import Decimal
from email.message import Message as EmailMessage
from typing import Any

import pytest
from pydantic import JsonValue

from clawdence.agent import (
    API_VERSION,
    CATALOGUE,
    DEFAULT_SECRET_NAME,
    ELIDED,
    AnthropicModels,
    ProviderHttpError,
    from_payload,
    to_payload,
)
from clawdence.agent.anthropic import read_http_error, require_secure
from clawdence.domain import ModelCapability
from clawdence.ports import (
    ContextWindowExceededError,
    Message,
    MessageRole,
    ModelRequest,
    NullSecrets,
    PermanentError,
    QuotaExhaustedError,
    RateLimitedError,
    SecretNotFoundError,
    StaticSecrets,
    StopReason,
    ToolSpec,
    TransientError,
    UnknownModelError,
)
from clawdence.ports.model import estimate_tokens
from tests.ports.factories import run

MODEL = "claude-sonnet-5"


def secrets() -> StaticSecrets:
    return StaticSecrets({DEFAULT_SECRET_NAME: "not-a-real-key"})


def request(**overrides: Any) -> ModelRequest:
    fields: dict[str, Any] = {
        "model": MODEL,
        "system": "you are a business analyst",
        "messages": (Message(role=MessageRole.USER, text="what is wanted?"),),
        "max_output_tokens": 1_024,
    }
    fields.update(overrides)
    return ModelRequest(**fields)


def reply(text: str = '{"ok": true}', **overrides: Any) -> dict[str, JsonValue]:
    answer: dict[str, JsonValue] = {
        "model": MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    answer.update(overrides)
    return answer


def provider(transport: Any) -> AnthropicModels:
    return AnthropicModels(secrets(), transport=transport)


def answering(answer: JsonValue) -> Any:
    async def transport(payload: JsonValue) -> JsonValue:
        return answer

    return transport


def raising(error: BaseException) -> Any:
    async def transport(payload: JsonValue) -> JsonValue:
        raise error

    return transport


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


def test_the_catalogue_answers_for_the_models_it_serves() -> None:
    descriptor = provider(answering(reply())).describe(MODEL)
    assert descriptor.model == MODEL
    assert ModelCapability.STRUCTURED_OUTPUT in descriptor.capabilities
    assert descriptor.context_window_tokens > 0
    assert descriptor.prices.input_usd > 0


def test_an_unknown_model_is_a_configuration_error() -> None:
    """A table that has gone stale fails a workflow at validation time, naming the
    model — a far better failure than the silent truncation it replaces."""
    with pytest.raises(UnknownModelError):
        provider(answering(reply())).describe("claude-opus-4")


def test_every_catalogued_model_can_price_a_dollar_cap() -> None:
    """A cap the adapter cannot evaluate is a cap that enforces nothing."""
    for descriptor in CATALOGUE.values():
        assert descriptor.prices.output_usd > 0
        assert descriptor.max_output_tokens <= descriptor.context_window_tokens


# --------------------------------------------------------------------------- #
# The payload
# --------------------------------------------------------------------------- #


def test_the_payload_carries_the_system_prompt_on_its_own_channel() -> None:
    payload = to_payload(request(), CATALOGUE[MODEL])
    assert isinstance(payload, dict)
    assert payload["system"] == "you are a business analyst"
    assert payload["messages"] == [{"role": "user", "content": "what is wanted?"}]


def test_max_tokens_is_capped_by_the_model() -> None:
    payload = to_payload(request(max_output_tokens=10**6), CATALOGUE[MODEL])
    assert isinstance(payload, dict)
    assert payload["max_tokens"] == CATALOGUE[MODEL].max_output_tokens


def test_temperature_is_sent_only_when_pinned() -> None:
    """Sent so an eval measures prompt changes rather than sampling noise; omitted
    otherwise so the provider's own default is not silently replaced."""
    assert "temperature" not in to_payload(request(), CATALOGUE[MODEL])  # type: ignore[operator]
    payload = to_payload(request(temperature=0.0), CATALOGUE[MODEL])
    assert isinstance(payload, dict)
    assert payload["temperature"] == 0.0


def test_seed_is_deliberately_not_sent() -> None:
    """This API has no such parameter. Inventing a field the provider ignores
    would make a workflow that pinned a seed look reproducible while being nothing
    of the kind."""
    payload = to_payload(request(seed=17), CATALOGUE[MODEL])
    assert isinstance(payload, dict)
    assert "seed" not in payload


def test_tools_travel_with_a_schema() -> None:
    payload = to_payload(
        request(tools=(ToolSpec(name="ask", description="ask", input_schema={"type": "object"}),)),
        CATALOGUE[MODEL],
    )
    assert isinstance(payload, dict)
    assert payload["tools"] == [
        {"name": "ask", "description": "ask", "input_schema": {"type": "object"}}
    ]


def test_a_tool_without_a_schema_gets_an_empty_object() -> None:
    """A missing ``input_schema`` is a 400 from the provider, which is a worse
    error than a tool that takes no arguments."""
    payload = to_payload(
        request(tools=(ToolSpec(name="ping", description="ping"),)), CATALOGUE[MODEL]
    )
    assert isinstance(payload, dict)
    tools = payload["tools"]
    assert isinstance(tools, list)
    assert tools[0]["input_schema"] == {"type": "object"}  # type: ignore[index,call-overload]


def test_the_payload_is_a_pure_function_of_the_request() -> None:
    """The digest that identifies a recorded interaction must not depend on which
    instance of the adapter produced it."""
    assert to_payload(request(), CATALOGUE[MODEL]) == to_payload(request(), CATALOGUE[MODEL])


# --------------------------------------------------------------------------- #
# The reply
# --------------------------------------------------------------------------- #


def test_text_blocks_are_joined() -> None:
    response = from_payload(
        reply(content=[{"type": "text", "text": "one "}, {"type": "text", "text": "two"}]),
        requested=MODEL,
    )
    assert response.text == "one two"


def test_unknown_block_types_are_ignored() -> None:
    """A provider adding a block type must not fail a completion whose text
    arrived intact."""
    response = from_payload(
        reply(content=[{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "hi"}]),
        requested=MODEL,
    )
    assert response.text == "hi"


def test_tool_calls_are_read() -> None:
    response = from_payload(
        reply(
            content=[{"type": "tool_use", "id": "t1", "name": "ask", "input": {"q": "why"}}],
            stop_reason="tool_use",
        ),
        requested=MODEL,
    )
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.tool_calls[0].name == "ask"
    assert response.tool_calls[0].arguments == {"q": "why"}


def test_the_resolved_model_is_reported_not_the_alias() -> None:
    """ "claude-sonnet-5" in a run record six months old does not identify the
    weights that wrote it."""
    response = from_payload(reply(model="claude-sonnet-5-20260714"), requested=MODEL)
    assert response.model == "claude-sonnet-5-20260714"


def test_a_reply_without_a_model_falls_back_to_what_was_asked() -> None:
    answer = dict(reply())
    del answer["model"]
    assert from_payload(answer, requested=MODEL).model == MODEL


def test_cache_reads_and_writes_are_both_counted_as_cached_input() -> None:
    """Priced differently from fresh input, and a ledger that cannot see the
    difference cannot explain its own numbers."""
    response = from_payload(
        reply(
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 50,
            }
        ),
        requested=MODEL,
    )
    assert response.usage.cached_input_tokens == 150


def test_missing_usage_is_zero_rather_than_a_crash() -> None:
    """Usage is metadata. A completion that arrived is not worth discarding because
    the token count was absent."""
    answer = dict(reply())
    del answer["usage"]
    assert from_payload(answer, requested=MODEL).usage.input_tokens == 0


def test_a_nonsense_usage_value_is_zero() -> None:
    response = from_payload(reply(usage={"input_tokens": "lots"}), requested=MODEL)
    assert response.usage.input_tokens == 0


@pytest.mark.parametrize(
    ("provider_reason", "expected"),
    [
        ("end_turn", StopReason.END_TURN),
        ("max_tokens", StopReason.MAX_TOKENS),
        ("tool_use", StopReason.TOOL_USE),
        ("stop_sequence", StopReason.STOP_SEQUENCE),
        ("pause_turn", StopReason.ABORTED),
        ("something_new", StopReason.ABORTED),
        (None, StopReason.ABORTED),
    ],
)
def test_stop_reasons_map_and_an_unknown_one_reads_as_aborted(
    provider_reason: str | None, expected: StopReason
) -> None:
    """A provider adding a reason must not turn every completion into a failure,
    and ``ABORTED`` is the honest reading of "it stopped for a reason we do not
    understand"."""
    assert from_payload(reply(stop_reason=provider_reason), requested=MODEL).stop_reason is expected


def test_a_reply_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(PermanentError, match="not a JSON object"):
        from_payload(["nope"], requested=MODEL)


def test_a_completion_goes_through_the_transport() -> None:
    sent: list[JsonValue] = []

    async def transport(payload: JsonValue) -> JsonValue:
        sent.append(payload)
        return reply("hello")

    response = run(provider(transport).complete(request()))
    assert response.text == "hello"
    assert sent and isinstance(sent[0], dict)
    assert sent[0]["model"] == MODEL


# --------------------------------------------------------------------------- #
# The error mapping
# --------------------------------------------------------------------------- #


def fails(
    status: int, error_type: str = "", detail: str = "", retry_after: float | None = None
) -> Any:
    return raising(
        ProviderHttpError(status, error_type=error_type, detail=detail, retry_after=retry_after)
    )


def test_an_exhausted_balance_is_quota_not_a_malformed_request() -> None:
    """It arrives as a 400 on this provider. Reading it as a bad payload would send
    somebody looking for a bug in the request."""
    with pytest.raises(QuotaExhaustedError) as caught:
        run(
            provider(
                fails(400, "invalid_request_error", "Your credit balance is too low")
            ).complete(request())
        )
    assert caught.value.retryable is False


def test_insufficient_quota_is_quota_too() -> None:
    """v1's exact marker, from a different provider. Cheap to keep."""
    with pytest.raises(QuotaExhaustedError):
        run(provider(fails(429, "insufficient_quota")).complete(request()))


def test_a_rate_limit_is_transient_and_keeps_the_interval() -> None:
    with pytest.raises(RateLimitedError) as caught:
        run(provider(fails(429, "rate_limit_error", retry_after=42.0)).complete(request()))
    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 42.0


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
def test_provider_trouble_is_transient(status: int) -> None:
    """529 is the overloaded signal and is not in the 5xx range every client
    already retries."""
    with pytest.raises(TransientError) as caught:
        run(provider(fails(status)).complete(request()))
    assert caught.value.kind == "model-provider-unavailable"


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_are_permanent(status: int) -> None:
    """An unconfigured credential is a deployment problem, and retrying makes it
    look like a flaky service for another two attempts."""
    with pytest.raises(PermanentError) as caught:
        run(provider(fails(status)).complete(request()))
    assert caught.value.kind == "model-auth-rejected"


def test_a_404_reads_as_an_unknown_model() -> None:
    with pytest.raises(UnknownModelError):
        run(provider(fails(404)).complete(request()))


def test_an_overlong_prompt_maps_to_the_context_window() -> None:
    with pytest.raises(ContextWindowExceededError):
        run(
            provider(
                fails(400, "invalid_request_error", "prompt is too long: 250000 tokens")
            ).complete(request())
        )


def test_anything_else_is_a_rejected_request() -> None:
    with pytest.raises(PermanentError) as caught:
        run(provider(fails(400, "invalid_request_error", "something odd")).complete(request()))
    assert caught.value.kind == "model-request-rejected"
    assert "invalid_request_error" in caught.value.message


def test_the_providers_message_never_reaches_the_error() -> None:
    """A provider's message quotes the request back, the request contains the
    prompt, and a prompt is where a pasted credential turns up (threat model T11)."""
    leak = "here is your prompt: sk-ant-api03-thisisnotarealkeybutlooksliketone"
    with pytest.raises(PermanentError) as caught:
        run(provider(fails(400, "invalid_request_error", leak)).complete(request()))
    assert "sk-ant" not in caught.value.message
    assert "sk-ant" not in str(caught.value)


def test_the_error_type_is_still_reachable_for_classification() -> None:
    error = ProviderHttpError(400, error_type="invalid_request_error", detail="credit balance")
    assert "credit balance" in error.markers
    assert "credit balance" not in str(error)


# --------------------------------------------------------------------------- #
# The HTTP layer, without HTTP
# --------------------------------------------------------------------------- #


def test_reading_an_http_error_keeps_the_type_the_message_and_retry_after() -> None:
    parsed = read_http_error(
        429,
        json.dumps(
            {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
        ).encode("utf-8"),
        "7",
    )
    assert parsed.status == 429
    assert parsed.error_type == "rate_limit_error"
    assert "slow down" in parsed.markers
    assert parsed.retry_after == 7.0


def test_a_body_that_is_not_the_expected_shape_still_yields_a_status() -> None:
    parsed = read_http_error(500, b'["unexpected"]', None)
    assert parsed.status == 500
    assert parsed.error_type == ""


def test_a_body_that_is_not_json_at_all_still_yields_a_status() -> None:
    """A gateway between here and the provider answers in HTML, and it does it at
    exactly the moment things are already going wrong."""
    parsed = read_http_error(502, b"<html>Bad Gateway</html>", None)
    assert parsed.status == 502
    assert parsed.markers.strip() == ""


def test_plaintext_is_refused_before_the_key_is_resolved() -> None:
    """This is the line that actually sends the credential, so it is the line that
    checks. A ``SecretProvider`` that would have failed anyway is not the control."""
    adapter = AnthropicModels(secrets(), base_url="http://provider.invalid")
    with pytest.raises(PermanentError) as caught:
        run(adapter.complete(request()))
    assert caught.value.kind == "insecure-provider-url"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.anthropic.com/v1/messages",
        "http://localhost:8080/v1/messages",
        "http://127.0.0.1:8080/v1/messages",
    ],
)
def test_https_and_loopback_are_allowed(url: str) -> None:
    """A test server on loopback is not a credential on the wire. Anything else is,
    which is why the allowance is two literal prefixes and not a flag."""
    require_secure(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.invalid/v1/messages",
        "http://127.0.0.1.evil.example/v1/messages",
        "http://localhost.evil.example/v1/messages",
        "http://evil.example/?x=http://127.0.0.1/",
        "ftp://provider.invalid/v1/messages",
        "//provider.invalid/v1/messages",
    ],
)
def test_everything_else_is_refused(url: str) -> None:
    """A prefix check was written here first and this caught it:
    ``http://127.0.0.1.evil.example/`` starts with ``http://127.0.0.1`` and is a
    different machine entirely."""
    with pytest.raises(PermanentError, match="insecure-provider-url"):
        require_secure(url)


def test_the_api_version_is_pinned() -> None:
    """The mechanism the provider offers for not having the response shape change
    underneath a deployed system."""
    assert API_VERSION == "2023-06-01"


def test_the_estimator_agrees_with_the_payload_it_guards() -> None:
    """Not a property of the adapter, but the one place both halves are visible: the
    estimate must run over the same text the payload carries."""
    built = to_payload(request(), CATALOGUE[MODEL])
    assert isinstance(built, dict)
    assert estimate_tokens(str(built["system"])) > 0


def test_prices_are_decimals_not_floats() -> None:
    """A budget that drifts is a budget that does not fire when it should."""
    assert isinstance(CATALOGUE[MODEL].prices.input_usd, Decimal)


def test_reading_a_real_http_error_needs_no_monkeypatching() -> None:
    """``_http_error`` is the two lines between the stdlib exception and the
    parsing, and it is worth covering with a real ``HTTPError`` rather than a
    stand-in — the body arrives through the file object, which is the part a
    hand-built double gets wrong."""
    from clawdence.agent.anthropic import _http_error

    body = json.dumps({"error": {"type": "overloaded_error", "message": "busy"}}).encode("utf-8")
    headers = EmailMessage()
    headers["retry-after"] = "3"
    error = urllib.error.HTTPError(
        "https://api.anthropic.com/v1/messages", 529, "", headers, io.BytesIO(body)
    )

    parsed = _http_error(error)
    assert parsed.status == 529
    assert parsed.error_type == "overloaded_error"
    assert parsed.retry_after == 3.0


def test_truncating_a_prompt_to_nothing_still_leaves_the_marker() -> None:
    """An allowance the role prompt alone has already spent. One elided message
    rather than an empty conversation, because ``ModelRequest`` requires at least
    one and a request that asked nothing is worse than one that says it was cut."""
    from clawdence.agent.handler import _truncate
    from clawdence.ports import Message, MessageRole

    cut = _truncate([Message(role=MessageRole.USER, text="a task")], 0)
    assert [message.text for message in cut] == [ELIDED]


def test_the_headers_pin_the_api_version_and_carry_the_resolved_key() -> None:
    headers = provider(answering(reply())).headers()
    assert headers["anthropic-version"] == API_VERSION
    assert headers["x-api-key"] == "not-a-real-key"
    assert headers["content-type"] == "application/json"


def test_a_missing_credential_fails_by_name_rather_than_as_a_401() -> None:
    """An unconfigured credential is a deployment problem. Retrying makes it look
    like a flaky service for another two attempts, so it never reaches the wire."""
    adapter = AnthropicModels(NullSecrets())
    with pytest.raises(SecretNotFoundError, match=DEFAULT_SECRET_NAME):
        adapter.headers()
