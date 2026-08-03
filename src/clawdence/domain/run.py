"""Runs and step results — the execution record.

Per ADR-0005 the ``runs``/``steps`` tables are the source of truth and the
audit log is not; these are the types those tables hold.

``StepResult`` keeps ``output`` and ``response`` as separate fields. That is
the distinction adopted from Lobster and it is worth the extra field: ``output``
is what a step *produced* (an agent's JSON, a script's parsed stdout) and
``response`` is what a *human* submitted at a gate. A workflow branching on a
human decision is doing something categorically different from one branching on
a computed verdict, and collapsing them would hide that in the audit trail.

Every step result is persisted, not just the last one. Lobster surfaces only
the final step's output, which for a system whose premise is observable
multi-stage work is a structural mismatch — and per-step results are what
replay (S20) is built on.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, JsonValue

from clawdence.domain._base import DomainModel
from clawdence.domain.budget import Budget
from clawdence.domain.ids import (
    Identifier,
    RepoId,
    RunId,
    SemVer,
    Slug,
    StageId,
    StepResultId,
    WorkItemId,
)
from clawdence.domain.workflow import StepType


class RunStatus(StrEnum):
    """Where a run is. Mirrors the state machine in ARCHITECTURE.md §4.

    ``RUNNING`` is the one member with no node in that diagram, and it arrived
    with S4 for the reason S3 predicted: the state store holds a row while a run
    is *live*, and until something persisted an in-flight run there was nothing
    for it to say. The pipeline statuses answer "how far has this got"; this one
    answers "is a process working on it right now", which is what crash-resume
    and the watchdog read.
    """

    QUEUED = "queued"
    RUNNING = "running"
    TRIAGED = "triaged"
    PLANNING = "planning"
    CONSENSUS = "consensus"
    SPLITTING = "splitting"
    EXPLORING = "exploring"
    CODING = "coding"
    VERIFYING = "verifying"
    REVIEW = "review"
    REVERIFYING = "reverifying"
    MERGED = "merged"
    HALTED = "halted"
    CANCELLED = "cancelled"
    DONE = "done"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class StepError(DomainModel):
    """Why a step failed, in a form something can branch on.

    ``kind`` is a stable, matchable token; ``message`` is for humans. A retry
    policy that has to regex the message is a retry policy that breaks when
    someone improves the wording.
    """

    kind: str
    message: str
    retryable: bool = False


class StepResult(DomainModel):
    """One execution of one stage."""

    id: StepResultId
    run_id: RunId
    #: The concrete execution node. At top level this is the declaration id;
    #: inside fan-out, parallel branches, loops and sub-workflows it is a
    #: deterministic opaque slug, so two copies of ``code`` are distinct rows.
    stage_id: StageId
    #: The id written in the workflow. Kept separately for nested executions so
    #: an opaque execution id never makes the audit trail opaque to a person.
    definition_id: StageId | None = None
    #: Human-readable route from the root workflow to this execution.
    scope: tuple[str, ...] = ()
    type: StepType
    status: StepStatus

    attempt: int = Field(default=1, ge=1)

    #: Derived from run, stage, and attempt. Unique, so a redelivered dispatch
    #: collides instead of duplicating — v1's duplicate-event guards, made
    #: structural rather than hand-written per handler.
    idempotency_key: Identifier

    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

    #: The timeout this attempt actually ran under, pinned from the stage when
    #: it started. Recorded rather than looked up because it is the watchdog's
    #: whole input: a step row that says ``running`` is only *stalled* relative
    #: to the limit it was started with, and editing the workflow file must not
    #: retroactively change whether an in-flight step is overdue.
    timeout_seconds: float | None = Field(default=None, gt=0)

    #: What the step produced. Addressed as ``$<stage_id>.json.<path>``.
    output: JsonValue | None = None

    #: What a human submitted. Addressed as ``$<stage_id>.response.<field>``.
    response: JsonValue | None = None

    error: StepError | None = None


class Run(DomainModel):
    """One execution of one workflow against one work item."""

    id: RunId
    work_item_id: WorkItemId

    workflow: Slug

    #: Pinned at run start. An in-flight run keeps executing the definition it
    #: started with, so editing a workflow file cannot change the meaning of a
    #: run already halfway through it.
    workflow_version: SemVer

    status: RunStatus = RunStatus.QUEUED
    repo_id: RepoId | None = None

    budget: Budget = Budget()

    created_at: AwareDatetime
    updated_at: AwareDatetime

    #: Set when the run reaches a terminal status.
    finished_at: AwareDatetime | None = None
