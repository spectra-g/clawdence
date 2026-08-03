"""One story, four contracts, and the loop that runs until it stops.

This is S13's own verification, written as the scenario rather than as unit
assertions. A single fixture story — "proration is off by a cent" — is put
through all four contracts with the *same* agent behaviour, and the four
contracts disagree about whether it is done. That disagreement is the deliverable:
in v1 there was one answer, TDD's, and 175 references to it inside the
orchestrator meant there was nowhere else to stand.

Then the three things that must hold whatever the contract says:

- a deliberately broken test is caught by the TDD contract;
- exhausting the retries halts rather than proceeding, and the halted run says
  which state it is in and which resumptions that state admits;
- a rebase invalidates evidence, and re-verification rebinds it to the tree that
  will actually land.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clawdence.domain import (
    ContractKind,
    DiffStat,
    HaltState,
    RepoProfile,
    ResumeVerb,
    RunnerOutcome,
    Shortfall,
    VerificationContract,
    VerificationResult,
)
from clawdence.domain import TestReporter as Reporter
from clawdence.verify import (
    Attempt,
    CommandResult,
    Halt,
    Proceed,
    Recheck,
    Retry,
    collect,
    decide,
    evaluate,
    into_attempt,
    is_fresh,
    parse,
    require_fresh,
    sequential,
    stale,
)
from clawdence.verify import run as recheck_run
from clawdence.verify.evidence import StaleEvidence, check
from tests.ports.factories import run
from tests.verify import reports

AT = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
CODED = "1" * 40
REBASED = "2" * 40

PROFILE = RepoProfile(
    id="acme-billing",
    name="billing",
    remote_url="https://example.invalid/acme/billing.git",
    test_command=("pytest", "--json-report"),
    test_reporter=Reporter.PYTEST_JSON_REPORT,
)

GREEN = parse(Reporter.PYTEST_JSON_REPORT, reports.PYTEST_PASSING)
RED = parse(Reporter.PYTEST_JSON_REPORT, reports.PYTEST_FAILING)


def contract(kind: ContractKind, max_attempts: int = 3) -> VerificationContract:
    return VerificationContract(kind=kind, max_attempts=max_attempts)


#: What the agent did on the story, as the runner observed it: it changed two
#: files, ran the suite before and after, and the suite is green. One set of
#: observations, fed to four contracts.
DID_TDD = Attempt(
    tree_hash=CODED,
    outcome=RunnerOutcome.SUCCEEDED,
    diff=DiffStat(files_changed=2, insertions=31, deletions=4),
    evidence=GREEN,
    red_evidence=RED,
    build_succeeded=True,
    full_suite=True,
)


class TestOneStoryUnderFourContracts:
    """The same work, judged four ways."""

    def test_an_agent_that_did_the_work_properly_satisfies_all_four(self) -> None:
        for kind in ContractKind:
            assert evaluate(contract(kind), DID_TDD, now=AT).passed, kind

    def test_an_agent_that_skipped_the_red_phase_satisfies_three(self) -> None:
        """The story that separates ``outside-in-tdd`` from ``test-after``.

        The code works. The tests pass. Nothing shows a test was ever written
        for the new behaviour — which a green suite cannot show, because a suite
        with no test for it is green too.
        """
        skipped = replace(DID_TDD, red_evidence=None)

        assert evaluate(contract(ContractKind.NONE), skipped, now=AT).passed
        assert evaluate(contract(ContractKind.BUILD_ONLY), skipped, now=AT).passed
        assert evaluate(contract(ContractKind.TEST_AFTER), skipped, now=AT).passed

        tdd = evaluate(contract(ContractKind.OUTSIDE_IN_TDD), skipped, now=AT)
        assert not tdd.passed
        assert tdd.shortfalls == (Shortfall.NO_RED_PHASE,)

    def test_an_agent_that_wrote_no_tests_satisfies_two(self) -> None:
        """A spike or a docs change. Legitimate under two contracts, and v1 had
        no way to say so — it sent this through a TDD gate with nothing to check.
        """
        untested = replace(DID_TDD, evidence=None, red_evidence=None)

        assert evaluate(contract(ContractKind.NONE), untested, now=AT).passed
        assert evaluate(contract(ContractKind.BUILD_ONLY), untested, now=AT).passed
        assert not evaluate(contract(ContractKind.TEST_AFTER), untested, now=AT).passed
        assert not evaluate(contract(ContractKind.OUTSIDE_IN_TDD), untested, now=AT).passed

    def test_an_agent_that_changed_nothing_satisfies_none_of_them(self) -> None:
        """v1's ``_EmptyPRError``: a run that changes nothing is a failed run,
        not a successful one, under every contract including ``none``."""
        idle = replace(DID_TDD, diff=DiffStat())

        for kind in ContractKind:
            result = evaluate(contract(kind), idle, now=AT)
            assert not result.passed, kind
            assert Shortfall.EMPTY_DIFF in result.shortfalls

    def test_the_deliberately_broken_test_is_caught(self) -> None:
        """The step's own words. A failing suite is caught by both test
        contracts, and ``outside-in-tdd`` does not get to be laxer for having
        checked something extra."""
        broken = replace(DID_TDD, evidence=RED)

        assert evaluate(contract(ContractKind.NONE), broken, now=AT).passed
        assert evaluate(contract(ContractKind.BUILD_ONLY), broken, now=AT).passed

        for kind in (ContractKind.TEST_AFTER, ContractKind.OUTSIDE_IN_TDD):
            result = evaluate(contract(kind), broken, now=AT)
            assert not result.passed, kind
            assert Shortfall.TESTS_FAILED in result.shortfalls

    def test_the_failing_assertion_reaches_the_result(self) -> None:
        """And nothing else does. This is what a retry's prompt is built from:
        the assertion, not four hundred lines of pytest internals."""
        broken = replace(DID_TDD, evidence=RED)

        result = evaluate(contract(ContractKind.TEST_AFTER), broken, now=AT)

        assert result.evidence is not None
        failure = result.evidence.failures[0]
        assert "12.49" in failure.message
        assert "12.50" in failure.message
        assert failure.file == "billing/proration.py"
        assert "_pytest" not in " ".join(failure.frames)


class TestTheLoopRunsOut:
    """Three attempts, all failing, and what the fourth thing is."""

    def drive(self, max_attempts: int = 3) -> list[object]:
        """Run the loop to its conclusion, collecting each decision.

        Written as the loop rather than as three separate calls because the
        thing under test is the sequence — that it ends, that it ends in a
        halt, and that it never reaches a fourth attempt.
        """
        broken = replace(DID_TDD, evidence=RED)
        decisions: list[object] = []
        attempts = 0

        while attempts < 10:  # a bound, so a bug is a failure rather than a hang
            attempts += 1
            result = evaluate(
                contract(ContractKind.OUTSIDE_IN_TDD, max_attempts),
                replace(broken, number=attempts),
                now=AT + timedelta(minutes=attempts),
            )
            decision = decide(
                result,
                run_id="run-fixture",
                attempts_made=attempts,
                work_item_id="wi-proration",
                stage_id="code",
                outcome=RunnerOutcome.TESTS_FAILED,
                now=AT + timedelta(minutes=attempts),
            )
            decisions.append(decision)
            if not isinstance(decision, Retry):
                break

        return decisions

    def test_it_retries_to_the_cap_and_then_halts(self) -> None:
        decisions = self.drive(max_attempts=3)

        assert len(decisions) == 3
        assert isinstance(decisions[0], Retry)
        assert isinstance(decisions[1], Retry)
        assert isinstance(decisions[2], Halt)

    def test_it_never_proceeds(self) -> None:
        """The invariant, at the level of the loop rather than the table: no
        exhausted-retry path force-proceeds."""
        assert not any(isinstance(decision, Proceed) for decision in self.drive())

    def test_the_halted_run_says_which_state_it_is_in(self) -> None:
        halted = self.drive()[-1]

        assert isinstance(halted, Halt)
        assert halted.record.state is HaltState.RETRIES_EXHAUSTED
        assert halted.record.attempts == 3
        assert halted.record.max_attempts == 3
        assert "not met in 3 attempts" in halted.record.summary

    def test_the_halted_run_says_which_resumptions_it_admits(self) -> None:
        """Self-describing, so S17 spends none of its budget re-deriving it and
        an operator reading a stored record months later does not need our
        table to interpret it."""
        halted = self.drive()[-1]

        assert isinstance(halted, Halt)
        assert halted.record.admits == (ResumeVerb.RETRY, ResumeVerb.RESTART, ResumeVerb.SKIP)
        assert ResumeVerb.APPROVE not in halted.record.admits

    def test_the_halted_run_carries_the_evidence_a_person_needs(self) -> None:
        halted = self.drive()[-1]

        assert isinstance(halted, Halt)
        last = halted.record.last_result
        assert last is not None
        assert last.evidence is not None
        assert "12.49" in last.evidence.failures[0].message
        assert last.detail is not None

    def test_a_single_attempt_contract_halts_immediately(self) -> None:
        decisions = self.drive(max_attempts=1)

        assert len(decisions) == 1
        assert isinstance(decisions[0], Halt)


class TestARebaseInvalidatesTheEvidence:
    """The failure S15b would otherwise ship: a merge whose tests ran elsewhere."""

    def passing_result(self) -> VerificationResult:
        result = evaluate(contract(ContractKind.OUTSIDE_IN_TDD), DID_TDD, now=AT)
        assert result.passed
        return result

    def test_evidence_justifies_a_merge_of_the_tree_it_ran_against(self) -> None:
        assert is_fresh(self.passing_result(), CODED)

    def test_the_same_evidence_does_not_justify_the_rebased_tree(self) -> None:
        """Story tests pass at commit X, a conflict forces a rebase onto an
        advanced base, and the merge would land commit Y — which nothing ever
        ran against, while every dashboard reads green."""
        result = self.passing_result()

        assert not is_fresh(result, REBASED)
        with pytest.raises(StaleEvidence, match="never ran against the tree that would land"):
            require_fresh(result, REBASED)

    def test_the_merge_gate_halts_with_a_state_a_person_can_act_on(self) -> None:
        result = self.passing_result()
        refusal = check(result, REBASED)
        assert refusal is not None

        halted = stale(
            refusal,
            result.contract,
            run_id="run-fixture",
            tree_hash=REBASED,
            last_result=result,
            work_item_id="wi-proration",
            now=AT,
        )

        assert halted.record.state is HaltState.EVIDENCE_STALE
        assert halted.record.admits == (ResumeVerb.RETRY, ResumeVerb.RESTART)
        assert "rebased or its base advanced" in halted.record.summary

    def test_re_verification_rebinds_the_evidence_without_re_running_the_agent(
        self, tmp_path: Path
    ) -> None:
        """The repair, end to end.

        The code is finished and correct; what is missing is a run of the tests
        against the tree that will land. So the tests are re-run — no model, no
        coding budget — and the fresh evidence binds to the rebased commit.
        """
        (tmp_path / ".report.json").write_bytes(reports.PYTEST_PASSING)
        plan = Recheck.plan(PROFILE, contract(ContractKind.OUTSIDE_IN_TDD), tmp_path)

        rechecked = run(recheck_run(plan, PROFILE, sequential([CommandResult(exit_code=0)])))
        folded = into_attempt(rechecked, DID_TDD, tree_hash=REBASED)
        result = evaluate(contract(ContractKind.OUTSIDE_IN_TDD), folded, now=AT)

        assert result.passed
        assert result.tree_hash == REBASED
        assert is_fresh(result, REBASED)
        # And the original evidence is still not good for the new tree — nothing
        # was mutated, a second result was produced.
        assert not is_fresh(self.passing_result(), REBASED)

    def test_re_verification_that_fails_does_not_produce_mergeable_evidence(
        self, tmp_path: Path
    ) -> None:
        """The rebase surfaced a real conflict in behaviour: the code passed
        against its old base and does not against the new one. This is the case
        the whole mechanism exists to catch."""
        (tmp_path / ".report.json").write_bytes(reports.PYTEST_FAILING)
        plan = Recheck.plan(PROFILE, contract(ContractKind.OUTSIDE_IN_TDD), tmp_path)

        rechecked = run(recheck_run(plan, PROFILE, sequential([CommandResult(exit_code=1)])))
        folded = into_attempt(rechecked, DID_TDD, tree_hash=REBASED)
        result = evaluate(contract(ContractKind.OUTSIDE_IN_TDD), folded, now=AT)

        assert not result.passed
        assert not is_fresh(result, REBASED)
        with pytest.raises(StaleEvidence):
            require_fresh(result, REBASED)


class TestTheReporterIsWhatTheProfileDeclares:
    """The other half of "structured output": it is found, not guessed."""

    def test_evidence_is_read_from_the_declared_reporter(self, tmp_path: Path) -> None:
        (tmp_path / ".report.json").write_bytes(reports.PYTEST_FAILING)

        evidence = collect(tmp_path, PROFILE.test_reporter)

        assert evidence is not None
        assert evidence.reporter is Reporter.PYTEST_JSON_REPORT
        assert evidence.failed == 1

    def test_a_repository_that_declares_a_reporter_it_does_not_emit_halts(
        self, tmp_path: Path
    ) -> None:
        """Not a failing test — a configuration error. Sending someone to debug
        their suite over a misconfigured reporter is sending them to the wrong
        place, so it gets its own state."""
        (tmp_path / ".report.json").write_bytes(b"<testsuite/>")
        plan = Recheck.plan(PROFILE, contract(ContractKind.TEST_AFTER), tmp_path)

        rechecked = run(recheck_run(plan, PROFILE, sequential([CommandResult(exit_code=0)])))
        folded = into_attempt(rechecked, DID_TDD, tree_hash=CODED)
        result = evaluate(contract(ContractKind.TEST_AFTER), folded, now=AT)

        decision = decide(
            result,
            run_id="run-fixture",
            attempts_made=1,
            verification_error=rechecked.error,
            now=AT,
        )

        assert isinstance(decision, Halt)
        assert decision.record.state is HaltState.VERIFICATION_ERROR
