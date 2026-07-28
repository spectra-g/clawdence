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
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field

from clawdence.domain._base import DomainModel
from clawdence.domain.ids import TreeHash


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
    meanings. Four verbs, each distinct.
    """

    RESTART = "restart"
    RETRY = "retry"
    APPROVE = "approve"
    SKIP = "skip"


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

    allowed_resume_verbs: tuple[ResumeVerb, ...] = (
        ResumeVerb.RESTART,
        ResumeVerb.RETRY,
        ResumeVerb.APPROVE,
        ResumeVerb.SKIP,
    )


class VerificationResult(DomainModel):
    """Evidence that a contract was met, bound to the tree it was met on."""

    contract: VerificationContract
    passed: bool

    #: Required. Any tree mutation — rebase, force-push, base advance —
    #: invalidates this result and forces re-verification before merge.
    tree_hash: TreeHash

    attempt: int = Field(ge=1)
    evidence: TestEvidence | None = None
    checked_at: AwareDatetime
    detail: str | None = None
