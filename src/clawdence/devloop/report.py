"""What the dev-loop commands print.

Four surfaces, one rule: **say what was not looked at.** A reset that lists what
it removed and not what it kept, a replay that reports "identical" without
mentioning the fields it cannot see, an audit view that shows twenty lines
without saying they are the last twenty — each of those is a report that reads
as more complete than it is, and every one of them is a way to debug for an hour
against a screen that is quietly answering a different question.

The JSON forms exist for the same reason the engine's does: S19's timeline and a
test would both rather assert on a structure than on a paragraph.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from clawdence.devloop.replay import UNOBSERVABLE, Replay
from clawdence.devloop.reset import Reset
from clawdence.domain import Event, Run, StepResult, StepStatus
from clawdence.store.audit import DeadLetter

#: The engine's marks, reused. A step reads the same in ``runs show`` as in a
#: live trace, which is the point of having them at all.
_MARKS = {
    StepStatus.SUCCEEDED: "ok",
    StepStatus.FAILED: "FAIL",
    StepStatus.TIMED_OUT: "TIME",
    StepStatus.SKIPPED: "skip",
    StepStatus.PENDING: "....",
    StepStatus.RUNNING: "....",
    StepStatus.AWAITING_APPROVAL: "gate",
    StepStatus.CANCELLED: "canc",
}


# ------------------------------------------------------------------- reset


def render_reset(result: Reset) -> str:
    verb = "would remove" if result.dry_run else "removed"
    lines: list[str] = []
    if result.abandoned:
        # First, and not a footnote. Under ``--force`` this is the sentence that
        # explains whatever happens to those runs next.
        lines.append(
            f"{'would abandon' if result.dry_run else 'abandoned'} "
            f"{len(result.abandoned)} run(s) that were still running: "
            f"{', '.join(result.abandoned)}"
        )
    for table, count in result.rows.items():
        lines.append(f"{verb} {count} row(s) from {table}")
    if result.kept_inbox:
        lines.append("kept the inbox — submitted requests were not cleared")
    if result.requeued:
        lines.append(
            f"{'would put' if result.dry_run else 'put'} {result.requeued} acknowledged "
            f"request(s) back in the queue — the runs that had picked them up are gone"
        )
    for name in result.debris.containers:
        lines.append(f"{verb} container {name}")
    for path in (*result.debris.worktrees, *result.debris.caches):
        lines.append(f"{verb} {path}")
    for path in result.debris.failed:
        lines.append(f"could not remove {path}")
    if not result:
        lines.append("already clean")
    return "\n".join(lines)


# ------------------------------------------------------------------ replay


def render_replay(result: Replay) -> str:
    lines = [
        f"run {result.run_id}  {result.folded} of {result.events} event(s) folded",
        "",
    ]
    run = result.run
    if run is None:
        lines.append("  the log has nothing for this run")
    else:
        lines.append(f"  workflow    {run.workflow}@{run.workflow_version}")
        lines.append(f"  work item   {run.work_item_id}")
        lines.append(f"  status      {run.status}")
        lines.append(f"  started     {_stamp(run.created_at)}")
        lines.append(f"  finished    {_stamp(run.finished_at)}")
        if run.starts > 1:
            # Something state cannot say: the row has one status column, so a
            # run that crashed and resumed looks like a run that did not.
            lines.append(f"  opened      {run.starts} times — this run was resumed")

    if result.steps:
        lines.append("")
        for step in result.steps:
            lines.append(
                f"  {step.status or '?':<10} {step.label}"
                f"{'' if step.type is None else f' [{step.type}]'}"
                f"{'' if step.error_kind is None else f'  {step.error_kind}'}"
            )

    lines.append("")
    if result.truncated:
        lines.append(
            "not compared: only part of the log was folded, and the stored state is "
            "the end of the run — every later event would read as a divergence"
        )
    elif result.divergences:
        lines.append(f"{len(result.divergences)} divergence(s) from the stored state:")
        lines.extend(f"  {divergence.describe()}" for divergence in result.divergences)
    else:
        lines.append("the log and the stored state agree")

    if result.unmodelled:
        lines.append(
            f"not folded: {', '.join(result.unmodelled)} — this reconstruction is "
            f"missing whatever those record"
        )
    if not result.truncated:
        lines.append("not carried by the log, so not compared: " + ", ".join(UNOBSERVABLE))
    return "\n".join(lines)


def render_replay_json(result: Replay) -> str:
    run = result.run
    payload: dict[str, Any] = {
        "run_id": result.run_id,
        "events": result.events,
        "folded": result.folded,
        "truncated": result.truncated,
        "agrees": result.agrees,
        "run": None
        if run is None
        else {
            "id": run.id,
            "work_item_id": run.work_item_id,
            "workflow": run.workflow,
            "workflow_version": run.workflow_version,
            "status": run.status,
            "created_at": _iso(run.created_at),
            "finished_at": _iso(run.finished_at),
            "starts": run.starts,
        },
        "steps": [
            {
                "stage_id": step.stage_id,
                "attempt": step.attempt,
                "status": step.status,
                "type": step.type,
                "timeout_seconds": step.timeout_seconds,
                "started_at": _iso(step.started_at),
                "finished_at": _iso(step.finished_at),
                "error_kind": step.error_kind,
            }
            for step in result.steps
        ],
        "divergences": [
            {
                "subject": divergence.subject,
                "field": divergence.field,
                "in_log": divergence.in_log,
                "in_state": divergence.in_state,
            }
            for divergence in result.divergences
        ],
        "unmodelled": list(result.unmodelled),
        "unobservable": list(UNOBSERVABLE),
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


# ------------------------------------------------------------------- audit


def render_events(events: Sequence[Event], *, tail: bool = False) -> str:
    """One line per record, oldest first.

    ``tail`` is stated in the output rather than assumed, because "the last
    twenty" and "the first twenty" look identical on the screen and mean
    opposite things about what you are not seeing.
    """
    if not events:
        return "no audit records match"
    lines = [
        f"{event.at.isoformat(timespec='seconds')}  {event.kind.value:<22} "
        f"{_subject(event):<28}  {_actor(event)}{_payload(event)}"
        for event in events
    ]
    if tail:
        lines.append(f"\n(the {len(events)} most recent matching records)")
    return "\n".join(lines)


def render_events_json(events: Sequence[Event]) -> str:
    return json.dumps(
        [event.model_dump(mode="json") for event in events],
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )


def render_dead_letters(letters: Sequence[DeadLetter]) -> str:
    if not letters:
        return "no records are parked"
    lines: list[str] = []
    for letter in letters:
        lines.append(
            f"#{letter.id}  {letter.at.isoformat(timespec='seconds')}  from {letter.origin}  "
            f"({letter.tries} tr{'y' if letter.tries == 1 else 'ies'})"
        )
        # The newest reason, which is the one that matters: the usual cause of a
        # replay is that something was changed in between.
        lines.append(f"    {letter.reason.splitlines()[0] if letter.reason else 'no reason given'}")
    lines.append("\n`clawdence runs recover` tries them again.")
    return "\n".join(lines)


# --------------------------------------------------------------------- run


def render_run(run: Run, steps: Sequence[StepResult], *, events: int) -> str:
    lines = [
        f"run {run.id}  workflow {run.workflow}@{run.workflow_version}",
        f"work item {run.work_item_id}  status {run.status.value}",
        f"started {run.created_at.isoformat(timespec='seconds')}  "
        f"{_span(run.created_at, run.finished_at or run.updated_at)}"
        f"{'' if run.finished_at else ' (unfinished)'}",
        "",
    ]
    for step in steps:
        attempt = f" (attempt {step.attempt})" if step.attempt > 1 else ""
        duration = _span(step.started_at, step.finished_at)
        lines.append(
            f"  {_MARKS[step.status]:>4}  {step.stage_id}  [{step.type.value}]{attempt}"
            f"{'' if duration == '-' else f'  {duration}'}"
        )
        if step.error is not None:
            lines.append(f"        {step.error.kind}: {step.error.message}")
    if not steps:
        lines.append("  no steps recorded")
    lines.append("")
    lines.append(f"{events} audit record(s) — `clawdence audit --run {run.id}`")
    return "\n".join(lines)


def render_run_json(run: Run, steps: Sequence[StepResult], *, events: int) -> str:
    return json.dumps(
        {
            "run": run.model_dump(mode="json"),
            "steps": [step.model_dump(mode="json") for step in steps],
            "events": events,
        },
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )


# ------------------------------------------------------------------ shared


def _subject(event: Event) -> str:
    if event.stage_id is not None:
        return event.stage_id
    return event.work_item_id or event.run_id or ""


def _actor(event: Event) -> str:
    if event.actor is None:
        return "-"
    return f"{event.actor.kind.value}:{event.actor.id or '?'}"


def _payload(event: Event) -> str:
    """The payload, compact and on one line.

    Metadata only by policy (S4), so this is short by construction and there is
    nothing to truncate — if that ever stops being true, the log has started
    carrying something it should not, and a viewer that hid it would be the
    reason nobody noticed.
    """
    if event.payload is None:
        return ""
    return "  " + json.dumps(
        event.payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _stamp(moment: datetime | None) -> str:
    return "-" if moment is None else moment.isoformat(timespec="seconds")


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _span(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "-"
    return f"{(end - start).total_seconds():.2f}s"
