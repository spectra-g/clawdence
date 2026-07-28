"""The audit trail, and the queue for records that could not join it.

Append-only, and **not** the source of truth (ADR-0005). That inversion is what
makes this cheap: nothing rebuilds state from these rows, so the log can be
rotated, truncated or lost without losing a run — v1's 300MB processing log
becomes a housekeeping problem rather than a correctness one.

**What goes in a payload is a security decision, and S4's answer is: metadata.**
Identifiers, statuses, attempt numbers, error *kinds*. Never step output, never
stderr, never a prompt. The reason is the obligation ADR-0005 refused to drop:
an append-only store cannot un-write a key someone pasted, and redaction at
write time is S4b's. Until that exists, the honest mitigation is for this log
not to carry the payloads worth redacting — and ``redacted`` is written
**false**, because a record that claims to have been screened when no screening
ran is worse than one that admits it. ``Redactor`` is the seam S4b fills; when
it does, the flag starts telling the truth on its own.

The dead-letter queue guards the one boundary where a record arrives *untyped*:
``submit``, for records from outside this process (S10's webhooks, S6's runner
results, and replay itself). Internal writers construct an ``Event`` and cannot
poison anything. A record that will not validate is parked with the reason
rather than dropped or raised — the audit log is not the source of truth, so a
malformed audit record must never be able to fail a run — and ``replay`` drains
the queue once the code or the record is fixed.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import JsonValue, ValidationError

from clawdence.domain import Actor, Event, EventKind
from clawdence.store import codec
from clawdence.store.schema import iso, transaction

#: How much of a validation failure is kept. Enough to recognise the fault,
#: bounded because these are stored and pydantic's messages are not short.
MAX_REASON_CHARS = 2_000


class Redactor(Protocol):
    """Screens a payload on the way in.

    Returns the payload to store and whether screening actually ran. S4b
    replaces the default with one that finds and masks secrets; everything
    about the interface is already in place so that substitution is one line.
    """

    def __call__(self, payload: JsonValue) -> tuple[JsonValue, bool]: ...


def unscreened(payload: JsonValue) -> tuple[JsonValue, bool]:
    """The default: store as given, and say so. See the module docstring."""
    return payload, False


def new_event_id() -> str:
    return f"ev.{secrets.token_hex(8)}"


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """A record that could not become an ``Event``, kept for another try."""

    id: int
    at: datetime
    origin: str
    reason: str
    body: str
    tries: int

    def decoded(self) -> JsonValue:
        """The parked record, or ``None`` if even its text was not JSON."""
        try:
            return codec.loads(self.body)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """What one drain of the queue achieved."""

    replayed: tuple[int, ...] = ()
    remaining: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.remaining


class AuditLog:
    """Appends to, and reads, the ``audit`` table."""

    __slots__ = ("_connection", "_new_id", "_redactor")

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        redactor: Redactor = unscreened,
        new_id: Callable[[], str] = new_event_id,
    ) -> None:
        self._connection = connection
        self._redactor = redactor
        self._new_id = new_id

    def append(self, event: Event) -> Event:
        """Write a record built in this process. Screens the payload first."""
        payload, screened = self._redactor(event.payload)
        stored = event.model_copy(update={"payload": payload, "redacted": screened})
        self._insert(stored)
        return stored

    def record(
        self,
        kind: EventKind,
        *,
        at: datetime,
        run_id: str | None = None,
        work_item_id: str | None = None,
        stage_id: str | None = None,
        actor: Actor | None = None,
        payload: JsonValue = None,
    ) -> Event:
        """``append`` with the id minted and the boilerplate filled in."""
        return self.append(
            Event(
                id=self._new_id(),
                kind=kind,
                at=at,
                run_id=run_id,
                work_item_id=work_item_id,
                stage_id=stage_id,
                actor=actor,
                payload=payload,
            )
        )

    def submit(
        self,
        raw: Mapping[str, Any],
        *,
        at: datetime,
        origin: str = "submit",
    ) -> Event | None:
        """Append a record that arrived from outside, or dead-letter it.

        Returns the stored event, or ``None`` if it was parked. Never raises for
        a bad record: this log is not the source of truth, and a malformed audit
        entry must not be able to take a run down with it.
        """
        try:
            event = Event.model_validate(dict(raw))
        except ValidationError as exc:
            self._park(raw, at=at, origin=origin, reason=str(exc))
            return None
        return self._store(event)

    def _store(self, event: Event) -> Event:
        try:
            return self.append(event)
        except sqlite3.IntegrityError:
            # A duplicate id means this record is already in the log. Replay is
            # meant to be safe to run twice, so that is a success, not a fault.
            return event

    def read(
        self,
        *,
        run_id: str | None = None,
        kinds: Iterable[EventKind] | None = None,
        limit: int | None = None,
    ) -> tuple[Event, ...]:
        """Records in ``seq`` order — the order they were written."""
        sql = "SELECT * FROM audit"
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if kinds is not None:
            wanted = [kind.value for kind in kinds]
            clauses.append(f"kind IN ({', '.join('?' for _ in wanted)})")
            params.extend(wanted)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._connection.execute(sql, params).fetchall()
        return tuple(codec.row_to_event(row) for row in rows)

    def dead_letters(self) -> tuple[DeadLetter, ...]:
        rows = self._connection.execute("SELECT * FROM dead_letters ORDER BY id").fetchall()
        return tuple(
            DeadLetter(
                id=row["id"],
                at=datetime.fromisoformat(row["at"]),
                origin=row["origin"],
                reason=row["reason"],
                body=row["body"],
                tries=row["tries"],
            )
            for row in rows
        )

    def replay(
        self,
        *,
        at: datetime,
        repair: Callable[[JsonValue], JsonValue] | None = None,
    ) -> ReplayReport:
        """Try every parked record again, optionally repairing it first.

        ``repair`` is the escape hatch for the case where the *record* is wrong
        rather than the code: a producer that emitted a field this build renamed
        can be fixed in one pass instead of by hand. A record that still fails
        stays parked with its try count incremented and its newest reason, so a
        queue that is not draining says why.
        """
        replayed: list[int] = []
        remaining: list[int] = []
        for letter in self.dead_letters():
            candidate = letter.decoded()
            if repair is not None:
                candidate = repair(candidate)
            # Deliberately not routed through ``submit``: parking a record that
            # is already parked would grow the queue every time it is drained.
            try:
                if not isinstance(candidate, dict):
                    raise ValueError(f"a record must be an object, not {type(candidate).__name__}")
                event = Event.model_validate(candidate)
            except (ValidationError, ValueError) as exc:
                self._retried(letter.id, at=at, reason=str(exc))
                remaining.append(letter.id)
                continue
            self._store(event)
            self._discard(letter.id)
            replayed.append(letter.id)
        return ReplayReport(replayed=tuple(replayed), remaining=tuple(remaining))

    def _insert(self, event: Event) -> None:
        sql, values = codec.insert_statement("audit", codec.event_to_row(event))
        with transaction(self._connection) as connection:
            connection.execute(sql, values)

    def _park(self, raw: object, *, at: datetime, origin: str, reason: str) -> None:
        try:
            body = codec.dumps(raw)
        except (TypeError, ValueError):
            # Not even serialisable. Keeping the repr beats keeping nothing:
            # a human still has to be able to see what arrived.
            body = json.dumps(repr(raw))
        with transaction(self._connection) as connection:
            connection.execute(
                "INSERT INTO dead_letters (at, origin, reason, body) VALUES (?, ?, ?, ?)",
                (iso(at), origin, reason[:MAX_REASON_CHARS], body),
            )

    def _discard(self, letter_id: int) -> None:
        with transaction(self._connection) as connection:
            connection.execute("DELETE FROM dead_letters WHERE id = ?", (letter_id,))

    def _retried(self, letter_id: int, *, at: datetime, reason: str) -> None:
        """A record that failed again: newest reason wins, count goes up.

        A queue that is not draining should be able to say why *now* rather than
        why it first failed, since the usual reason for replaying is that
        something was changed in between.
        """
        with transaction(self._connection) as connection:
            connection.execute(
                "UPDATE dead_letters SET tries = tries + 1, at = ?, reason = ? WHERE id = ?",
                (iso(at), reason[:MAX_REASON_CHARS], letter_id),
            )
