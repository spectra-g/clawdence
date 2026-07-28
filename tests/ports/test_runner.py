"""The fake runner against the contract, and every rule ``validate_result`` has."""

from __future__ import annotations

import pytest

from clawdence.domain import RunnerOutcome
from clawdence.ports import FakeRunner, PermanentError, RefusingRunner, validate_result
from tests.ports import factories as make
from tests.ports.contract import RunnerContract
from tests.ports.factories import run


class TestFakeRunner(RunnerContract):
    @pytest.fixture
    def runner(self) -> FakeRunner:
        return FakeRunner(default=make.runner_result())


def test_the_default_runner_refuses_and_names_the_step() -> None:
    """A stub returning ``SUCCEEDED`` would make a workflow look like it did
    work, which is the most expensive way to be wrong about an orchestrator."""
    with pytest.raises(PermanentError) as caught:
        run(RefusingRunner().dispatch(make.runner_request("code")))
    assert caught.value.kind == "no-runner"
    assert "S6" in caught.value.message
    assert caught.value.retryable is False


def test_a_refusing_runner_cancels_nothing() -> None:
    assert run(RefusingRunner().cancel(make.runner_request("code"))) is False


def test_canned_results_are_per_stage() -> None:
    runner = FakeRunner()
    runner.returns("code", make.runner_result("code"))
    runner.returns("docs", make.runner_result("docs", outcome=RunnerOutcome.EMPTY_DIFF))

    assert run(runner.dispatch(make.runner_request("code"))).outcome is RunnerOutcome.SUCCEEDED
    assert run(runner.dispatch(make.runner_request("docs"))).outcome is RunnerOutcome.EMPTY_DIFF


def test_a_stage_with_no_canned_result_is_an_error_not_a_success() -> None:
    """A fake that invented a success for an unconfigured stage would let a
    test assert on work the test never described."""
    with pytest.raises(PermanentError) as caught:
        run(FakeRunner().dispatch(make.runner_request("code")))
    assert caught.value.kind == "no-canned-result"


def test_dispatched_requests_are_recorded() -> None:
    runner = FakeRunner(default=make.runner_result())
    run(runner.dispatch(make.runner_request("code", attempt=1)))
    run(runner.dispatch(make.runner_request("code", attempt=2)))
    assert [request.idempotency_key for request in runner.dispatched] == [
        "run.test:code:1",
        "run.test:code:2",
    ]


def test_a_redispatch_is_not_recorded_twice() -> None:
    runner = FakeRunner(default=make.runner_result())
    request = make.runner_request("code")
    run(runner.dispatch(request))
    run(runner.dispatch(request))
    assert len(runner.dispatched) == 1


def test_cancelling_an_undispatched_request_succeeds() -> None:
    runner = FakeRunner(default=make.runner_result())
    request = make.runner_request("code")
    assert run(runner.cancel(request)) is True
    assert runner.cancelled == ("run.test:code:1",)


def test_a_dispatch_failure_is_not_a_failed_run() -> None:
    """The distinction the port insists on: the daemon being unreachable is an
    exception, a run that went badly is a ``RunnerResult`` with an outcome. v1
    collapsed both into "runner failed" and could not tell a flaky test from an
    OOM kill, so it retried them identically."""
    runner = FakeRunner(default=make.runner_result())
    runner.fail_with(PermanentError("image-missing", "no such image"))
    with pytest.raises(PermanentError):
        run(runner.dispatch(make.runner_request("code")))


# --------------------------------------------------------------------------- #
# validate_result
# --------------------------------------------------------------------------- #


def test_a_valid_result_passes_through() -> None:
    request = make.runner_request("code")
    result = make.runner_result("code")
    assert validate_result(request, result) is result


def test_a_result_for_another_run_is_rejected() -> None:
    request = make.runner_request("code", run_id="run.a")
    result = make.runner_result("code", run_id="run.b")
    with pytest.raises(PermanentError) as caught:
        validate_result(request, result)
    assert caught.value.kind == "runner-result-mismatch"


def test_a_result_for_another_stage_is_rejected() -> None:
    with pytest.raises(PermanentError):
        validate_result(make.runner_request("code"), make.runner_result("docs"))


def test_a_tree_hash_on_an_outcome_that_committed_nothing_is_rejected() -> None:
    """A hash attached to a timeout is a hash something downstream will try to
    verify, and then to merge."""
    result = make.runner_result("code", outcome=RunnerOutcome.TIMED_OUT, tree_hash=make.commit(9))
    with pytest.raises(PermanentError) as caught:
        validate_result(make.runner_request("code"), result)
    assert "tree hash" in caught.value.message


def test_success_without_a_tree_hash_is_rejected() -> None:
    """Evidence binds to a tree; a success with no tree leaves it unbindable."""
    result = make.runner_result("code").model_copy(update={"tree_hash": None})
    with pytest.raises(PermanentError):
        validate_result(make.runner_request("code"), result)


def test_time_must_run_forwards() -> None:
    """A negative duration lands in cost attribution and in stall detection."""
    result = make.runner_result("code", started=10.0, finished=0.0)
    with pytest.raises(PermanentError) as caught:
        validate_result(make.runner_request("code"), result)
    assert "precedes" in caught.value.message


def test_success_with_an_empty_diff_is_rejected_when_the_contract_requires_one() -> None:
    """v1's ``_EmptyPRError``, caught one step earlier: a pull request with no
    content is what this becomes if it gets through."""
    result = make.runner_result("code", files_changed=0)
    with pytest.raises(PermanentError) as caught:
        validate_result(make.runner_request("code"), result)
    assert "empty-diff" in caught.value.message


def test_an_empty_diff_is_allowed_when_the_contract_permits_it() -> None:
    request = make.runner_request("code", require_non_empty_diff=False)
    validate_result(request, make.runner_result("code", files_changed=0))


def test_a_missing_diff_counts_as_empty() -> None:
    result = make.runner_result("code").model_copy(update={"diff": None})
    with pytest.raises(PermanentError):
        validate_result(make.runner_request("code"), result)


def test_a_failing_outcome_needs_no_diff() -> None:
    """Only success is held to the contract's diff requirement. A timeout that
    changed nothing is a timeout, not a second kind of error."""
    result = make.runner_result("code", outcome=RunnerOutcome.TIMED_OUT, files_changed=0)
    validate_result(make.runner_request("code"), result)


def test_tests_failed_may_carry_a_tree() -> None:
    """It committed work; the tests disagreed with it. That tree is exactly
    what the next attempt starts from."""
    result = make.runner_result("code", outcome=RunnerOutcome.TESTS_FAILED)
    assert validate_result(make.runner_request("code"), result).tree_hash is not None
