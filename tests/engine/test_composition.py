"""S3b composition: bounded work, barriers, scopes, and restart safety."""

from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar

import pytest

from clawdence.domain import StepStatus, StepType, Workflow
from clawdence.engine import (
    HandlerOutcome,
    HandlerRegistry,
    Ledger,
    RunReport,
    StepFailure,
    WorkflowLoadError,
    execute,
    load_workflow,
    parse_reference,
    parse_workflow,
    render_text,
)
from clawdence.engine.executor import MAX_FAN_OUT_ITEMS
from clawdence.store import SqliteLedger, StateStore
from tests.engine.factories import RUN_ID, run


def load(body: str) -> Workflow:
    return parse_workflow(body, origin="composition.yaml")


def registry(handler: Any) -> HandlerRegistry:
    return HandlerRegistry(dict.fromkeys(StepType, handler))


def go(
    workflow: Workflow,
    handler: Any,
    *,
    ledger: Ledger | None = None,
) -> RunReport:
    return run(
        execute(
            workflow,
            run_id=RUN_ID,
            work_item_id="wi.test",
            registry=registry(handler),
            ledger=ledger,
        )
    )


FAN_OUT = """
name: composition
version: 1.0.0
stages:
  - id: decompose
    type: script
    command: [decompose]
  - id: implement
    type: for_each
    items: $decompose.json.items
    max_parallel: 2
    stages:
      - id: work
        type: script
        command: [work, '${item.json.name}']
  - id: join
    type: script
    command: [join, '${implement.json.count}']
"""


class TestDynamicFanOut:
    def test_shipped_composition_example_executes(self) -> None:
        workflow = load_workflow(Path("examples/composition.yaml"))
        report = run(execute(workflow, run_id=RUN_ID, work_item_id="wi.test"))
        assert report.succeeded is True
        output = report.final["build"].output
        assert isinstance(output, dict)
        assert output["count"] == 3

    def test_runtime_count_is_bounded_and_join_waits_for_every_item(self) -> None:
        class Handler:
            concurrent = 0
            peak = 0
            completed = 0
            join_saw = -1

            async def __call__(self, context: Any) -> HandlerOutcome:
                if context.stage.id == "decompose":
                    return HandlerOutcome(
                        output={"items": [{"name": name} for name in ("a", "b", "c")]}
                    )
                if context.stage.id == "join":
                    type(self).join_saw = type(self).completed
                    return HandlerOutcome()
                type(self).concurrent += 1
                type(self).peak = max(type(self).peak, type(self).concurrent)
                await asyncio.sleep(0.01)
                type(self).concurrent -= 1
                type(self).completed += 1
                return HandlerOutcome(output={"done": True})

        report = go(load(FAN_OUT), Handler())

        assert Handler.peak == 2
        assert Handler.join_saw == 3
        fan_out = report.final["implement"].output
        assert isinstance(fan_out, dict)
        assert fan_out["count"] == 3
        children = [result for result in report.attempts if result.definition_id == "work"]
        assert len(children) == 3
        assert len({result.stage_id for result in children}) == 3
        assert {result.scope for result in children} == {
            ("implement[0]",),
            ("implement[1]",),
            ("implement[2]",),
        }
        assert "implement[0] / work" in render_text(report)

    def test_an_untrusted_array_cannot_allocate_unbounded_child_tasks(self) -> None:
        class Handler:
            async def __call__(self, context: Any) -> HandlerOutcome:
                return HandlerOutcome(output={"items": [None] * (MAX_FAN_OUT_ITEMS + 1)})

        report = go(load(FAN_OUT), Handler())
        assert report.final["implement"].status is StepStatus.FAILED
        assert report.final["implement"].error is not None
        assert report.final["implement"].error.kind == "fan-out-too-large"
        assert not any(result.definition_id == "work" for result in report.attempts)

    def test_equal_serial_keys_never_overlap_but_other_keys_can(self) -> None:
        workflow = load(
            FAN_OUT.replace(
                "max_parallel: 2",
                "max_parallel: 3\n    serial_key: '${item.json.repo}'",
            )
        )

        class Handler:
            active: ClassVar[Counter[str]] = Counter()
            repo_peak: ClassVar[Counter[str]] = Counter()
            global_active = 0
            global_peak = 0

            async def __call__(self, context: Any) -> HandlerOutcome:
                if context.stage.id == "decompose":
                    return HandlerOutcome(
                        output={
                            "items": [
                                {"name": "a", "repo": "one"},
                                {"name": "b", "repo": "one"},
                                {"name": "c", "repo": "two"},
                            ]
                        }
                    )
                if context.stage.id == "join":
                    return HandlerOutcome()
                item = context.resolver.resolve(parse_reference("$item.json"))
                assert isinstance(item, dict)
                repo = item["repo"]
                type(self).active[repo] += 1
                type(self).repo_peak[repo] = max(
                    type(self).repo_peak[repo], type(self).active[repo]
                )
                type(self).global_active += 1
                type(self).global_peak = max(type(self).global_peak, type(self).global_active)
                await asyncio.sleep(0.01)
                type(self).active[repo] -= 1
                type(self).global_active -= 1
                return HandlerOutcome()

        go(workflow, Handler())
        assert Handler.repo_peak == Counter({"one": 1, "two": 1})
        assert Handler.global_peak == 2

    def test_serial_keys_are_shared_across_parallel_fanouts(self) -> None:
        workflow = load(
            """
name: shared-keys
version: 1.0.0
stages:
  - {id: seed, type: script, command: [seed]}
  - id: batches
    type: parallel
    branches:
      - id: left
        stages:
          - id: left-items
            type: for_each
            items: $seed.json.items
            serial_key: '${item.json.repo}'
            stages: [{id: left-work, type: script, command: [work]}]
      - id: right
        stages:
          - id: right-items
            type: for_each
            items: $seed.json.items
            serial_key: '${item.json.repo}'
            stages: [{id: right-work, type: script, command: [work]}]
"""
        )
        active = 0
        peak = 0

        class Handler:
            async def __call__(self, context: Any) -> HandlerOutcome:
                nonlocal active, peak
                if context.stage.id == "seed":
                    return HandlerOutcome(output={"items": [{"repo": "same"}]})
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1
                return HandlerOutcome()

        go(workflow, Handler())
        assert peak == 1

    def test_cancel_then_resume_runs_only_incomplete_items(self, db: sqlite3.Connection) -> None:
        workflow = load(FAN_OUT)
        ledger = SqliteLedger(StateStore(db), run_id=RUN_ID)
        first_finished = asyncio.Event()
        release = asyncio.Event()
        calls: Counter[str] = Counter()

        class Handler:
            async def __call__(self, context: Any) -> HandlerOutcome:
                if context.stage.id == "decompose":
                    return HandlerOutcome(
                        output={"items": [{"name": name} for name in ("a", "b", "c")]}
                    )
                if context.stage.id == "join":
                    return HandlerOutcome()
                item = context.resolver.resolve(parse_reference("$item.json.name"))
                assert isinstance(item, str)
                calls[item] += 1
                if item == "a":
                    first_finished.set()
                else:
                    await release.wait()
                return HandlerOutcome(output={"item": item})

        async def interrupt() -> None:
            task = asyncio.create_task(
                execute(
                    workflow,
                    run_id=RUN_ID,
                    work_item_id="wi.test",
                    registry=registry(Handler()),
                    ledger=ledger,
                )
            )
            await first_finished.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        run(interrupt())
        release.set()
        report = go(workflow, Handler(), ledger=ledger)

        assert report.succeeded is True
        assert calls["a"] == 1
        assert calls["b"] >= 1
        assert calls["c"] >= 1
        a_results = [
            result
            for result in report.attempts
            if result.definition_id == "work" and result.output == {"item": "a"}
        ]
        assert len(a_results) == 1


class TestSubWorkflows:
    def test_explicit_json_input_reaches_a_reusable_pipeline(self) -> None:
        workflow = load(
            """
name: nested
version: 1.0.0
sub_workflows:
  deliver:
    inputs: [story]
    stages:
      - id: code
        type: script
        command: [code, '${story.json.title}']
      - id: verify
        type: script
        command: [verify, '${code.json.title}']
stages:
  - id: plan
    type: script
    command: [plan]
  - id: delivery
    type: workflow
    workflow: deliver
    inputs:
      story: $plan.json.story
"""
        )

        class Handler:
            async def __call__(self, context: Any) -> HandlerOutcome:
                if context.stage.id == "plan":
                    return HandlerOutcome(output={"story": {"title": "bounded work"}})
                if context.stage.id == "code":
                    title = context.resolver.resolve(parse_reference("$story.json.title"))
                    return HandlerOutcome(output={"title": title})
                return HandlerOutcome()

        report = go(workflow, Handler())
        assert report.succeeded is True
        assert [result.definition_id for result in report.attempts] == [
            "plan",
            "code",
            "verify",
            "delivery",
        ]

    def test_cycle_is_rejected_while_loading(self) -> None:
        with pytest.raises(WorkflowLoadError, match=r"a -> b -> a"):
            load(
                """
name: cyclic
version: 1.0.0
sub_workflows:
  a:
    stages: [{id: call-b, type: workflow, workflow: b}]
  b:
    stages: [{id: call-a, type: workflow, workflow: a}]
stages: [{id: start, type: workflow, workflow: a}]
"""
            )


class TestLoopsAndParallelBranches:
    def test_loop_reads_previous_iteration_and_stops_at_until(self) -> None:
        workflow = load(
            """
name: loop
version: 1.0.0
stages:
  - id: retry
    type: repeat
    max_iterations: 3
    until: $check.json.done
    stages:
      - id: check
        type: script
        command: [check, '${iteration.json}']
"""
        )
        previous_seen: list[Any] = []

        class Handler:
            async def __call__(self, context: Any) -> HandlerOutcome:
                iteration = context.resolver.resolve(parse_reference("$iteration.json"))
                previous_seen.append(context.resolver.resolve(parse_reference("$previous.json")))
                return HandlerOutcome(output={"done": iteration == 2})

        report = go(workflow, Handler())
        loop = report.final["retry"].output
        assert isinstance(loop, dict)
        assert loop["count"] == 2
        assert previous_seen[0] is None
        assert previous_seen[1]["check"]["output"] == {"done": False}

    def test_loop_exhaustion_halts_instead_of_force_proceeding(self) -> None:
        workflow = load(
            """
name: loop
version: 1.0.0
stages:
  - id: retry
    type: repeat
    max_iterations: 2
    until: $check.json.done
    stages:
      - {id: check, type: script, command: [check]}
  - {id: after, type: script, command: [after]}
"""
        )
        report = go(workflow, lambda_context_handler({"done": False}))
        assert report.final["retry"].status is StepStatus.FAILED
        assert report.final["retry"].error is not None
        assert report.final["retry"].error.kind == "loop-exhausted"
        assert report.final["after"].status is StepStatus.SKIPPED

    def test_parallel_stage_is_a_barrier(self) -> None:
        workflow = load(
            """
name: parallel
version: 1.0.0
stages:
  - id: both
    type: parallel
    max_parallel: 2
    branches:
      - {id: left, stages: [{id: one, type: script, command: [one]}]}
      - {id: right, stages: [{id: two, type: script, command: [two]}]}
  - {id: join, type: script, command: [join]}
"""
        )
        completed: set[str] = set()

        class Handler:
            async def __call__(self, context: Any) -> HandlerOutcome:
                if context.stage.id == "join":
                    assert completed == {"one", "two"}
                else:
                    await asyncio.sleep(0.005)
                    completed.add(context.stage.id)
                return HandlerOutcome()

        assert go(workflow, Handler()).succeeded is True

    def test_nested_continue_failure_remains_visible_in_the_report(self) -> None:
        workflow = load(
            """
name: parallel
version: 1.0.0
stages:
  - id: branch
    type: parallel
    branches:
      - id: only
        stages:
          - {id: optional-check, type: script, command: [check], on_error: continue}
"""
        )

        class Handler:
            async def __call__(self, context: Any) -> HandlerOutcome:
                raise StepFailure("check-failed", "visible", retryable=False)

        report = go(workflow, Handler())
        assert report.succeeded is True
        assert report.failed_stages == ("branch.only / optional-check",)


def lambda_context_handler(output: Any) -> Any:
    class Handler:
        async def __call__(self, context: Any) -> HandlerOutcome:
            return HandlerOutcome(output=output)

    return Handler()
