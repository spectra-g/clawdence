"""Round-trip serialisation — S2's stated verification.

Every contract must survive ``model → JSON → model`` unchanged. This is the
property everything downstream assumes: the state store writes these as JSON,
the runner protocol crosses a process boundary as JSON, and the audit log
replays as JSON. A type that loses a field or reshapes one in transit breaks
all three, and it breaks them quietly.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from tests.domain.samples import SAMPLES

CASES = sorted(SAMPLES.items())


@pytest.mark.parametrize(("name", "instance"), CASES, ids=[name for name, _ in CASES])
def test_json_round_trip_is_lossless(name: str, instance: BaseModel) -> None:
    payload = instance.model_dump_json()
    restored = type(instance).model_validate_json(payload)
    assert restored == instance


@pytest.mark.parametrize(("name", "instance"), CASES, ids=[name for name, _ in CASES])
def test_round_trip_is_stable_under_repetition(name: str, instance: BaseModel) -> None:
    """A second pass must produce byte-identical JSON.

    Catches the case where deserialising coerces a value into something that
    serialises differently — a ``Decimal`` becoming a float, a tuple becoming a
    list of a different shape. One round trip can hide that; two cannot.
    """
    once = instance.model_dump_json()
    twice = type(instance).model_validate_json(once).model_dump_json()
    assert once == twice


@pytest.mark.parametrize(("name", "instance"), CASES, ids=[name for name, _ in CASES])
def test_json_payload_is_plain_data(name: str, instance: BaseModel) -> None:
    """The serialised form must contain no Python-specific encoding.

    ``json.loads`` succeeding proves the payload is portable — which matters
    because the schemas in ``schemas/`` promise exactly that to consumers that
    are not this codebase.
    """
    assert isinstance(json.loads(instance.model_dump_json()), dict)


def test_decimal_money_survives_as_an_exact_value() -> None:
    """Money must not become a float anywhere in the round trip.

    A budget is a hard cap that has to fire. Binary floating point accumulates
    drift across a run's cost entries, and a cap that drifts is a cap that lets
    a run through it.
    """
    from decimal import Decimal

    from clawdence.domain import Budget

    budget = Budget(max_usd=Decimal("4.50"))
    restored = Budget.model_validate_json(budget.model_dump_json())
    assert restored.max_usd == Decimal("4.50")
    assert isinstance(restored.max_usd, Decimal)


def test_aware_datetimes_survive_with_their_offset() -> None:
    """Timestamps keep their timezone.

    Naive datetimes are rejected at the type level (``AwareDatetime``); this
    checks the offset also survives the wire, since watchdogs and stall
    detection compare timestamps written by different processes.
    """
    from clawdence.domain import Run

    restored = Run.model_validate_json(SAMPLES["Run"].model_dump_json())
    assert isinstance(restored, Run)
    assert restored.created_at.tzinfo is not None
    assert restored.created_at.utcoffset() is not None
