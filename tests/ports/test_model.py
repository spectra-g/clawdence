"""The scripted model against the contract, and every rule the port states."""

from __future__ import annotations

from decimal import Decimal

import pytest

from clawdence.domain import ModelCapability, TokenUsage
from clawdence.ports import (
    CHARS_PER_TOKEN,
    FAKE_MODEL,
    CapabilityError,
    ContextWindowExceededError,
    Message,
    MessageRole,
    ModelDescriptor,
    ModelRequest,
    ModelResponse,
    PermanentError,
    QuotaExhaustedError,
    RateLimitedError,
    RefusingModel,
    ScriptedModel,
    StopReason,
    TokenPrice,
    ToolCall,
    UnknownModelError,
    estimate_tokens,
)
from tests.ports.contract import ModelContract
from tests.ports.factories import run


def request(system: str = "you are a business analyst", text: str = "hello") -> ModelRequest:
    return ModelRequest(
        model=FAKE_MODEL.model,
        system=system,
        messages=(Message(role=MessageRole.USER, text=text),),
        max_output_tokens=64,
    )


class TestScriptedModel(ModelContract):
    @pytest.fixture
    def model(self) -> ScriptedModel:
        return ScriptedModel(
            {"business analyst": "an answer"},
            catalogue={FAKE_MODEL.model: FAKE_MODEL},
        )


# --------------------------------------------------------------------------- #
# The refusing default
# --------------------------------------------------------------------------- #


def test_the_default_provider_refuses_and_names_what_to_wire() -> None:
    """A model port returning canned text would make an agent step look like it
    consulted a model."""
    with pytest.raises(PermanentError) as caught:
        run(RefusingModel().complete(request()))
    assert caught.value.kind == "no-model-provider"
    assert "AnthropicModels" in caught.value.message
    assert caught.value.retryable is False


def test_the_default_provider_knows_no_models() -> None:
    with pytest.raises(UnknownModelError):
        RefusingModel().describe("claude-opus-5")


# --------------------------------------------------------------------------- #
# The fake
# --------------------------------------------------------------------------- #


def test_replies_are_keyed_on_the_role_prompt_not_on_order() -> None:
    """Two agent stages reordered — or interleaved at S3b — must not silently
    receive each other's answers."""
    model = ScriptedModel(
        {"business analyst": "requirements", "reviewing completed work": "a verdict"},
        catalogue={FAKE_MODEL.model: FAKE_MODEL},
    )
    second = run(model.complete(request(system="You are reviewing completed work"))).text
    first = run(model.complete(request(system="You are a business analyst"))).text
    assert (first, second) == ("requirements", "a verdict")


def test_an_unscripted_prompt_is_an_error_not_an_invented_answer() -> None:
    """The rule ``StubHandler`` follows: a fake that answered anything would let
    a test assert on work the test never described."""
    with pytest.raises(PermanentError) as caught:
        run(ScriptedModel(catalogue={FAKE_MODEL.model: FAKE_MODEL}).complete(request()))
    assert caught.value.kind == "no-scripted-reply"


def test_matching_is_case_insensitive() -> None:
    model = ScriptedModel({"BUSINESS ANALYST": "ok"}, catalogue={FAKE_MODEL.model: FAKE_MODEL})
    assert run(model.complete(request(system="you are a Business Analyst"))).text == "ok"


def test_a_scripted_response_takes_the_requested_model() -> None:
    """The script is written by a test that does not know which candidate routing
    chose, so identity comes from the request."""
    model = ScriptedModel(
        {"analyst": ModelResponse(model="whatever", text="{}", stop_reason=StopReason.END_TURN)},
        catalogue={FAKE_MODEL.model: FAKE_MODEL},
    )
    assert run(model.complete(request(system="analyst"))).model == FAKE_MODEL.model


def test_requests_are_recorded_in_order() -> None:
    model = ScriptedModel({"analyst": "ok"}, catalogue={FAKE_MODEL.model: FAKE_MODEL})
    run(model.complete(request(system="analyst", text="first")))
    run(model.complete(request(system="analyst", text="second")))
    assert [message.messages[0].text for message in model.requests] == ["first", "second"]


def test_one_model_can_be_made_to_fail() -> None:
    """How quota fallback is tested without a provider."""
    model = ScriptedModel({"analyst": "ok"}, catalogue={FAKE_MODEL.model: FAKE_MODEL})
    model.fail_with(FAKE_MODEL.model, QuotaExhaustedError(FAKE_MODEL.model))
    with pytest.raises(QuotaExhaustedError):
        run(model.complete(request(system="analyst")))

    model.fail_with(FAKE_MODEL.model, None)
    assert run(model.complete(request(system="analyst"))).text == "ok"


def test_the_fake_model_is_obviously_not_real() -> None:
    """A fixture that said ``claude-opus-5`` is one somebody eventually points at
    a provider — the ``NULL_PREFIX`` instinct applied to a model name."""
    assert FAKE_MODEL.model == "fake-model"
    assert FAKE_MODEL.prices.usd(TokenUsage(input_tokens=10**9)) == 0


# --------------------------------------------------------------------------- #
# Retryability, which is the whole point of the taxonomy
# --------------------------------------------------------------------------- #


def test_quota_is_permanent_and_a_rate_limit_is_not() -> None:
    """The distinction v1 got wrong: a dead billing account looked like a busy
    provider for three retries and a delay."""
    assert QuotaExhaustedError("m").retryable is False
    assert RateLimitedError("m").retryable is True


def test_a_rate_limit_keeps_the_provider_s_own_interval() -> None:
    """A caller that backs off for a guessed interval either waits too long or
    gets limited again."""
    assert RateLimitedError("m", 30.0).retry_after_seconds == 30.0
    assert "30.0s" in RateLimitedError("m", 30.0).message


def test_a_capability_error_names_what_is_missing() -> None:
    error = CapabilityError(
        "cheap-model", [ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING]
    )
    assert error.retryable is False
    assert "structured_output, tool_calling" in error.message


def test_a_context_window_error_carries_both_numbers() -> None:
    error = ContextWindowExceededError("m", requested=300_000, limit=200_000)
    assert error.retryable is False
    assert "300000 tokens against a window of 200000" in error.message


def test_an_unknown_model_is_a_configuration_error() -> None:
    error = UnknownModelError("clawd-opus")
    assert error.retryable is False
    assert error.model == "clawd-opus"


# --------------------------------------------------------------------------- #
# Estimation and pricing
# --------------------------------------------------------------------------- #


def test_the_estimate_runs_high_rather_than_low() -> None:
    """It decides whether a request is *sent*. An estimate that ran under the
    true count is a refusal that never happens."""
    text = "x" * 1_000
    assert estimate_tokens(text) > len(text) / 4
    assert CHARS_PER_TOKEN < 4


def test_the_estimate_of_nothing_is_nothing() -> None:
    assert estimate_tokens("") == 0


def test_prices_are_per_million_and_split_cached_input() -> None:
    prices = TokenPrice(
        input_usd=Decimal("3"), output_usd=Decimal("15"), cached_input_usd=Decimal("0.3")
    )
    cost = prices.usd(
        TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000, cached_input_tokens=1_000_000)
    )
    assert cost == Decimal("18.3")


def test_unattributed_tokens_are_priced_at_the_output_rate() -> None:
    """A cap that errs towards firing early is still a cap; one that errs the
    other way is decoration."""
    prices = TokenPrice(input_usd=Decimal("1"), output_usd=Decimal("10"))
    assert prices.usd(TokenUsage(), unattributed=1_000_000) == Decimal("10")


# --------------------------------------------------------------------------- #
# Response shape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reason", "incomplete"),
    [
        (StopReason.END_TURN, False),
        (StopReason.TOOL_USE, False),
        (StopReason.STOP_SEQUENCE, False),
        (StopReason.MAX_TOKENS, True),
        (StopReason.ABORTED, True),
    ],
)
def test_incomplete_is_the_two_reasons_that_leave_a_fragment(
    reason: StopReason, incomplete: bool
) -> None:
    """Both can still carry usable content, which is why they are not
    exceptions — v1's aborted sessions retained partial output."""
    assert ModelResponse(model="m", text="…", stop_reason=reason).incomplete is incomplete


def test_a_response_can_carry_tool_calls() -> None:
    response = ModelResponse(
        model="m",
        text="",
        stop_reason=StopReason.TOOL_USE,
        tool_calls=(ToolCall(id="t1", name="read_file", arguments={"path": "x"}),),
    )
    assert response.tool_calls[0].name == "read_file"


def test_a_descriptor_is_frozen() -> None:
    """Domain records are records. A descriptor a caller could edit is a price
    table that drifts per call site."""
    with pytest.raises(ValueError, match="frozen"):
        FAKE_MODEL.model = "something-else"  # type: ignore[misc]


def test_a_request_needs_at_least_one_message() -> None:
    """An empty conversation is a paid-for call that asked nothing."""
    with pytest.raises(ValueError, match="messages"):
        ModelRequest(model="m", system="s", messages=(), max_output_tokens=10)


def test_a_descriptor_with_no_capabilities_is_legal_and_fails_a_requiring_stage() -> None:
    """Declaring nothing is honest for a model nobody has checked; it just cannot
    be used by a step that requires something."""
    plain = ModelDescriptor(
        model="plain",
        context_window_tokens=1_000,
        max_output_tokens=100,
        prices=TokenPrice(input_usd=Decimal("0"), output_usd=Decimal("0")),
    )
    assert plain.capabilities == ()
