"""The ``runner`` step type: what the engine does with each outcome."""

from __future__ import annotations

from decimal import Decimal

import pytest

from clawdence.domain import (
    Budget,
    ContractKind,
    DiffStat,
    RunnerOutcome,
    RunnerStage,
    VerificationContract,
)
from clawdence.engine import StepContext, StepFailure
from clawdence.ports import FakeRunner, PermanentError, TransientError
from clawdence.runners import Dispatch, RunnerHandler
from tests.engine.factories import RUN_ID, resolver_for, run
from tests.ports import factories as make
from tests.runners.conftest import host_profile


def dispatch_to(**overrides: object) -> Dispatch:
    fields: dict[str, object] = {
        "profile": host_profile(),
        "work_item_id": "wi.test",
        "branch": "clawdence/code",
        "base_commit": make.commit(1),
        "worktree_path": "/clawdence/work/run.test",
        "contract": VerificationContract(kind=ContractKind.TEST_AFTER),
    }
    fields.update(overrides)
    return Dispatch(**fields)  # type: ignore[arg-type]


def context(stage: RunnerStage, *, attempt: int = 1, **outputs: object) -> StepContext:
    return StepContext(RUN_ID, stage, attempt, resolver_for(**outputs))  # type: ignore[arg-type]


def handler(runner: FakeRunner, *, plan: str = "add a function", **kwargs: object) -> RunnerHandler:
    return RunnerHandler(
        runner=runner,
        dispatch=dispatch_to(**kwargs),
        plan_template=plan,
    )


def test_a_successful_run_becomes_a_step_output() -> None:
    runner = FakeRunner(default=make.runner_result(files_changed=2))
    outcome = run(handler(runner)(context(RunnerStage(id="code"))))

    assert outcome.output == {
        "outcome": "succeeded",
        "tree_hash": make.commit(2),
        "exit_code": None,
        "files_changed": 2,
        "insertions": 2,
        "deletions": 0,
        "tests_failed": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "usd": None,
    }


def test_the_request_carries_the_dispatch_target() -> None:
    runner = FakeRunner(default=make.runner_result())
    run(handler(runner, carried_stubs=("retry policy",))(context(RunnerStage(id="code"))))

    request = runner.dispatched[0]
    assert request.worktree_path == "/clawdence/work/run.test"
    assert request.branch == "clawdence/code"
    assert request.carried_stubs == ("retry policy",)


def test_the_idempotency_key_is_the_ledgers() -> None:
    """So a redelivered dispatch collides with the row the previous incarnation
    wrote rather than running the work — and charging for it — twice."""
    runner = FakeRunner(default=make.runner_result())
    run(handler(runner)(context(RunnerStage(id="code"), attempt=2)))
    assert runner.dispatched[0].idempotency_key == "run.test:code:2"


def test_the_plan_is_interpolated_from_an_earlier_stage() -> None:
    """A template rather than a field on ``RunnerStage``, because what a plan
    *is* is S12's question."""
    runner = FakeRunner(default=make.runner_result())
    step = handler(runner, plan="${plan.json.text}")
    run(step(context(RunnerStage(id="code"), plan={"text": "rewrite the parser"})))
    assert runner.dispatched[0].plan == "rewrite the parser"


def test_an_unresolvable_plan_is_permanent() -> None:
    """Running the agent with a half-expanded plan would spend a coding budget
    on a prompt containing ``${plan.json.text}``."""
    runner = FakeRunner(default=make.runner_result())
    step = handler(runner, plan="${plan.json.text}")
    with pytest.raises(StepFailure) as caught:
        run(step(context(RunnerStage(id="code"))))
    assert caught.value.kind == "plan-unresolved"
    assert caught.value.retryable is False


def test_a_stage_budget_overrides_the_dispatch_default() -> None:
    runner = FakeRunner(default=make.runner_result())
    stage = RunnerStage(id="code", budget=Budget(max_usd=Decimal("2")))
    run(handler(runner)(context(stage)))
    assert runner.dispatched[0].budget.max_usd == Decimal("2")


@pytest.mark.parametrize(
    ("outcome", "retryable"),
    [
        (RunnerOutcome.TESTS_FAILED, True),
        (RunnerOutcome.TIMED_OUT, True),
        (RunnerOutcome.STARTUP_FAILED, True),
        (RunnerOutcome.NETWORK_DENIED, True),
        (RunnerOutcome.EMPTY_DIFF, False),
        (RunnerOutcome.BLOCKED, False),
        (RunnerOutcome.BUDGET_EXCEEDED, False),
        (RunnerOutcome.OOM_KILLED, False),
        (RunnerOutcome.DISK_FULL, False),
        (RunnerOutcome.NON_ZERO_EXIT, False),
        (RunnerOutcome.CANCELLED, False),
    ],
)
def test_which_failures_are_worth_a_second_attempt(outcome: RunnerOutcome, retryable: bool) -> None:
    """A handler that mapped every outcome to "failed" would make the taxonomy
    decorative, which is precisely what v1 did."""
    runner = FakeRunner(default=make.runner_result(outcome=outcome))
    with pytest.raises(StepFailure) as caught:
        run(handler(runner)(context(RunnerStage(id="code"))))

    assert caught.value.kind == f"runner-{outcome.value}"
    assert caught.value.retryable is retryable


def test_a_dispatch_failure_carries_the_adapters_own_verdict() -> None:
    """The distinction the port insists on: the daemon being unreachable is an
    exception, a run that went badly is a result with an outcome."""
    runner = FakeRunner(default=make.runner_result())
    runner.fail_with(TransientError("daemon-unreachable", "connection refused"))
    with pytest.raises(StepFailure) as caught:
        run(handler(runner)(context(RunnerStage(id="code"))))
    assert (caught.value.kind, caught.value.retryable) == ("daemon-unreachable", True)


def test_a_refused_request_is_not_retried() -> None:
    runner = FakeRunner(default=make.runner_result())
    runner.fail_with(PermanentError("isolation-tier-mismatch", "asks for container"))
    with pytest.raises(StepFailure) as caught:
        run(handler(runner)(context(RunnerStage(id="code"))))
    assert caught.value.retryable is False


def test_the_stages_dispatched_are_recorded() -> None:
    runner = FakeRunner(default=make.runner_result())
    step = handler(runner)
    run(step(context(RunnerStage(id="code"))))
    assert step.calls == ["code"]


def test_a_result_with_no_diff_reports_zeroes_not_nulls() -> None:
    """One output shape whatever happened, for the reason ``ScriptHandler``
    gives: a condition reading ``$code.json.files_changed`` should mean the same
    thing on every run."""
    result = make.runner_result().model_copy(
        update={"diff": None, "outcome": RunnerOutcome.BLOCKED}
    )
    runner = FakeRunner(default=result)
    with pytest.raises(StepFailure):
        run(handler(runner)(context(RunnerStage(id="code"))))


def test_diff_numbers_reach_the_output() -> None:
    result = make.runner_result().model_copy(
        update={"diff": DiffStat(files_changed=3, insertions=40, deletions=12)}
    )
    runner = FakeRunner(default=result)
    outcome = run(handler(runner)(context(RunnerStage(id="code"))))
    assert outcome.output is not None
    assert outcome.output["deletions"] == 12  # type: ignore[index,call-overload]
