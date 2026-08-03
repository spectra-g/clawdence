"""Verification contracts — the definition of *done* as evidence.

This replaces v1's 405-line ``_check_tdd_verdict`` with a declaration. Two
properties are encoded structurally rather than left to the implementation,
because both were correctness holes in v1:

**Evidence binds to a tree.** ``VerificationResult.tree_hash`` is required. A
result is evidence for exactly the tree it names and for no other. Without
this, an auto-rebase merges code whose tests ran against a different base:
story tests pass at commit X, a conflict forces a rebase onto an advanced
base, and the merge lands a tree nothing ever verified.

**Exhausting retries halts.** ``on_exhausted`` is a one-value ``Literal``.
v1's rule was that no exhausted-retry path ever force-proceeds or
force-approves, enforced across 12 call sites by convention. Here there is no
value that expresses "give up and merge anyway".

**Which resumptions a halt admits is a property of the state, not of the
contract.** S13 previously gave ``VerificationContract`` an
``allowed_resume_verbs`` tuple defaulting to all four verbs, which quietly
contradicted the paragraph above: a contract could list ``approve`` and a run
halted for exhausted retries would then admit exactly the "merge it anyway"
resumption the ``Literal`` exists to make unsayable. The table now lives with
the states (``clawdence.verify.halt``), where it is one mapping the whole
system reads rather than a field each caller sets — which is the correction
the S13/S17 split was drawn to force.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field

from clawdence.domain._base import DomainModel
from clawdence.domain.ids import RunId, StageId, TreeHash, WorkItemId


class ContractKind(StrEnum):
    """The four contracts v2.0 ships.

    ``OUTSIDE_IN_TDD`` is one option among four rather than the welded-in
    default it was in v1 — 175 references to it inside the orchestrator is
    what made the process impossible to vary per work item.
    """

    OUTSIDE_IN_TDD = "outside-in-tdd"
    TEST_AFTER = "test-after"
    BUILD_ONLY = "build-only"
    NONE = "none"


class TestReporter(StrEnum):
    """Machine-readable test output formats.

    Declared per repo, consumed here. Raw stdout is not an option: a failing
    suite emits thousands of lines, feeding them back exhausts the agent's
    context budget, and head/tail truncation drops the assertion the model
    actually needs — producing retry loops that burn to the cost cap without
    ever seeing the error.
    """

    JUNIT_XML = "junit-xml"
    JEST_JSON = "jest-json"
    PYTEST_JSON_REPORT = "pytest-json-report"
    GO_TEST_JSON = "go-test-json"
    CARGO_JSON = "cargo-json"
    NONE = "none"


class ResumeVerb(StrEnum):
    """What a human may do with a halted run.

    v1 accumulated ``restart`` / ``retry`` / ``retry-coding`` /
    ``retry-consensus`` / ``approve`` / ``skip`` ad hoc, with overlapping
    meanings — one verb per call site, because the states were never named in
    one place. Four verbs, each distinct, and which of them a given halt admits
    is derived from its ``HaltState`` rather than chosen per call.

    ``SKIP`` is the one worth defining precisely, because a careless reading
    makes it the force-proceed this module forbids. It **abandons** the work
    item: the run ends, nothing merges, and the branch is left for a person.
    It is not "proceed to the next stage without the evidence". Nothing in the
    verb set can produce a merge on unmet evidence, which is the invariant
    ``clawdence.verify.halt`` asserts over the whole state table.
    """

    RESTART = "restart"
    RETRY = "retry"
    APPROVE = "approve"
    SKIP = "skip"


class Shortfall(StrEnum):
    """Why a contract was not met — matchable, not prose.

    ``VerificationResult.detail`` is for a person; this is what the retry
    policy, the halt states and (later) S17's approval context branch on. The
    same reasoning as ``StepError.kind``: a policy that has to regex an English
    sentence is one that breaks when somebody improves the wording.

    The distinctions that earn their place are the ones handled differently.
    ``NO_TEST_EVIDENCE`` and ``TESTS_FAILED`` are both "the tests do not show
    this works" and both worth another attempt — but only the first can be
    caused by a repository whose reporter is misconfigured, which is a fact
    about the setup rather than about the code. ``NO_RED_PHASE`` and
    ``VACUOUS_RED`` are the two ways outside-in TDD is faked, and they are
    separate because the second one is a test that exists and asserts nothing,
    which is the more interesting failure to show a human.
    """

    #: Nothing was committed, so there is no tree to bind evidence to.
    NO_TREE = "no-tree"

    #: The runner did not report success. The taxonomy in ``RunnerOutcome``
    #: says which failure; this only says the attempt did not get far enough
    #: for the contract's own questions to be worth asking.
    RUNNER_FAILED = "runner-failed"

    #: The contract required a change and there was none (v1's ``_EmptyPRError``).
    EMPTY_DIFF = "empty-diff"

    #: The ``pre_verify`` argv exited non-zero, so whatever ran after it ran
    #: against a workspace that was never prepared.
    PRE_VERIFY_FAILED = "pre-verify-failed"

    BUILD_FAILED = "build-failed"

    #: The contract's definition of done is passing tests and nothing shows
    #: that they ran at all.
    NO_TEST_EVIDENCE = "no-test-evidence"

    TESTS_FAILED = "tests-failed"

    #: The full suite was required and only part of it ran.
    PARTIAL_SUITE = "partial-suite"

    #: Outside-in TDD, and no failing run was recorded before the change.
    NO_RED_PHASE = "no-red-phase"

    #: A red phase was recorded and nothing in it failed — a test written after
    #: the code, or one that asserts nothing.
    VACUOUS_RED = "vacuous-red"

    #: The green run has fewer tests than the red one. The cheapest way to turn
    #: red into green is to delete the test, and it is the way that leaves both
    #: runs looking correct in isolation.
    TESTS_REMOVED = "tests-removed"


class HaltState(StrEnum):
    """Why a run stopped and is waiting for a person.

    v1 had twelve ``_halt_story_for_human`` call sites and no enumeration of
    what they meant, so the resume vocabulary grew one verb per site. These are
    the states; ``clawdence.verify.halt`` maps each to the resumptions it
    admits, and no state admits ``APPROVE``.

    They are distinguished by **what a person would have to change** to make a
    resumption worth anything, which is the only distinction a halt state is
    read to make:

    ``RETRIES_EXHAUSTED``
        The contract was not met within ``max_attempts``. Retrying without
        changing something outside the run gets the same answer at the same
        price — but a human who *has* changed something (a fixture, a
        dependency, the plan) has a real reason to retry, so the verb stays.
    ``BLOCKED``
        Something outside the agent's control stopped it. The repair is
        elsewhere and the attempts are irrelevant, which is why this is not
        just an exhaustion with a lower count.
    ``BUDGET_EXCEEDED``
        The money ran out. ``RETRY`` is pointless — the same budget is spent
        again to reach the same cap — so only a ``RESTART`` carrying a new
        budget, or abandoning the item, means anything here.
    ``EVIDENCE_STALE``
        The tree moved under evidence that had passed: a rebase, a force-push,
        an advanced base. Nothing is wrong with the work and nothing is wrong
        with the tests; what is missing is a run of them against the tree that
        would actually land. This is the state S15b's auto-rebase produces, and
        the reason evidence carries a tree hash at all.
    ``VERIFICATION_ERROR``
        The contract could not be evaluated — an unparseable report, a reporter
        the repository declares and does not emit. A failure of our plumbing or
        the repository's configuration rather than of the work, and kept
        separate so it is not silently counted as a failing test.
    """

    RETRIES_EXHAUSTED = "retries-exhausted"
    BLOCKED = "blocked"
    BUDGET_EXCEEDED = "budget-exceeded"
    EVIDENCE_STALE = "evidence-stale"
    VERIFICATION_ERROR = "verification-error"


class FailingAssertion(DomainModel):
    """One failure, extracted from a reporter — not a stack trace dump."""

    test_id: str
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    message: str
    #: The immediate frames only. Deep frames are noise to a model deciding
    #: what to change.
    frames: tuple[str, ...] = ()


class TestEvidence(DomainModel):
    """Structured result of a test run."""

    reporter: TestReporter
    total: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    failures: tuple[FailingAssertion, ...] = ()


class VerificationContract(DomainModel):
    """A declaration of what counts as done, and what happens when it isn't."""

    kind: ContractKind

    #: Bounded retry. v1's caps were 3; the number is config, the bound is not.
    max_attempts: int = Field(default=3, ge=1, le=10)

    #: Optional argv run before verification — cache warm, fixture seed, the
    #: ``cp -n .env.example .env`` class of repo-specific setup.
    pre_verify: tuple[str, ...] | None = None

    #: An empty diff is a distinct failure, not a pass (v1's ``_EmptyPRError``).
    require_non_empty_diff: bool = True
    require_full_test_suite: bool = False

    #: Not configurable. Retries exhausted means a human decides.
    on_exhausted: Literal["halt_to_human"] = "halt_to_human"


class VerificationResult(DomainModel):
    """Evidence that a contract was met, bound to the tree it was met on."""

    contract: VerificationContract
    passed: bool

    #: Required. Any tree mutation — rebase, force-push, base advance —
    #: invalidates this result and forces re-verification before merge.
    tree_hash: TreeHash

    attempt: int = Field(ge=1)
    evidence: TestEvidence | None = None

    #: The failing run recorded *before* the change, for contracts that require
    #: one. Absent for every other contract, and its absence under
    #: ``outside-in-tdd`` is a ``NO_RED_PHASE`` shortfall rather than a pass.
    red_evidence: TestEvidence | None = None

    #: Empty exactly when ``passed``. Structured so a policy can branch without
    #: reading ``detail``.
    shortfalls: tuple[Shortfall, ...] = ()

    checked_at: AwareDatetime
    detail: str | None = None


class HaltRecord(DomainModel):
    """What a halted run records — the thing a person is handed.

    Everything needed to understand the halt without re-deriving it, because
    re-deriving it is what S17 must not spend its budget on and what v1 made
    impossible: the state, the evidence that led to it, and the resumptions it
    admits.

    ``admits`` is **denormalised onto the record** rather than looked up from
    the state. That is a deliberate duplication of
    ``clawdence.verify.halt.RESUMPTIONS``: a stored halt is read months later,
    by an operator surface that may be a different version of this code, and a
    record that says only ``retries-exhausted`` needs our table to mean
    anything. Written from the table by ``halt_for``, so the two cannot
    disagree at the moment of writing, and a test asserts the round trip.
    """

    state: HaltState
    run_id: RunId
    work_item_id: WorkItemId | None = None
    stage_id: StageId | None = None

    at: AwareDatetime

    #: How many attempts were made, and how many the contract allowed. Both,
    #: because "3" means nothing without "of 3".
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1)

    contract: ContractKind

    #: The tree the halt is about. ``None`` when nothing was ever committed.
    tree_hash: TreeHash | None = None

    #: The last evaluation, carrying the failing assertions a person needs.
    last_result: VerificationResult | None = None

    #: What this state admits. See the class docstring for why it is stored.
    admits: tuple[ResumeVerb, ...] = ()

    #: One line for a human, naming the state and what would change it.
    summary: str
