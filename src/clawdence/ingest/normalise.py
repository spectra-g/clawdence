"""Turning what a source hands you into a ``WorkItem``.

One function, and the interesting part of it is everything it refuses to do.

**The envelope is normalised; the content is not.** v1 routed repositories off
the BA's rewritten title and the rewrite dropped product names — the
``slackMessageRaw`` lesson, and the reason ``raw_text`` is stored byte for byte.
Nothing here summarises, reflows, strips markup, expands a mention or trims
anything except the outer whitespace a shell adds. A normaliser that "cleans up"
input is a normaliser that has silently answered a question the reader was going
to ask.

**A derived title is a restatement, not a summary.** With no title given, the
first non-empty line becomes one — that is a *selection* from the text, which
survives the rule above, where "summarise this into a title" would not. If the
line is longer than a title may be it is cut at a word boundary with an ellipsis,
which is visibly truncated rather than plausibly complete.

**The type defaults to the one that claims least.** v1 modelled Epic→Story only,
so every request became an epic and went through full planning whether it needed
it or not. Triage (S11) is what decides properly; until it exists the default is
``task``, because a task that turns out to be an epic costs a reclassification
and an epic that turns out to be a task costs a planning pipeline.

**Nothing here fills in ``repos``.** Repository routing reads ``raw_text`` and is
S11's; a guess made at ingestion time would be committed to the record before
anything had looked at the repositories, and the field exists precisely so that
the routing decision is visible as its own event.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import datetime

from clawdence.domain import (
    IngestSource,
    SourceRef,
    Submitter,
    WorkItem,
    WorkItemType,
)
from clawdence.ports.ingest import MAX_TITLE_CHARS

#: What a request is until triage says otherwise. See the module docstring.
DEFAULT_TYPE = WorkItemType.TASK

#: Marks a title this module cut rather than one somebody wrote that length.
ELLIPSIS = "…"


class NormaliseError(ValueError):
    """The arrival cannot be made into a work item.

    Only for what is missing or unusable in the *envelope*. Everything about
    policy — is this person allowed, is this too much, is this our own voice —
    is intake's, so that one refusal set covers every adapter rather than the
    CLI having its own.
    """


def normalise(
    *,
    source: IngestSource,
    external_id: str,
    raw_text: str,
    submitter: Submitter,
    at: datetime,
    title: str | None = None,
    item_type: WorkItemType = DEFAULT_TYPE,
    conversation_id: str | None = None,
    url: str | None = None,
    labels: Sequence[str] = (),
    workflow_override: str | None = None,
    work_item_id: str | None = None,
) -> WorkItem:
    """Build the ``WorkItem`` every adapter produces.

    ``work_item_id`` is minted here unless given. It is *discarded* on any
    arrival that turns out to be a redelivery — intake keeps the id it assigned
    first — so minting one per delivery is safe, and it is what lets an adapter
    build a complete item before knowing whether the request is new.
    """
    text = raw_text.strip()
    if not text:
        raise NormaliseError("there is no request here — the text is empty")
    if not external_id.strip():
        raise NormaliseError(
            "this arrival has no source id, and without one a redelivery of it "
            "would become a second work item"
        )

    heading = (title or "").strip() or _first_line(text)
    if not heading:
        raise NormaliseError("no title was given and the text has no first line to take one from")

    return WorkItem(
        id=work_item_id or f"wi.{secrets.token_hex(8)}",
        type=item_type,
        title=heading,
        # Not ``text``. The stripped copy is what the title was taken from; what
        # is stored is what arrived, because the reader that matters most —
        # repository routing — is looking for product names, and leading
        # whitespace is not ours to decide is meaningless.
        raw_text=raw_text,
        submitter=submitter,
        source_ref=SourceRef(
            source=source,
            external_id=external_id.strip(),
            conversation_id=conversation_id,
            url=url,
        ),
        labels=tuple(labels),
        workflow_override=workflow_override,
        created_at=at,
    )


def _first_line(text: str) -> str:
    """The first non-empty line, cut to a title's length if it is longer.

    Cut at a word boundary and marked. A hard cut mid-word reads as corruption
    and an unmarked one reads as a title somebody chose, which is worse: it
    would be the string on the branch and the pull request.
    """
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if len(candidate) <= MAX_TITLE_CHARS:
            return candidate
        head = candidate[: MAX_TITLE_CHARS - len(ELLIPSIS)]
        cut, _, _ = head.rpartition(" ")
        return (cut or head).rstrip() + ELLIPSIS
    return ""
