"""Builders for store tests — data only.

Opening things is ``conftest``'s job, because opened things have to be closed.
What is here is the boilerplate of a ``Run`` and a ``StepResult``, so a test
reads as the thing it is asserting.

Every instant comes from ``at()``, offset from one fixed start. Real clocks make
"is this overdue" tests depend on how fast the machine is, which is the one
thing a watchdog test must not do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from clawdence.domain import Run, RunStatus, StepError, StepResult, StepStatus, StepType
from clawdence.engine.executor import idempotency_key

RUN_ID = "run.test"
WORK_ITEM_ID = "wi.test"
START = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    """An instant, ``seconds`` after the fixed start of every test's world."""
    return START + timedelta(seconds=seconds)


def make_run(
    run_id: str = RUN_ID,
    *,
    status: RunStatus = RunStatus.RUNNING,
    created: float = 0.0,
    updated: float | None = None,
    workflow: str = "toy",
    version: str = "1.0.0",
) -> Run:
    return Run(
        id=run_id,
        work_item_id=WORK_ITEM_ID,
        workflow=workflow,
        workflow_version=version,
        status=status,
        created_at=at(created),
        updated_at=at(created if updated is None else updated),
    )


def make_step(
    stage_id: str,
    *,
    run_id: str = RUN_ID,
    status: StepStatus = StepStatus.SUCCEEDED,
    attempt: int = 1,
    started: float | None = 0.0,
    finished: float | None = 1.0,
    timeout: float | None = None,
    output: JsonValue = None,
    response: JsonValue = None,
    error: StepError | None = None,
) -> StepResult:
    key = idempotency_key(run_id, stage_id, attempt)
    return StepResult(
        id=f"sr.{key}",
        run_id=run_id,
        stage_id=stage_id,
        type=StepType.SCRIPT,
        status=status,
        attempt=attempt,
        idempotency_key=key,
        timeout_seconds=timeout,
        started_at=None if started is None else at(started),
        finished_at=None if finished is None else at(finished),
        output=output,
        response=response,
        error=error,
    )


def running_step(
    stage_id: str,
    *,
    run_id: str = RUN_ID,
    started: float = 0.0,
    attempt: int = 1,
    timeout: float | None = None,
) -> StepResult:
    """A step that has begun and not reported — what a killed process leaves."""
    return make_step(
        stage_id,
        run_id=run_id,
        status=StepStatus.RUNNING,
        attempt=attempt,
        started=started,
        finished=None,
        timeout=timeout,
    )
