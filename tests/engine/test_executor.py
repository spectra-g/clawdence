"""Control flow: guards, retries, timeouts, ``on_error``, and the run record.

Uses ``StubHandler`` throughout. What a step *does* is tested in
``test_handlers``; what the executor does *around* a step is this file, and
mixing the two would make every control-flow test depend on subprocess timing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clawdence.domain import (
    AgentStage,
    ModelSelector,
    OnError,
    RetryPolicy,
    RunStatus,
    StepStatus,
    StepType,
    Workflow,
)
from clawdence.engine import HandlerRegistry, RunReport, StepFailure, StubHandler, execute
from clawdence.engine.executor import idempotency_key
from tests.engine.factories import RUN_ID, run, script, ticking_clock, workflow


def registry(handler: Any) -> HandlerRegistry:
    return HandlerRegistry(dict.fromkeys(StepType, handler))


def go(wf: Workflow, handler: Any, **kwargs: Any) -> RunReport:
    return run(
        execute(
            wf,
            run_id=RUN_ID,
            work_item_id="wi.test",
            registry=registry(handler),
            clock=ticking_clock(),
            **kwargs,
        )
    )


class TestSequencing:
    def test_stages_run_in_declared_order(self) -> None:
        stub = StubHandler(output={"ok": True})
        go(workflow(script("a"), script("b"), script("c")), stub)
        assert stub.calls == ["a", "b", "c"]

    def test_a_clean_run_is_done(self) -> None:
        report = go(workflow(script("a")), StubHandler())
        assert report.run.status is RunStatus.DONE
        assert report.succeeded is True
        assert report.failed_stages == ()

    def test_the_run_record_pins_the_workflow_version(self) -> None:
        report = go(workflow(script("a"), name="quick-fix", version="2.1.0"), StubHandler())
        assert report.run.workflow == "quick-fix"
        assert report.run.workflow_version == "2.1.0"

    def test_timestamps_bracket_the_run(self) -> None:
        report = go(workflow(script("a")), StubHandler())
        assert report.run.finished_at is not None
        assert report.run.created_at < report.run.finished_at


class TestGuards:
    def test_false_guard_skips_the_stage(self) -> None:
        stub = StubHandler(output={"size": "M"})
        wf = workflow(script("a"), script("b", when='$a.json.size == "L"'))
        report = go(wf, stub)
        assert stub.calls == ["a"]
        assert report.final["b"].status is StepStatus.SKIPPED

    def test_true_guard_runs_the_stage(self) -> None:
        stub = StubHandler(output={"size": "L"})
        wf = workflow(script("a"), script("b", when='$a.json.size == "L"'))
        assert go(wf, stub).final["b"].status is StepStatus.SUCCEEDED

    def test_a_skipped_stage_still_gets_a_result(self) -> None:
        # "$b.skipped" has to mean something, and a run record with holes in it
        # cannot answer "why is there no PR" months later.
        wf = workflow(script("a"), script("b", when="false"), script("c", when="$b.skipped"))
        report = go(wf, StubHandler())
        assert report.final["b"].error is not None
        assert report.final["b"].error.kind == "skipped"
        assert report.final["c"].status is StepStatus.SUCCEEDED

    def test_nonsense_comparison_fails_the_stage(self) -> None:
        wf = workflow(script("a"), script("b", when='$a.json.n > "seven"'))
        report = go(wf, StubHandler(output={"n": 3}))
        assert report.final["b"].status is StepStatus.FAILED
        assert report.final["b"].error is not None
        assert report.final["b"].error.kind == "condition-error"

    def test_every_condition_is_parsed_before_anything_runs(self) -> None:
        # The workflow is built in Python, bypassing the loader — the executor
        # still refuses it, and refuses it before stage "a" spends anything.
        from clawdence.engine import ConditionSyntaxError

        stub = StubHandler()
        wf = workflow(script("a"), script("b", when="$a.json.x == APPROVED"))
        with pytest.raises(ConditionSyntaxError):
            go(wf, stub)
        assert stub.calls == []


class TestRetries:
    def test_a_retryable_failure_is_retried_to_the_declared_cap(self) -> None:
        stub = StubHandler(failure=StepFailure("boom", "no", retryable=True))
        wf = workflow(script("a", retry=RetryPolicy(max_attempts=3)))
        report = go(wf, stub)
        assert stub.calls == ["a", "a", "a"]
        assert [r.attempt for r in report.attempts] == [1, 2, 3]
        assert report.final["a"].attempt == 3

    def test_a_non_retryable_failure_is_not_retried(self) -> None:
        # Attempt two would reference the same absent field and cost the same.
        stub = StubHandler(failure=StepFailure("interpolation", "nope", retryable=False))
        wf = workflow(script("a", retry=RetryPolicy(max_attempts=3)))
        report = go(wf, stub)
        assert stub.calls == ["a"]
        assert len(report.attempts) == 1

    def test_backoff_is_honoured_between_attempts(self) -> None:
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        stub = StubHandler(failure=StepFailure("boom", "no", retryable=True))
        wf = workflow(script("a", retry=RetryPolicy(max_attempts=3, backoff_seconds=1.5)))
        go(wf, stub, sleep=sleep)
        assert slept == [1.5, 1.5]

    def test_every_attempt_is_recorded_but_only_the_last_is_addressable(self) -> None:
        # The audit question and the data-flow question have different answers,
        # and S20's replay needs the first one answerable.
        attempts = iter(
            [StepFailure("boom", "first", retryable=True), None],
        )

        class Flaky:
            async def __call__(self, ctx: Any) -> Any:
                from clawdence.engine import HandlerOutcome

                failure = next(attempts)
                if failure is not None:
                    raise failure
                return HandlerOutcome(output={"attempt": 2})

        report = go(workflow(script("a", retry=RetryPolicy(max_attempts=2))), Flaky())
        assert [r.status for r in report.attempts] == [StepStatus.FAILED, StepStatus.SUCCEEDED]
        assert report.final["a"].output == {"attempt": 2}

    def test_idempotency_keys_are_unique_per_attempt(self) -> None:
        stub = StubHandler(failure=StepFailure("boom", "no", retryable=True))
        wf = workflow(script("a", retry=RetryPolicy(max_attempts=3)))
        report = go(wf, stub)
        keys = [r.idempotency_key for r in report.attempts]
        assert keys == [idempotency_key(RUN_ID, "a", n) for n in (1, 2, 3)]
        assert len(set(keys)) == 3


class TestTimeouts:
    def test_a_slow_step_is_recorded_timed_out(self) -> None:
        class Slow:
            async def __call__(self, ctx: Any) -> Any:
                await asyncio.sleep(10)
                raise AssertionError("should have been cancelled")

        report = go(workflow(script("a", timeout_seconds=0.01)), Slow())
        assert report.final["a"].status is StepStatus.TIMED_OUT
        assert report.final["a"].error is not None
        assert report.final["a"].error.kind == "timeout"

    def test_a_timeout_is_retryable(self) -> None:
        class Slow:
            calls = 0

            async def __call__(self, ctx: Any) -> Any:
                type(self).calls += 1
                await asyncio.sleep(10)

        slow = Slow()
        wf = workflow(script("a", timeout_seconds=0.01, retry=RetryPolicy(max_attempts=2)))
        go(wf, slow)
        assert Slow.calls == 2

    def test_no_declared_timeout_means_no_deadline(self) -> None:
        report = go(workflow(script("a")), StubHandler())
        assert report.final["a"].status is StepStatus.SUCCEEDED


class TestOnError:
    def test_fail_stops_the_run_and_skips_the_rest(self) -> None:
        stub = StubHandler(failure=StepFailure("boom", "no"))
        wf = workflow(script("a", on_error=OnError.FAIL), script("b"))
        report = go(wf, stub)
        assert stub.calls == ["a"]
        assert report.final["b"].status is StepStatus.SKIPPED
        assert report.run.status is RunStatus.HALTED

    def test_skip_rest_stops_the_run_and_skips_the_rest(self) -> None:
        stub = StubHandler(failure=StepFailure("boom", "no"))
        wf = workflow(script("a", on_error=OnError.SKIP_REST), script("b"), script("c"))
        report = go(wf, stub)
        assert stub.calls == ["a"]
        assert [report.final[s].status for s in ("b", "c")] == [StepStatus.SKIPPED] * 2
        assert report.run.status is RunStatus.HALTED

    def test_continue_carries_on(self) -> None:
        calls: list[str] = []

        class Handler:
            async def __call__(self, ctx: Any) -> Any:
                from clawdence.engine import HandlerOutcome

                calls.append(ctx.stage.id)
                if ctx.stage.id == "a":
                    raise StepFailure("boom", "no")
                return HandlerOutcome()

        wf = workflow(script("a", on_error=OnError.CONTINUE), script("b"))
        report = go(wf, Handler())
        assert calls == ["a", "b"]
        assert report.run.status is RunStatus.DONE

    def test_continue_still_reports_the_failure(self) -> None:
        # "The run completed" and "nothing failed" are different facts.
        class Handler:
            async def __call__(self, ctx: Any) -> Any:
                from clawdence.engine import HandlerOutcome

                if ctx.stage.id == "a":
                    raise StepFailure("boom", "no")
                return HandlerOutcome()

        report = go(workflow(script("a", on_error=OnError.CONTINUE), script("b")), Handler())
        assert report.succeeded is True
        assert report.failed_stages == ("a",)

    def test_fail_is_the_default(self) -> None:
        assert script("a").on_error is OnError.FAIL


class TestStepResults:
    def test_type_is_carried_from_the_stage(self) -> None:
        stage = AgentStage(
            id="a", role="ba", task="do it", model=ModelSelector(model="claude-opus-5")
        )
        report = go(workflow(stage), StubHandler())
        assert report.final["a"].type is StepType.AGENT

    def test_output_and_response_stay_separate(self) -> None:
        stub = StubHandler(output={"computed": True}, response={"human": True})
        report = go(workflow(script("a")), stub)
        assert report.final["a"].output == {"computed": True}
        assert report.final["a"].response == {"human": True}

    def test_a_skipped_stage_has_no_timestamps(self) -> None:
        report = go(workflow(script("a", when="false")), StubHandler())
        assert report.final["a"].started_at is None
        assert report.final["a"].finished_at is None


class TestTheRequest:
    """``execute(request=...)`` — the work item the run is for (S11).

    Passed rather than stored, because a run's request is fixed for its life: an
    amendment arriving mid-flight re-queues the item rather than changing what
    the stage running right now was asked to do.
    """

    def test_a_stage_can_read_the_request_that_started_the_run(self) -> None:
        stub = StubHandler()
        report = go(
            workflow(script("a", "echo", "${request.json.text}")),
            stub,
            request={"text": "fix the reaper"},
        )
        assert report.succeeded is True

    def test_a_guard_can_branch_on_it(self) -> None:
        """Which is what makes one workflow serve two kinds of request."""
        stub = StubHandler()
        report = go(
            workflow(
                script("a", when='$request.json.type == "bug"'),
                script("b", when='$request.json.type == "spike"'),
            ),
            stub,
            request={"type": "bug"},
        )
        assert stub.calls == ["a"]
        assert report.final["b"].status is StepStatus.SKIPPED

    def test_an_ad_hoc_run_has_none_and_that_is_not_an_error(self) -> None:
        """``clawdence run`` against a file has no work item behind it.

        The absence resolves to ``MISSING``, not to a crash and not to ``null`` —
        ``refs`` keeps those two apart deliberately, so a guard written for a
        pipeline run simply does not fire outside one. That is the safe
        direction: the alternative is a workflow silently taking the
        request-shaped branch on a run that has no request.
        """
        report = go(
            workflow(
                script("a", when='$request.json.type == "bug"'),
                script("b"),
            ),
            StubHandler(),
        )
        assert report.final["a"].status is StepStatus.SKIPPED
        assert report.final["b"].status is StepStatus.SUCCEEDED
        assert report.succeeded is True
