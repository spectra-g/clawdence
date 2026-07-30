"""Named response schemas, and the one thing their error messages must not carry."""

from __future__ import annotations

import json

import pytest

from clawdence.agent import (
    DEFAULT_SCHEMAS,
    Requirements,
    ResponseInvalidError,
    ResponseSchemas,
    SchemaNotFoundError,
)


def test_the_shipped_schemas_are_the_four_shipped_roles_produce() -> None:
    assert ResponseSchemas().names() == ("assessment", "plan", "requirements", "review")
    assert set(DEFAULT_SCHEMAS) == set(ResponseSchemas().names())


def test_an_unknown_schema_lists_what_is_registered() -> None:
    with pytest.raises(SchemaNotFoundError) as caught:
        ResponseSchemas().model_for("vibes")
    assert "registered schemas are assessment, plan, requirements, review" in str(caught.value)


def test_an_empty_registry_says_so_rather_than_listing_nothing() -> None:
    with pytest.raises(SchemaNotFoundError, match="none registered"):
        ResponseSchemas({}).model_for("requirements")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def test_a_valid_document_comes_back_normalised() -> None:
    """Two responses differing only in which optional fields the model bothered
    with should produce the same shape for a later stage to read."""
    validated = ResponseSchemas().validate("requirements", {"summary": "s", "confidence": 0.5})
    assert validated == {
        "summary": "s",
        "acceptance_criteria": [],
        "out_of_scope": [],
        "open_questions": [],
        "confidence": 0.5,
        "unusual_request": None,
    }


def test_a_missing_required_field_is_reported_by_path() -> None:
    with pytest.raises(ResponseInvalidError) as caught:
        ResponseSchemas().validate("requirements", {"summary": "s"})
    assert "confidence" in caught.value.explanation


def test_an_out_of_range_value_is_reported() -> None:
    """``confidence`` is read by ``when`` conditions, so its range is part of the
    contract rather than advice."""
    with pytest.raises(ResponseInvalidError):
        ResponseSchemas().validate("requirements", {"summary": "s", "confidence": 4})


def test_an_extra_field_is_rejected_rather_than_dropped() -> None:
    """A chatty model gets a correction it can act on; silently dropping the field
    would hide that the prompt and the schema disagree."""
    with pytest.raises(ResponseInvalidError) as caught:
        ResponseSchemas().validate(
            "requirements", {"summary": "s", "confidence": 0.5, "vibe": "good"}
        )
    assert "vibe" in caught.value.explanation


def test_a_document_that_is_not_an_object_fails_cleanly() -> None:
    with pytest.raises(ResponseInvalidError):
        ResponseSchemas().validate("requirements", ["not", "an", "object"])


def test_the_explanation_never_quotes_the_value() -> None:
    """Model output routinely quotes the request, and the request is where a
    pasted credential turns up (threat model T11). This message goes to a human
    *and* back to the model as a turn of feedback."""
    secret = "sk-ant-api03-thisisnotarealkeybutitlookslikeone"  # noqa: S105 - the point
    with pytest.raises(ResponseInvalidError) as caught:
        ResponseSchemas().validate(
            "requirements", {"summary": secret, "confidence": "not a number"}
        )
    assert secret not in str(caught.value)
    assert secret not in caught.value.explanation
    assert "confidence" in caught.value.explanation


def test_several_failures_are_all_reported() -> None:
    """One turn of feedback should fix everything, not the first thing."""
    with pytest.raises(ResponseInvalidError) as caught:
        ResponseSchemas().validate("assessment", {"size": "XL"})
    assert "size" in caught.value.explanation
    assert "approach" in caught.value.explanation


# --------------------------------------------------------------------------- #
# What the model is told
# --------------------------------------------------------------------------- #


def test_the_instruction_carries_the_generated_schema() -> None:
    """Appended to the request rather than described in the role prompt, so the
    shape and the validator cannot drift."""
    instruction = ResponseSchemas().instruction("review")
    assert "single JSON object" in instruction
    schema = json.loads(instruction[instruction.index("{") :])
    assert set(schema["properties"]) == {
        "verdict",
        "summary",
        "findings",
        "verified",
        "unverifiable",
    }
    assert schema["additionalProperties"] is False


def test_the_instruction_is_deterministic() -> None:
    """A cassette keys on the whole request, and the schema projection is part of
    it. Sorted keys are what make the digest stable across runs."""
    schemas = ResponseSchemas()
    assert schemas.instruction("plan") == schemas.instruction("plan")


# --------------------------------------------------------------------------- #
# The shapes themselves
# --------------------------------------------------------------------------- #


def test_requirements_permit_no_criteria_so_that_confidence_zero_is_expressible() -> None:
    """The role is told to answer confidence 0 for a request that is not a request
    for software work. A schema demanding criteria would make the only honest
    answer to "hello" unrepresentable."""
    assert Requirements(summary="not a work request", confidence=0.0).acceptance_criteria == ()


def test_a_plan_needs_at_least_one_step() -> None:
    """A plan with no steps is not a plan, and the runner would be handed it."""
    with pytest.raises(ValueError, match="steps"):
        ResponseSchemas().validate("plan", {"steps": []})


@pytest.mark.parametrize("verdict", ["approved", "changes_requested", "rejected"])
def test_a_review_has_three_verdicts_not_two(verdict: str) -> None:
    """ "The approach is right and something is wrong" is the common case, and
    collapsing it into a rejection throws away a fixable branch."""
    assert ResponseSchemas().validate("review", {"verdict": verdict, "summary": "s"})


def test_an_unrecognised_verdict_is_refused() -> None:
    """A value outside the set would make every guard reading it silently false."""
    with pytest.raises(ResponseInvalidError):
        ResponseSchemas().validate("review", {"verdict": "lgtm", "summary": "s"})
