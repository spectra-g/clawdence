"""Stall detection and recovery.

Every instant is derived from a fixed start, so "is this overdue" never depends
on how fast the machine running the test is. That is not incidental tidiness: a
watchdog test that sleeps is a watchdog test that is flaky on a loaded CI box,
and a flaky watchdog test gets muted.
"""

from __future__ import annotations

from clawdence.domain import ActorKind, EventKind, RunStatus, StepStatus
from clawdence.store import Cancellations, StallKind, StateStore, detect, recover, sweep
from clawdence.store.watchdog import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_SILENCE_SECONDS,
    DEFAULT_STEP_SECONDS,
    WATCHDOG,
)
from tests.store.factories import RUN_ID, at, make_run, make_step, running_step

#: Big enough that the silence detector never fires in a test that is about one
#: of the other two. Needed because the silence budget is *shorter* than the
#: default step timeout by design — a step nobody declared a limit for gets an
#: hour, and one that has said nothing gets forty-five minutes — so a fixture
#: that runs long enough to prove the step default now trips silence first.
NEVER_SILENT = DEFAULT_STEP_SECONDS * 100


class TestDetection:
    def test_a_step_inside_its_timeout_is_not_stalled(self, state: StateStore) -> None:
        state.create_run(make_run())
        state.start_step(running_step("build", started=0, timeout=60))
        assert detect(state, now=at(59)) == ()

    def test_a_step_past_its_timeout_is_stalled(self, state: StateStore) -> None:
        state.create_run(make_run())
        state.start_step(running_step("build", started=0, timeout=60))

        (stall,) = detect(state, now=at(75))
        assert stall.kind is StallKind.STEP
        assert stall.stage_id == "build"
        assert stall.limit_seconds == 60
        assert stall.overdue_by_seconds == 15

    def test_a_step_that_declared_no_timeout_still_gets_one(self, state: StateStore) -> None:
        """A step with no limit is a step that can hang forever."""
        state.create_run(make_run())
        state.start_step(running_step("build", started=0))

        assert detect(state, now=at(DEFAULT_STEP_SECONDS - 1), silence_seconds=NEVER_SILENT) == ()
        (stall,) = detect(state, now=at(DEFAULT_STEP_SECONDS + 1), silence_seconds=NEVER_SILENT)
        assert stall.limit_seconds == DEFAULT_STEP_SECONDS

    def test_a_run_with_nothing_in_flight_and_no_heartbeat_is_stalled(
        self, state: StateStore
    ) -> None:
        """The process died *between* steps, so nothing is overdue to find."""
        state.create_run(make_run(status=RunStatus.RUNNING, created=0, updated=0))
        state.finish_step(make_step("a"))

        assert detect(state, now=at(DEFAULT_HEARTBEAT_SECONDS - 1)) == ()
        (stall,) = detect(state, now=at(DEFAULT_HEARTBEAT_SECONDS + 30))
        assert stall.kind is StallKind.RUN
        assert stall.stage_id is None

    def test_a_run_whose_step_is_healthy_is_left_alone(self, state: StateStore) -> None:
        """However long it has been going, *as long as it is still talking*.

        S4 wrote this as "however long it has been going: something is working
        on it", with a fixture whose heartbeat never moved — and that was the
        gap §3.11 names, asserted as if it were the intent. A step in flight was
        unfalsifiable evidence of health: it exempted the run from the heartbeat
        check below and nothing else looked at it again. The claim that is
        actually true, and the one this now makes, needs the heartbeat to keep
        moving.
        """
        long_enough = DEFAULT_SILENCE_SECONDS * 5
        state.create_run(make_run(created=0, updated=long_enough))
        state.start_step(running_step("build", started=0, timeout=long_enough * 10))
        assert detect(state, now=at(long_enough)) == ()

    def test_a_finished_run_is_never_stalled(self, state: StateStore) -> None:
        state.create_run(make_run(status=RunStatus.DONE, created=0, updated=0))
        assert detect(state, now=at(DEFAULT_HEARTBEAT_SECONDS * 10)) == ()

    def test_one_run_is_reported_once(self, state: StateStore) -> None:
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("build", started=0, timeout=1))
        assert len(detect(state, now=at(DEFAULT_HEARTBEAT_SECONDS * 2))) == 1

    def test_a_step_in_flight_that_has_gone_quiet_is_stalled(self, state: StateStore) -> None:
        """§3.11's case, and the one neither S4 detector can see.

        The process is alive, the step is a long way inside the timeout its
        author declared, and nothing has been heard. The motivating incident was this
        exact shape and reported healthy throughout.
        """
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("code", started=0, timeout=DEFAULT_SILENCE_SECONDS * 100))

        (stall,) = detect(state, now=at(DEFAULT_SILENCE_SECONDS + 300))

        assert stall.kind is StallKind.SILENT
        assert stall.stage_id == "code"
        assert stall.limit_seconds == DEFAULT_SILENCE_SECONDS
        assert stall.overdue_by_seconds == 300
        assert "has said nothing" in stall.describe()

    def test_a_slow_step_that_is_merely_quiet_is_not_stalled(self, state: StateStore) -> None:
        """A long dependency install with no output is not a hang.

        The whole discriminator is the budget, which is why it is generous and
        why the default is written down as a decision rather than a number: a
        false positive here kills work that was about to succeed.
        """
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("code", started=0, timeout=DEFAULT_SILENCE_SECONDS * 100))
        assert detect(state, now=at(DEFAULT_SILENCE_SECONDS - 1)) == ()

    def test_silence_is_measured_from_the_last_heartbeat(self, state: StateStore) -> None:
        """A run that spoke ten minutes ago has been silent for ten minutes."""
        spoke_at = DEFAULT_SILENCE_SECONDS * 2
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("code", started=0, timeout=DEFAULT_SILENCE_SECONDS * 100))
        state.touch_run(RUN_ID, at=at(spoke_at))

        assert detect(state, now=at(spoke_at + DEFAULT_SILENCE_SECONDS - 1)) == ()
        (stall,) = detect(state, now=at(spoke_at + DEFAULT_SILENCE_SECONDS + 1))
        assert stall.kind is StallKind.SILENT

    def test_a_step_that_has_only_just_started_is_never_silent(self, state: StateStore) -> None:
        """The step's own start is a floor under the heartbeat.

        Without it, a run that idled past the budget and *then* started a step
        would be reported silent before the work had a chance to print a line.
        """
        idle_for = DEFAULT_SILENCE_SECONDS * 3
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("code", started=idle_for, timeout=idle_for * 10))

        assert detect(state, now=at(idle_for + 1), heartbeat_seconds=idle_for * 10) == ()

    def test_an_overdue_step_is_reported_overdue_rather_than_silent(
        self, state: StateStore
    ) -> None:
        """Both are true; the declared timeout is the more specific fact.

        It is also the one with a recovery that can act alone — the silent path
        needs a live process to ask, and a step past its timeout may well not
        have one.
        """
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("code", started=0, timeout=60))

        (stall,) = detect(state, now=at(DEFAULT_SILENCE_SECONDS * 2))
        assert stall.kind is StallKind.STEP

    def test_a_run_already_asked_to_stop_is_not_reported_again(self, state: StateStore) -> None:
        """The sweep runs on a timer; a run shutting down is not a new problem."""
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("code", started=0, timeout=DEFAULT_SILENCE_SECONDS * 100))
        Cancellations(state).request(RUN_ID, at=at(1), reason="already asked")

        assert detect(state, now=at(DEFAULT_SILENCE_SECONDS * 2)) == ()

    def test_detection_changes_nothing(self, state: StateStore) -> None:
        """Looking is free, so an operator can ask without committing."""
        state.create_run(make_run())
        state.start_step(running_step("build", started=0, timeout=1))

        detect(state, now=at(100))

        assert state.running_steps()[0].status is StepStatus.RUNNING
        assert state.require_run(RUN_ID).status is RunStatus.RUNNING
        assert state.audit.read() == ()


class TestRecovery:
    def test_an_overdue_step_is_timed_out_and_its_run_halted(self, state: StateStore) -> None:
        state.create_run(make_run())
        state.start_step(running_step("build", started=0, timeout=60))

        (stall,) = sweep(state, now=at(120))

        (step,) = state.steps_for(RUN_ID)
        assert step.status is StepStatus.TIMED_OUT
        assert step.finished_at == at(120)
        assert step.error is not None
        assert step.error.kind == "watchdog-timeout"
        assert step.error.retryable is True
        assert state.require_run(RUN_ID).status is RunStatus.HALTED
        assert stall.describe().startswith("run run.test step build")

    def test_the_timeline_says_who_found_it(self, state: StateStore) -> None:
        """The executor timing a step out means the engine was alive. This does not."""
        state.create_run(make_run())
        state.start_step(running_step("build", started=0, timeout=60))

        sweep(state, now=at(120))

        (timed_out,) = state.audit.read(kinds=[EventKind.STEP_TIMED_OUT])
        assert timed_out.payload == {
            "detected_by": "watchdog",
            "attempt": 1,
            "limit_seconds": 60.0,
            "overdue_by_seconds": 60.0,
        }

    def test_a_halt_says_how_to_restart_the_work(self, state: StateStore) -> None:
        """Recovery makes the state honest; resuming is a separate decision."""
        state.create_run(make_run())
        state.start_step(running_step("build", started=0, timeout=60))

        sweep(state, now=at(120))

        (halted,) = state.audit.read(kinds=[EventKind.HALTED_FOR_HUMAN])
        assert isinstance(halted.payload, dict)
        assert halted.payload["resume_with"] == "clawdence run --resume"

    def test_a_stalled_run_with_no_step_is_halted(self, state: StateStore) -> None:
        state.create_run(make_run(created=0, updated=0))
        state.finish_step(make_step("a"))

        (stall,) = sweep(state, now=at(DEFAULT_HEARTBEAT_SECONDS + 60))

        assert state.require_run(RUN_ID).status is RunStatus.HALTED
        assert "has not progressed" in stall.describe()
        assert state.steps_for(RUN_ID)[0].status is StepStatus.SUCCEEDED

    def test_recovery_is_safe_to_run_twice(self, state: StateStore) -> None:
        state.create_run(make_run())
        state.start_step(running_step("build", started=0, timeout=60))
        (stall,) = detect(state, now=at(120))

        recover(state, stall, now=at(120))
        recover(state, stall, now=at(180))

        assert state.require_run(RUN_ID).status is RunStatus.HALTED
        assert state.steps_for(RUN_ID)[0].finished_at == at(120)

    def test_a_healthy_store_sweeps_to_nothing(self, state: StateStore) -> None:
        state.create_run(make_run(status=RunStatus.DONE))
        assert sweep(state, now=at(10_000)) == ()

    def test_a_recovered_run_can_be_resumed(self, state: StateStore) -> None:
        """The point of halting rather than retrying: the work is still there."""
        state.create_run(make_run())
        state.start_step(running_step("build", started=0, timeout=60))
        sweep(state, now=at(120))

        assert state.require_run(RUN_ID).status is RunStatus.HALTED
        assert state.running_steps() == ()

    def test_a_silent_run_is_asked_to_stop_rather_than_halted(self, state: StateStore) -> None:
        """The restraint is the recovery.

        §3.11 requires a silent run to fail through the *normal collection
        path*, so whatever the agent committed before it hung is preserved. Only
        the process holding the worktree can walk that path, and it walks it on
        its way out of a cancellation. Halting the row from here would abandon
        the work and leave that process running against a row saying it is not.
        """
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("code", started=0, timeout=DEFAULT_SILENCE_SECONDS * 100))

        (stall,) = sweep(state, now=at(DEFAULT_SILENCE_SECONDS * 2))

        assert stall.kind is StallKind.SILENT
        assert state.require_run(RUN_ID).status is RunStatus.RUNNING
        assert state.steps_for(RUN_ID)[0].status is StepStatus.RUNNING

        request = Cancellations(state).pending(RUN_ID)
        assert request is not None
        assert request.requested_by == WATCHDOG
        assert "has said nothing" in request.reason

    def test_the_timeline_says_the_watchdog_asked_not_a_person(self, state: StateStore) -> None:
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("code", started=0, timeout=DEFAULT_SILENCE_SECONDS * 100))

        sweep(state, now=at(DEFAULT_SILENCE_SECONDS * 2))

        (cancelled,) = state.audit.read(kinds=[EventKind.RUN_CANCELLED])
        assert cancelled.actor is not None
        assert cancelled.actor.kind is ActorKind.SYSTEM
        assert cancelled.actor.id == WATCHDOG

    def test_a_step_whose_timeout_passes_later_is_still_caught_the_old_way(
        self, state: StateStore
    ) -> None:
        """The backstop. If nobody is attending the run, nothing acknowledges the
        request — and the step's own timeout catches it as an ordinary overdue
        step, which is why the silent path can afford to be polite."""
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("code", started=0, timeout=DEFAULT_SILENCE_SECONDS * 2))

        assert sweep(state, now=at(DEFAULT_SILENCE_SECONDS + 1))[0].kind is StallKind.SILENT
        (stall,) = sweep(state, now=at(DEFAULT_SILENCE_SECONDS * 3))

        assert stall.kind is StallKind.STEP
        assert state.require_run(RUN_ID).status is RunStatus.HALTED
        assert state.steps_for(RUN_ID)[0].status is StepStatus.TIMED_OUT

    def test_only_the_overdue_step_of_a_run_is_timed_out(self, state: StateStore) -> None:
        """Matters from S3b onwards, when a run has several steps in flight."""
        state.create_run(make_run())
        state.start_step(running_step("slow", started=0, timeout=60))
        state.start_step(running_step("fresh", started=100, timeout=600))

        sweep(state, now=at(120))

        by_stage = {step.stage_id: step.status for step in state.steps_for(RUN_ID)}
        assert by_stage == {"slow": StepStatus.TIMED_OUT, "fresh": StepStatus.RUNNING}
