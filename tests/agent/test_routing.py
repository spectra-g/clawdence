"""Capability validation over the whole chain, and quota fallback down it."""

from __future__ import annotations

from decimal import Decimal

import pytest

from clawdence.agent import Route, candidates
from clawdence.agent.routing import complete, validate
from clawdence.domain import ModelCapability, ModelSelector
from clawdence.ports import (
    CapabilityError,
    Message,
    MessageRole,
    ModelDescriptor,
    ModelRequest,
    QuotaExhaustedError,
    RateLimitedError,
    ScriptedModel,
    TokenPrice,
    UnknownModelError,
)
from tests.ports.factories import run

FREE = TokenPrice(input_usd=Decimal("0"), output_usd=Decimal("0"))


def descriptor(name: str, *capabilities: ModelCapability) -> ModelDescriptor:
    return ModelDescriptor(
        model=name,
        capabilities=capabilities,
        context_window_tokens=100_000,
        max_output_tokens=4_096,
        prices=FREE,
    )


def catalogue(*descriptors: ModelDescriptor) -> dict[str, ModelDescriptor]:
    return {item.model: item for item in descriptors}


def port(*descriptors: ModelDescriptor, reply: str = "ok") -> ScriptedModel:
    return ScriptedModel({"": reply}, catalogue=catalogue(*descriptors))


def build(chosen: ModelDescriptor) -> ModelRequest:
    return ModelRequest(
        model=chosen.model,
        system="you are a business analyst",
        messages=(Message(role=MessageRole.USER, text="hello"),),
        max_output_tokens=min(64, chosen.max_output_tokens),
    )


# --------------------------------------------------------------------------- #
# The candidate chain
# --------------------------------------------------------------------------- #


def test_candidates_are_the_model_then_its_fallbacks() -> None:
    selector = ModelSelector(model="primary", fallbacks=("second", "third"))
    assert candidates(selector) == ("primary", "second", "third")


def test_a_duplicated_fallback_is_tried_once() -> None:
    """Trying it twice would report two quota failures for one account, and make
    the record read as if two models were out of credit."""
    selector = ModelSelector(model="primary", fallbacks=("primary", "second", "second"))
    assert candidates(selector) == ("primary", "second")


# --------------------------------------------------------------------------- #
# Validation before anything is spent
# --------------------------------------------------------------------------- #


def test_validation_resolves_every_candidate() -> None:
    resolved = validate(
        port(descriptor("primary"), descriptor("second")),
        ModelSelector(model="primary", fallbacks=("second",)),
    )
    assert [item.model for item in resolved] == ["primary", "second"]


def test_a_fallback_that_cannot_do_the_work_fails_validation() -> None:
    """A fallback nobody checked fails the first time it is used, which is by
    construction the worst possible moment: the primary has just run out of quota,
    so the run is already degraded."""
    provider = port(
        descriptor("primary", ModelCapability.STRUCTURED_OUTPUT),
        descriptor("cheap"),
    )
    selector = ModelSelector(
        model="primary",
        fallbacks=("cheap",),
        requires=(ModelCapability.STRUCTURED_OUTPUT,),
    )
    with pytest.raises(CapabilityError) as caught:
        validate(provider, selector)
    assert caught.value.model == "cheap"
    assert "structured_output" in caught.value.message


def test_an_unknown_fallback_fails_validation() -> None:
    with pytest.raises(UnknownModelError):
        validate(
            port(descriptor("primary")),
            ModelSelector(model="primary", fallbacks=("typo-model",)),
        )


def test_a_capability_error_lists_every_missing_capability() -> None:
    with pytest.raises(CapabilityError) as caught:
        validate(
            port(descriptor("plain")),
            ModelSelector(
                model="plain",
                requires=(ModelCapability.VISION, ModelCapability.TOOL_CALLING),
            ),
        )
    assert "tool_calling, vision" in caught.value.message


# --------------------------------------------------------------------------- #
# Completion and fallback
# --------------------------------------------------------------------------- #


def test_the_primary_answers_and_nothing_is_recorded_as_attempted() -> None:
    route = run(
        complete(
            port(descriptor("primary")),
            ModelSelector(model="primary"),
            build,
        )
    )
    assert isinstance(route, Route)
    assert route.model == "primary"
    assert route.attempted == ()
    assert route.response.text == "ok"


def test_quota_exhaustion_moves_down_the_chain_and_says_so() -> None:
    """Without ``attempted``, a quota fallback is invisible, and "why did the
    review read differently yesterday" has no answer in the record."""
    provider = port(descriptor("primary"), descriptor("second"))
    provider.fail_with("primary", QuotaExhaustedError("primary"))

    route = run(complete(provider, ModelSelector(model="primary", fallbacks=("second",)), build))
    assert route.model == "second"
    assert route.attempted == ("primary",)


def test_a_rate_limit_does_not_move_down_the_chain() -> None:
    """This model will answer shortly. Falling through changes the answer for no
    reason and pays more for it — and whether to wait is the engine's declared
    ``RetryPolicy``, not a second policy here."""
    provider = port(descriptor("primary"), descriptor("second"))
    provider.fail_with("primary", RateLimitedError("primary", 5.0))

    with pytest.raises(RateLimitedError):
        run(complete(provider, ModelSelector(model="primary", fallbacks=("second",)), build))


def test_every_candidate_exhausted_still_reports_quota_and_names_the_chain() -> None:
    """ "claude-opus-5 has no quota" is a misleading summary of an account that has
    none for anything."""
    provider = port(descriptor("primary"), descriptor("second"))
    provider.fail_with("primary", QuotaExhaustedError("primary"))
    provider.fail_with("second", QuotaExhaustedError("second"))

    with pytest.raises(QuotaExhaustedError) as caught:
        run(complete(provider, ModelSelector(model="primary", fallbacks=("second",)), build))
    assert "and so is every fallback: primary, second" in caught.value.message
    assert caught.value.retryable is False


def test_the_request_is_built_per_candidate() -> None:
    """``max_output_tokens`` is capped by what the model will emit, so a fallback
    with a smaller limit must not be sent a request built for the primary's."""
    small = ModelDescriptor(
        model="small",
        context_window_tokens=8_000,
        max_output_tokens=256,
        prices=FREE,
    )
    provider = port(descriptor("primary"), small)
    provider.fail_with("primary", QuotaExhaustedError("primary"))

    def sized(chosen: ModelDescriptor) -> ModelRequest:
        return ModelRequest(
            model=chosen.model,
            system="you are a business analyst",
            messages=(Message(role=MessageRole.USER, text="hello"),),
            max_output_tokens=min(4_096, chosen.max_output_tokens),
        )

    run(complete(provider, ModelSelector(model="primary", fallbacks=("small",)), sized))
    assert [request.max_output_tokens for request in provider.requests] == [4_096, 256]


def test_validation_happens_before_the_first_call() -> None:
    """A run that has spent nothing is the only place a capability mismatch is
    cheap to discover."""
    provider = port(descriptor("plain"))
    with pytest.raises(CapabilityError):
        run(
            complete(
                provider,
                ModelSelector(model="plain", requires=(ModelCapability.VISION,)),
                build,
            )
        )
    assert provider.requests == ()
