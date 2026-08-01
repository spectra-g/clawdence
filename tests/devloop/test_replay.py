"""Replay: the log rebuilt, and diffed against the state it describes.

Every run here is executed through ``execute`` with a real ``SqliteLedger``, so
what is being compared is what the product writes rather than what a fixture
says it writes. That is the whole value of the check: if the ledger's row and
its event ever stop agreeing, these fail, and nothing else in the suite would.

The negative tests are the interesting half. Each one writes to the store
*around* the ledger — which is exactly what a new writer that forgets to audit
would do — and asserts that replay names it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from clawdence.devloop import Replay, fold, replay
from clawdence.domain import (
    Actor,
    ActorKind,
    Event,
    EventKind,
    RunStatus,
    StepStatus,
)
from clawdence.engine import StepFailure, StubHandler
from clawdence.store import StateStore
from tests.devloop.factories import RUN_ID, go
from tests.engine.factories import script, workflow
from tests.store.factories import at, make_step


def field(result: Replay, subject: str, name: str) -> str:
    (found,) = [
        divergence
        for divergence in result.divergences
        if divergence.subject == subject and divergence.field == name
    ]
    return f"{found.in_log}/{found.in_state}"


class TestAgreement:
    def test_a_completed_run_replays_to_the_state_it_produced(self, state: StateStore) -> None:
        """S20's acceptance criterion, in the only form ADR-0005 allows it.

        Not "the log can rebuild the state" — that is the design the ADR
        refused — but "the log agrees with the state", which is checkable and
        costs no upcasters.
        """
        go(state, workflow(script("a"), script("b")), StubHandler())

        result = replay(state, RUN_ID)

        assert result.agrees
        assert result.divergences == ()
        assert result.run is not None
        assert result.run.status == RunStatus.DONE.value
        assert {step.label for step in result.steps} == {"a#1", "b#1"}

    def test_retries_failures_and_skips_all_replay(self, state: StateStore) -> None:
        """The shapes a real run actually has, rather than the happy one."""
        wf = workflow(
            script("a", retry={"max_attempts": 2}),
            script("b", when='$a.json.size == "L"'),
        )
        go(state, wf, StubHandler(failure=StepFailure("script-exit", "no", retryable=True)))

        result = replay(state, RUN_ID)

        assert result.agrees, [d.describe() for d in result.divergences]
        assert {step.label for step in result.steps} == {"a#1", "a#2", "b#1"}

    def test_a_skipped_stage_has_a_kind_and_no_finish_time(self, state: StateStore) -> None:
        """Both halves of what the first real replay found.

        The type is only in the finish event, because a skipped stage never
        starts; and the instant on that event is when the skip was *recorded*,
        so folding it into ``finished_at`` would invent a fact the step row
        correctly does not have.
        """
        wf = workflow(script("a"), script("b", when='$a.json.size == "L"'))
        go(state, wf, StubHandler(output={"size": "M"}))

        result = replay(state, RUN_ID)
        (skipped,) = [step for step in result.steps if step.stage_id == "b"]

        assert result.agrees
        assert skipped.status == StepStatus.SKIPPED.value
        assert skipped.type == "script"
        assert skipped.finished_at is None

    def test_a_resumed_run_is_visible_as_two_starts(self, state: StateStore) -> None:
        """A fact the ``runs`` row cannot hold: it has one status column."""
        wf = workflow(script("a"), script("b"))
        go(state, wf, StubHandler())
        go(state, wf, StubHandler())

        result = replay(state, RUN_ID)

        assert result.run is not None
        assert result.run.starts == 2
        assert result.agrees


class TestDivergence:
    def test_a_step_finished_without_an_audit_record_is_named(self, state: StateStore) -> None:
        """The finding this module is worth building for.

        ``finish_step`` writes the row; the ledger is what also writes the
        event. A writer that calls the first and not the second changes the
        world silently — and that is what every new step type from S12 onward
        is one forgotten line away from doing.
        """
        go(state, workflow(script("a")), StubHandler())
        state.finish_step(make_step("ghost", run_id=RUN_ID, status=StepStatus.SUCCEEDED))

        result = replay(state, RUN_ID)

        assert not result.agrees
        assert field(result, "step ghost#1", "exists") == "no/yes"

    def test_an_event_with_no_step_behind_it_is_named(self, state: StateStore) -> None:
        """The mirror case: the log claims something the state never recorded."""
        go(state, workflow(script("a")), StubHandler())
        state.audit.record(
            EventKind.STEP_FINISHED,
            at=at(90),
            run_id=RUN_ID,
            stage_id="phantom",
            actor=Actor(kind=ActorKind.SYSTEM, id="test"),
            payload={"attempt": 1, "status": "succeeded", "type": "script"},
        )

        result = replay(state, RUN_ID)

        assert field(result, "step phantom#1", "exists") == "yes/no"

    def test_a_status_changed_behind_the_log_is_named(self, state: StateStore) -> None:
        go(state, workflow(script("a")), StubHandler())
        state.update_run(RUN_ID, lambda run: run.model_copy(update={"status": RunStatus.MERGED}))

        result = replay(state, RUN_ID)

        assert field(result, "run", "status") == f"{RunStatus.DONE.value}/{RunStatus.MERGED.value}"

    def test_a_step_whose_recorded_status_differs_is_named(self, state: StateStore) -> None:
        go(state, workflow(script("a")), StubHandler())
        (recorded,) = state.steps_for(RUN_ID)
        state.finish_step(recorded.model_copy(update={"status": StepStatus.FAILED}))

        result = replay(state, RUN_ID)

        assert field(result, "step a#1", "status") == "succeeded/failed"

    def test_a_run_in_the_log_and_not_in_the_state(self, state: StateStore) -> None:
        state.audit.record(EventKind.RUN_STARTED, at=at(0), run_id="run.vanished", payload={})

        result = replay(state, "run.vanished")

        assert field(result, "run", "exists") == "yes/no"


class TestBoundaries:
    def test_nothing_at_all_is_an_answer_rather_than_an_error(self, state: StateStore) -> None:
        result = replay(state, "run.never-existed")

        assert result.events == 0
        assert result.run is None
        assert result.divergences == ()

    def test_replaying_writes_nothing(self, state: StateStore) -> None:
        """Not a restore path, and this is what says so.

        ADR-0005 keeps state out of the log's hands. A replay that quietly
        repaired a divergence would be event sourcing arriving through the back
        door, so the whole database is compared before and after.
        """
        go(state, workflow(script("a"), script("b")), StubHandler())
        state.finish_step(make_step("ghost", run_id=RUN_ID))
        before = _snapshot(state)

        replay(state, RUN_ID)

        assert _snapshot(state) == before

    def test_a_truncated_replay_makes_no_claim(self, state: StateStore) -> None:
        """Comparing a prefix against the end of the run would report the rest
        of the run as divergence."""
        go(state, workflow(script("a"), script("b")), StubHandler())

        result = replay(state, RUN_ID, through=3)

        assert result.truncated
        assert result.folded == 3
        assert result.divergences == ()
        assert not result.agrees
        assert {step.label for step in result.steps} == {"a#1"}

    def test_a_through_at_or_past_the_end_is_not_truncated(self, state: StateStore) -> None:
        go(state, workflow(script("a")), StubHandler())
        total = len(state.audit.read(run_id=RUN_ID))

        assert replay(state, RUN_ID, through=total).agrees
        assert replay(state, RUN_ID, through=total + 10).agrees

    def test_an_unfolded_kind_is_reported_rather_than_ignored(self) -> None:
        """A kind nobody folds is a hole in the reconstruction.

        Silence there is the failure mode: the projection would look complete
        and be missing whatever the new event recorded. Folded in memory, with
        no store, because there is nothing to compare against.
        """
        moment = datetime(2026, 7, 29, tzinfo=UTC)
        events = [
            Event(id="ev.1", kind=EventKind.RUN_STARTED, at=moment, run_id=RUN_ID, payload={}),
            Event(id="ev.2", kind=EventKind.DEGRADED, at=moment, run_id=RUN_ID, payload={}),
            Event(id="ev.3", kind=EventKind.BUDGET_EXCEEDED, at=moment, run_id=RUN_ID),
        ]

        _, _, unmodelled = fold(RUN_ID, events)

        assert unmodelled == ("degraded", "budget.exceeded")

    def test_a_step_event_with_no_stage_is_reported(self) -> None:
        """A producer bug, and swallowing it would hide the producer."""
        moment = datetime(2026, 7, 29, tzinfo=UTC)
        events = [Event(id="ev.1", kind=EventKind.STEP_FINISHED, at=moment, run_id=RUN_ID)]

        _, steps, unmodelled = fold(RUN_ID, events)

        assert steps == ()
        assert unmodelled == ("step.finished (no stage_id)",)

    def test_events_before_the_start_do_not_lose_the_rest(self) -> None:
        """A rotated log begins in the middle, which is not a reason to stop."""
        moment = datetime(2026, 7, 29, tzinfo=UTC)
        events = [
            Event(
                id="ev.1",
                kind=EventKind.STEP_FINISHED,
                at=moment,
                run_id=RUN_ID,
                stage_id="a",
                payload={"attempt": 1, "status": "succeeded", "type": "script"},
            ),
            Event(
                id="ev.2",
                kind=EventKind.RUN_FINISHED,
                at=moment,
                run_id=RUN_ID,
                payload={"status": "done"},
            ),
        ]

        run, steps, _ = fold(RUN_ID, events)

        assert run is not None
        assert run.status == "done"
        assert [step.label for step in steps] == ["a#1"]


def _snapshot(state: StateStore) -> dict[str, list[tuple[object, ...]]]:
    """Every row in every table, so "wrote nothing" means nothing at all."""
    tables = [
        name
        for (name,) in state.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    ]
    return {
        table: [
            tuple(row)
            for row in state.connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        ]
        for table in tables
    }
