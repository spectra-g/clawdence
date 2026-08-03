"""The four contracts, and the plug that makes them four rather than one.

The claim this file exists to hold up is "TDD is optional" — not as a sentence
in a README but as behaviour: the *same* attempt is a pass under one contract
and a failure under another, and no contract can see the others' rules.

The other claim is that ``outside-in-tdd`` checks something. A contract that
demanded TDD and verified "the tests pass" would be ``test-after`` with a
sterner docstring, because a suite with no test for the new behaviour is green
too. The red-phase cases below are the three ways that gap gets faked.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clawdence.domain import (
    ContractKind,
    DiffStat,
    RunnerOutcome,
    Shortfall,
    VerificationContract,
)
from clawdence.domain import TestEvidence as Evidence
from clawdence.domain import TestReporter as Reporter
from clawdence.verify import Attempt, Registry, evaluate
from clawdence.verify.contracts import explain

AT = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
TREE = "a" * 40


def green(total: int = 10, passed: int = 10) -> Evidence:
    return Evidence(reporter=Reporter.PYTEST_JSON_REPORT, total=total, passed=passed)


def red(total: int = 10, failed: int = 1) -> Evidence:
    return Evidence(
        reporter=Reporter.PYTEST_JSON_REPORT,
        total=total,
        passed=total - failed,
        failed=failed,
    )


def contract(kind: ContractKind, **overrides: object) -> VerificationContract:
    fields: dict[str, object] = {"kind": kind}
    fields.update(overrides)
    return VerificationContract(**fields)


def attempt(**overrides: object) -> Attempt:
    """An attempt that satisfies every contract, so a test breaks exactly one thing."""
    fields: dict[str, object] = {
        "tree_hash": TREE,
        "outcome": RunnerOutcome.SUCCEEDED,
        "diff": DiffStat(files_changed=3, insertions=40, deletions=2),
        "evidence": green(),
        "red_evidence": red(),
        "build_succeeded": True,
        "full_suite": True,
    }
    fields.update(overrides)
    return Attempt(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", list(ContractKind))
def test_every_contract_passes_a_complete_attempt(kind: ContractKind) -> None:
    result = evaluate(contract(kind), attempt(), now=AT)

    assert result.passed
    assert result.shortfalls == ()
    assert result.detail is None
    assert result.tree_hash == TREE


class TestTheContractsDisagree:
    """One attempt, four verdicts. This is the whole point of the step."""

    def test_a_run_with_no_tests_at_all(self) -> None:
        """A spike, a docs change, a repo with no suite.

        v1 put this through a TDD gate that had nothing to check, which is why
        ``none`` exists as a first-class contract rather than an omission.
        """
        bare = attempt(evidence=None, red_evidence=None)

        assert evaluate(contract(ContractKind.NONE), bare, now=AT).passed
        assert evaluate(contract(ContractKind.BUILD_ONLY), bare, now=AT).passed
        assert not evaluate(contract(ContractKind.TEST_AFTER), bare, now=AT).passed
        assert not evaluate(contract(ContractKind.OUTSIDE_IN_TDD), bare, now=AT).passed

    def test_tests_written_after_the_code(self) -> None:
        """Green suite, no red phase. Fine for three contracts, not for TDD."""
        after = attempt(red_evidence=None)

        assert evaluate(contract(ContractKind.NONE), after, now=AT).passed
        assert evaluate(contract(ContractKind.BUILD_ONLY), after, now=AT).passed
        assert evaluate(contract(ContractKind.TEST_AFTER), after, now=AT).passed

        tdd = evaluate(contract(ContractKind.OUTSIDE_IN_TDD), after, now=AT)
        assert not tdd.passed
        assert tdd.shortfalls == (Shortfall.NO_RED_PHASE,)

    def test_a_build_that_did_not_run(self) -> None:
        """Only ``build-only`` is looking at it.

        ``None`` rather than ``False``: a contract whose single requirement is a
        build, satisfied by an attempt that never reported running one, is a
        contract satisfied by silence.
        """
        unbuilt = attempt(build_succeeded=None)

        assert evaluate(contract(ContractKind.NONE), unbuilt, now=AT).passed
        assert evaluate(contract(ContractKind.TEST_AFTER), unbuilt, now=AT).passed
        assert evaluate(contract(ContractKind.BUILD_ONLY), unbuilt, now=AT).shortfalls == (
            Shortfall.BUILD_FAILED,
        )


class TestTheRedPhase:
    """The three ways outside-in TDD gets faked, and the arithmetic that catches them."""

    def test_a_deliberately_broken_test_is_caught(self) -> None:
        """The step's own verification: the TDD contract still catches one.

        A test that fails after the change is a failure under both test
        contracts — TDD does not get to be *laxer* than test-after because it
        checked something extra.
        """
        broken = attempt(evidence=red(failed=1))

        for kind in (ContractKind.TEST_AFTER, ContractKind.OUTSIDE_IN_TDD):
            result = evaluate(contract(kind), broken, now=AT)
            assert not result.passed
            assert Shortfall.TESTS_FAILED in result.shortfalls

    def test_a_red_phase_in_which_nothing_failed(self) -> None:
        """A test written after the code, or one that asserts nothing.

        Reported separately from a missing red phase because it is the more
        interesting thing to show a human: a test exists, ran, and proved
        nothing.
        """
        vacuous = attempt(red_evidence=green())

        result = evaluate(contract(ContractKind.OUTSIDE_IN_TDD), vacuous, now=AT)

        assert result.shortfalls == (Shortfall.VACUOUS_RED,)

    def test_deleting_the_failing_test_is_caught(self) -> None:
        """The cheapest way to turn red into green.

        Both runs look correct in isolation — a red run with a failure, a green
        run with none — and only the comparison shows a test went missing
        between them.
        """
        deleted = attempt(red_evidence=red(total=10), evidence=green(total=9, passed=9))

        result = evaluate(contract(ContractKind.OUTSIDE_IN_TDD), deleted, now=AT)

        assert result.shortfalls == (Shortfall.TESTS_REMOVED,)

    def test_adding_tests_is_not_removing_them(self) -> None:
        """The obvious false positive: outside-in TDD *adds* a test, so the
        green run legitimately has more than the red one."""
        grown = attempt(red_evidence=red(total=10), evidence=green(total=11, passed=11))

        assert evaluate(contract(ContractKind.OUTSIDE_IN_TDD), grown, now=AT).passed

    def test_the_red_phase_is_reported_alongside_a_failing_green_one(self) -> None:
        """Two problems at once rather than one per attempt.

        A retry that fixes the tests only to be told about the missing red
        phase has spent an attempt to learn something we already knew.
        """
        both = attempt(evidence=red(failed=2), red_evidence=None)

        result = evaluate(contract(ContractKind.OUTSIDE_IN_TDD), both, now=AT)

        assert result.shortfalls == (Shortfall.TESTS_FAILED, Shortfall.NO_RED_PHASE)


class TestTheUniversalChecks:
    """What is short of every contract, ``none`` included."""

    def test_nothing_committed(self) -> None:
        for kind in list(ContractKind):
            result = evaluate(contract(kind), attempt(tree_hash=None), now=AT)
            assert Shortfall.NO_TREE in result.shortfalls

    def test_a_runner_that_did_not_succeed(self) -> None:
        stopped = attempt(outcome=RunnerOutcome.TIMED_OUT)

        assert evaluate(contract(ContractKind.NONE), stopped, now=AT).shortfalls == (
            Shortfall.RUNNER_FAILED,
        )

    def test_an_empty_diff(self) -> None:
        """v1's ``_EmptyPRError``: a run that changes nothing is a failed run."""
        nothing = attempt(diff=DiffStat())

        assert (
            Shortfall.EMPTY_DIFF
            in evaluate(contract(ContractKind.NONE), nothing, now=AT).shortfalls
        )

    def test_an_empty_diff_the_contract_permits(self) -> None:
        nothing = attempt(diff=DiffStat())

        assert evaluate(
            contract(ContractKind.NONE, require_non_empty_diff=False), nothing, now=AT
        ).passed

    def test_a_failed_pre_verify_hook(self) -> None:
        """Whatever ran after an unprepared workspace proves nothing either way."""
        unprepared = attempt(pre_verify_ok=False)
        hooked = contract(ContractKind.TEST_AFTER, pre_verify=("make", "fixtures"))

        assert Shortfall.PRE_VERIFY_FAILED in evaluate(hooked, unprepared, now=AT).shortfalls

    def test_a_hook_that_was_not_declared_is_not_missed(self) -> None:
        """``pre_verify_ok=False`` on a contract with no hook is not a shortfall
        — there was no hook to fail."""
        assert evaluate(
            contract(ContractKind.TEST_AFTER), attempt(pre_verify_ok=False), now=AT
        ).passed

    def test_evidence_that_could_not_be_produced(self) -> None:
        """Not a failing test. "The tests failed" and "we could not tell whether
        the tests ran" are different answers with different repairs."""
        unreadable = attempt(evidence=None, verification_error="report is not well-formed XML")

        result = evaluate(contract(ContractKind.NONE), unreadable, now=AT)

        assert result.shortfalls == (Shortfall.NO_TEST_EVIDENCE,)


class TestTheTestRequirement:
    def test_a_suite_that_ran_no_tests_is_evidence_of_nothing(self) -> None:
        """Green by arithmetic. Zero of zero tests passed."""
        empty = attempt(evidence=Evidence(reporter=Reporter.JUNIT_XML, total=0))

        assert evaluate(contract(ContractKind.TEST_AFTER), empty, now=AT).shortfalls == (
            Shortfall.NO_TEST_EVIDENCE,
        )

    def test_a_partial_suite_when_the_full_one_was_required(self) -> None:
        partial = attempt(full_suite=False)
        strict = contract(ContractKind.TEST_AFTER, require_full_test_suite=True)

        assert evaluate(strict, partial, now=AT).shortfalls == (Shortfall.PARTIAL_SUITE,)

    def test_a_partial_suite_the_contract_allows(self) -> None:
        assert evaluate(contract(ContractKind.TEST_AFTER), attempt(full_suite=False), now=AT).passed


class TestTheResultRecord:
    def test_evidence_records_the_tree_it_was_produced_against(self) -> None:
        result = evaluate(contract(ContractKind.TEST_AFTER), attempt(), now=AT)

        assert result.tree_hash == TREE
        assert result.checked_at == AT
        assert result.attempt == 1

    def test_a_failure_with_no_tree_still_produces_a_readable_record(self) -> None:
        """The placeholder is git's null object id, so anything that compares it
        against a real commit gets a mismatch rather than a match."""
        result = evaluate(contract(ContractKind.NONE), attempt(tree_hash=None), now=AT)

        assert result.tree_hash == "0" * 40
        assert not result.passed

    def test_both_test_runs_are_carried_on_the_record(self) -> None:
        """So a person reading a halt sees the comparison the contract made,
        not just its conclusion."""
        result = evaluate(contract(ContractKind.OUTSIDE_IN_TDD), attempt(), now=AT)

        assert result.evidence is not None
        assert result.red_evidence is not None

    def test_shortfalls_are_deduplicated_and_ordered(self) -> None:
        messy = attempt(tree_hash=None, diff=DiffStat(), outcome=RunnerOutcome.TESTS_FAILED)

        result = evaluate(contract(ContractKind.NONE), messy, now=AT)

        assert result.shortfalls == (
            Shortfall.RUNNER_FAILED,
            Shortfall.NO_TREE,
            Shortfall.EMPTY_DIFF,
        )
        assert len(set(result.shortfalls)) == len(result.shortfalls)

    def test_the_detail_explains_every_shortfall(self) -> None:
        result = evaluate(contract(ContractKind.OUTSIDE_IN_TDD), attempt(red_evidence=None), now=AT)

        assert result.detail is not None
        assert "failing test run" in result.detail

    def test_every_shortfall_has_an_explanation(self) -> None:
        """A halt whose only content is ``('tests-removed',)`` makes the reader
        go and find the table, so the table travels with the record."""
        for shortfall in Shortfall:
            assert explain((shortfall,)).strip()


class TestPluggability:
    """The plug in "pluggable": a kind this release does not ship."""

    def test_a_caller_supplied_rule_is_dispatched_to(self) -> None:
        def never_satisfied(
            _contract: VerificationContract, _attempt: Attempt
        ) -> tuple[Shortfall, ...]:
            return (Shortfall.BUILD_FAILED,)

        registry = Registry().with_rule(ContractKind.NONE, never_satisfied)

        assert not registry.evaluate(contract(ContractKind.NONE), attempt(), now=AT).passed
        # The default table is untouched — `with_rule` returns a new registry.
        assert evaluate(contract(ContractKind.NONE), attempt(), now=AT).passed

    def test_the_universal_checks_still_apply_to_a_custom_rule(self) -> None:
        """A rule cannot opt out of "something must have been committed"."""
        registry = Registry().with_rule(ContractKind.NONE, lambda _c, _a: ())

        result = registry.evaluate(contract(ContractKind.NONE), attempt(tree_hash=None), now=AT)

        assert Shortfall.NO_TREE in result.shortfalls
