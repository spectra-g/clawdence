"""Arrivals, and everything that happens to one after it arrives.

``ports.ingest`` says what a source of work looks like from above: hand over
what has not been acknowledged, and be told when it has. This is where an
arrival is *kept* between the process that received it and the process that will
act on it — which for the CLI adapter is not an optimisation but the whole
mechanism, because ``clawdence submit`` is one process and the thing that runs
the work is another. ``InMemoryIngest`` dedupes within a process; the CLI has no
process to dedupe within, so the guard has to be a unique constraint in a file.

**Four verbs, not one, and the reason is that a request is not a message.** v1
took Slack messages and every message was a new request, so it never had to
model what a request *does* over its life. Every source this system will speak
to does all four:

``submit``
    The first arrival, or a redelivery of one, or a re-open of one that was
    withdrawn. GitHub redelivers webhooks and an issue can be closed and
    reopened; both look identical from here and neither is new work.

``amend``
    The same request, said differently. Slack messages get edited, issue bodies
    get rewritten, and a person who mistyped a repository name fixes it. The
    identity is unchanged; the content is not.

``withdraw``
    "Never mind." Distinct from cancelling a run, because the two are answerable
    at different times by different mechanisms — see ``withdraw`` itself.

``reply``
    A follow-up in an existing conversation. The BA asking a clarifying question
    and getting an answer is one exchange, not two unrelated requests, and the
    thing that makes it one is ``SourceRef.conversation_id`` — v1's ``slackTs``,
    generalised. A reply that opened a second work item is exactly the bug.

**Amendment is inferred from content, never from a verb the source sent.** A
redelivery and an edit arrive the same way; what tells them apart is whether the
text changed. So ``submit`` compares what arrived against what is stored, with
the identity fields excluded — see ``_content`` — and only calls it an amendment
if something a human would recognise as the request is different. The inverse
mistake is the expensive one: treating every redelivery as an edit would bump
the revision on every webhook retry and re-queue work that was already running.

**An amendment after acknowledgement re-queues the item.** The alternative is
that a correction sent thirty seconds too late is silently ignored, which is the
worst of the available behaviours because the person who sent it has no way to
find out. ``acknowledged_revision`` survives the re-queue, so the record still
says which version was handed on — and consumers are already obliged to be
idempotent on ``work_item_id`` by the at-least-once contract, so seeing one
twice is a case they have.

**The work item is one JSON column, and that is not the same choice ``runs``
made.** ``codec`` writes runs and steps out column by column because they are
*queried* — the watchdog asks which steps are still running, HQ asks what is
queued. Nothing queries into a request: the questions asked here are which key,
which state, which conversation, which revision, and those five are columns. The
body is read whole or not at all, so a column per field would buy a migration
every time the domain model grows a field and answer no question.

**What goes in the audit log is the envelope, never the body.** ``audit``'s
policy is metadata only (S4), and ``raw_text`` is the single most
attacker-controlled string in the system. So the ``WORK_ITEM_RECEIVED`` payload
carries ids, source, revision and disposition, and the request itself lives in
``intake.item`` where it can be deleted.

**Attachments are dropped**, and this is where that is recorded rather than in a
field that would always be empty. Screenshots in Slack and images in issues are
real, but nothing downstream can read one — no agent step takes an image, and
the runner has no path for a binary — so carrying them would mean a column, a
blob store and a retention policy in exchange for bytes with no reader. A
``NOTE``-shaped limitation, in S9's sense: seen, and deliberately not acted on.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from clawdence.domain import Actor, ActorKind, EventKind, IngestSource, WorkItem
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.ingest import (
    MAX_REQUEST_CHARS,
    MAX_TITLE_CHARS,
    dedupe_key,
    is_self,
)
from clawdence.store import codec
from clawdence.store.errors import (
    SubmissionRejectedError,
    UnknownConversationError,
    UnknownSubmissionError,
)
from clawdence.store.schema import iso, parse_iso, transaction
from clawdence.store.state import StateStore

#: Default page size for ``collect``. Matches ``IngestPort.collect``'s default,
#: because a source that has been unreachable for a day comes back with a
#: backlog and the control plane should start working through it rather than
#: load all of it first.
DEFAULT_LIMIT: Final = 50

#: Fields that say *which request this is* rather than *what it says*. An
#: amendment may not touch them: the dedupe key would then point at something
#: else, and every downstream reference to ``work_item_id`` would be stale.
IDENTITY_FIELDS: Final = frozenset({"id", "created_at", "submitter", "source_ref"})


class ArrivalState(StrEnum):
    """Where a request is in its life.

    Three, and only ``pending`` is collectable. ``withdrawn`` is kept rather
    than deleted for the same reason a cancelled run is: "we never received it"
    and "you took it back" are different answers to the same question, and a row
    that vanished cannot tell them apart.
    """

    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    WITHDRAWN = "withdrawn"


class Disposition(StrEnum):
    """What an arrival turned out to be.

    The return value of every verb, and the thing a submitting surface prints.
    It exists because "we stored it" is not the useful answer: a script that
    resubmits on retry needs to know it did not create a second request, and a
    person who edited something already running needs to know that it was.
    """

    #: First arrival. A work item now exists that did not.
    CREATED = "created"

    #: Same key, same content. A redelivery, and nothing happened.
    DUPLICATE = "duplicate"

    #: Same key, different content. The revision bumped.
    AMENDED = "amended"

    #: Same key, arriving after a withdrawal. Closed and reopened.
    REOPENED = "reopened"

    #: Taken back before anything acted on it.
    WITHDRAWN = "withdrawn"

    #: A follow-up recorded against an existing request.
    REPLIED = "replied"


@dataclass(frozen=True, slots=True)
class Admission:
    """What intake did with an arrival, and what stands afterwards."""

    disposition: Disposition
    item: WorkItem
    state: ArrivalState
    revision: int

    #: The revision handed to the pipeline, if any. Not necessarily
    #: ``revision``: an amendment moves one and not the other.
    acknowledged_revision: int | None = None

    #: True when this arrival put a previously-acknowledged item back in the
    #: queue. The one outcome a caller must not miss, because the work already
    #: under way is on an older version of the request.
    requeued: bool = False

    @property
    def created(self) -> bool:
        return self.disposition is Disposition.CREATED

    @property
    def changed(self) -> bool:
        """Did anything about the stored request move? ``False`` for a redelivery."""
        return self.disposition is not Disposition.DUPLICATE


@dataclass(frozen=True, slots=True)
class Turn:
    """One message in a conversation that already has a request attached."""

    id: str
    dedupe_key: str
    author: str
    body: str
    at: datetime


class Intake:
    """The ``intake`` table: submit, amend, withdraw, reply, collect, acknowledge."""

    __slots__ = ("_clock", "_store")

    def __init__(self, store: StateStore, *, clock: Clock = utc_now) -> None:
        self._store = store
        self._clock = clock

    # ------------------------------------------------------------- arrivals

    def submit(self, item: WorkItem, *, at: datetime | None = None) -> Admission:
        """Take an arrival. Create, recognise, amend or reopen.

        The caller hands over a fully-formed ``WorkItem`` with an id it minted,
        because that is what ``IngestPort`` normalises to and an adapter should
        not have to ask the store what shape to build. On a repeat arrival that
        minted id is *discarded* — the stored one is the identity, and this is
        precisely why ``dedupe_key`` is the source's id and not ours.
        """
        moment = at or self._clock()
        self._screen(item)
        key = dedupe_key(item)

        with transaction(self._store.connection) as connection:
            row = self._row(key)
            if row is None:
                return self._insert(connection, item, key=key, at=moment)

            stored = _item(row)
            state = ArrivalState(row["state"])
            changed = _content(stored) != _content(item)
            if not changed and state is not ArrivalState.WITHDRAWN:
                # A redelivery. Not even a timestamp moves: touching
                # ``updated_at`` on every webhook retry would make "when did
                # this last change" mean "when did GitHub last retry".
                return _admission(row, Disposition.DUPLICATE)

            disposition = (
                Disposition.REOPENED if state is ArrivalState.WITHDRAWN else Disposition.AMENDED
            )
            return self._revise(
                connection,
                row,
                _merge(stored, item),
                disposition=disposition,
                changed=changed,
                at=moment,
            )

    def amend(self, item: WorkItem, *, at: datetime | None = None) -> Admission:
        """Replace the content of a request that already exists.

        ``submit`` would do this too. This exists for the surface that *knows*
        it is editing — ``clawdence submit --amend``, a Slack ``message_changed``
        event — because there the absence of the request is a mistake worth
        reporting rather than a first arrival to create. Amending something
        never submitted is a typo in a reference, and creating a work item out
        of it is how the typo becomes work.
        """
        moment = at or self._clock()
        self._screen(item)
        key = dedupe_key(item)

        with transaction(self._store.connection) as connection:
            row = self._require(key)
            stored = _item(row)
            state = ArrivalState(row["state"])
            changed = _content(stored) != _content(item)
            if not changed and state is not ArrivalState.WITHDRAWN:
                return _admission(row, Disposition.DUPLICATE)
            disposition = (
                Disposition.REOPENED if state is ArrivalState.WITHDRAWN else Disposition.AMENDED
            )
            return self._revise(
                connection,
                row,
                _merge(stored, item),
                disposition=disposition,
                changed=changed,
                at=moment,
            )

    def withdraw(
        self,
        key: str,
        *,
        reason: str,
        at: datetime | None = None,
    ) -> Admission:
        """Take a request back.

        **This is not a cancel, and conflating the two would hide a real
        difference.** Withdrawal is answerable here because nothing has acted on
        the request yet — the row leaves the queue and no other subsystem needs
        to be told. Once it has been acknowledged, stopping the work is a
        cancellation: it involves a run, a process, and a container, and it is
        ``store.control``'s latch rather than a state change in this table.

        So a withdrawal after acknowledgement still records — the person did ask,
        and their asking is part of the timeline — and the ``Admission`` says
        ``acknowledged_revision`` is set, which is how the caller knows to say
        "this had already been picked up" instead of "done".
        """
        moment = at or self._clock()
        with transaction(self._store.connection) as connection:
            row = self._require(key)
            connection.execute(
                "UPDATE intake SET state = ?, closed_at = ?, updated_at = ?, reason = ? "
                "WHERE dedupe_key = ?",
                (ArrivalState.WITHDRAWN.value, iso(moment), iso(moment), reason, key),
            )
            self._audit(row, Disposition.WITHDRAWN, at=moment)
        return _admission(self._require(key), Disposition.WITHDRAWN)

    def reply(
        self,
        *,
        source: IngestSource,
        conversation_id: str,
        body: str,
        author: str,
        at: datetime | None = None,
    ) -> tuple[Admission, Turn]:
        """Record a follow-up against the request that owns this conversation.

        The property being bought is a negative one: **this does not create a
        work item.** A clarification answer arriving as a fresh request is the
        failure v1 had, and it produced duplicate epics that both went through
        planning.

        Where the turn *goes* afterwards is not settled here, deliberately. The
        obvious destination for a reply to a running request is the steering
        inbox (§3.11), but nothing yet maps a work item to the run working on it
        — that is the routing S11 owns — and a reply to a request that has not
        started has no run to steer at all. So the turn is stored against the
        conversation, where ``clawdence inbox show`` reads it, and the step that
        knows about runs can forward it without this one having guessed.
        """
        moment = at or self._clock()
        text = body.strip()
        if not text:
            raise SubmissionRejectedError("a reply with nothing in it says nothing")
        if len(text) > MAX_REQUEST_CHARS:
            raise SubmissionRejectedError(
                f"this reply is {len(text)} characters and the limit is {MAX_REQUEST_CHARS}"
            )

        row = self._conversation_row(source, conversation_id)
        turn_id = f"turn.{secrets.token_hex(8)}"
        with transaction(self._store.connection) as connection:
            connection.execute(
                "INSERT INTO intake_turns (id, dedupe_key, author, body, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (turn_id, row["dedupe_key"], author, text, iso(moment)),
            )
        turn = Turn(
            id=turn_id,
            dedupe_key=row["dedupe_key"],
            author=author,
            body=text,
            at=moment,
        )
        return _admission(row, Disposition.REPLIED), turn

    # ------------------------------------------------------------- delivery

    def collect(self, *, limit: int = DEFAULT_LIMIT) -> tuple[WorkItem, ...]:
        """Unacknowledged requests, oldest first. The same ones until acknowledged."""
        rows = self._store.connection.execute(
            "SELECT * FROM intake WHERE state = ? ORDER BY seq LIMIT ?",
            (ArrivalState.PENDING.value, limit),
        ).fetchall()
        return tuple(_item(row) for row in rows)

    def acknowledge(self, *work_item_ids: str, at: datetime | None = None) -> int:
        """Mark requests as handed on. Returns how many were still outstanding.

        Keyed on ``work_item_id`` rather than on the dedupe key, because the
        caller is the pipeline and a ``WorkItem`` is what it was given. It
        records *which revision* was acknowledged, which is the fact an
        amendment later has to be compared against.
        """
        moment = at or self._clock()
        acknowledged = 0
        with transaction(self._store.connection) as connection:
            for work_item_id in work_item_ids:
                cursor = connection.execute(
                    "UPDATE intake SET state = ?, acknowledged_at = ?, updated_at = ?, "
                    "acknowledged_revision = revision WHERE work_item_id = ? AND state = ?",
                    (
                        ArrivalState.ACKNOWLEDGED.value,
                        iso(moment),
                        iso(moment),
                        work_item_id,
                        ArrivalState.PENDING.value,
                    ),
                )
                acknowledged += cursor.rowcount
        return acknowledged

    # ---------------------------------------------------------------- reads

    def get(self, key: str) -> Admission | None:
        """The current standing of one request, by dedupe key."""
        row = self._row(key)
        return None if row is None else _admission(row, Disposition.DUPLICATE)

    def for_work_item(self, work_item_id: str) -> Admission | None:
        row: sqlite3.Row | None = self._store.connection.execute(
            "SELECT * FROM intake WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        return None if row is None else _admission(row, Disposition.DUPLICATE)

    def list(
        self,
        *,
        state: ArrivalState | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> tuple[Admission, ...]:
        """Everything intake holds, newest first — the order somebody looking
        for what they just submitted wants."""
        sql = "SELECT * FROM intake"
        params: list[Any] = []
        if state is not None:
            sql += " WHERE state = ?"
            params.append(state.value)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        rows = self._store.connection.execute(sql, params).fetchall()
        return tuple(_admission(row, Disposition.DUPLICATE) for row in rows)

    def turns(self, key: str) -> tuple[Turn, ...]:
        """The conversation attached to a request, oldest first."""
        rows = self._store.connection.execute(
            "SELECT * FROM intake_turns WHERE dedupe_key = ? ORDER BY seq", (key,)
        ).fetchall()
        return tuple(
            Turn(
                id=row["id"],
                dedupe_key=row["dedupe_key"],
                author=row["author"],
                body=row["body"],
                at=parse_iso(row["at"]),
            )
            for row in rows
        )

    # -------------------------------------------------------------- private

    def _screen(self, item: WorkItem) -> None:
        """The three refusals every arrival goes through, wherever it came from."""
        if is_self(item.submitter):
            raise SubmissionRejectedError(
                "this request was submitted by clawdence itself, and taking it would "
                "start a loop: the system posts to the channels it reads from, so its "
                "own summary would become work, which would produce another summary"
            )
        if not item.raw_text.strip():
            raise SubmissionRejectedError("a request with no text in it asks for nothing")
        if len(item.raw_text) > MAX_REQUEST_CHARS:
            raise SubmissionRejectedError(
                f"this request is {len(item.raw_text)} characters and the limit is "
                f"{MAX_REQUEST_CHARS} — refused rather than truncated, because half a "
                f"request can ask for the opposite of the whole one"
            )
        if len(item.title) > MAX_TITLE_CHARS:
            raise SubmissionRejectedError(
                f"this title is {len(item.title)} characters and the limit is {MAX_TITLE_CHARS}"
            )

    def _insert(
        self,
        connection: sqlite3.Connection,
        item: WorkItem,
        *,
        key: str,
        at: datetime,
    ) -> Admission:
        connection.execute(
            "INSERT INTO intake (dedupe_key, work_item_id, source, conversation_id, state, "
            "revision, item, received_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                key,
                item.id,
                item.source_ref.source.value,
                item.source_ref.conversation_id,
                ArrivalState.PENDING.value,
                codec.dumps(item.model_dump(mode="json")),
                iso(at),
                iso(at),
            ),
        )
        row = self._require(key)
        self._audit(row, Disposition.CREATED, at=at)
        return _admission(row, Disposition.CREATED)

    def _revise(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        item: WorkItem,
        *,
        disposition: Disposition,
        changed: bool,
        at: datetime,
    ) -> Admission:
        """Write the request back, and put it back in the queue.

        Back in the queue *unconditionally*, including from ``acknowledged``.
        See the module docstring: a correction that arrives while the work is
        running is still the request, and the alternative is dropping it
        silently. ``acknowledged_revision`` is left where it was, so what was
        handed on stays readable next to what now stands.

        ``revision`` moves only when the content did. Re-opening a withdrawn
        request that nobody edited is a state change, not a new version of the
        text, and bumping it there would make the number mean two things.
        """
        connection.execute(
            f"UPDATE intake SET item = ?, revision = revision + {int(changed)}, state = ?, "  # noqa: S608
            "updated_at = ?, closed_at = NULL, reason = NULL WHERE dedupe_key = ?",
            (
                codec.dumps(item.model_dump(mode="json")),
                ArrivalState.PENDING.value,
                iso(at),
                row["dedupe_key"],
            ),
        )
        updated = self._require(row["dedupe_key"])
        self._audit(updated, disposition, at=at)
        return _admission(
            updated,
            disposition,
            requeued=ArrivalState(row["state"]) is ArrivalState.ACKNOWLEDGED,
        )

    def _audit(self, row: sqlite3.Row, disposition: Disposition, *, at: datetime) -> None:
        """Envelope only. ``raw_text`` never reaches the log — see the docstring."""
        self._store.audit.record(
            EventKind.WORK_ITEM_RECEIVED,
            at=at,
            work_item_id=row["work_item_id"],
            actor=Actor(kind=ActorKind.SYSTEM, id="intake"),
            payload={
                "disposition": disposition.value,
                "source": row["source"],
                "dedupe_key": row["dedupe_key"],
                "revision": row["revision"],
                "state": row["state"],
            },
        )

    def _row(self, key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._store.connection.execute(
            "SELECT * FROM intake WHERE dedupe_key = ?", (key,)
        ).fetchone()
        return row

    def _require(self, key: str) -> sqlite3.Row:
        row = self._row(key)
        if row is None:
            raise UnknownSubmissionError(f"nothing has been submitted under {key!r}")
        return row

    def _conversation_row(self, source: IngestSource, conversation_id: str) -> sqlite3.Row:
        """The most recent request in a conversation.

        Most recent rather than only: a conversation can carry more than one
        request over time — the same Slack thread used twice — and a reply
        belongs to the one being talked about now.
        """
        row: sqlite3.Row | None = self._store.connection.execute(
            "SELECT * FROM intake WHERE source = ? AND conversation_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (source.value, conversation_id),
        ).fetchone()
        if row is None:
            raise UnknownConversationError(
                f"no request has been submitted in {source.value} conversation "
                f"{conversation_id!r}, so there is nothing for this reply to continue"
            )
        return row


class StoreIngest:
    """``IngestPort`` over an ``Intake``. What the control plane is handed.

    Thin on purpose, and the same seam ``StoreControl`` is: everything
    interesting is in ``Intake``, and this is what means the pipeline never
    imports ``clawdence.store``. Arrival is not on it — ``submit`` and the rest
    are reached through ``intake``, because arrival has no common shape across
    sources and ``InMemoryIngest.offer`` makes the same split for the same
    reason.

    ``async`` over synchronous SQLite, as in ``StoreControl``: these are
    single-statement reads and writes against a local file, and a thread pool
    for them would buy nothing but a second place for connection affinity to go
    wrong.
    """

    __slots__ = ("_closed", "_intake")

    def __init__(self, store: StateStore, *, clock: Clock = utc_now) -> None:
        self._intake = Intake(store, clock=clock)
        self._closed = False

    @property
    def intake(self) -> Intake:
        return self._intake

    async def collect(self, *, limit: int = DEFAULT_LIMIT) -> Sequence[WorkItem]:
        return self._intake.collect(limit=limit)

    async def acknowledge(self, *item_ids: str) -> int:
        return self._intake.acknowledge(*item_ids)

    async def close(self) -> None:
        """Idempotent, and it does not close the store.

        The store outlives the adapter: the same ``StateStore`` is holding runs
        and steps, and an ingest adapter shutting it down would take the control
        plane's record with it. Whoever opened it closes it.
        """
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


def _item(row: sqlite3.Row) -> WorkItem:
    return WorkItem.model_validate(codec.loads(row["item"]))


def _content(item: WorkItem) -> dict[str, Any]:
    """What "the same request" means: everything except who and when.

    ``id`` and ``created_at`` are minted per delivery, so comparing them would
    make every redelivery an edit. ``submitter`` and ``source_ref`` are the
    identity: a request belongs to whoever made it, and an arrival that changed
    either is not a new version of this request but a different one.
    """
    data = item.model_dump(mode="json")
    return {name: value for name, value in data.items() if name not in IDENTITY_FIELDS}


def _merge(stored: WorkItem, arriving: WorkItem) -> WorkItem:
    """The stored identity, the arriving content. See ``IDENTITY_FIELDS``.

    ``source_ref`` is carried from the arrival rather than the store for one
    field only — ``url``, which a source can legitimately move — and the rest of
    it is pinned, because ``external_id`` changing under a dedupe key would mean
    the key no longer names what it points at.
    """
    source_ref = stored.source_ref.model_copy(update={"url": arriving.source_ref.url})
    return arriving.model_copy(
        update={
            "id": stored.id,
            "created_at": stored.created_at,
            "submitter": stored.submitter,
            "source_ref": source_ref,
        }
    )


def _admission(
    row: sqlite3.Row,
    disposition: Disposition,
    *,
    requeued: bool = False,
) -> Admission:
    return Admission(
        disposition=disposition,
        item=_item(row),
        state=ArrivalState(row["state"]),
        revision=row["revision"],
        acknowledged_revision=row["acknowledged_revision"],
        requeued=requeued,
    )
