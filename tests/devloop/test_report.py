"""What the dev-loop commands print — and, mostly, what they say they did not look at.

These assert on sentences, which is usually a smell. It is not here: the whole
claim of this package is that a report does not read as more complete than it
is, and that claim lives in the wording. A replay that printed "identical"
without naming the fields the log cannot carry would pass every structural test
in the suite and still be the thing this step exists to avoid.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from clawdence.devloop import (
    Replay,
    Reset,
    render_dead_letters,
    render_events,
    render_events_json,
    render_replay,
    render_replay_json,
    render_reset,
    render_run,
    render_run_json,
    replay,
)
from clawdence.domain import Actor, ActorKind, Event, EventKind, StepError, StepStatus
from clawdence.engine import StepFailure, StubHandler
from clawdence.runners import Reclaimed
from clawdence.store import StateStore
from tests.devloop.factories import RUN_ID, go
from tests.engine.factories import script, workflow
from tests.store.factories import make_run, make_step

MOMENT = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


class TestReset:
    def test_a_dry_run_is_in_the_conditional(self) -> None:
        """The one distinction that must survive being skim-read."""
        planned = render_reset(Reset(rows={"runs": 2}, dry_run=True))
        done = render_reset(Reset(rows={"runs": 2}))

        assert "would remove 2 row(s) from runs" in planned
        assert "removed 2 row(s) from runs" in done

    def test_a_table_with_nothing_in_it_is_still_listed(self) -> None:
        """ "It was already empty" and "it was not touched" are different facts."""
        assert "0 row(s) from audit" in render_reset(Reset(rows={"audit": 0, "runs": 1}))

    def test_a_kept_inbox_is_stated(self) -> None:
        report = render_reset(Reset(rows={"runs": 1}, requeued=2, kept_inbox=True))

        assert "kept the inbox" in report
        assert "back in the queue" in report

    def test_abandoned_runs_come_first(self) -> None:
        report = render_reset(Reset(rows={"runs": 1}, abandoned=("run.a",)))

        assert report.splitlines()[0].startswith("abandoned 1 run(s)")

    def test_nothing_to_do_says_so(self) -> None:
        assert render_reset(Reset()) == "already clean"

    def test_what_could_not_be_removed_is_reported(self, tmp_path: Path) -> None:
        report = render_reset(Reset(debris=Reclaimed(failed=(tmp_path / "stuck",))))

        assert "could not remove" in report


class TestReplay:
    def test_agreement_still_names_what_was_not_compared(self, state: StateStore) -> None:
        """The sentence this module exists for.

        Half the step record is unobservable by S4's design. A report that said
        "agree" and stopped would be claiming more than it checked.
        """
        go(state, workflow(script("a")), StubHandler())

        report = render_replay(replay(state, RUN_ID))

        assert "the log and the stored state agree" in report
        assert "not carried by the log, so not compared" in report
        assert "step.output" in report

    def test_a_divergence_is_named_with_both_sides(self, state: StateStore) -> None:
        go(state, workflow(script("a")), StubHandler())
        state.finish_step(make_step("ghost", run_id=RUN_ID))

        report = render_replay(replay(state, RUN_ID))

        assert "1 divergence(s)" in report
        assert "step ghost#1.exists: log says no, state says yes" in report

    def test_a_truncated_replay_says_it_did_not_look(self, state: StateStore) -> None:
        go(state, workflow(script("a"), script("b")), StubHandler())

        report = render_replay(replay(state, RUN_ID, through=2))

        assert "not compared" in report
        assert "not carried by the log" not in report

    def test_a_resume_is_visible(self, state: StateStore) -> None:
        go(state, workflow(script("a")), StubHandler())
        go(state, workflow(script("a")), StubHandler())

        assert "this run was resumed" in render_replay(replay(state, RUN_ID))

    def test_an_empty_log_says_so(self, state: StateStore) -> None:
        assert "the log has nothing for this run" in render_replay(replay(state, "run.nothing"))

    def test_an_unfolded_kind_is_in_the_report(self) -> None:
        report = render_replay(Replay(run_id=RUN_ID, events=1, folded=1, unmodelled=("degraded",)))

        assert "not folded: degraded" in report

    def test_the_json_form_carries_the_divergences(self, state: StateStore) -> None:
        go(state, workflow(script("a")), StubHandler())
        state.finish_step(make_step("ghost", run_id=RUN_ID))

        payload = json.loads(render_replay_json(replay(state, RUN_ID)))

        assert payload["agrees"] is False
        assert payload["divergences"][0]["subject"] == "step ghost#1"
        assert "step.output" in payload["unobservable"]


class TestEvents:
    def events(self) -> list[Event]:
        return [
            Event(
                id="ev.1",
                kind=EventKind.STEP_FINISHED,
                at=MOMENT,
                run_id=RUN_ID,
                stage_id="verify",
                actor=Actor(kind=ActorKind.SYSTEM, id="engine"),
                payload={"attempt": 1, "status": "failed"},
            )
        ]

    def test_a_line_carries_the_kind_the_subject_the_actor_and_the_payload(self) -> None:
        line = render_events(self.events())

        assert "step.finished" in line
        assert "verify" in line
        assert "system:engine" in line
        assert '"status":"failed"' in line

    def test_a_tail_says_it_is_a_tail(self) -> None:
        """ "The last twenty" and "the first twenty" look identical on screen."""
        assert "most recent" in render_events(self.events(), tail=True)
        assert "most recent" not in render_events(self.events())

    def test_nothing_matching_is_not_an_empty_screen(self) -> None:
        assert render_events([]) == "no audit records match"

    def test_the_json_form_round_trips(self) -> None:
        (payload,) = json.loads(render_events_json(self.events()))

        assert payload["kind"] == "step.finished"
        assert Event.model_validate(payload).id == "ev.1"


class TestDeadLetters:
    def test_a_parked_record_shows_its_newest_reason(self, state: StateStore) -> None:
        state.audit.submit({"not": "an event"}, at=MOMENT)

        report = render_dead_letters(state.audit.dead_letters())

        assert "from submit" in report
        assert "runs recover" in report

    def test_an_empty_queue_says_so(self, state: StateStore) -> None:
        assert render_dead_letters(()) == "no records are parked"


class TestRun:
    def test_a_step_shows_its_duration_and_its_error_message(self) -> None:
        """The message, unlike in the log — this reads the ``steps`` table,
        which is where the message survives."""
        run = make_run("run.shown")
        failed = make_step(
            "verify",
            run_id="run.shown",
            status=StepStatus.FAILED,
            started=1.0,
            finished=3.5,
            error=StepError(kind="script-exit", message="python3 exited 1"),
        )

        report = render_run(run, [failed], events=4)

        assert "FAIL  verify" in report
        assert "2.50s" in report
        assert "script-exit: python3 exited 1" in report
        assert "4 audit record(s)" in report

    def test_an_unfinished_run_says_so_rather_than_inventing_an_end(self) -> None:
        assert "(unfinished)" in render_run(make_run("run.live"), [], events=0)

    def test_a_run_with_no_steps_says_so(self) -> None:
        assert "no steps recorded" in render_run(make_run("run.empty"), [], events=0)

    def test_a_skipped_step_has_no_duration(self) -> None:
        """It never ran, so there is no span to print — and a ``0.00s`` there
        would read as a step that ran instantly."""
        skipped = make_step("b", status=StepStatus.SKIPPED, started=None, finished=None)

        (line,) = [
            line
            for line in render_run(make_run(), [skipped], events=0).splitlines()
            if " b " in line
        ]
        assert line.rstrip() == "  skip  b  [script]"

    def test_the_json_form_carries_every_attempt(self, state: StateStore) -> None:
        wf = workflow(script("a", retry={"max_attempts": 2}))
        go(state, wf, StubHandler(failure=StepFailure("script-exit", "no", retryable=True)))
        run = state.require_run(RUN_ID)

        payload = json.loads(render_run_json(run, state.steps_for(RUN_ID), events=6))

        assert [step["attempt"] for step in payload["steps"]] == [1, 2]
        assert payload["run"]["id"] == RUN_ID
