"""Runs and steps: writing them, reading them back, and not losing them."""

from __future__ import annotations

import pytest

from clawdence.domain import RunStatus, StepStatus
from clawdence.store import DuplicateAttemptError, StateStore, UnknownRunError
from tests.store.factories import RUN_ID, TEST_CREDENTIAL, at, make_run, make_step, running_step


class TestRuns:
    def test_a_created_run_reads_back(self, state: StateStore) -> None:
        run = state.create_run(make_run())
        assert state.get_run(RUN_ID) == run

    def test_an_unknown_run_is_absent_rather_than_an_error(self, state: StateStore) -> None:
        assert state.get_run("run.nope") is None

    def test_requiring_an_unknown_run_says_which(self, state: StateStore) -> None:
        with pytest.raises(UnknownRunError, match=r"run\.nope"):
            state.require_run("run.nope")

    def test_listing_is_newest_first(self, state: StateStore) -> None:
        state.create_run(make_run("run.old", created=0))
        state.create_run(make_run("run.new", created=60))
        assert [run.id for run in state.list_runs()] == ["run.new", "run.old"]

    def test_listing_filters_by_status_and_limit(self, state: StateStore) -> None:
        state.create_run(make_run("run.a", status=RunStatus.RUNNING, created=0))
        state.create_run(make_run("run.b", status=RunStatus.DONE, created=1))
        state.create_run(make_run("run.c", status=RunStatus.RUNNING, created=2))
        assert [run.id for run in state.list_runs(status=RunStatus.RUNNING)] == ["run.c", "run.a"]
        assert [run.id for run in state.list_runs(limit=1)] == ["run.c"]

    def test_updating_applies_the_mutation(self, state: StateStore) -> None:
        state.create_run(make_run())
        updated = state.update_run(
            RUN_ID, lambda run: run.model_copy(update={"status": RunStatus.DONE})
        )
        assert updated.status is RunStatus.DONE
        assert state.require_run(RUN_ID).status is RunStatus.DONE

    def test_updating_an_unknown_run_is_an_error(self, state: StateStore) -> None:
        with pytest.raises(UnknownRunError):
            state.update_run("run.nope", lambda run: run)

    def test_the_heartbeat_only_moves_forward(self, state: StateStore) -> None:
        """A late writer must not make a live run look abandoned."""
        state.create_run(make_run(created=0, updated=60))
        state.touch_run(RUN_ID, at=at(30))
        assert state.require_run(RUN_ID).updated_at == at(60)

        state.touch_run(RUN_ID, at=at(90))
        assert state.require_run(RUN_ID).updated_at == at(90)


class TestSteps:
    def test_step_output_is_screened_before_it_is_stored(self, state: StateStore) -> None:
        state.create_run(make_run())
        state.finish_step(make_step("agent", output={"transcript": f"token={TEST_CREDENTIAL}"}))

        assert state.steps_for(RUN_ID)[0].output == {"transcript": "token=[redacted]"}

    def test_a_started_step_reads_back(self, state: StateStore) -> None:
        state.create_run(make_run())
        step = running_step("build")
        state.start_step(step)
        assert state.steps_for(RUN_ID) == (step,)

    def test_the_same_attempt_cannot_be_started_twice(self, state: StateStore) -> None:
        """A redelivered dispatch collides. v1's guards were hand-written."""
        state.create_run(make_run())
        state.start_step(running_step("build"))
        with pytest.raises(DuplicateAttemptError, match="attempt 1 of stage 'build'"):
            state.start_step(running_step("build"))

    def test_a_later_attempt_is_a_new_row(self, state: StateStore) -> None:
        state.create_run(make_run())
        state.start_step(running_step("build", attempt=1))
        state.start_step(running_step("build", attempt=2))
        assert [step.attempt for step in state.steps_for(RUN_ID)] == [1, 2]

    def test_finishing_supersedes_the_row_that_started(self, state: StateStore) -> None:
        state.create_run(make_run())
        state.start_step(running_step("build"))
        state.finish_step(make_step("build", status=StepStatus.SUCCEEDED, output={"ok": True}))

        (stored,) = state.steps_for(RUN_ID)
        assert stored.status is StepStatus.SUCCEEDED
        assert stored.output == {"ok": True}
        assert stored.finished_at == at(1)

    def test_a_result_with_no_start_is_still_recorded(self, state: StateStore) -> None:
        """A stage skipped by a false guard never ran, and still gets a row."""
        state.create_run(make_run())
        state.finish_step(
            make_step("split", status=StepStatus.SKIPPED, started=None, finished=None)
        )
        assert state.steps_for(RUN_ID)[0].status is StepStatus.SKIPPED

    def test_steps_come_back_in_the_order_they_were_written(self, state: StateStore) -> None:
        state.create_run(make_run())
        for stage_id in ("a", "b", "c"):
            state.finish_step(make_step(stage_id))
        assert [step.stage_id for step in state.steps_for(RUN_ID)] == ["a", "b", "c"]

    def test_running_steps_are_the_watchdogs_query(self, state: StateStore) -> None:
        state.create_run(make_run())
        state.create_run(make_run("run.other"))
        state.finish_step(make_step("done-one"))
        state.start_step(running_step("in-flight"))
        state.start_step(running_step("elsewhere", run_id="run.other"))

        assert [step.stage_id for step in state.running_steps()] == ["in-flight", "elsewhere"]
        assert [step.stage_id for step in state.running_steps(run_id=RUN_ID)] == ["in-flight"]

    def test_steps_of_one_run_do_not_leak_into_another(self, state: StateStore) -> None:
        state.create_run(make_run())
        state.create_run(make_run("run.other"))
        state.finish_step(make_step("a"))
        state.finish_step(make_step("b", run_id="run.other"))
        assert [step.stage_id for step in state.steps_for(RUN_ID)] == ["a"]
