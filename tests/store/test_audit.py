"""The audit trail: ordering, redaction honesty, and reading old records."""

from __future__ import annotations

from pydantic import JsonValue

from clawdence.domain import EVENT_SCHEMA_VERSION, Actor, ActorKind, EventKind
from clawdence.store import StateStore
from clawdence.store.schema import iso
from tests.store.conftest import StoreFactory
from tests.store.factories import RUN_ID, at


class TestAppending:
    def test_records_come_back_in_the_order_they_were_written(self, state: StateStore) -> None:
        for index, kind in enumerate((EventKind.RUN_STARTED, EventKind.STEP_STARTED)):
            state.audit.record(kind, at=at(index), run_id=RUN_ID)
        assert [event.kind for event in state.audit.read()] == [
            EventKind.RUN_STARTED,
            EventKind.STEP_STARTED,
        ]

    def test_order_is_the_databases_and_not_the_clocks(self, state: StateStore) -> None:
        """Two events in the same instant still have an order, and it is stable."""
        state.audit.record(EventKind.STEP_STARTED, at=at(5), stage_id="a")
        state.audit.record(EventKind.STEP_FINISHED, at=at(5), stage_id="a")
        state.audit.record(EventKind.RUN_STARTED, at=at(0))
        assert [event.kind for event in state.audit.read()] == [
            EventKind.STEP_STARTED,
            EventKind.STEP_FINISHED,
            EventKind.RUN_STARTED,
        ]

    def test_reading_filters_by_run_and_kind(self, state: StateStore) -> None:
        state.audit.record(EventKind.RUN_STARTED, at=at(0), run_id=RUN_ID)
        state.audit.record(EventKind.STEP_STARTED, at=at(1), run_id=RUN_ID, stage_id="a")
        state.audit.record(EventKind.RUN_STARTED, at=at(2), run_id="run.other")

        assert len(state.audit.read(run_id=RUN_ID)) == 2
        assert len(state.audit.read(kinds=[EventKind.RUN_STARTED])) == 2
        assert len(state.audit.read(limit=1)) == 1

    def test_the_actor_survives(self, state: StateStore) -> None:
        """Who decided is the question an audit trail is read to answer."""
        approver = Actor(kind=ActorKind.HUMAN, id="u1", display_name="A Person")
        state.audit.record(EventKind.APPROVAL_DECIDED, at=at(0), actor=approver)
        assert state.audit.read()[0].actor == approver


class TestRedaction:
    def test_an_unscreened_payload_says_so(self, state: StateStore) -> None:
        """Claiming to have screened when nothing screened is the worse lie."""
        state.audit.record(EventKind.STEP_FINISHED, at=at(0), payload={"attempt": 1})
        assert state.audit.read()[0].redacted is False

    def test_a_redactor_is_applied_and_recorded(self, stores: StoreFactory) -> None:
        def mask(payload: JsonValue) -> tuple[JsonValue, bool]:
            if isinstance(payload, dict) and "token" in payload:
                return {**payload, "token": "***"}, True
            return payload, True

        store: StateStore = stores(redactor=mask)
        store.audit.record(EventKind.WORK_ITEM_RECEIVED, at=at(0), payload={"token": "sk-secret"})

        stored = store.audit.read()[0]
        assert stored.payload == {"token": "***"}
        assert stored.redacted is True


class TestReadingOldRecords:
    def test_a_record_written_under_an_older_schema_still_reads(self, state: StateStore) -> None:
        """The log outlives the build that wrote it.

        ADR-0005 dropped replay-from-log, and with it upcasters — but the
        timeline and S20's replay tooling still read across versions. A reader
        that refuses old records is a reader that cannot answer questions about
        last month.
        """
        state.connection.execute(
            "INSERT INTO audit (id, schema_version, kind, at, run_id, work_item_id, stage_id, "
            "actor, payload, redacted) VALUES (?, 0, ?, ?, ?, NULL, NULL, 'null', ?, 1)",
            (
                "ev.ancient",
                EventKind.RUN_STARTED.value,
                iso(at(0)),
                RUN_ID,
                '{"a_field_this_build_never_heard_of": true}',
            ),
        )
        (event,) = state.audit.read()
        assert event.schema_version == 0 < EVENT_SCHEMA_VERSION
        assert event.payload == {"a_field_this_build_never_heard_of": True}

    def test_new_records_carry_the_current_version(self, state: StateStore) -> None:
        state.audit.record(EventKind.RUN_STARTED, at=at(0))
        assert state.audit.read()[0].schema_version == EVENT_SCHEMA_VERSION
