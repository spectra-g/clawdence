"""Replaying a run from the audit log — and comparing it with what is stored.

S20 asks that "a completed run can be replayed from the event log and produces
identical state". Taken literally that asks for the thing ADR-0005 refused: if
state can be *rebuilt* from the log, the log is the source of truth, and back
come the upcasters, the snapshots, the compaction and the projection rebuild.

So replay here reconstructs and then **compares**. It never writes. What the
fold produces is a projection held in memory for exactly as long as it takes to
diff it against ``runs``/``steps``, and the deliverable is the diff. Read that
way the criterion is not weakened, it is sharpened: "the log agrees with the
state" is a claim you can check, and "the log can replace the state" is a
design you have to maintain forever.

**What it is actually good for is catching a writer that does not audit.** Every
state transition in ``store.ledger`` writes its row and its event in one
transaction, and nothing enforces that but the fact that somebody wrote both
lines. A step finished by a path that skipped the log shows up here as a step
present in state and absent from the timeline, named. That check did not exist
before, and it gets stronger with every writer added — S12's agent steps, S17's
approvals — because each one either audits or diverges.

**Half the record is unobservable, on purpose, and the report says so.** S4's
policy is that audit payloads are metadata: identifiers, statuses, error *kinds*
— never step output, never a message, never a prompt, because an append-only
table cannot un-write a pasted key. A reconstruction therefore cannot know what
a step produced, and ``UNOBSERVABLE`` names those fields rather than letting
them be quietly excluded from a comparison that then reports "identical".

**A truncated replay does not compare.** ``through`` folds the first N events of
a run, which is the debugging question — what did this look like before the
stage that went wrong. The stored state is the *end* of the run, so diffing a
prefix against it would report divergences that are simply the rest of the run
happening. The comparison is skipped and the report says which of the two it is
looking at.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

from clawdence.domain import Event, EventKind, Run, StepResult
from clawdence.store.state import StateStore

#: Fields the audit log deliberately does not carry, so replay cannot speak to
#: them either way. Reported, never compared — see the module docstring.
UNOBSERVABLE: Final[tuple[str, ...]] = (
    "step.output",
    "step.response",
    "step.error.message",
    "step.error.retryable",
    "run.repo_id",
    "run.budget",
    # The heartbeat. Deliberately unaudited: ``touch_run`` fires on every step
    # transition and logging it would double the log to record a clock.
    "run.updated_at",
)

#: The one status that means the step never executed. Spelled out because the
#: fold reads statuses as strings off a payload and must not import a decision
#: about which ones imply execution from anywhere it cannot see.
_SKIPPED: Final = "skipped"

#: Event kinds the fold understands. Anything else is reported as unmodelled
#: rather than ignored: a kind nobody folds is a hole in the reconstruction, and
#: the reconstruction claiming to be complete is the failure worth avoiding.
MODELLED: Final[frozenset[EventKind]] = frozenset(
    {
        EventKind.RUN_STARTED,
        EventKind.RUN_STATUS_CHANGED,
        EventKind.RUN_FINISHED,
        EventKind.RUN_CANCELLED,
        EventKind.STEP_STARTED,
        EventKind.STEP_RETRIED,
        EventKind.STEP_FINISHED,
        EventKind.STEP_TIMED_OUT,
    }
)


@dataclass(frozen=True, slots=True)
class StepProjection:
    """One attempt, as the timeline describes it."""

    stage_id: str
    attempt: int
    status: str | None = None
    type: str | None = None
    timeout_seconds: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_kind: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        return self.stage_id, self.attempt

    @property
    def label(self) -> str:
        return f"{self.stage_id}#{self.attempt}"


@dataclass(frozen=True, slots=True)
class RunProjection:
    """A run, as the timeline describes it."""

    id: str
    work_item_id: str | None = None
    workflow: str | None = None
    workflow_version: str | None = None
    status: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None

    #: How many times this run was opened. Two means it crashed and resumed,
    #: which is a fact the ``runs`` row cannot hold — it has one status column —
    #: and one of the few things replay knows that state does not.
    starts: int = 0


@dataclass(frozen=True, slots=True)
class Divergence:
    """One thing the log and the state disagree about."""

    subject: str
    field: str
    in_log: str
    in_state: str

    def describe(self) -> str:
        return f"{self.subject}.{self.field}: log says {self.in_log}, state says {self.in_state}"


@dataclass(frozen=True, slots=True)
class Replay:
    """A reconstruction, and how it compared."""

    run_id: str
    events: int
    folded: int
    run: RunProjection | None = None
    steps: tuple[StepProjection, ...] = ()
    divergences: tuple[Divergence, ...] = ()

    #: Kinds seen and not folded. Empty today; non-empty the moment a new event
    #: kind is written by a producer and not taught to this module.
    unmodelled: tuple[str, ...] = ()

    #: True when only part of the log was folded, so no comparison was made.
    truncated: bool = False

    @property
    def agrees(self) -> bool:
        """Nothing observable disagrees. False for a truncated replay, which
        did not look."""
        return not self.truncated and not self.divergences


def replay(store: StateStore, run_id: str, *, through: int | None = None) -> Replay:
    """Fold this run's timeline, and diff it against what is stored.

    Returns a ``Replay`` even when the run has no events and no row: "there is
    nothing here" is an answer to the question, and raising would make the
    caller handle absence twice.
    """
    events = store.audit.read(run_id=run_id)
    folded = events if through is None else events[:through]
    truncated = len(folded) < len(events)

    projection, steps, unmodelled = fold(run_id, folded)
    divergences: tuple[Divergence, ...] = ()
    if not truncated:
        divergences = compare(
            projection,
            steps,
            stored=store.get_run(run_id),
            results=store.steps_for(run_id),
        )
    return Replay(
        run_id=run_id,
        events=len(events),
        folded=len(folded),
        run=projection,
        steps=steps,
        divergences=divergences,
        unmodelled=unmodelled,
        truncated=truncated,
    )


def fold(
    run_id: str,
    events: Sequence[Event],
) -> tuple[RunProjection | None, tuple[StepProjection, ...], tuple[str, ...]]:
    """Apply the timeline in order. Nothing here touches the database."""
    run: RunProjection | None = None
    steps: dict[tuple[str, int], StepProjection] = {}
    unmodelled: list[str] = []

    for event in events:
        if event.kind not in MODELLED:
            if event.kind.value not in unmodelled:
                unmodelled.append(event.kind.value)
            continue
        payload = _mapping(event.payload)

        if event.kind is EventKind.RUN_STARTED:
            if run is None:
                run = RunProjection(
                    id=run_id,
                    work_item_id=event.work_item_id,
                    workflow=_text(payload.get("workflow")),
                    workflow_version=_text(payload.get("workflow_version")),
                    status="running",
                    created_at=event.at,
                    starts=1,
                )
            else:
                # A resume. ``finished_at`` goes back to ``None`` for the reason
                # the ledger clears it: a live run that still carries the
                # instant it previously ended looks finished to every reader
                # that checks that field first.
                run = replace(run, status="running", finished_at=None, starts=run.starts + 1)
            continue

        if run is None:
            # An event for a run whose start is not in the log. Possible after
            # a rotation, and not a reason to drop everything after it.
            run = RunProjection(id=run_id, work_item_id=event.work_item_id)

        if event.kind in {
            EventKind.RUN_STATUS_CHANGED,
            EventKind.RUN_FINISHED,
            EventKind.RUN_CANCELLED,
        }:
            status = _text(payload.get("status")) or (
                "cancelled" if event.kind is EventKind.RUN_CANCELLED else None
            )
            terminal = event.kind is not EventKind.RUN_STATUS_CHANGED
            run = replace(
                run,
                status=status if status is not None else run.status,
                finished_at=event.at if terminal else run.finished_at,
            )
            continue

        stage_id = event.stage_id
        if stage_id is None:
            # A step event with no stage on it is a producer bug, and swallowing
            # it would hide the producer rather than the record.
            if event.kind.value not in unmodelled:
                unmodelled.append(f"{event.kind.value} (no stage_id)")
            continue

        attempt = _integer(payload.get("attempt")) or 1
        current = steps.get((stage_id, attempt), StepProjection(stage_id, attempt))
        if event.kind in {EventKind.STEP_STARTED, EventKind.STEP_RETRIED}:
            steps[current.key] = replace(
                current,
                status="running",
                type=_text(payload.get("type")),
                timeout_seconds=_number(payload.get("timeout_seconds")),
                started_at=event.at,
            )
        else:
            status = _text(payload.get("status"))
            steps[current.key] = replace(
                current,
                status=status,
                type=_text(payload.get("type")) or current.type,
                # A skipped stage never ran, so nothing about it finished. Every
                # event carries an instant, and for this one it is *when the
                # skip was recorded* rather than when a step ended — folding it
                # into ``finished_at`` would manufacture a fact the step row
                # correctly does not have. This is the second thing replay found
                # when it was first pointed at a real run.
                finished_at=None if status == _SKIPPED else event.at,
                error_kind=_text(payload.get("error_kind")),
            )

    return run, tuple(steps.values()), tuple(unmodelled)


def compare(
    projection: RunProjection | None,
    steps: Sequence[StepProjection],
    *,
    stored: Run | None,
    results: Sequence[StepResult],
) -> tuple[Divergence, ...]:
    """Diff a reconstruction against the source of truth, observable fields only."""
    found: list[Divergence] = []

    if projection is None or stored is None:
        if projection is not None or stored is not None:
            found.append(
                Divergence(
                    subject="run",
                    field="exists",
                    in_log="yes" if projection is not None else "no",
                    in_state="yes" if stored is not None else "no",
                )
            )
        return tuple(found)

    run_fields: tuple[tuple[str, Any, Any], ...] = (
        ("work_item_id", projection.work_item_id, stored.work_item_id),
        ("workflow", projection.workflow, stored.workflow),
        ("workflow_version", projection.workflow_version, stored.workflow_version),
        ("status", projection.status, stored.status.value),
        ("created_at", projection.created_at, stored.created_at),
        ("finished_at", projection.finished_at, stored.finished_at),
    )
    for name, in_log, in_state in run_fields:
        _differ(found, "run", name, in_log, in_state)

    from_log = {step.key: step for step in steps}
    from_state = {(result.stage_id, result.attempt): result for result in results}
    for key in sorted(from_log.keys() | from_state.keys()):
        step, result = from_log.get(key), from_state.get(key)
        label = f"step {key[0]}#{key[1]}"
        if step is None or result is None:
            # The finding this whole module is worth building for: a step that
            # is in one and not the other means a writer changed state without
            # recording it, or recorded something it did not do.
            found.append(
                Divergence(
                    subject=label,
                    field="exists",
                    in_log="yes" if step is not None else "no",
                    in_state="yes" if result is not None else "no",
                )
            )
            continue
        step_fields: tuple[tuple[str, Any, Any], ...] = (
            ("status", step.status, result.status.value),
            ("type", step.type, result.type.value),
            ("timeout_seconds", step.timeout_seconds, result.timeout_seconds),
            ("started_at", step.started_at, result.started_at),
            ("finished_at", step.finished_at, result.finished_at),
            ("error_kind", step.error_kind, None if result.error is None else result.error.kind),
        )
        for name, in_log, in_state in step_fields:
            _differ(found, label, name, in_log, in_state)
    return tuple(found)


def _differ(found: list[Divergence], subject: str, field_name: str, log: Any, state: Any) -> None:
    if log == state:
        return
    found.append(
        Divergence(subject=subject, field=field_name, in_log=_show(log), in_state=_show(state))
    )


def _show(value: Any) -> str:
    if value is None:
        return "nothing"
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    return str(value)


def _mapping(payload: Any) -> Mapping[str, Any]:
    return payload if isinstance(payload, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
