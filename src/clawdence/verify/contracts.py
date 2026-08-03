"""What *done* means, as four rules over one record of observations.

This is v1's ``_check_tdd_verdict`` — 405 lines, one function, TDD welded into
every branch of it — replaced by a registry. The shape is deliberately the one
``runners.outcome.classify`` already uses here and for the same reason: a **pure
function over a record**, so the interesting rules are testable without a
repository, a process, or a model.

**Why a registry rather than a longer function.** The plan calls TDD "the second
of v1's three structural problems", and the problem was never that v1's TDD
enforcement was wrong — it was that *there was nowhere else to stand*. 175
references to a TDD verdict inside the orchestrator is a process that cannot be
varied per work item, so a repository that does not work that way cannot use the
system at all. Here each contract is an object implementing one method, looked up
by kind, and ``evaluate`` never branches on which one it got. "TDD is optional"
is then a property of the code rather than a claim in a README.

The four that ship:

``none``
    Nothing is required. Not a degenerate case to be embarrassed about — it is
    what a spike, a docs change, or a repository with no test suite honestly
    needs, and its absence in v1 is why those went through a TDD gate that had
    nothing to check.
``build-only``
    The build succeeded. No test evidence is required and none is invented.
``test-after``
    The tests ran and passed. Silent about *when* they were written.
``outside-in-tdd``
    The tests ran and passed, **and a failing run was recorded before the
    change**. This is the difference between the contract and a prompt.

**The red phase is what makes ``outside-in-tdd`` more than a sterner docstring.**
Under ``test-after`` a passing suite proves the code works. It does not prove a
test was written for the new behaviour — a suite that passes because the new
behaviour has no test at all passes just as green. So this contract asks for the
*red* run too, and checks three things a fake cannot satisfy at once: something
failed before the change (``NO_RED_PHASE`` / ``VACUOUS_RED``), nothing fails
after it, and the green run has at least as many tests as the red one
(``TESTS_REMOVED``). That last one is not paranoia: deleting the failing test is
the cheapest way to turn red into green, and it leaves both runs looking correct
in isolation.

What this deliberately does **not** do is trust the agent's own ``status``
field. ``RunnerVerdict.status`` is a claim; the evidence is parsed from the
reporter the repository declares. Keeping the claim and the observation apart is
what ``runners.verdict`` set up for exactly this module to use.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

from clawdence.domain import (
    ContractKind,
    DiffStat,
    RunnerOutcome,
    Shortfall,
    TestEvidence,
    VerificationContract,
    VerificationResult,
)


@dataclass(frozen=True, slots=True)
class Attempt:
    """Everything observed about one attempt, before it is judged.

    A record rather than eight arguments, matching ``outcome.Completion``: the
    set of observations only grows, and a signature that grows to eight
    parameters is one whose call sites disagree about the order.

    The fields are *observations*, not claims. ``evidence`` is parsed from the
    reporter file the run left behind (``verify.reporters``), not read from the
    agent's verdict — which is why a contract can catch an agent that reported
    success over a failing suite.
    """

    #: The commit the work landed on. ``None`` when nothing was committed,
    #: which no contract can pass: there would be nothing to bind evidence to.
    tree_hash: str | None

    #: What the runner concluded. A contract does not re-derive this; it refuses
    #: to judge work that never completed.
    outcome: RunnerOutcome = RunnerOutcome.SUCCEEDED

    number: int = 1

    diff: DiffStat | None = None

    #: The test run after the change.
    evidence: TestEvidence | None = None

    #: The test run *before* it. Only ``outside-in-tdd`` reads this.
    red_evidence: TestEvidence | None = None

    #: Whether the repository's build command succeeded. ``None`` where no
    #: build was run — which ``build-only`` treats as a shortfall and the test
    #: contracts ignore, since a suite that ran at all was compiled to do it.
    build_succeeded: bool | None = None

    #: Whether the whole suite ran, rather than a subset. Only consulted when
    #: the contract or the profile asks for the full suite.
    full_suite: bool = False

    #: Whether ``contract.pre_verify`` ran and exited zero. ``None`` when the
    #: contract declares no hook.
    pre_verify_ok: bool | None = None

    #: Set when evidence could not be produced at all — an unparseable report,
    #: a reporter the repository declares and does not emit. Distinct from
    #: failing tests, and it halts rather than retrying.
    verification_error: str | None = None


class ContractRule(Protocol):
    """One contract's definition of done.

    Returns the shortfalls it found, empty for a pass. It is handed the
    ``VerificationContract`` as well as the attempt because the config the
    contract carries — ``require_non_empty_diff``, ``require_full_test_suite``
    — is per-instance, and a rule that closed over its own copy would be a
    second place for those values to live.
    """

    #: Positional-only for the reason ``recheck.Executor`` gives: a rule
    #: supplied by a caller should have to match this shape, not our
    #: parameter names.
    def __call__(
        self, contract: VerificationContract, attempt: Attempt, /
    ) -> tuple[Shortfall, ...]: ...


def evaluate(
    contract: VerificationContract,
    attempt: Attempt,
    *,
    rules: Mapping[ContractKind, ContractRule] | None = None,
    now: datetime | None = None,
) -> VerificationResult:
    """Judge one attempt against one contract.

    The universal checks run first and are not the rules' business: an attempt
    with no tree, a runner that did not succeed, a ``pre_verify`` that failed,
    or evidence that could not be produced is short of *every* contract
    including ``none``, and asking each rule to remember that is how four rules
    acquire three implementations of it.

    ``rules`` is the plug in "pluggable": pass a mapping with an extra kind in
    it and ``evaluate`` dispatches to it without knowing anything about it.
    """
    table = DEFAULT_RULES if rules is None else rules
    at = now or datetime.now(UTC)

    shortfalls = list(_universal(contract, attempt))
    if attempt.tree_hash is not None:
        # The rule still runs when the universal checks already found something,
        # so a person sees every reason at once rather than one per attempt. The
        # exception is a missing tree: nothing was committed, so there is nothing
        # for tests to have run against, and every rule would only be describing
        # the same absence in its own vocabulary.
        shortfalls.extend(table[contract.kind](contract, attempt))

    ordered = tuple(dict.fromkeys(shortfalls))
    passed = not ordered

    # `passed` requires a tree by construction: `_universal` reports NO_TREE for
    # a missing one, so this cast is safe wherever it matters. The placeholder
    # keeps the record constructible for a failure that never committed, and
    # `evidence.check` refuses it against any real tree anyway.
    return VerificationResult(
        contract=contract,
        passed=passed,
        tree_hash=attempt.tree_hash or _NO_TREE,
        attempt=attempt.number,
        evidence=attempt.evidence,
        red_evidence=attempt.red_evidence,
        shortfalls=ordered,
        checked_at=at,
        detail=explain(ordered) if ordered else None,
    )


def explain(shortfalls: tuple[Shortfall, ...]) -> str:
    """One sentence per shortfall, for a person.

    Nothing branches on this string — that is what ``shortfalls`` is for — but
    a halt record whose only content is ``('tests-removed',)`` makes the reader
    go and find this table anyway, so it travels with the record instead.
    """
    return " ".join(_REASONS[shortfall] for shortfall in shortfalls)


def _universal(contract: VerificationContract, attempt: Attempt) -> tuple[Shortfall, ...]:
    """What is short of every contract, including ``none``."""
    found: list[Shortfall] = []
    if attempt.verification_error is not None:
        # Deliberately not a test failure. "The tests failed" and "we could not
        # tell whether the tests ran" are different answers, and only the second
        # one is about our plumbing rather than the work.
        found.append(Shortfall.NO_TEST_EVIDENCE)
    if attempt.outcome is not RunnerOutcome.SUCCEEDED:
        found.append(Shortfall.RUNNER_FAILED)
    if attempt.tree_hash is None:
        found.append(Shortfall.NO_TREE)
    if contract.pre_verify and attempt.pre_verify_ok is False:
        found.append(Shortfall.PRE_VERIFY_FAILED)
    if contract.require_non_empty_diff and _empty(attempt.diff):
        found.append(Shortfall.EMPTY_DIFF)
    return tuple(found)


def _empty(diff: DiffStat | None) -> bool:
    """No diff and an empty diff are the same shortfall.

    They are not the same *event* — one is a runner that reported nothing, the
    other a run that changed nothing — but neither satisfies a contract that
    asked for a change, and ``RunnerOutcome`` already distinguishes them for
    anybody who needs to know which.
    """
    return diff is None or diff.files_changed == 0


def _no_requirements(contract: VerificationContract, attempt: Attempt) -> tuple[Shortfall, ...]:
    """``none``: the universal checks are the whole contract.

    Worth having as an explicit rule rather than a missing key. ``none`` is a
    real choice a repository makes, and a lookup that fell through to a default
    would make it indistinguishable from a kind nobody implemented.
    """
    return ()


def _build_only(contract: VerificationContract, attempt: Attempt) -> tuple[Shortfall, ...]:
    """``build-only``: it compiles, and that is the claim.

    ``build_succeeded is None`` counts as a shortfall rather than a pass. A
    contract whose single requirement is a build, satisfied by an attempt that
    never reported running one, is a contract satisfied by silence.
    """
    if attempt.build_succeeded:
        return ()
    return (Shortfall.BUILD_FAILED,)


def _test_after(contract: VerificationContract, attempt: Attempt) -> tuple[Shortfall, ...]:
    """``test-after``: the tests ran and passed. Silent about when they were written."""
    return _tests_pass(contract, attempt)


def _outside_in_tdd(contract: VerificationContract, attempt: Attempt) -> tuple[Shortfall, ...]:
    """``outside-in-tdd``: ``test-after``, plus the red phase that came first.

    The three red-phase checks are each closing a specific way the first one
    alone can be satisfied without doing TDD — see the module docstring. They
    run even when the green phase failed, because "your tests fail *and* you
    never wrote a failing test first" is two things worth telling somebody at
    once rather than across two attempts.
    """
    found = list(_tests_pass(contract, attempt))
    red = attempt.red_evidence

    if red is None:
        found.append(Shortfall.NO_RED_PHASE)
        return tuple(found)
    if red.failed == 0:
        # A recorded red phase in which nothing failed. Either the test was
        # written after the code, or it asserts nothing — and the second is the
        # more interesting thing to show a human, which is why this is not
        # folded into NO_RED_PHASE.
        found.append(Shortfall.VACUOUS_RED)

    green = attempt.evidence
    if green is not None and green.total < red.total:
        found.append(Shortfall.TESTS_REMOVED)
    return tuple(found)


def _tests_pass(contract: VerificationContract, attempt: Attempt) -> tuple[Shortfall, ...]:
    """The requirement shared by both test contracts.

    Absent evidence is *not* neutral here and the collapse is deliberate, for
    the reason ``outcome._tests_failed`` gives one layer down: to a contract
    whose definition of done is passing tests, "the tests passed" and "nothing
    shows the tests passed" are the same answer.
    """
    found: list[Shortfall] = []
    evidence = attempt.evidence
    if evidence is None:
        found.append(Shortfall.NO_TEST_EVIDENCE)
    elif evidence.failed > 0:
        found.append(Shortfall.TESTS_FAILED)
    elif evidence.total == 0:
        # A reporter that parsed cleanly and found no tests at all. Green by
        # arithmetic, and evidence of nothing.
        found.append(Shortfall.NO_TEST_EVIDENCE)

    if contract.require_full_test_suite and not attempt.full_suite:
        found.append(Shortfall.PARTIAL_SUITE)
    return tuple(found)


#: The four contracts v2.0 ships. A mapping rather than a match statement so a
#: caller can hand ``evaluate`` a different one.
DEFAULT_RULES: Final[Mapping[ContractKind, ContractRule]] = {
    ContractKind.NONE: _no_requirements,
    ContractKind.BUILD_ONLY: _build_only,
    ContractKind.TEST_AFTER: _test_after,
    ContractKind.OUTSIDE_IN_TDD: _outside_in_tdd,
}

#: Stands in for a tree on a result that never had one. Not a valid ``TreeHash``
#: by accident — it is forty zeroes, git's own null object id, so anything that
#: compares it against a real commit gets a mismatch rather than a match.
_NO_TREE: Final = "0" * 40

_REASONS: Final[Mapping[Shortfall, str]] = {
    Shortfall.NO_TREE: "Nothing was committed, so there is no tree to verify.",
    Shortfall.RUNNER_FAILED: "The attempt did not complete.",
    Shortfall.EMPTY_DIFF: "The contract requires a change and no file was changed.",
    Shortfall.PRE_VERIFY_FAILED: "The pre-verify command failed, so nothing after it was prepared.",
    Shortfall.BUILD_FAILED: "The build did not succeed.",
    Shortfall.NO_TEST_EVIDENCE: "Nothing shows the tests ran.",
    Shortfall.TESTS_FAILED: "Tests failed.",
    Shortfall.PARTIAL_SUITE: "The full suite was required and only part of it ran.",
    Shortfall.NO_RED_PHASE: "No failing test run was recorded before the change.",
    Shortfall.VACUOUS_RED: "The run recorded before the change had no failing test in it.",
    Shortfall.TESTS_REMOVED: "There are fewer tests after the change than before it.",
}


@dataclass(frozen=True, slots=True)
class Registry:
    """A contract table with room for kinds this release does not ship.

    Thin on purpose — it exists so "pluggable" is something a caller can hold
    rather than a convention about a dict literal. ``with_rule`` returns a new
    registry, matching every other value in this codebase.
    """

    rules: Mapping[ContractKind, ContractRule] = field(default_factory=lambda: DEFAULT_RULES)

    def with_rule(self, kind: ContractKind, rule: ContractRule) -> Registry:
        return Registry(rules={**self.rules, kind: rule})

    def evaluate(
        self,
        contract: VerificationContract,
        attempt: Attempt,
        *,
        now: datetime | None = None,
    ) -> VerificationResult:
        return evaluate(contract, attempt, rules=self.rules, now=now)
