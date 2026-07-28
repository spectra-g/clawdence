"""Builders for engine tests.

Two things every engine test needs and neither is interesting: a ``Resolver``
over some step results, and a ``Workflow`` with the boilerplate filled in.
Built here so a test reads as the thing it is asserting.

Tests drive the executor through ``run`` rather than ``pytest-asyncio``. One
``asyncio.run`` per test is the whole of what the plugin would provide, and this
project pins every dependency exactly — a dependency is a maintenance
obligation, and this one would buy a decorator.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import JsonValue

from clawdence.domain import (
    ScriptStage,
    Stage,
    StepResult,
    StepStatus,
    StepType,
    Workflow,
)
from clawdence.engine import Resolver
from clawdence.engine.executor import idempotency_key

RUN_ID = "run.test"
START = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def ticking_clock(step: float = 1.0) -> Any:
    """A clock that advances a fixed amount per call.

    Fixed rather than real so a report's durations are assertable and a test
    never depends on how fast the machine running it happens to be.
    """
    state = {"n": 0}

    def clock() -> datetime:
        now = START + timedelta(seconds=step * state["n"])
        state["n"] += 1
        return now

    return clock


def step_result(
    stage_id: str,
    *,
    status: StepStatus = StepStatus.SUCCEEDED,
    output: JsonValue = None,
    response: JsonValue = None,
    step_type: StepType = StepType.SCRIPT,
    attempt: int = 1,
) -> StepResult:
    key = idempotency_key(RUN_ID, stage_id, attempt)
    return StepResult(
        id=f"sr.{key}",
        run_id=RUN_ID,
        stage_id=stage_id,
        type=step_type,
        status=status,
        attempt=attempt,
        idempotency_key=key,
        output=output,
        response=response,
    )


def resolver(**results: StepResult) -> Resolver:
    """``resolver(plan=step_result("plan", output={...}))``."""
    return Resolver(dict(results))


def resolver_for(**outputs: JsonValue) -> Resolver:
    """``resolver_for(plan={"confidence": "high"})`` — the common shorthand."""
    return Resolver(
        {stage_id: step_result(stage_id, output=output) for stage_id, output in outputs.items()}
    )


def script(stage_id: str, *command: str, **kwargs: Any) -> ScriptStage:
    return ScriptStage(id=stage_id, command=tuple(command) or ("true",), **kwargs)


def workflow(*stages: Stage, name: str = "test", version: str = "1.0.0") -> Workflow:
    return Workflow(name=name, version=version, stages=tuple(stages))
