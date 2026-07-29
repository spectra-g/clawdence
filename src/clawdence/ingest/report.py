"""What a submitting surface prints back.

The line worth getting right is the *disposition*, because the four outcomes a
person cares about look identical if you only report success: the request was
created, it already existed and nothing happened, it was changed, or it was
changed after something had already picked it up. A script retrying a submission
needs the second; a person who edited a running request needs the fourth, and
needs it loudly, because the work in flight is on an older version of what they
asked for.

The JSON form carries the same facts with the work item id first, because the
thing a caller does next is refer to it.
"""

from __future__ import annotations

import json

from clawdence.store.intake import Admission, ArrivalState, Disposition, Turn

_LINES = {
    Disposition.CREATED: "created",
    Disposition.DUPLICATE: "already submitted — nothing changed",
    Disposition.AMENDED: "amended",
    Disposition.REOPENED: "reopened",
    Disposition.WITHDRAWN: "withdrawn",
    Disposition.REPLIED: "reply recorded",
}


def render_text(admission: Admission) -> str:
    item = admission.item
    lines = [
        f"{_LINES[admission.disposition]}  {item.id}  rev {admission.revision}",
        f"  {item.type.value}: {item.title}",
        f"  {item.source_ref.source.value}:{item.source_ref.external_id}"
        + (f"  in {item.source_ref.conversation_id}" if item.source_ref.conversation_id else ""),
    ]

    if admission.requeued:
        # The one outcome that must not be a footnote. An amendment landing on a
        # request already handed on means something is working from the old text.
        lines.append(
            f"  ! this had already been picked up at revision "
            f"{admission.acknowledged_revision}, and is back in the queue — "
            f"whatever is working on it is on the older version"
        )
    elif (
        admission.disposition is Disposition.WITHDRAWN
        and admission.acknowledged_revision is not None
    ):
        lines.append(
            "  ! this had already been picked up, so withdrawing it takes it out of "
            "the queue but does not stop work that has started"
        )
    return "\n".join(lines)


def render_json(admission: Admission, *, turn: Turn | None = None) -> str:
    payload = {
        "work_item_id": admission.item.id,
        "disposition": admission.disposition.value,
        "state": admission.state.value,
        "revision": admission.revision,
        "acknowledged_revision": admission.acknowledged_revision,
        "requeued": admission.requeued,
        "work_item": admission.item.model_dump(mode="json"),
    }
    if turn is not None:
        payload["turn"] = {
            "id": turn.id,
            "author": turn.author,
            "at": turn.at.isoformat(),
        }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def render_listing(admissions: tuple[Admission, ...]) -> str:
    if not admissions:
        return "nothing submitted"
    return "\n".join(
        f"{admission.item.source_ref.source.value}:"
        f"{admission.item.source_ref.external_id:<24}  "
        f"{_state(admission):<16}  {admission.item.title}"
        for admission in admissions
    )


def render_detail(admission: Admission, turns: tuple[Turn, ...]) -> str:
    item = admission.item
    lines = [
        f"{item.source_ref.source.value}:{item.source_ref.external_id}",
        f"  work item  {item.id}",
        f"  state      {_state(admission)}",
        f"  type       {item.type.value}",
        f"  title      {item.title}",
        f"  submitter  {item.submitter.display_name or item.submitter.external_id}"
        f"  ({'trusted' if item.submitter.trusted else 'untrusted'})",
    ]
    if item.source_ref.conversation_id:
        lines.append(f"  conversation {item.source_ref.conversation_id}")
    if item.labels:
        lines.append(f"  labels     {', '.join(item.labels)}")
    if item.workflow_override:
        lines.append(f"  workflow   {item.workflow_override} (override)")

    lines.extend(
        ["", "  request, verbatim:", *(f"    {line}" for line in item.raw_text.split("\n"))]
    )

    if turns:
        lines.extend(["", f"  {len(turns)} follow-up(s):"])
        for turn in turns:
            stamp = turn.at.isoformat(timespec="seconds")
            lines.append(f"    {stamp}  {turn.author}")
            lines.extend(f"      {line}" for line in turn.body.split("\n"))
    return "\n".join(lines)


def _state(admission: Admission) -> str:
    """The state, with the revision gap spelled out when there is one.

    ``pending`` on a request that was acknowledged at revision 1 is not the same
    ``pending`` as one nobody has touched, and a listing that renders them the
    same hides the only interesting row on the screen.
    """
    state = admission.state
    if state is ArrivalState.PENDING and admission.acknowledged_revision is not None:
        return f"pending (was {admission.acknowledged_revision})"
    if state is ArrivalState.ACKNOWLEDGED:
        return f"picked up (r{admission.revision})"
    return state.value
