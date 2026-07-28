"""The failure taxonomy: every value, and the order they are decided in.

Each of the eleven outcomes is produced here from a record of observations. Six
of them are also produced end to end by ``test_host``, driving a real subprocess;
the ones that are not are the ones whose signal belongs to a tier that does not
exist yet — a container is *told* it was OOM-killed, and nothing denies egress
until S7b. Splitting classification from observation is what makes those
testable now instead of when the tier arrives.
"""

from __future__ import annotations

import pytest

from clawdence.domain import RunnerOutcome

# Aliased: pytest tries to collect any module-level name starting with `Test`
# and then warns that it cannot, which fails a suite that treats warnings as
# errors.
from clawdence.domain import TestEvidence as Evidence
from clawdence.domain import TestReporter as Reporter
from clawdence.runners import Completion, classify
from clawdence.runners.verdict import RunnerVerdict, VerdictStatus


def passing(**overrides: object) -> Completion:
    """A completion that succeeded, so a test breaks exactly one thing."""
    fields: dict[str, object] = {
        "exit_code": 0,
        "files_changed": 3,
        "verdict": RunnerVerdict(status=VerdictStatus.PASSED),
    }
    fields.update(overrides)
    return Completion(**fields)  # type: ignore[arg-type]


def test_the_happy_path() -> None:
    assert classify(passing()) is RunnerOutcome.SUCCEEDED


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        ({"cancelled": True}, RunnerOutcome.CANCELLED),
        ({"startup_error": "no such binary"}, RunnerOutcome.STARTUP_FAILED),
        ({"budget_exceeded": True}, RunnerOutcome.BUDGET_EXCEEDED),
        ({"timed_out": True}, RunnerOutcome.TIMED_OUT),
        ({"egress_denied": True}, RunnerOutcome.NETWORK_DENIED),
        ({"disk_full": True}, RunnerOutcome.DISK_FULL),
        ({"oom_killed": True}, RunnerOutcome.OOM_KILLED),
        ({"exit_code": 1}, RunnerOutcome.NON_ZERO_EXIT),
        ({"files_changed": 0}, RunnerOutcome.EMPTY_DIFF),
    ],
)
def test_each_signal_has_its_own_name(observation: dict[str, object], expected: object) -> None:
    """v1 collapsed nearly all of these into "runner failed", so its retry
    policy could not tell a flaky test from an OOM kill."""
    assert classify(passing(**observation)) is expected


def test_a_sigkill_nobody_sent_reads_as_an_oom_kill() -> None:
    """Best effort, and only on the host tier: nothing else routinely SIGKILLs a
    process it is not supervising."""
    assert classify(passing(exit_code=-9)) is RunnerOutcome.OOM_KILLED
    assert classify(passing(exit_code=137)) is RunnerOutcome.OOM_KILLED


def test_a_sigkill_we_sent_is_not_an_oom_kill() -> None:
    """Without ``killed_by_us``, every timeout would be reported as an OOM."""
    assert classify(passing(exit_code=-9, timed_out=True, killed_by_us=True)) is (
        RunnerOutcome.TIMED_OUT
    )


def test_the_disk_is_read_from_stderr_when_nothing_else_can_say() -> None:
    """The container tier is told by the runtime; the host tier has one channel
    and this is it."""
    assert classify(passing(exit_code=1, stderr_tail="write: No space left on device")) is (
        RunnerOutcome.DISK_FULL
    )


def test_a_budget_kill_is_not_reported_as_a_non_zero_exit() -> None:
    """It also exits non-zero. Reporting that would send a retry after work that
    was stopped on purpose, which is the whole reason the ranking exists."""
    stopped = passing(exit_code=-9, budget_exceeded=True, killed_by_us=True, files_changed=0)
    assert classify(stopped) is RunnerOutcome.BUDGET_EXCEEDED


def test_cancelling_outranks_everything() -> None:
    cancelled = passing(cancelled=True, timed_out=True, exit_code=1, startup_error="also this")
    assert classify(cancelled) is RunnerOutcome.CANCELLED


# --------------------------------------------------------------------------- #
# What the agent said
# --------------------------------------------------------------------------- #


def test_a_failed_verdict_is_failing_tests() -> None:
    assert classify(passing(verdict=RunnerVerdict(status=VerdictStatus.FAILED))) is (
        RunnerOutcome.TESTS_FAILED
    )


def test_a_success_claim_contradicted_by_its_own_numbers_is_not_a_claim() -> None:
    verdict = RunnerVerdict(
        status=VerdictStatus.PASSED,
        tests=Evidence(reporter=Reporter.JUNIT_XML, total=10, passed=8, failed=2),
    )
    assert classify(passing(verdict=verdict)) is RunnerOutcome.TESTS_FAILED


def test_no_verdict_fails_a_contract_that_needs_evidence() -> None:
    """ "The tests passed" and "nothing shows the tests passed" are the same
    thing to a contract, and both are worth another attempt."""
    assert classify(passing(verdict=None, requires_evidence=True)) is RunnerOutcome.TESTS_FAILED


def test_no_verdict_is_fine_for_a_contract_that_needs_none() -> None:
    assert classify(passing(verdict=None, requires_evidence=False)) is RunnerOutcome.SUCCEEDED


def test_a_verdict_with_no_test_counts_fails_a_contract_that_needs_them() -> None:
    verdict = RunnerVerdict(status=VerdictStatus.PASSED, summary="all good, trust me")
    assert classify(passing(verdict=verdict, requires_evidence=True)) is RunnerOutcome.TESTS_FAILED


def test_blocked_is_not_retried_as_a_failing_test() -> None:
    """An agent stopped by a missing dependency, retried three times, is v1's
    budget being spent to learn that the dependency is still missing."""
    blocked = passing(verdict=RunnerVerdict(status=VerdictStatus.BLOCKED, summary="no postgres"))
    assert classify(blocked) is RunnerOutcome.BLOCKED


def test_blocked_outranks_an_empty_diff() -> None:
    """An agent that could not start has changed nothing, and reporting that as
    "it decided there was nothing to do" loses the reason."""
    blocked = passing(verdict=RunnerVerdict(status=VerdictStatus.BLOCKED), files_changed=0)
    assert classify(blocked) is RunnerOutcome.BLOCKED


def test_a_failing_test_run_that_changed_nothing_is_still_a_failing_test_run() -> None:
    """Ordered this way because it is the more informative of the two."""
    verdict = RunnerVerdict(status=VerdictStatus.FAILED)
    assert classify(passing(verdict=verdict, files_changed=0)) is RunnerOutcome.TESTS_FAILED


def test_a_crash_outranks_anything_the_agent_claimed() -> None:
    """A verdict written before the process died says what it hoped, not what
    happened."""
    assert classify(passing(exit_code=2)) is RunnerOutcome.NON_ZERO_EXIT
