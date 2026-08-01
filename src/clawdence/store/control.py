"""The steering inbox and the cancel latch — §3.11, made durable.

``ports.control`` says what a live run can be told; this is where the telling is
kept between the process that says it and the process that is running the agent.
Both are here rather than in two modules because they are the same fact from two
angles — a table of things the outside world has said to a run — and because the
runner reads them in one poll.

**The delivery lifecycle, and why it never goes backwards.** A message is
``unread`` until a run claims it, ``delivered`` once it has been, and ``failed``
if it was still open when the run it was addressed to stopped being able to read
it. There is no path from ``delivered`` back to ``unread``, and that is the
decision the whole design rests on:

- A steering message is an *instruction*. Delivering it twice is following it
  twice, which for "revert the change you just made" is destructive in a way
  that losing it is not.
- The window in which a message can be lost is one poll wide: the control plane
  has to die between claiming it and writing it into the worktree. If it does,
  the message is ``failed`` with a reason, so the person who sent it can see
  that it never arrived — which is the property that makes at-most-once
  acceptable rather than merely cheaper.
- ``unread`` messages *do* survive a crash and are delivered when the run
  resumes, because nobody has seen them and the reason to be careful does not
  apply. This is the half of "survives a crash" that matters.

``abandon`` and ``close`` are what keep the middle state from being permanent.
The ledger calls ``abandon`` when it resumes a run — the process those messages
went to is gone, so they are closed out as ``failed`` rather than left claiming
to be in flight — and ``close`` when the run finishes, which fails everything
still open including the never-delivered.

**Cancellation is a latch, not a queue.** One row per run, ``run_id`` as the
primary key, first writer wins. A second request to stop the same run is the
same request; recording it twice would produce a timeline in which a run was
cancelled by two people and make "who stopped this" unanswerable. It is
monotone, which is why it needs no version check for the same reason
``touch_run`` does not: there is no lost update to protect against when the only
transition is absent → present.

What is deliberately *not* here is the operator-facing verb. Sending a message
and asking for a stop are authorised actions and S17b owns the surface for them;
this is the plumbing underneath, and it makes no decision about who may call it.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime
from enum import StrEnum
from typing import Final

from clawdence.domain import Actor, ActorKind, EventKind
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.control import (
    MAX_STEERING_CHARS,
    Cancellation,
    Signal,
    Steer,
)
from clawdence.store.errors import MessageRejectedError, UnknownRunError
from clawdence.store.schema import iso, parse_iso, transaction
from clawdence.store.state import StateStore

#: Most messages a single poll hands over. A bound rather than a policy: an
#: operator who queued forty things while a run was between polls gets them in
#: claim order across a few polls instead of forty files landing at once, and
#: the agent's next turn sees a readable directory either way.
MAX_CLAIM: Final = 8

#: The actor a steering message or a cancel is recorded against when the caller
#: names nobody. ``HUMAN`` because both verbs exist for a person to use; a
#: watchdog that asks for a cancel says so (see ``watchdog._ask_to_stop``).
DEFAULT_SENDER: Final = "operator"


class MessageState(StrEnum):
    """Where a message is in its life. See the module docstring."""

    UNREAD = "unread"
    DELIVERED = "delivered"
    FAILED = "failed"


class SteeringMessage:
    """One row of ``steering``, as the store sees it.

    A plain class rather than a domain model on purpose. Nothing outside the
    control plane ever receives one of these — the runner gets a ``Steer``, which
    is the port's type and carries only what an agent needs — so putting it in
    ``domain`` would export an internal table's shape as a published contract and
    grow ``schemas/`` by a file that describes a queue.
    """

    __slots__ = (
        "body",
        "closed_at",
        "created_at",
        "delivered_at",
        "id",
        "ordinal",
        "priority",
        "reason",
        "run_id",
        "sender",
        "seq",
        "state",
    )

    def __init__(self, row: sqlite3.Row) -> None:
        self.seq: int = row["seq"]
        self.id: str = row["id"]
        self.run_id: str = row["run_id"]
        self.priority: int = row["priority"]
        self.state = MessageState(row["state"])
        self.body: str = row["body"]
        self.sender: str = row["sender"]
        self.created_at = parse_iso(row["created_at"])
        self.delivered_at = _maybe(row["delivered_at"])
        self.closed_at = _maybe(row["closed_at"])
        self.ordinal: int | None = row["ordinal"]
        self.reason: str | None = row["reason"]

    def steer(self) -> Steer:
        """The port's view: what the agent is told, and nothing else."""
        return Steer(
            id=self.id,
            body=self.body,
            priority=self.priority,
            sender=self.sender,
            ordinal=self.ordinal or 0,
            at=self.created_at,
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"SteeringMessage(id={self.id!r}, state={self.state.value!r})"


class Inbox:
    """Per-run steering messages, with the delivery lifecycle attached."""

    __slots__ = ("_store",)

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def send(
        self,
        run_id: str,
        body: str,
        *,
        at: datetime,
        priority: int = 0,
        sender: str = DEFAULT_SENDER,
    ) -> SteeringMessage:
        """Queue something for a run to pick up on its next turn.

        Refuses a run that does not exist rather than letting the foreign key
        say so: "no run with id …" is what the operator needs to read, and a
        constraint violation from two layers down is not it.
        """
        text = body.strip()
        if not text:
            raise MessageRejectedError("a steering message with nothing in it says nothing")
        if len(text) > MAX_STEERING_CHARS:
            raise MessageRejectedError(
                f"this message is {len(text)} characters and the limit is "
                f"{MAX_STEERING_CHARS} — it is pasted into the agent's context whole, and a "
                f"truncated instruction can mean the opposite of the one that was sent"
            )
        self._store.require_run(run_id)

        message_id = f"st.{secrets.token_hex(8)}"
        with transaction(self._store.connection) as connection:
            connection.execute(
                "INSERT INTO steering (id, run_id, priority, state, body, sender, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, run_id, priority, MessageState.UNREAD.value, text, sender, iso(at)),
            )
        return self._require(message_id)

    def pending(self, run_id: str, *, limit: int = MAX_CLAIM) -> tuple[SteeringMessage, ...]:
        """What the next claim would take, without taking it.

        The same detect/recover split the watchdog has, and for the same reason:
        an operator asking what is queued for a run should not have to deliver it
        to find out.
        """
        rows = self._store.connection.execute(
            "SELECT * FROM steering WHERE run_id = ? AND state = ? "
            "ORDER BY priority DESC, seq LIMIT ?",
            (run_id, MessageState.UNREAD.value, limit),
        ).fetchall()
        return tuple(SteeringMessage(row) for row in rows)

    def claim(
        self,
        run_id: str,
        *,
        at: datetime,
        limit: int = MAX_CLAIM,
    ) -> tuple[SteeringMessage, ...]:
        """Take what is waiting, and record it as delivered in the same breath.

        One transaction covering the read and the write, so two processes racing
        the same inbox cannot both take the same message. The ordinal is assigned
        here because this is the moment the order exists: it is chosen by
        priority, which is not knowable at send time.
        """
        with transaction(self._store.connection) as connection:
            waiting = self.pending(run_id, limit=limit)
            if not waiting:
                return ()
            next_ordinal = self._next_ordinal(run_id)
            claimed: list[str] = []
            for offset, message in enumerate(waiting):
                connection.execute(
                    "UPDATE steering SET state = ?, delivered_at = ?, ordinal = ? WHERE id = ?",
                    (MessageState.DELIVERED.value, iso(at), next_ordinal + offset, message.id),
                )
                claimed.append(message.id)
        return tuple(self._require(message_id) for message_id in claimed)

    def abandon(self, run_id: str, *, at: datetime, reason: str) -> int:
        """Close out messages handed to a process that is gone.

        ``delivered`` only. An ``unread`` message has been seen by nobody, and a
        resumed run is exactly the reader it was waiting for.
        """
        return self._fail(run_id, at=at, reason=reason, states=(MessageState.DELIVERED,))

    def close(self, run_id: str, *, at: datetime, reason: str) -> int:
        """The run is over; nothing will read this inbox again."""
        return self._fail(
            run_id,
            at=at,
            reason=reason,
            states=(MessageState.UNREAD, MessageState.DELIVERED),
        )

    def messages_for(self, run_id: str) -> tuple[SteeringMessage, ...]:
        """Everything ever sent to this run, oldest first."""
        rows = self._store.connection.execute(
            "SELECT * FROM steering WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return tuple(SteeringMessage(row) for row in rows)

    def get(self, message_id: str) -> SteeringMessage | None:
        row = self._store.connection.execute(
            "SELECT * FROM steering WHERE id = ?", (message_id,)
        ).fetchone()
        return None if row is None else SteeringMessage(row)

    def _fail(
        self,
        run_id: str,
        *,
        at: datetime,
        reason: str,
        states: tuple[MessageState, ...],
    ) -> int:
        placeholders = ", ".join("?" * len(states))
        with transaction(self._store.connection) as connection:
            cursor = connection.execute(
                f"UPDATE steering SET state = ?, closed_at = ?, reason = ? "  # noqa: S608
                f"WHERE run_id = ? AND state IN ({placeholders})",
                (
                    MessageState.FAILED.value,
                    iso(at),
                    reason,
                    run_id,
                    *(state.value for state in states),
                ),
            )
        return int(cursor.rowcount)

    def _next_ordinal(self, run_id: str) -> int:
        highest: int | None = self._store.connection.execute(
            "SELECT MAX(ordinal) FROM steering WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        return (highest or 0) + 1

    def _require(self, message_id: str) -> SteeringMessage:
        message = self.get(message_id)
        if message is None:  # pragma: no cover - it was written in this transaction
            raise MessageRejectedError(f"message {message_id!r} vanished after it was written")
        return message


class Cancellations:
    """One outstanding request to stop each run. First writer wins."""

    __slots__ = ("_store",)

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def request(
        self,
        run_id: str,
        *,
        at: datetime,
        reason: str,
        requested_by: str = DEFAULT_SENDER,
        actor_kind: ActorKind = ActorKind.HUMAN,
    ) -> Cancellation:
        """Ask for a run to stop. Idempotent, and audited once.

        ``INSERT OR IGNORE`` rather than a read-then-write: the second caller is
        not a conflict to resolve, it is somebody asking for something that has
        already been asked for, and the answer they want is the request that is
        actually outstanding.

        The audit entry is written only for the request that won, so a timeline
        says one run was cancelled once, by whoever got there first.
        """
        self._store.require_run(run_id)
        with transaction(self._store.connection) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO cancellations "
                "(run_id, requested_at, requested_by, reason) VALUES (?, ?, ?, ?)",
                (run_id, iso(at), requested_by, reason),
            )
            if cursor.rowcount == 1:
                self._store.audit.record(
                    EventKind.RUN_CANCELLED,
                    at=at,
                    run_id=run_id,
                    actor=Actor(kind=actor_kind, id=requested_by),
                    payload={"reason": reason, "requested_by": requested_by},
                )
        pending = self.pending(run_id)
        if pending is None:  # pragma: no cover - written in the transaction above
            raise UnknownRunError(f"the cancellation for {run_id!r} vanished after it was written")
        return pending

    def pending(self, run_id: str) -> Cancellation | None:
        """The outstanding request, acknowledged or not.

        Acknowledgement does not clear it. A cancel that has reached the runner
        is still the reason that run is stopping, and a reader that saw the
        request disappear the moment it was picked up would have to guess.
        """
        row = self._store.connection.execute(
            "SELECT * FROM cancellations WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return Cancellation(
            run_id=row["run_id"],
            reason=row["reason"],
            requested_by=row["requested_by"],
            at=parse_iso(row["requested_at"]),
        )

    def acknowledge(self, run_id: str, *, at: datetime) -> bool:
        """Record that a runner has seen it. ``False`` if it already had.

        This is what turns "nobody picked the cancel up" into a visible state:
        a request with no acknowledgement after a minute means no process is
        attending that run, which is a different problem from a run refusing to
        die.
        """
        with transaction(self._store.connection) as connection:
            cursor = connection.execute(
                "UPDATE cancellations SET acknowledged_at = ? "
                "WHERE run_id = ? AND acknowledged_at IS NULL",
                (iso(at), run_id),
            )
        return cursor.rowcount == 1

    def clear(self, run_id: str) -> bool:
        """Drop the outstanding request. ``True`` if there was one.

        Called when a run is resumed, and only then. Without it a stop that has
        already been honoured is still sitting there when somebody runs
        ``clawdence run --resume``, and the resumed run polls once and dies —
        which reads as the resume being broken rather than as an old row being
        obeyed twice. A resume is a *new decision to run this*, so the previous
        incarnation's stop does not carry over.

        Nothing is lost by dropping it: the ``RUN_CANCELLED`` audit entry is
        what the timeline is read from, and this table only ever held the
        request that is currently outstanding.
        """
        with transaction(self._store.connection) as connection:
            cursor = connection.execute("DELETE FROM cancellations WHERE run_id = ?", (run_id,))
        return cursor.rowcount == 1

    def acknowledged_at(self, run_id: str) -> datetime | None:
        row = self._store.connection.execute(
            "SELECT acknowledged_at FROM cancellations WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _maybe(row["acknowledged_at"])


class StoreControl:
    """``ControlPort`` over a ``StateStore``. What the runner is handed.

    Thin on purpose: everything interesting is in ``Inbox`` and
    ``Cancellations``, and this is the seam that means the runner never imports
    either — the same relationship ``SqliteLedger`` has with ``engine.Ledger``.

    ``async`` methods over synchronous SQLite, which is not an oversight. The
    calls are single-statement reads and writes against a local file with a
    5-second busy timeout, and the alternative — a thread pool for two
    statements every three seconds — buys nothing except a second place for the
    connection's thread affinity to go wrong. If a future store is genuinely
    remote, the signature is already right.
    """

    __slots__ = ("_cancellations", "_clock", "_inbox", "_limit", "_store")

    def __init__(
        self,
        store: StateStore,
        *,
        limit: int = MAX_CLAIM,
        clock: Clock = utc_now,
    ) -> None:
        self._store = store
        self._inbox = Inbox(store)
        self._cancellations = Cancellations(store)
        self._limit = limit
        self._clock = clock

    @property
    def inbox(self) -> Inbox:
        return self._inbox

    @property
    def cancellations(self) -> Cancellations:
        return self._cancellations

    async def poll(self, run_id: str) -> Signal:
        cancel = self._cancellations.pending(run_id)
        if cancel is not None:
            # Nothing is claimed alongside a stop. A message delivered in the
            # same poll that ends the run would be marked delivered and never
            # read, which is the one outcome the lifecycle is meant to make
            # visible rather than manufacture.
            self._cancellations.acknowledge(run_id, at=self._clock())
            return Signal(cancel=cancel)
        claimed = self._inbox.claim(run_id, at=self._clock(), limit=self._limit)
        return Signal(messages=tuple(message.steer() for message in claimed))

    async def heartbeat(self, run_id: str, *, at: datetime) -> None:
        self._store.touch_run(run_id, at=at)


def _maybe(value: str | None) -> datetime | None:
    return None if value is None else parse_iso(value)
