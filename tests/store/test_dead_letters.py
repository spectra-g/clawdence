"""The dead-letter queue: park what will not validate, drain it when it will.

Ported from v1, where it was the thing that stopped one malformed message from
wedging the pipeline. The property that matters most is the one at the top:
**a bad audit record never raises into the caller.** The audit log is not the
source of truth (ADR-0005), so it must not be able to fail a run.
"""

from __future__ import annotations

from pydantic import JsonValue

from clawdence.domain import EventKind
from clawdence.store import StateStore
from tests.store.factories import RUN_ID, at

WRONG_KIND = {
    "id": "ev.one",
    "kind": "step.exploded",
    "at": "2026-07-28T12:00:00+00:00",
    "run_id": RUN_ID,
}


def good_record(event_id: str = "ev.good") -> dict[str, JsonValue]:
    return {
        "id": event_id,
        "kind": EventKind.RUN_STARTED.value,
        "at": "2026-07-28T12:00:00+00:00",
        "run_id": RUN_ID,
    }


class TestParking:
    def test_a_bad_record_is_parked_rather_than_raised(self, state: StateStore) -> None:
        assert state.audit.submit(WRONG_KIND, at=at(0)) is None
        assert state.audit.read() == ()

        (letter,) = state.audit.dead_letters()
        assert letter.origin == "submit"
        assert "step.exploded" in letter.reason
        assert letter.tries == 1

    def test_the_parked_record_is_kept_intact(self, state: StateStore) -> None:
        state.audit.submit(WRONG_KIND, at=at(0))
        assert state.audit.dead_letters()[0].decoded() == WRONG_KIND

    def test_a_good_record_goes_straight_in(self, state: StateStore) -> None:
        event = state.audit.submit(good_record(), at=at(0))
        assert event is not None
        assert state.audit.read()[0].id == "ev.good"
        assert state.audit.dead_letters() == ()

    def test_a_record_that_is_not_even_json_keeps_something_readable(
        self, state: StateStore
    ) -> None:
        state.audit.submit({"id": "ev.x", "at": object()}, at=at(0))
        (letter,) = state.audit.dead_letters()
        assert "object object" in letter.body


class TestReplay:
    def test_a_repaired_record_replays_cleanly(self, state: StateStore) -> None:
        state.audit.submit(WRONG_KIND, at=at(0))

        def repair(record: JsonValue) -> JsonValue:
            assert isinstance(record, dict)
            return {**record, "kind": EventKind.STEP_TIMED_OUT.value}

        report = state.audit.replay(at=at(60), repair=repair)

        assert report.ok
        assert len(report.replayed) == 1
        assert state.audit.dead_letters() == ()
        assert state.audit.read()[0].kind is EventKind.STEP_TIMED_OUT

    def test_a_record_that_still_fails_stays_parked_and_says_why_now(
        self, state: StateStore
    ) -> None:
        state.audit.submit(WRONG_KIND, at=at(0))

        def half_repair(record: JsonValue) -> JsonValue:
            assert isinstance(record, dict)
            return {**record, "kind": EventKind.RUN_STARTED.value, "at": "not-a-timestamp"}

        report = state.audit.replay(at=at(60), repair=half_repair)

        assert not report.ok
        (letter,) = state.audit.dead_letters()
        assert letter.tries == 2
        assert "not-a-timestamp" in letter.reason or "datetime" in letter.reason
        assert state.audit.read() == ()

    def test_replaying_does_not_grow_the_queue(self, state: StateStore) -> None:
        """Parking an already-parked record would make draining lose ground."""
        state.audit.submit(WRONG_KIND, at=at(0))
        state.audit.replay(at=at(60))
        state.audit.replay(at=at(120))
        assert len(state.audit.dead_letters()) == 1
        assert state.audit.dead_letters()[0].tries == 3

    def test_replaying_the_same_record_twice_is_safe(self, state: StateStore) -> None:
        """Idempotent by event id: replay is meant to be runnable again."""
        state.audit.submit(good_record("ev.dup"), at=at(0))
        state.audit.submit(
            {"id": "ev.dup", "kind": "nope", "at": "2026-07-28T12:00:00+00:00"}, at=at(1)
        )

        report = state.audit.replay(
            at=at(60),
            repair=lambda record: good_record("ev.dup"),
        )

        assert report.ok
        assert len(state.audit.read()) == 1
        assert state.audit.dead_letters() == ()

    def test_a_body_that_is_not_an_object_is_reported_not_crashed(self, state: StateStore) -> None:
        state.audit.submit(WRONG_KIND, at=at(0))
        report = state.audit.replay(at=at(60), repair=lambda record: "just a string")
        assert not report.ok
        assert "must be an object" in state.audit.dead_letters()[0].reason

    def test_an_empty_queue_replays_to_nothing(self, state: StateStore) -> None:
        assert state.audit.replay(at=at(0)).ok


def test_a_body_that_is_not_json_at_all_is_still_reported(state: StateStore) -> None:
    """Nothing writes one, but a hand-edited or truncated row must not crash."""
    state.connection.execute(
        "INSERT INTO dead_letters (at, origin, reason, body) VALUES (?, ?, ?, ?)",
        ("2026-07-28T12:00:00.000000+00:00", "hand-written", "was already broken", "not json"),
    )
    assert state.audit.dead_letters()[0].decoded() is None

    report = state.audit.replay(at=at(0))
    assert not report.ok
    assert "must be an object" in state.audit.dead_letters()[0].reason
