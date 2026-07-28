"""The audit trail.

Per ADR-0005 this is **not** the source of truth — the ``runs``/``steps`` tables
are. That inversion is the whole point of the decision: an append-only log that
state is *derived* from needs upcasters, snapshots, compaction, and projection
rebuild, forever, for every schema change. An append-only log that state is
merely *recorded* in needs none of them.

What survives from the event-sourcing design is the obligation that could not
be dropped: **redaction happens at write time**. Payloads carry Slack text,
issue bodies, plans, and logs, any of which can contain a key someone pasted,
and in an append-only store there is no deleting it afterwards. ``redacted``
records that the pass ran, so a payload that was never screened is
distinguishable from one that was screened and found clean.

``schema_version`` is still here despite ADR-0005 removing the replay
requirement. The log is read by the HQ timeline and by replay tooling (S20), and
those readers span versions even when nothing rebuilds state from it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, JsonValue

from clawdence.domain._base import DomainModel
from clawdence.domain.ids import EventId, RunId, StageId, WorkItemId

EVENT_SCHEMA_VERSION = 1


class EventKind(StrEnum):
    """What happened. Coarse by design.

    These name transitions worth reconstructing months later, not every
    function call. A log granular enough to trace execution is a log nobody
    reads and a disk nobody has — v1's processing log reached 300MB.
    """

    WORK_ITEM_RECEIVED = "work_item.received"
    WORK_ITEM_ROUTED = "work_item.routed"

    RUN_STARTED = "run.started"
    RUN_STATUS_CHANGED = "run.status_changed"
    RUN_FINISHED = "run.finished"
    RUN_CANCELLED = "run.cancelled"

    STEP_STARTED = "step.started"
    STEP_FINISHED = "step.finished"
    STEP_RETRIED = "step.retried"
    STEP_TIMED_OUT = "step.timed_out"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"

    VERIFICATION_RECORDED = "verification.recorded"
    BUDGET_EXCEEDED = "budget.exceeded"
    HALTED_FOR_HUMAN = "halted_for_human"
    DEGRADED = "degraded"


class ActorKind(StrEnum):
    """Whether a human, a model, or the system itself did the thing.

    Worth separating: "an approval was recorded" means something different
    depending on which of these three produced it, and that difference is
    exactly what an audit trail is read to establish.
    """

    HUMAN = "human"
    AGENT = "agent"
    RUNNER = "runner"
    SYSTEM = "system"


class Actor(DomainModel):
    """Who or what caused an event."""

    kind: ActorKind
    id: str | None = None
    display_name: str | None = None


class Event(DomainModel):
    """One append-only audit record."""

    id: EventId
    schema_version: int = EVENT_SCHEMA_VERSION
    kind: EventKind
    at: AwareDatetime

    run_id: RunId | None = None
    work_item_id: WorkItemId | None = None
    stage_id: StageId | None = None

    actor: Actor | None = None

    #: Screened before it got here. See the module docstring.
    payload: JsonValue = None

    #: True once the redaction pass has run over ``payload``. A record with
    #: this False was written by a path that skipped screening, which is a bug
    #: worth being able to find.
    redacted: bool = True
