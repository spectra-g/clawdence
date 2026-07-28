"""Stall detection and recovery.

Every instant is derived from a fixed start, so "is this overdue" never depends
on how fast the machine running the test is. That is not incidental tidiness: a
watchdog test that sleeps is a watchdog test that is flaky on a loaded CI box,
and a flaky watchdog test gets muted.
"""

from __future__ import annotations

from clawdence.domain import EventKind, RunStatus, StepStatus
from clawdence.store import StallKind, StateStore, detect, recover, sweep
from clawdence.store.watchdog import DEFAULT_HEARTBEAT_SECONDS, DEFAULT_STEP_SECONDS
from tests.store.factories import RUN_ID, at, make_run, make_step, running_step


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

        assert detect(state, now=at(DEFAULT_STEP_SECONDS - 1)) == ()
        (stall,) = detect(state, now=at(DEFAULT_STEP_SECONDS + 1))
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
        """However long it has been going: something is working on it."""
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("build", started=0, timeout=DEFAULT_HEARTBEAT_SECONDS * 10))
        assert detect(state, now=at(DEFAULT_HEARTBEAT_SECONDS * 5)) == ()

    def test_a_finished_run_is_never_stalled(self, state: StateStore) -> None:
        state.create_run(make_run(status=RunStatus.DONE, created=0, updated=0))
        assert detect(state, now=at(DEFAULT_HEARTBEAT_SECONDS * 10)) == ()

    def test_one_run_is_reported_once(self, state: StateStore) -> None:
        state.create_run(make_run(created=0, updated=0))
        state.start_step(running_step("build", started=0, timeout=1))
        assert len(detect(state, now=at(DEFAULT_HEARTBEAT_SECONDS * 2))) == 1

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

    def test_only_the_overdue_step_of_a_run_is_timed_out(self, state: StateStore) -> None:
        """Matters from S3b onwards, when a run has several steps in flight."""
        state.create_run(make_run())
        state.start_step(running_step("slow", started=0, timeout=60))
        state.start_step(running_step("fresh", started=100, timeout=600))

        sweep(state, now=at(120))

        by_stage = {step.stage_id: step.status for step in state.steps_for(RUN_ID)}
        assert by_stage == {"slow": StepStatus.TIMED_OUT, "fresh": StepStatus.RUNNING}
