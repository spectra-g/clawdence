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

**Intent in, artifacts out** (§3.10, added in S6b). The request says what to do;
the result carries everything needed to decide what happened. The control plane
does not go and look at the workspace afterwards, because "afterwards" is not
the moment the work exists: the container is gone by then, and on the ``host``
tier an agent's stray dev server may still be writing. So the tier collects the
artifacts where the workspace is, at the moment it collects the work, and the
control plane decides from the payload rather than from a directory it may not
be able to reach.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final

from pydantic import AwareDatetime, Field, StringConstraints

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

    The last three are §3.7a's, added in S6b. Each names a failure that never
    reaches the process's exit status, which is what everything above them is
    anchored on, and each has cost somebody a production incident:

    - ``PROVIDER_ERROR`` — the agent exited **0** having emitted a final turn
      that carried an error: a rate limit, a 400, "your credit balance is too
      low". Reported as ``SUCCEEDED`` this is a **false success**, and a false
      success is a different severity of bug from a misclassified failure —
      everything downstream, from the merge gate to the epic aggregator, is
      built to trust that one value.
    - ``DROPPED_COMMIT`` — the agent edited files and never committed them.
      Reported as ``EMPTY_DIFF`` it reads as a deliberate no-op, and it is the
      characteristic weak-model failure rather than a rare one.
    - ``NO_MODEL_RESPONSE`` — events flowed but not one model turn did, because
      the credential was rejected. Reported as ``STARTUP_FAILED`` it is
      indistinguishable from a missing image, which is a different repair.
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
    PROVIDER_ERROR = "provider-error"
    DROPPED_COMMIT = "dropped-commit"
    NO_MODEL_RESPONSE = "no-model-response"


#: How many dirty paths a result carries. The tier truncates to this rather than
#: the model rejecting a run for having been messy: a result that fails
#: validation is a ``PermanentError``, and losing a real outcome because the
#: agent touched too many files would be the validation causing the failure.
MAX_DIRTY_PATHS: Final = 200

#: One path, as git reported it. Length-capped for the same reason every other
#: string crossing this boundary is: it comes from a process that ran
#: model-generated code, and a filename is something that process chooses.
DirtyPath = Annotated[str, StringConstraints(max_length=1024)]


class DiffStat(DomainModel):
    """Shape of the change, not the change itself.

    The diff can be megabytes; the control plane usually needs to know only
    whether there is one and roughly how large.
    """

    files_changed: int = Field(default=0, ge=0)
    insertions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)


class RunnerRequest(DomainModel):
    """What the control plane hands a runner. **Intent, not addressing.**"""

    run_id: RunId
    stage_id: StageId
    work_item_id: WorkItemId

    #: Absolute path, and **a hint the tier that needs it consumes** — not a
    #: promise to the domain that a filesystem is shared. Path identity is not
    #: cosmetic where it is used: testcontainers passes host paths to the daemon
    #: when it mounts volumes for sibling containers (§3.3), so a differing path
    #: breaks those mounts silently, and both tiers shipping today honour it.
    #:
    #: What this field does *not* assert, and used to imply, is that the control
    #: plane can reach the workspace after the run and go and look at it. It
    #: cannot: the container is removed, and on ``host`` a process the agent left
    #: behind may still be writing. Everything the control plane needs to decide
    #: an outcome comes back on ``RunnerResult`` instead (§3.10).
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
    """What comes back. Untrusted until validated.

    The artifacts below ``diff`` are §3.10's half of the boundary: they are
    gathered by the tier, in the workspace, at the moment the work is collected,
    because that is the only moment they are true. Deriving them afterwards from
    ``worktree_path`` is what this replaces.
    """

    run_id: RunId
    stage_id: StageId
    outcome: RunnerOutcome

    #: The tree the work landed on. ``VerificationResult`` binds evidence to
    #: this, so it is absent only when the runner produced no commit at all.
    tree_hash: TreeHash | None = None

    exit_code: int | None = None
    diff: DiffStat | None = None
    test_evidence: TestEvidence | None = None

    #: Commits **the agent itself** made on top of ``RunnerRequest.base_commit``,
    #: counted before the runner's own safety commit. Zero with a non-empty
    #: ``diff`` is exactly §3.7a's dropped commit: the work exists because the
    #: runner rescued it, and the agent never claimed it.
    commits_ahead: int = Field(default=0, ge=0)

    #: Whether the agent left work uncommitted. Paths the runner installed
    #: itself do not count — our own conventions file, plan and verdict are
    #: sitting in that tree, and a naive dirtiness probe reports every run dirty.
    dirty: bool = False

    #: A sample of ``dirty``, for a person reading the failure. Capped because an
    #: agent can create a hundred thousand files and this is persisted with the
    #: step result; ``dirty`` is the answer, this is the evidence for it.
    dirty_paths: tuple[DirtyPath, ...] = Field(default=(), max_length=MAX_DIRTY_PATHS)

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
