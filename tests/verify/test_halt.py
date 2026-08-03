"""The halt states, and the one rule that is not configurable.

v1's failure was not that it halted. It was that twelve call sites each invented
a resumption locally, so the verb set grew to nine overlapping words and nobody
could say what a halted run admitted. These tests hold the replacement: one
table, one derivation, and an invariant asserted over the whole enum rather than
per call site — so a state added next year fails the suite until somebody
decides what it admits.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clawdence.domain import (
    ContractKind,
    HaltState,
    ResumeVerb,
    RunnerOutcome,
    Shortfall,
    VerificationContract,
    VerificationResult,
)
from clawdence.verify import RESUMPTIONS, Decision, Halt, Proceed, Retry, admits, decide
from clawdence.verify.evidence import Stale, Staleness
from clawdence.verify.halt import stale

AT = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
TREE = "a" * 40
MOVED = "b" * 40


def contract(max_attempts: int = 3) -> VerificationContract:
    return VerificationContract(kind=ContractKind.OUTSIDE_IN_TDD, max_attempts=max_attempts)


def result(*, passed: bool = False, attempt: int = 1, max_attempts: int = 3) -> VerificationResult:
    return VerificationResult(
        contract=contract(max_attempts),
        passed=passed,
        tree_hash=TREE,
        attempt=attempt,
        shortfalls=() if passed else (Shortfall.TESTS_FAILED,),
        checked_at=AT,
        detail=None if passed else "Tests failed.",
    )


class TestTheInvariant:
    """No exhausted-retry path ever force-proceeds or force-approves."""

    def test_no_state_admits_approval(self) -> None:
        """Iterated over the enum, not the table.

        Deliberately: a state added later with no entry raises ``KeyError`` here
        and a state added later that admits ``APPROVE`` fails the assertion. The
        rule is meant to outlive the person who wrote it.
        """
        for state in HaltState:
            assert ResumeVerb.APPROVE not in RESUMPTIONS[state]
            assert not admits(state, ResumeVerb.APPROVE)

    def test_every_state_admits_something(self) -> None:
        """A halt nobody can resume is a hang with better paperwork."""
        for state in HaltState:
            assert RESUMPTIONS[state]

    def test_every_state_can_be_restarted(self) -> None:
        """There is no halt that starting over cannot in principle address."""
        for state in HaltState:
            assert admits(state, ResumeVerb.RESTART)

    def test_the_contract_cannot_widen_the_verb_set(self) -> None:
        """The field that used to let it is gone.

        ``allowed_resume_verbs`` defaulted to all four verbs including
        ``approve``, which made the rule above configurable — and a
        one-value ``on_exhausted`` guarding the same thing one field up would
        have been decoration.
        """
        assert "allowed_resume_verbs" not in VerificationContract.model_fields


class TestTheDecision:
    def test_a_met_contract_proceeds(self) -> None:
        decision = decide(result(passed=True), run_id="run-1", attempts_made=1, now=AT)

        assert isinstance(decision, Proceed)
        assert decision.result.tree_hash == TREE

    def test_an_unmet_contract_with_attempts_left_retries(self) -> None:
        decision = decide(result(), run_id="run-1", attempts_made=1, now=AT)

        assert isinstance(decision, Retry)
        assert decision.attempt == 2

    def test_the_retry_carries_what_fell_short(self) -> None:
        """The difference between a retry worth its money and v1's, which
        re-ran the same instructions and hoped."""
        decision = decide(result(), run_id="run-1", attempts_made=1, now=AT)

        assert isinstance(decision, Retry)
        assert decision.shortfalls == (Shortfall.TESTS_FAILED,)

    def test_exhausting_the_attempts_halts_rather_than_proceeding(self) -> None:
        """The step's own verification, and v1's twelve call sites in one line."""
        decision = decide(result(attempt=3), run_id="run-1", attempts_made=3, now=AT)

        assert isinstance(decision, Halt)
        assert decision.record.state is HaltState.RETRIES_EXHAUSTED

    def test_a_halted_run_says_which_state_and_which_resumptions(self) -> None:
        """What a person is handed, and what S17 does not have to re-derive."""
        decision = decide(
            result(attempt=3),
            run_id="run-1",
            attempts_made=3,
            work_item_id="wi-9",
            stage_id="code",
            now=AT,
        )

        assert isinstance(decision, Halt)
        record = decision.record
        assert record.state is HaltState.RETRIES_EXHAUSTED
        assert record.admits == (ResumeVerb.RETRY, ResumeVerb.RESTART, ResumeVerb.SKIP)
        assert ResumeVerb.APPROVE not in record.admits
        assert record.attempts == 3
        assert record.max_attempts == 3
        assert record.contract is ContractKind.OUTSIDE_IN_TDD
        assert record.tree_hash == TREE
        assert record.run_id == "run-1"
        assert record.work_item_id == "wi-9"
        assert record.stage_id == "code"
        assert "outside-in-tdd" in record.summary

    def test_the_failing_assertions_reach_the_record(self) -> None:
        """A halt a person cannot act on without going and finding the logs is
        a halt that costs them the thing the log was supposed to save."""
        decision = decide(result(attempt=3), run_id="run-1", attempts_made=3, now=AT)

        assert isinstance(decision, Halt)
        assert decision.record.last_result is not None
        assert decision.record.last_result.shortfalls == (Shortfall.TESTS_FAILED,)


class TestWhatOutranksTheCount:
    """The reasons another attempt is pointless, checked before the count is.

    This ordering is the specific v1 behaviour being prevented: a run blocked on
    a missing fixture, retried three times, is a budget spent to learn the
    fixture is still missing.
    """

    def test_a_blocked_attempt_halts_on_the_first_one(self) -> None:
        decision = decide(
            result(), run_id="run-1", attempts_made=1, outcome=RunnerOutcome.BLOCKED, now=AT
        )

        assert isinstance(decision, Halt)
        assert decision.record.state is HaltState.BLOCKED
        assert decision.record.attempts == 1

    @pytest.mark.parametrize(
        "outcome",
        [
            RunnerOutcome.BLOCKED,
            RunnerOutcome.EMPTY_DIFF,
            RunnerOutcome.OOM_KILLED,
            RunnerOutcome.DISK_FULL,
            RunnerOutcome.PROVIDER_ERROR,
            RunnerOutcome.NO_MODEL_RESPONSE,
        ],
    )
    def test_every_blocking_outcome_halts(self, outcome: RunnerOutcome) -> None:
        decision = decide(result(), run_id="run-1", attempts_made=1, outcome=outcome, now=AT)

        assert isinstance(decision, Halt)
        assert decision.record.state is HaltState.BLOCKED

    def test_failing_tests_are_still_worth_another_attempt(self) -> None:
        """The loop the whole system is built around."""
        decision = decide(
            result(), run_id="run-1", attempts_made=1, outcome=RunnerOutcome.TESTS_FAILED, now=AT
        )

        assert isinstance(decision, Retry)

    def test_an_exhausted_budget_halts_and_refuses_a_retry(self) -> None:
        """The same budget spent again reaches the same cap."""
        decision = decide(result(), run_id="run-1", attempts_made=1, budget_exceeded=True, now=AT)

        assert isinstance(decision, Halt)
        assert decision.record.state is HaltState.BUDGET_EXCEEDED
        assert ResumeVerb.RETRY not in decision.record.admits
        assert ResumeVerb.RESTART in decision.record.admits

    def test_an_unevaluable_contract_halts_separately_from_a_failing_one(self) -> None:
        """Not a failing test. A person sent to debug their suite over a
        misconfigured reporter is a person sent to the wrong place."""
        decision = decide(
            result(),
            run_id="run-1",
            attempts_made=1,
            verification_error="report is not well-formed XML",
            now=AT,
        )

        assert isinstance(decision, Halt)
        assert decision.record.state is HaltState.VERIFICATION_ERROR
        assert "well-formed" in decision.record.summary
        assert "cannot tell" in decision.record.summary

    def test_a_passing_result_beats_a_spent_budget(self) -> None:
        """The money is already gone; discarding the evidence too would be
        paying for it and throwing it away."""
        decision = decide(
            result(passed=True), run_id="run-1", attempts_made=3, budget_exceeded=True, now=AT
        )

        assert isinstance(decision, Proceed)


class TestTheStaleHalt:
    """The one that happens at the merge, not in the loop."""

    def test_a_moved_tree_halts_as_stale_evidence(self) -> None:
        halted = stale(
            Stale(reason=Staleness.TREE_MOVED, wanted=MOVED, evidence_for=TREE),
            contract(),
            run_id="run-1",
            tree_hash=MOVED,
            last_result=result(passed=True),
            now=AT,
        )

        assert halted.record.state is HaltState.EVIDENCE_STALE
        assert halted.record.tree_hash == MOVED

    def test_it_does_not_tell_a_person_their_tests_broke(self) -> None:
        """Nothing failed. Saying "verification failed" sends them looking in
        the wrong place, which is the whole reason this is a separate state."""
        halted = stale(
            Stale(reason=Staleness.TREE_MOVED, wanted=MOVED, evidence_for=TREE),
            contract(),
            run_id="run-1",
            tree_hash=MOVED,
            now=AT,
        )

        assert "rebased or its base advanced" in halted.record.summary
        assert "re-verification" in halted.record.summary

    def test_it_admits_re_verification_but_not_abandonment(self) -> None:
        """The work is finished and correct; only a test run is missing.
        Abandoning it would throw away completed work over a bookkeeping gap."""
        halted = stale(
            Stale(reason=Staleness.TREE_MOVED, wanted=MOVED, evidence_for=TREE),
            contract(),
            run_id="run-1",
            tree_hash=MOVED,
            now=AT,
        )

        assert ResumeVerb.RETRY in halted.record.admits
        assert ResumeVerb.SKIP not in halted.record.admits
        assert ResumeVerb.APPROVE not in halted.record.admits

    def test_a_never_verified_branch_reports_its_own_reason(self) -> None:
        halted = stale(
            Stale(reason=Staleness.NEVER_VERIFIED, wanted=MOVED),
            contract(),
            run_id="run-1",
            tree_hash=MOVED,
            now=AT,
        )

        assert "no verification evidence" in halted.record.summary


def test_the_record_and_the_table_cannot_disagree() -> None:
    """``HaltRecord.admits`` is denormalised so a stored halt is self-describing
    months later. The duplication is safe because one function writes it."""
    halted: Decision
    for state in HaltState:
        if state is HaltState.EVIDENCE_STALE:
            halted = stale(
                Stale(reason=Staleness.TREE_MOVED, wanted=MOVED, evidence_for=TREE),
                contract(),
                run_id="run-1",
                tree_hash=MOVED,
                now=AT,
            )
        elif state is HaltState.RETRIES_EXHAUSTED:
            halted = decide(result(), run_id="run-1", attempts_made=3, now=AT)
        elif state is HaltState.BLOCKED:
            halted = decide(
                result(), run_id="run-1", attempts_made=1, outcome=RunnerOutcome.BLOCKED, now=AT
            )
        elif state is HaltState.BUDGET_EXCEEDED:
            halted = decide(result(), run_id="run-1", attempts_made=1, budget_exceeded=True, now=AT)
        else:
            halted = decide(
                result(), run_id="run-1", attempts_made=1, verification_error="bad report", now=AT
            )

        assert isinstance(halted, Halt)
        assert halted.record.state is state
        assert halted.record.admits == RESUMPTIONS[state]
