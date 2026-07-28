"""The runner protocol — the one contract that crosses the trust boundary.

Everything else in this package is passed between components that trust each
other. ``RunnerRequest`` goes *out* to a process that executes repo code, and
``RunnerResult`` comes *back* from it. Both directions are hostile-adjacent:

- The request must carry no control-plane secrets. There is no field for one,
  and there should never be. Credentials reach the runner as scoped, per-run
  environment (see ``RepoProfile.mcp_servers``), not as request payload.
- The response is untrusted output. ``worktree_path`` is bind-mounted, so the
  runner writes directly to a host path — a deliberate hole, since it is how
  work gets out, but it means paths and text coming back are re-validated
  before the control plane acts on them, never used to derive control-plane
  paths, and never evaluated.
- ``discovery_notes`` are written by a process that read repo content, so they
  are an injection vector into any later prompt that quotes them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from clawdence.domain._base import DomainModel
from clawdence.domain.budget import Budget, CostEntry, TokenUsage
from clawdence.domain.ids import Identifier, RunId, StageId, TreeHash, WorkItemId
from clawdence.domain.repo import RepoProfile
from clawdence.domain.verification import TestEvidence, VerificationContract


class RunnerOutcome(StrEnum):
    """The failure taxonomy from the plan's §3.7.

    v1 collapsed nearly all of these into "runner failed", which meant the
    retry policy could not tell a flaky test from a budget cap from an OOM
    kill — and so treated them identically. Each value here needs different
    handling, which is the entire reason they are distinct.

    ``BLOCKED`` is the one value not in the plan's §3.7 list, added in S6 when
    the runner needed somewhere to put "the agent stopped because something
    outside its control was missing". Reporting that as ``TESTS_FAILED`` gets it
    retried until the attempts run out, which is v1's behaviour and how a budget
    is spent re-discovering that a fixture still does not exist. It is a failure
    that a second identical attempt cannot change, so it halts to a human.
    """

    SUCCEEDED = "succeeded"
    TESTS_FAILED = "tests-failed"
    BLOCKED = "blocked"
    EMPTY_DIFF = "empty-diff"
    NON_ZERO_EXIT = "non-zero-exit"
    TIMED_OUT = "timed-out"
    OOM_KILLED = "oom-killed"
    DISK_FULL = "disk-full"
    BUDGET_EXCEEDED = "budget-exceeded"
    NETWORK_DENIED = "network-denied"
    STARTUP_FAILED = "startup-failed"
    CANCELLED = "cancelled"


class DiffStat(DomainModel):
    """Shape of the change, not the change itself.

    The diff can be megabytes; the control plane usually needs to know only
    whether there is one and roughly how large.
    """

    files_changed: int = Field(default=0, ge=0)
    insertions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)


class RunnerRequest(DomainModel):
    """What the control plane hands a runner."""

    run_id: RunId
    stage_id: StageId
    work_item_id: WorkItemId

    #: Absolute path, identical inside the runner and on the host. Path
    #: identity is not cosmetic: testcontainers passes host paths to the daemon
    #: when it mounts volumes for sibling containers, so a differing path
    #: breaks those mounts silently.
    worktree_path: str = Field(pattern=r"^/.+")

    branch: str
    base_commit: TreeHash

    profile: RepoProfile
    contract: VerificationContract
    budget: Budget

    #: The augmented plan text — v1's ``_build_runner_plan_input``, which grew
    #: to 151 lines of constraint hardening because everything that plan text
    #: fails to say, the agent invents.
    plan: str

    #: Unresolved stubs carried in from earlier stories in the same epic.
    carried_stubs: tuple[str, ...] = ()

    #: Stable across redelivery, so a retried dispatch cannot double-charge or
    #: double-apply.
    idempotency_key: Identifier

    created_at: AwareDatetime


class RunnerResult(DomainModel):
    """What comes back. Untrusted until validated."""

    run_id: RunId
    stage_id: StageId
    outcome: RunnerOutcome

    #: The tree the work landed on. ``VerificationResult`` binds evidence to
    #: this, so it is absent only when the runner produced no commit at all.
    tree_hash: TreeHash | None = None

    exit_code: int | None = None
    diff: DiffStat | None = None
    test_evidence: TestEvidence | None = None

    usage: TokenUsage = TokenUsage()
    cost: CostEntry | None = None

    #: Findings about the codebase, for the memory layer. Written by a process
    #: that read repo content — treat as data, never as instructions.
    discovery_notes: tuple[str, ...] = ()
    unresolved_stubs: tuple[str, ...] = ()

    started_at: AwareDatetime
    finished_at: AwareDatetime

    #: Human-readable detail. Diagnostic only; nothing branches on this string.
    message: str | None = None
