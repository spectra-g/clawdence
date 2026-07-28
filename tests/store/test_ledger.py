"""The durable ledger, driven the way the executor drives it.

These tests run real workflows through ``execute`` with a ``SqliteLedger``
rather than calling its methods directly, because what is being asserted is a
property of the pair: the executor's "re-run anything that did not succeed"
rule and the store's memory of what did. Testing them apart would test neither.
"""

from __future__ import annotations

from typing import Any

import pytest

from clawdence.domain import EventKind, RunStatus, StepStatus, StepType, Workflow
from clawdence.engine import (
    HandlerOutcome,
    HandlerRegistry,
    RunReport,
    StepContext,
    StepFailure,
    StubHandler,
    execute,
)
from clawdence.store import DuplicateAttemptError, SqliteLedger, StateStore
from tests.engine.factories import run as drive
from tests.engine.factories import script, ticking_clock, workflow
from tests.store.factories import WORK_ITEM_ID, make_run, running_step

RUN_ID = "run.ledger"


def registry(handler: Any) -> HandlerRegistry:
    return HandlerRegistry(dict.fromkeys(StepType, handler))


def go(
    store: StateStore,
    wf: Workflow,
    handler: Any,
    *,
    run_id: str = RUN_ID,
    clock: Any = None,
) -> RunReport:
    return drive(
        execute(
            wf,
            run_id=run_id,
            work_item_id=WORK_ITEM_ID,
            registry=registry(handler),
            ledger=SqliteLedger(store, run_id=run_id, clock=clock or ticking_clock()),
            clock=clock or ticking_clock(),
        )
    )


class TestRecording:
    def test_a_run_and_its_steps_are_persisted(self, state: StateStore) -> None:
        go(state, workflow(script("a"), script("b")), StubHandler(output={"ok": True}))

        run = state.require_run(RUN_ID)
        assert run.status is RunStatus.DONE
        assert run.work_item_id == WORK_ITEM_ID
        assert [(step.stage_id, step.status) for step in state.steps_for(RUN_ID)] == [
            ("a", StepStatus.SUCCEEDED),
            ("b", StepStatus.SUCCEEDED),
        ]

    def test_the_declared_timeout_is_pinned_on_the_row(self, state: StateStore) -> None:
        """The watchdog reads this, so it has to be what the attempt ran under."""
        go(state, workflow(script("a", timeout_seconds=30)), StubHandler())
        assert state.steps_for(RUN_ID)[0].timeout_seconds == 30

    def test_the_output_is_readable_afterwards(self, state: StateStore) -> None:
        go(state, workflow(script("a")), StubHandler(output={"size": "L"}))
        assert state.steps_for(RUN_ID)[0].output == {"size": "L"}

    def test_the_timeline_names_what_happened(self, state: StateStore) -> None:
        go(state, workflow(script("a")), StubHandler())
        assert [event.kind for event in state.audit.read(run_id=RUN_ID)] == [
            EventKind.RUN_STARTED,
            EventKind.STEP_STARTED,
            EventKind.STEP_FINISHED,
            EventKind.RUN_FINISHED,
        ]

    def test_a_retry_is_a_different_event_from_a_first_attempt(self, state: StateStore) -> None:
        wf = workflow(script("a", retry={"max_attempts": 2}))
        go(state, wf, StubHandler(failure=StepFailure("boom", "no", retryable=True)))
        kinds = [event.kind for event in state.audit.read(run_id=RUN_ID)]
        assert kinds.count(EventKind.STEP_STARTED) == 1
        assert kinds.count(EventKind.STEP_RETRIED) == 1

    def test_the_payload_carries_the_error_kind_and_not_its_message(
        self, state: StateStore
    ) -> None:
        """Messages carry stderr tails, and this log cannot un-write a key."""
        wf = workflow(script("a"))
        go(state, wf, StubHandler(failure=StepFailure("script-exit", "sk-secret-leaked-here")))
        (finished,) = state.audit.read(run_id=RUN_ID, kinds=[EventKind.STEP_FINISHED])
        assert finished.payload == {"attempt": 1, "status": "failed", "error_kind": "script-exit"}

    def test_a_skipped_stage_is_still_written(self, state: StateStore) -> None:
        wf = workflow(script("a"), script("b", when='$a.json.size == "L"'))
        go(state, wf, StubHandler(output={"size": "M"}))
        assert state.steps_for(RUN_ID)[1].status is StepStatus.SKIPPED


class TestResume:
    def halting_workflow(self) -> Workflow:
        return workflow(script("a"), script("b"), script("c"))

    def test_a_succeeded_stage_is_not_run_again(self, state: StateStore) -> None:
        # 'a' succeeds, 'b' fails and stops the run, 'c' never runs.
        first = _FailAt("b")
        go(state, self.halting_workflow(), first)
        assert state.require_run(RUN_ID).status is RunStatus.HALTED
        assert first.calls == ["a", "b"]

        second = StubHandler(output={"ok": True})
        report = go(state, self.halting_workflow(), second)

        assert second.calls == ["b", "c"], "'a' had succeeded and must not be re-run"
        assert report.run.status is RunStatus.DONE

    def test_a_resumed_run_keeps_its_original_identity(self, state: StateStore) -> None:
        go(state, self.halting_workflow(), _FailAt("b"))
        started = state.require_run(RUN_ID).created_at

        go(state, self.halting_workflow(), StubHandler())

        resumed = state.require_run(RUN_ID)
        assert resumed.created_at == started
        assert resumed.status is RunStatus.DONE
        assert resumed.finished_at is not None

    def test_attempt_numbers_continue_rather_than_collide(self, state: StateStore) -> None:
        """Attempt is half the idempotency key, so it cannot restart at 1."""
        go(state, self.halting_workflow(), _FailAt("b"))
        go(state, self.halting_workflow(), StubHandler())

        attempts = [(step.stage_id, step.attempt) for step in state.steps_for(RUN_ID)]
        assert ("b", 1) in attempts
        assert ("b", 2) in attempts
        assert len({step.idempotency_key for step in state.steps_for(RUN_ID)}) == len(attempts)

    def test_the_report_shows_both_incarnations(self, state: StateStore) -> None:
        go(state, self.halting_workflow(), _FailAt("b"))
        report = go(state, self.halting_workflow(), StubHandler())
        assert [result.stage_id for result in report.attempts] == ["a", "b", "c", "b", "c"]

    def test_the_retry_budget_is_fresh_on_resume(self, state: StateStore) -> None:
        """Resuming is an operator's decision and deserves a full budget."""
        wf = workflow(script("a", retry={"max_attempts": 2}))
        failing = StubHandler(failure=StepFailure("boom", "no", retryable=True))
        go(state, wf, failing)
        assert len(failing.calls) == 2

        again = StubHandler(failure=StepFailure("boom", "no", retryable=True))
        go(state, wf, again)
        assert len(again.calls) == 2

    def test_a_step_abandoned_by_a_dead_process_is_closed_out(self, state: StateStore) -> None:
        """The stale-spawn bug: a row that says running when nobody is."""
        state.create_run(make_run(RUN_ID))
        state.start_step(running_step("a", run_id=RUN_ID, started=0))

        go(state, workflow(script("a")), StubHandler())

        first, second = state.steps_for(RUN_ID)
        assert first.status is StepStatus.CANCELLED
        assert first.error is not None
        assert first.error.kind == "interrupted"
        assert second.attempt == 2
        assert second.status is StepStatus.SUCCEEDED

    def test_resuming_a_finished_run_changes_nothing_it_did(self, state: StateStore) -> None:
        handler = StubHandler(output={"ok": True})
        go(state, workflow(script("a")), handler)
        go(state, workflow(script("a")), handler)
        assert handler.calls == ["a"]
        assert state.require_run(RUN_ID).status is RunStatus.DONE


class TestAtomicity:
    def test_a_failed_step_write_leaves_no_audit_record(self, state: StateStore) -> None:
        """State and log commit together, so the log cannot describe a rollback."""
        state.create_run(make_run(RUN_ID))
        ledger = SqliteLedger(state, run_id=RUN_ID)
        step = running_step("a", run_id=RUN_ID)
        ledger.begin(step)

        with pytest.raises(DuplicateAttemptError):
            ledger.begin(step)  # the same attempt twice: a duplicate dispatch

        started = state.audit.read(run_id=RUN_ID, kinds=[EventKind.STEP_STARTED])
        assert len(started) == 1


class _FailAt:
    """Succeeds at every stage but one. Handlers are per-type, not per-stage."""

    def __init__(self, stage_id: str) -> None:
        self.stage_id = stage_id
        self.calls: list[str] = []

    async def __call__(self, ctx: StepContext) -> HandlerOutcome:
        self.calls.append(ctx.stage.id)
        if ctx.stage.id == self.stage_id:
            raise StepFailure("boom", "this stage was told to fail")
        return HandlerOutcome(output={"ok": True})
