"""Execution: ordered scopes, bounded composition, retries and timeouts.

S3 made each stage an awaited coroutine; S3b cashes that design bet. A scope is
still an ordered sequence, while ``for_each`` and ``parallel`` schedule child
scopes behind semaphores and join them before the next stage. Handlers did not
change: one invocation still receives one ``StepContext`` and returns one
``HandlerOutcome``.

Nested declarations get deterministic concrete ids derived from their scope.
The authored id and readable scope are recorded beside that id, which solves
both sides of composition: three concurrent copies of ``code`` cannot collide
in SQLite, and an operator can still tell which declaration and item a row is.

Timeouts remain structured cancellation: ``asyncio.wait_for`` cancels the handler,
the handler kills its child, and the step is recorded ``timed_out`` rather than
left running while the run moves on — which is the shape of v1's stale-spawn bug
that S4's watchdog exists to replace.

Where the record *goes* is the ``Ledger``'s business, not this file's. S4 put
SQLite behind that interface and nothing in the control flow below changed —
which is also why the executor hands handlers a ``Resolver`` rather than letting
them reach for step results themselves.
"""

from __future__ import annotations

import asyncio
from collections import ChainMap
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import blake2b
from typing import cast

from pydantic import JsonValue

from clawdence.domain import (
    ForEachStage,
    OnError,
    ParallelStage,
    RepeatStage,
    Run,
    RunStatus,
    Stage,
    StepError,
    StepResult,
    StepStatus,
    SubWorkflowStage,
    Workflow,
)
from clawdence.engine import conditions, interpolation
from clawdence.engine.conditions import Node
from clawdence.engine.errors import ConditionEvalError, InterpolationError, StepFailure
from clawdence.engine.handlers import (
    HandlerOutcome,
    HandlerRegistry,
    StepContext,
    default_registry,
)
from clawdence.engine.ledger import InMemoryLedger, Ledger
from clawdence.engine.refs import MISSING, Resolver, parse_reference

#: Statuses a stage can end in that mean "this did not do its job".
_UNSUCCESSFUL = frozenset({StepStatus.FAILED, StepStatus.TIMED_OUT})

#: A concurrency cap bounds handlers in flight, but creating a task per member
#: of an attacker-controlled million-item array can exhaust memory before the
#: first semaphore acquisition. Inputs above this explicit process-local bound
#: fail before child tasks are allocated. Distributed/bulk execution is not an
#: S3b capability.
MAX_FAN_OUT_ITEMS = 10_000

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
        latest: dict[str, StepResult] = {}
        for result in self.attempts:
            latest[result.stage_id] = result
        return tuple(
            _result_label(result) for result in latest.values() if result.status in _UNSUCCESSFUL
        )


async def execute(
    workflow: Workflow,
    *,
    run_id: str,
    work_item_id: str,
    registry: HandlerRegistry | None = None,
    ledger: Ledger | None = None,
    request: JsonValue = None,
    clock: Clock = _utc_now,
    sleep: Sleeper = asyncio.sleep,
) -> RunReport:
    """Run a workflow's stages in order and report what happened.

    Every condition in the workflow is parsed before the first stage starts, so
    a syntax error costs nothing rather than surfacing after the stages ahead of
    it have already spent the budget. ``sleep`` is injected so a test can assert
    retry backoff was honoured without spending the wall-clock time on it.

    ``request`` is the work item this run is for, readable by every stage as
    ``${request.json.…}`` (see ``refs``). It is passed rather than stored,
    because a run's request is fixed for the life of the run — an amendment
    arriving mid-flight re-queues the item (``store.intake``) rather than
    changing what the stage running right now was asked to do.

    Pass a durable ``ledger`` holding results for ``run_id`` and this resumes:
    **a stage is re-run unless it previously succeeded.** That rule is narrower
    than "skip what has a row" on purpose. A stage that failed is the reason
    someone is resuming; a stage the engine skipped was skipped *because of*
    that failure, or because a guard read a result that resuming may change, and
    re-evaluating a guard costs nothing and can only be more correct. The only
    thing worth trusting from a dead process is work it finished.
    """
    parsed = _parse_conditions(workflow)
    # Workflows built in Python deserve the same graph/reference guarantees as
    # YAML. Conditions were parsed first to preserve the executor's public
    # ``ConditionSyntaxError`` for programmatic callers.
    from clawdence.engine.loader import validate_references

    validate_references(workflow)

    handlers = registry if registry is not None else default_registry()
    book: Ledger = ledger if ledger is not None else InMemoryLedger()

    started_at = clock()
    # Opened before the resolver is built, because opening is what loads a
    # resumed run's prior results — and those are exactly what the first stage's
    # ``when`` may need to read. The return value is the *stored* record, which
    # on resume is older than the one just built; the executor does not need it,
    # since ``close_run`` returns the authoritative run at the end.
    book.open_run(
        Run(
            id=run_id,
            work_item_id=work_item_id,
            workflow=workflow.name,
            workflow_version=workflow.version,
            status=RunStatus.RUNNING,
            created_at=started_at,
            updated_at=started_at,
        )
    )
    sequence = await _execute_sequence(
        workflow.stages,
        scope=(),
        inherited={},
        variables={},
        request=request,
        run_id=run_id,
        handlers=handlers,
        ledger=book,
        workflow=workflow,
        parsed=parsed,
        serial_locks={},
        clock=clock,
        sleep=sleep,
    )

    run = book.close_run(
        status=RunStatus.HALTED if sequence.stopped else RunStatus.DONE, at=clock()
    )
    return RunReport(
        run=run,
        workflow=workflow,
        attempts=tuple(book.attempts),
        # Child executions remain in ``attempts`` and in the durable ledger,
        # while the root view retains the same authored-stage API S3 exposed.
        final=dict(sequence.final),
    )


@dataclass(frozen=True, slots=True)
class _SequenceOutcome:
    final: Mapping[str, StepResult]
    stopped: bool


def _parse_conditions(workflow: Workflow) -> dict[int, Node]:
    """Parse every guard and loop condition before the run spends anything."""
    parsed: dict[int, Node] = {}

    def visit(stages: Sequence[Stage]) -> None:
        for stage in stages:
            if stage.when is not None:
                parsed[id(stage)] = conditions.parse(stage.when)
            if isinstance(stage, RepeatStage):
                parsed[id(stage.until)] = conditions.parse(stage.until)
                visit(stage.stages)
            elif isinstance(stage, ForEachStage):
                visit(stage.stages)
            elif isinstance(stage, ParallelStage):
                for branch in stage.branches:
                    visit(branch.stages)
            elif isinstance(stage, SubWorkflowStage):
                # Definitions are visited once below. Following calls here
                # would recurse forever in a Python-built cyclic workflow.
                continue

    visit(workflow.stages)
    for definition in workflow.sub_workflows.values():
        visit(definition.stages)
    return parsed


async def _execute_sequence(
    stages: Sequence[Stage],
    *,
    scope: tuple[str, ...],
    inherited: Mapping[str, StepResult],
    variables: Mapping[str, JsonValue],
    request: JsonValue,
    run_id: str,
    handlers: HandlerRegistry,
    ledger: Ledger,
    workflow: Workflow,
    parsed: Mapping[int, Node],
    serial_locks: dict[str, asyncio.Lock],
    clock: Clock,
    sleep: Sleeper,
) -> _SequenceOutcome:
    """Execute one ordered scope; nested scopes call the same function."""
    local: dict[str, StepResult] = {}
    for stage in stages:
        existing = ledger.final.get(_execution_id(scope, stage.id))
        if existing is not None:
            local[stage.id] = existing

    visible: Mapping[str, StepResult] = ChainMap(local, dict(inherited))
    resolver = Resolver(visible, request=request, variables=variables)
    stopped = False

    for stage in stages:
        execution_id = _execution_id(scope, stage.id)
        previous = ledger.final.get(execution_id)
        if previous is not None and previous.status is StepStatus.SUCCEEDED:
            local[stage.id] = previous
            continue

        if stopped:
            result = _skipped(
                run_id,
                stage,
                execution_id=execution_id,
                scope=scope,
                attempt=ledger.next_attempt(execution_id),
                reason="an earlier stage stopped this scope",
            )
            ledger.record(result)
            local[stage.id] = result
            continue

        async def invoke(context: StepContext, current: Stage = stage) -> HandlerOutcome:
            if isinstance(current, ForEachStage | ParallelStage | SubWorkflowStage | RepeatStage):
                return await _run_composition(
                    current,
                    context=context,
                    scope=scope,
                    visible=visible,
                    variables=variables,
                    request=request,
                    run_id=run_id,
                    handlers=handlers,
                    ledger=ledger,
                    workflow=workflow,
                    parsed=parsed,
                    serial_locks=serial_locks,
                    clock=clock,
                    sleep=sleep,
                )
            return await handlers.for_type(current.type)(context)

        result = await _run_stage(
            stage,
            execution_id=execution_id,
            scope=scope,
            guard=parsed.get(id(stage)),
            invoke=invoke,
            run_id=run_id,
            resolver=resolver,
            ledger=ledger,
            clock=clock,
            sleep=sleep,
        )
        local[stage.id] = result
        if result.status in _UNSUCCESSFUL and stage.on_error is not OnError.CONTINUE:
            stopped = True

    return _SequenceOutcome(final=dict(local), stopped=stopped)


async def _run_composition(
    stage: ForEachStage | ParallelStage | SubWorkflowStage | RepeatStage,
    *,
    context: StepContext,
    scope: tuple[str, ...],
    visible: Mapping[str, StepResult],
    variables: Mapping[str, JsonValue],
    request: JsonValue,
    run_id: str,
    handlers: HandlerRegistry,
    ledger: Ledger,
    workflow: Workflow,
    parsed: Mapping[int, Node],
    serial_locks: dict[str, asyncio.Lock],
    clock: Clock,
    sleep: Sleeper,
) -> HandlerOutcome:
    if isinstance(stage, ForEachStage):
        reference = parse_reference(stage.items)
        items = context.resolver.resolve(reference)
        if items is MISSING or not isinstance(items, list):
            raise StepFailure(
                "fan-out-items",
                f"{stage.items!r} must resolve to a JSON array",
                retryable=False,
            )
        if len(items) > MAX_FAN_OUT_ITEMS:
            raise StepFailure(
                "fan-out-too-large",
                f"fan-out produced {len(items)} items; the limit is {MAX_FAN_OUT_ITEMS}",
                retryable=False,
            )

        semaphore = asyncio.Semaphore(stage.max_parallel)

        async def run_item(index: int, item: JsonValue) -> JsonValue:
            item_variables = {**variables, stage.item_var: item, stage.index_var: index}
            key: str | None = None
            if stage.serial_key is not None:
                try:
                    key = interpolation.expand(
                        stage.serial_key,
                        Resolver(visible, request=request, variables=item_variables),
                    )
                except InterpolationError as exc:
                    raise StepFailure("fan-out-serial-key", str(exc), retryable=False) from exc

            if key is None:
                async with semaphore:
                    outcome = await _execute_sequence(
                        stage.stages,
                        scope=(*scope, f"{stage.id}[{index}]"),
                        inherited=visible,
                        variables=item_variables,
                        request=request,
                        run_id=run_id,
                        handlers=handlers,
                        ledger=ledger,
                        workflow=workflow,
                        parsed=parsed,
                        serial_locks=serial_locks,
                        clock=clock,
                        sleep=sleep,
                    )
            else:
                # Waiting for another item from the same repository must not
                # consume a fleet slot; otherwise two queued siblings can
                # starve ready work for every other repository.
                async with serial_locks.setdefault(key, asyncio.Lock()):
                    async with semaphore:
                        outcome = await _execute_sequence(
                            stage.stages,
                            scope=(*scope, f"{stage.id}[{index}]"),
                            inherited=visible,
                            variables=item_variables,
                            request=request,
                            run_id=run_id,
                            handlers=handlers,
                            ledger=ledger,
                            workflow=workflow,
                            parsed=parsed,
                            serial_locks=serial_locks,
                            clock=clock,
                            sleep=sleep,
                        )
            if outcome.stopped:
                raise StepFailure(
                    "fan-out-branch-failed",
                    f"item {index} stopped after an unsuccessful stage",
                    retryable=True,
                )
            return {
                "index": index,
                "item": item,
                "stages": _result_view(outcome.final),
            }

        settled = await asyncio.gather(
            *(run_item(index, item) for index, item in enumerate(items)),
            return_exceptions=True,
        )
        completed = _completed(settled)
        return HandlerOutcome(output={"items": completed, "count": len(completed)})

    if isinstance(stage, ParallelStage):
        semaphore = asyncio.Semaphore(min(stage.max_parallel, len(stage.branches)))

        async def run_branch(branch_id: str, branch_stages: Sequence[Stage]) -> JsonValue:
            async with semaphore:
                outcome = await _execute_sequence(
                    branch_stages,
                    scope=(*scope, f"{stage.id}.{branch_id}"),
                    inherited=visible,
                    variables=variables,
                    request=request,
                    run_id=run_id,
                    handlers=handlers,
                    ledger=ledger,
                    workflow=workflow,
                    parsed=parsed,
                    serial_locks=serial_locks,
                    clock=clock,
                    sleep=sleep,
                )
            if outcome.stopped:
                raise StepFailure(
                    "parallel-branch-failed",
                    f"branch {branch_id!r} stopped after an unsuccessful stage",
                    retryable=True,
                )
            return {"id": branch_id, "stages": _result_view(outcome.final)}

        settled = await asyncio.gather(
            *(run_branch(branch.id, branch.stages) for branch in stage.branches),
            return_exceptions=True,
        )
        completed = _completed(settled)
        branches: dict[str, JsonValue] = {}
        for branch in completed:
            if not isinstance(branch, dict):  # pragma: no cover - run_branch owns the shape
                raise RuntimeError("parallel branch returned a non-object")
            branch_id = branch.get("id")
            if not isinstance(branch_id, str):  # pragma: no cover - same invariant
                raise RuntimeError("parallel branch returned no id")
            branches[branch_id] = branch.get("stages")
        return HandlerOutcome(output={"branches": branches})

    if isinstance(stage, SubWorkflowStage):
        definition = workflow.sub_workflows.get(stage.workflow)
        if definition is None:
            raise StepFailure(
                "sub-workflow-not-found",
                f"sub-workflow {stage.workflow!r} is not defined",
                retryable=False,
            )
        inputs: dict[str, JsonValue] = {}
        for name, value in stage.inputs.items():
            if isinstance(value, str) and value.startswith("$"):
                resolved = context.resolver.resolve(parse_reference(value))
                if resolved is MISSING:
                    raise StepFailure(
                        "sub-workflow-input",
                        f"input {name!r} resolves to nothing",
                        retryable=False,
                    )
                inputs[name] = cast(JsonValue, resolved)
            else:
                inputs[name] = value
        outcome = await _execute_sequence(
            definition.stages,
            scope=(*scope, f"{stage.id}.{stage.workflow}"),
            inherited={},
            variables=inputs,
            request=request,
            run_id=run_id,
            handlers=handlers,
            ledger=ledger,
            workflow=workflow,
            parsed=parsed,
            serial_locks=serial_locks,
            clock=clock,
            sleep=sleep,
        )
        if outcome.stopped:
            raise StepFailure(
                "sub-workflow-failed",
                f"sub-workflow {stage.workflow!r} stopped after an unsuccessful stage",
                retryable=True,
            )
        return HandlerOutcome(
            output={"workflow": stage.workflow, "stages": _result_view(outcome.final)}
        )

    previous: JsonValue = None
    iterations: list[JsonValue] = []
    for number in range(1, stage.max_iterations + 1):
        loop_variables = {**variables, "iteration": number, "previous": previous}
        outcome = await _execute_sequence(
            stage.stages,
            scope=(*scope, f"{stage.id}[{number}]"),
            inherited=visible,
            variables=loop_variables,
            request=request,
            run_id=run_id,
            handlers=handlers,
            ledger=ledger,
            workflow=workflow,
            parsed=parsed,
            serial_locks=serial_locks,
            clock=clock,
            sleep=sleep,
        )
        if outcome.stopped:
            raise StepFailure(
                "loop-body-failed",
                f"iteration {number} stopped after an unsuccessful stage",
                retryable=True,
            )
        view = _result_view(outcome.final)
        previous = view
        iterations.append({"iteration": number, "stages": view})
        until_resolver = Resolver(
            ChainMap(dict(outcome.final), dict(visible)),
            request=request,
            variables=loop_variables,
        )
        until = parsed.get(id(stage.until)) or conditions.parse(stage.until)
        try:
            finished = conditions.evaluate(until, until_resolver)
        except ConditionEvalError as exc:
            raise StepFailure("condition-error", str(exc), retryable=False) from exc
        if finished:
            return HandlerOutcome(output={"iterations": iterations, "count": number})
    raise StepFailure(
        "loop-exhausted",
        f"condition {stage.until!r} was still false after {stage.max_iterations} iterations",
        retryable=False,
    )


def _result_view(results: Mapping[str, StepResult]) -> dict[str, JsonValue]:
    return {
        stage_id: {
            "status": result.status.value,
            "output": result.output,
            "response": result.response,
            "error": result.error.model_dump(mode="json") if result.error is not None else None,
        }
        for stage_id, result in results.items()
    }


def _result_label(result: StepResult) -> str:
    if not result.scope:
        return result.stage_id
    definition = result.definition_id if result.definition_id is not None else result.stage_id
    return " / ".join((*result.scope, definition))


def _completed(settled: Sequence[JsonValue | BaseException]) -> list[JsonValue]:
    """Unwrap ``gather(return_exceptions=True)`` after every sibling settled."""
    for value in settled:
        if isinstance(value, BaseException):
            raise value
    return [cast(JsonValue, value) for value in settled]


def _execution_id(scope: tuple[str, ...], definition_id: str) -> str:
    if not scope:
        return definition_id
    route = "\0".join((*scope, definition_id)).encode()
    return "x" + blake2b(route, digest_size=30).hexdigest()


async def _run_stage(
    stage: Stage,
    *,
    execution_id: str,
    scope: tuple[str, ...],
    guard: Node | None,
    invoke: Callable[[StepContext], Awaitable[HandlerOutcome]],
    run_id: str,
    resolver: Resolver,
    ledger: Ledger,
    clock: Clock,
    sleep: Sleeper,
) -> StepResult:
    attempt = ledger.next_attempt(execution_id)

    if guard is not None:
        try:
            should_run = conditions.evaluate(guard, resolver)
        except ConditionEvalError as exc:
            at = clock()
            result = _result(
                run_id,
                stage,
                execution_id=execution_id,
                scope=scope,
                attempt=attempt,
                status=StepStatus.FAILED,
                started_at=at,
                finished_at=at,
                error=StepError(kind="condition-error", message=str(exc), retryable=False),
            )
            ledger.record(result)
            return result
        if not should_run:
            result = _skipped(
                run_id,
                stage,
                execution_id=execution_id,
                scope=scope,
                attempt=attempt,
                reason="its 'when' condition was false",
            )
            ledger.record(result)
            return result

    # Two counters, because they answer different questions. ``attempt`` is
    # global to (run, stage) and is half the idempotency key, so it continues
    # across a resume rather than restarting at 1 and colliding with a row the
    # dead process already wrote. ``tries`` is local to this execution and is
    # what the declared retry policy caps: resuming a halted run is an operator
    # decision and deserves a fresh retry budget, not the exhausted one.
    tries = 0
    while True:
        tries += 1
        started_at = clock()
        status = StepStatus.SUCCEEDED
        error: StepError | None = None
        output: JsonValue = None
        response: JsonValue = None

        # Written before the handler is awaited, so a process that dies mid-step
        # leaves a row saying so. That row — its ``started_at`` and the timeout
        # it was started under — is the watchdog's only input.
        ledger.begin(
            _result(
                run_id,
                stage,
                execution_id=execution_id,
                scope=scope,
                attempt=attempt,
                status=StepStatus.RUNNING,
                started_at=started_at,
                finished_at=None,
            )
        )

        context = StepContext(
            run_id=run_id,
            stage=stage,
            attempt=attempt,
            resolver=resolver,
            execution_id=execution_id,
        )
        try:
            outcome = await asyncio.wait_for(invoke(context), timeout=stage.timeout_seconds)
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
        except asyncio.CancelledError:
            cancelled = _result(
                run_id,
                stage,
                execution_id=execution_id,
                scope=scope,
                attempt=attempt,
                status=StepStatus.CANCELLED,
                started_at=started_at,
                finished_at=clock(),
                error=StepError(
                    kind="cancelled",
                    message="the enclosing composition was cancelled",
                    retryable=True,
                ),
            )
            ledger.record(cancelled)
            raise
        else:
            output = outcome.output
            response = outcome.response

        result = _result(
            run_id,
            stage,
            execution_id=execution_id,
            scope=scope,
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
        if tries >= stage.retry.max_attempts:
            return result

        if stage.retry.backoff_seconds:
            await sleep(stage.retry.backoff_seconds)
        attempt += 1


def _skipped(
    run_id: str,
    stage: Stage,
    *,
    execution_id: str,
    scope: tuple[str, ...],
    attempt: int,
    reason: str,
) -> StepResult:
    """A stage that did not run still gets a result.

    ``$plan.skipped`` has to mean something, and a run record with holes in it
    cannot answer "why is there no PR" months later. No timestamps: nothing
    started, so nothing has a duration.
    """
    return _result(
        run_id,
        stage,
        execution_id=execution_id,
        scope=scope,
        attempt=attempt,
        status=StepStatus.SKIPPED,
        started_at=None,
        finished_at=None,
        error=StepError(kind="skipped", message=reason, retryable=False),
    )


def _result(
    run_id: str,
    stage: Stage,
    *,
    execution_id: str,
    scope: tuple[str, ...],
    attempt: int,
    status: StepStatus,
    started_at: datetime | None,
    finished_at: datetime | None,
    output: JsonValue = None,
    response: JsonValue = None,
    error: StepError | None = None,
) -> StepResult:
    key = idempotency_key(run_id, execution_id, attempt)
    return StepResult(
        id=f"sr.{key}",
        run_id=run_id,
        stage_id=execution_id,
        definition_id=stage.id,
        scope=scope,
        type=stage.type,
        status=status,
        attempt=attempt,
        idempotency_key=key,
        timeout_seconds=stage.timeout_seconds,
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
