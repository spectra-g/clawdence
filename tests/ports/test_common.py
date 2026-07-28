"""The package-private helpers the fakes are built from."""

from __future__ import annotations

from datetime import UTC, datetime

from clawdence.ports._common import NULL_PREFIX, counting_clock, utc_now


def test_the_production_clock_is_aware_and_utc() -> None:
    """Every timestamp in the domain model is an ``AwareDatetime``. A naive one
    from the default clock would fail validation at the first port that stamps
    a receipt, which is a strange place to discover a timezone bug."""
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(None)


def test_the_counting_clock_advances_by_a_fixed_step() -> None:
    """Fixed rather than real, so an assertion about ordering or duration never
    depends on how fast the machine running it happens to be."""
    start = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    clock = counting_clock(start, step_seconds=2.0)
    assert [clock(), clock(), clock()] == [
        start,
        start.replace(second=2),
        start.replace(second=4),
    ]


def test_the_null_prefix_is_obviously_unreal() -> None:
    """An id shaped like ``PROJ-14`` ends up in a notification telling somebody
    to go and read something that was never written."""
    assert NULL_PREFIX == "null:"
