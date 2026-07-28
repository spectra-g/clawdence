"""Sequential execution: conditions, retries, timeouts, ``on_error``.

Async on purpose, and it is the one shape decision worth defending here. M1 runs
one stage at a time, and a synchronous executor would be simpler *today* — but
S3b's whole content is dynamic fan-out with a concurrency cap, and the plan's
own D9 says the Python answer is "a minimal native async DAG executor for
S3/S3b". Writing the sequential case as ``await``-ing one coroutine per stage
means S3b changes how stages are *scheduled* and touches nothing about how one
stage runs. Writing it synchronously would mean S3b rewrites this file and every
handler with it.

Timeouts come out correct for free: ``asyncio.wait_for`` cancels the handler,
the handler kills its child, and the step is recorded ``timed_out`` rather than
left running while the run moves on — which is the shape of v1's stale-spawn bug
that S4's watchdog exists to replace.

State lives in memory. S4 replaces ``_Ledger`` with SQLite rows and nothing else
in this file changes, which is why the executor hands handlers a ``Resolver``
rather than letting them reach for step results themselves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import JsonValue

from clawdence.domain import (
    OnError,
    Run,
    RunStatus,
    Stage,
    StepError,
    StepResult,
    StepStatus,
    Workflow,
)
from clawdence.engine import conditions
from clawdence.engine.conditions import Node
from clawdence.engine.errors import ConditionEvalError, StepFailure
from clawdence.engine.handlers import HandlerRegistry, StepContext, default_registry
from clawdence.engine.refs import Resolver

#: Statuses a stage can end in that mean "this did not do its job".
_UNSUCCESSFUL = frozenset({StepStatus.FAILED, StepStatus.TIMED_OUT})

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RunReport:
    """Everything one execution produced, in the order it happened.

    ``attempts`` holds every step result, superseded retries included; ``final``
    holds the one that counts per stage. Both, because "what did attempt 1 say?"
    and "what does ``$build.json`` mean?" are different questions, and S20's
    replay needs the first one answerable.
    """

    run: Run
    workflow: Workflow
    attempts: tuple[StepResult, ...]
    final: Mapping[str, StepResult]

    @property
    def succeeded(self) -> bool:
        return self.run.status is RunStatus.DONE

    @property
    def failed_stages(self) -> tuple[str, ...]:
        """Stages that ended unsuccessfully, including ones ``continue`` waved past.

        A run whose author declared a failure non-fatal still finishes ``done``.
        That is what ``on_error: continue`` means — and it is also why this
        property exists, because "the run completed" and "nothing failed" are
        different facts and a report that collapses them is lying by omission.
        """
        return tuple(
            stage_id for stage_id, result in self.final.items() if result.status in _UNSUCCESSFUL
        )


@dataclass(slots=True)
class _Ledger:
    """The in-memory stand-in for S4's ``runs`` / ``steps`` tables."""

    attempts: list[StepResult] = field(default_factory=list)
    final: dict[str, StepResult] = field(default_factory=dict)

    def record(self, result: StepResult) -> None:
        self.attempts.append(result)
        self.final[result.stage_id] = result


async def execute(
    workflow: Workflow,
    *,
    run_id: str,
    work_item_id: str,
    registry: HandlerRegistry | None = None,
    clock: Clock = _utc_now,
    sleep: Sleeper = asyncio.sleep,
) -> RunReport:
    """Run a workflow's stages in order and report what happened.

    Every condition in the workflow is parsed before the first stage starts, so
    a syntax error costs nothing rather than surfacing after the stages ahead of
    it have already spent the budget. ``sleep`` is injected so a test can assert
    retry backoff was honoured without spending the wall-clock time on it.
    """
    guards = {stage.id: conditions.parse(stage.when) for stage in workflow.stages if stage.when}

    handlers = registry if registry is not None else default_registry()
    ledger = _Ledger()
    resolver = Resolver(ledger.final)

    started_at = clock()
    stopped = False

    for stage in workflow.stages:
        if stopped:
            ledger.record(_skipped(run_id, stage, reason="an earlier stage stopped the run"))
            continue

        result = await _run_stage(
            stage,
            guard=guards.get(stage.id),
            run_id=run_id,
            handlers=handlers,
            resolver=resolver,
            ledger=ledger,
            clock=clock,
            sleep=sleep,
        )
        if result.status in _UNSUCCESSFUL and stage.on_error is not OnError.CONTINUE:
            stopped = True

    finished_at = clock()
    run = Run(
        id=run_id,
        work_item_id=work_item_id,
        workflow=workflow.name,
        workflow_version=workflow.version,
        status=RunStatus.HALTED if stopped else RunStatus.DONE,
        created_at=started_at,
        updated_at=finished_at,
        finished_at=finished_at,
    )
    return RunReport(
        run=run,
        workflow=workflow,
        attempts=tuple(ledger.attempts),
        final=dict(ledger.final),
    )


async def _run_stage(
    stage: Stage,
    *,
    guard: Node | None,
    run_id: str,
    handlers: HandlerRegistry,
    resolver: Resolver,
    ledger: _Ledger,
    clock: Clock,
    sleep: Sleeper,
) -> StepResult:
    if guard is not None:
        try:
            should_run = conditions.evaluate(guard, resolver)
        except ConditionEvalError as exc:
            at = clock()
            result = _result(
                run_id,
                stage,
                attempt=1,
                status=StepStatus.FAILED,
                started_at=at,
                finished_at=at,
                error=StepError(kind="condition-error", message=str(exc), retryable=False),
            )
            ledger.record(result)
            return result
        if not should_run:
            result = _skipped(run_id, stage, reason="its 'when' condition was false")
            ledger.record(result)
            return result

    handler = handlers.for_type(stage.type)
    attempt = 1
    while True:
        started_at = clock()
        status = StepStatus.SUCCEEDED
        error: StepError | None = None
        output: JsonValue = None
        response: JsonValue = None

        context = StepContext(run_id=run_id, stage=stage, attempt=attempt, resolver=resolver)
        try:
            outcome = await asyncio.wait_for(handler(context), timeout=stage.timeout_seconds)
        except TimeoutError:
            status = StepStatus.TIMED_OUT
            error = StepError(
                kind="timeout",
                message=f"exceeded the declared timeout of {stage.timeout_seconds}s",
                retryable=True,
            )
        except StepFailure as exc:
            status = StepStatus.FAILED
            error = StepError(kind=exc.kind, message=exc.message, retryable=exc.retryable)
        else:
            output = outcome.output
            response = outcome.response

        result = _result(
            run_id,
            stage,
            attempt=attempt,
            status=status,
            started_at=started_at,
            finished_at=clock(),
            output=output,
            response=response,
            error=error,
        )
        ledger.record(result)

        # Retry only while attempts remain *and* the failure is one a second
        # attempt could go differently. A workflow referencing a field the
        # previous stage never emits will reference it identically next time,
        # so retrying that spends the budget without changing the answer.
        if status is StepStatus.SUCCEEDED or error is None or not error.retryable:
            return result
        if attempt >= stage.retry.max_attempts:
            return result

        if stage.retry.backoff_seconds:
            await sleep(stage.retry.backoff_seconds)
        attempt += 1


def _skipped(run_id: str, stage: Stage, *, reason: str) -> StepResult:
    """A stage that did not run still gets a result.

    ``$plan.skipped`` has to mean something, and a run record with holes in it
    cannot answer "why is there no PR" months later. No timestamps: nothing
    started, so nothing has a duration.
    """
    return _result(
        run_id,
        stage,
        attempt=1,
        status=StepStatus.SKIPPED,
        started_at=None,
        finished_at=None,
        error=StepError(kind="skipped", message=reason, retryable=False),
    )


def _result(
    run_id: str,
    stage: Stage,
    *,
    attempt: int,
    status: StepStatus,
    started_at: datetime | None,
    finished_at: datetime | None,
    output: JsonValue = None,
    response: JsonValue = None,
    error: StepError | None = None,
) -> StepResult:
    key = idempotency_key(run_id, stage.id, attempt)
    return StepResult(
        id=f"sr.{key}",
        run_id=run_id,
        stage_id=stage.id,
        type=stage.type,
        status=status,
        attempt=attempt,
        idempotency_key=key,
        started_at=started_at,
        finished_at=finished_at,
        output=output,
        response=response,
        error=error,
    )


def idempotency_key(run_id: str, stage_id: str, attempt: int) -> str:
    """``run:stage:attempt`` — the unique constraint S4 will enforce in SQL.

    Derived rather than generated, so a redelivered dispatch collides instead of
    duplicating. v1's duplicate-event guards were hand-written per handler and
    drifted from each other; this is the same rule expressed once.
    """
    return f"{run_id}:{stage_id}:{attempt}"
