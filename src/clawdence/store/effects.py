"""Durable external effects: commit intent locally, deliver it safely later.

SQLite and an external service cannot share a transaction.  This store makes
the gap explicit: a caller first records an immutable command, then a drainer
claims and delivers it.  The state machine is generic; adapters retain
effect-specific idempotency so a crash after the remote write is harmless.

Effect bodies and provider error messages never enter the audit log.  The log
contains identifiers, kinds and attempt numbers only; the table is the source
of truth and carries the command needed for recovery.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from pydantic import JsonValue

from clawdence.domain import Actor, ActorKind, EventKind
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.errors import PermanentError, PortError, TransientError
from clawdence.store import codec
from clawdence.store.schema import iso, parse_iso, transaction

if TYPE_CHECKING:
    from clawdence.store.state import StateStore


DEFAULT_MAX_ATTEMPTS: Final = 5
DEFAULT_LEASE_SECONDS: Final = 60.0
BASE_BACKOFF_SECONDS: Final = 30.0
MAX_BACKOFF_SECONDS: Final = 60.0 * 60.0


class EffectKind(StrEnum):
    PUBLISH_PULL_REQUEST = "publish_pull_request"


class EffectState(StrEnum):
    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    PARKED = "parked"


@dataclass(frozen=True, slots=True)
class ExternalEffect:
    id: str
    idempotency_key: str
    run_id: str
    kind: str
    command: JsonValue
    state: EffectState
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    error_kind: str | None
    error_detail: str | None
    claim_owner: str | None
    claim_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None


def new_effect_id() -> str:
    return f"fx.{secrets.token_hex(8)}"


class ExternalEffects:
    """Transactional state machine for durable delivery commands."""

    __slots__ = ("_clock", "_store")

    def __init__(self, store: StateStore, *, clock: Clock = utc_now) -> None:
        self._store = store
        self._clock = clock

    def enqueue(
        self,
        *,
        effect_id: str,
        idempotency_key: str,
        run_id: str,
        kind: EffectKind | str,
        command: JsonValue,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> ExternalEffect:
        """Record an immutable command, idempotently by the caller's key."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        at = self._clock()
        encoded = codec.dumps(command)
        kind_value = str(kind)
        with transaction(self._store.connection) as connection:
            inserted = connection.execute(
                "INSERT INTO external_effects "
                "(id, idempotency_key, run_id, kind, command, state, attempts, max_attempts, "
                "next_attempt_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    effect_id,
                    idempotency_key,
                    run_id,
                    kind_value,
                    encoded,
                    EffectState.PENDING.value,
                    0,
                    max_attempts,
                    iso(at),
                    iso(at),
                    iso(at),
                ),
            )
            stored = self._by_key(idempotency_key, connection=connection)
            if stored is None:  # pragma: no cover - insert and read share a transaction
                raise LookupError(f"effect {idempotency_key!r} was not stored")
            if (
                stored.run_id != run_id
                or stored.kind != kind_value
                or stored.command != command
                or stored.max_attempts != max_attempts
            ):
                raise ValueError(f"idempotency key {idempotency_key!r} has a different effect")
            if inserted.rowcount == 1:
                self._audit(
                    EventKind.EXTERNAL_EFFECT_ENQUEUED,
                    stored,
                    at=at,
                    payload={"effect_id": stored.id, "effect_kind": stored.kind},
                )
        return stored

    def get(self, effect_id: str) -> ExternalEffect | None:
        row = self._store.connection.execute(
            "SELECT * FROM external_effects WHERE id = ?", (effect_id,)
        ).fetchone()
        return None if row is None else _from_row(row)

    def require(self, effect_id: str) -> ExternalEffect:
        found = self.get(effect_id)
        if found is None:
            raise LookupError(f"no external effect with id {effect_id!r}")
        return found

    def by_key(self, idempotency_key: str) -> ExternalEffect | None:
        return self._by_key(idempotency_key, connection=self._store.connection)

    def list(
        self,
        *,
        state: EffectState | None = None,
        run_id: str | None = None,
        limit: int | None = None,
    ) -> tuple[ExternalEffect, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        sql = "SELECT * FROM external_effects"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return tuple(_from_row(row) for row in self._store.connection.execute(sql, params))

    def due(
        self, *, at: datetime | None = None, limit: int | None = None
    ) -> tuple[ExternalEffect, ...]:
        """Effects claimable now, including abandoned expired claims."""
        now = at or self._clock()
        sql = (
            "SELECT * FROM external_effects WHERE "
            "(state = ? AND next_attempt_at <= ?) OR "
            "(state = ? AND claim_expires_at <= ?) "
            "ORDER BY next_attempt_at, created_at, id"
        )
        params: list[object] = [
            EffectState.PENDING.value,
            iso(now),
            EffectState.DELIVERING.value,
            iso(now),
        ]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return tuple(_from_row(row) for row in self._store.connection.execute(sql, params))

    def claim(
        self,
        effect_id: str,
        *,
        owner: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        at: datetime | None = None,
    ) -> ExternalEffect | None:
        """Claim one due effect. Two racing drainers cannot both win."""
        if not owner:
            raise ValueError("claim owner must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = at or self._clock()
        expires = now + timedelta(seconds=lease_seconds)
        with transaction(self._store.connection) as connection:
            row = connection.execute(
                "SELECT * FROM external_effects WHERE id = ? AND "
                "((state = ? AND next_attempt_at <= ?) OR "
                "(state = ? AND claim_expires_at <= ?))",
                (
                    effect_id,
                    EffectState.PENDING.value,
                    iso(now),
                    EffectState.DELIVERING.value,
                    iso(now),
                ),
            ).fetchone()
            if row is None:
                return None
            reclaimed = row["state"] == EffectState.DELIVERING.value
            connection.execute(
                "UPDATE external_effects SET state = ?, attempts = attempts + 1, "
                "claim_owner = ?, claim_expires_at = ?, error_kind = NULL, error_detail = NULL, "
                "updated_at = ? WHERE id = ?",
                (
                    EffectState.DELIVERING.value,
                    owner,
                    iso(expires),
                    iso(now),
                    effect_id,
                ),
            )
            claimed = self._require_in(effect_id, connection)
            self._audit(
                EventKind.EXTERNAL_EFFECT_CLAIMED,
                claimed,
                at=now,
                payload={
                    "effect_id": claimed.id,
                    "effect_kind": claimed.kind,
                    "attempt": claimed.attempts,
                    "reclaimed": reclaimed,
                },
            )
            return claimed

    def delivered(
        self, effect_id: str, *, owner: str, at: datetime | None = None
    ) -> ExternalEffect:
        now = at or self._clock()
        with transaction(self._store.connection) as connection:
            current = self._owned(effect_id, owner, connection)
            connection.execute(
                "UPDATE external_effects SET state = ?, claim_owner = NULL, "
                "claim_expires_at = NULL, error_kind = NULL, error_detail = NULL, "
                "delivered_at = ?, updated_at = ? WHERE id = ?",
                (EffectState.DELIVERED.value, iso(now), iso(now), effect_id),
            )
            stored = self._require_in(effect_id, connection)
            self._audit(
                EventKind.EXTERNAL_EFFECT_DELIVERED,
                current,
                at=now,
                payload={
                    "effect_id": stored.id,
                    "effect_kind": stored.kind,
                    "attempt": stored.attempts,
                },
            )
            return stored

    def failed(
        self,
        effect_id: str,
        *,
        owner: str,
        error: PortError,
        at: datetime | None = None,
    ) -> ExternalEffect:
        """Schedule a transient retry or park a permanent/exhausted error."""
        now = at or self._clock()
        with transaction(self._store.connection) as connection:
            current = self._owned(effect_id, owner, connection)
            transient = isinstance(error, TransientError)
            retrying = transient and current.attempts < current.max_attempts
            if retrying:
                delay = min(
                    BASE_BACKOFF_SECONDS * (2 ** max(0, current.attempts - 1)),
                    MAX_BACKOFF_SECONDS,
                )
                next_at = now + timedelta(seconds=delay)
                state = EffectState.PENDING
                event = EventKind.EXTERNAL_EFFECT_RETRY_SCHEDULED
            else:
                next_at = current.next_attempt_at
                state = EffectState.PARKED
                event = EventKind.EXTERNAL_EFFECT_PARKED
            connection.execute(
                "UPDATE external_effects SET state = ?, next_attempt_at = ?, error_kind = ?, "
                "error_detail = ?, claim_owner = NULL, claim_expires_at = NULL, updated_at = ? "
                "WHERE id = ?",
                (state.value, iso(next_at), error.kind, error.message, iso(now), effect_id),
            )
            stored = self._require_in(effect_id, connection)
            payload: dict[str, JsonValue] = {
                "effect_id": stored.id,
                "effect_kind": stored.kind,
                "attempt": stored.attempts,
                "error_kind": error.kind,
            }
            if retrying:
                payload["next_attempt_at"] = iso(next_at)
            else:
                payload["exhausted"] = transient
            self._audit(event, stored, at=now, payload=payload)
            return stored

    def park(
        self,
        effect_id: str,
        *,
        owner: str,
        error_kind: str,
        error_detail: str,
        at: datetime | None = None,
    ) -> ExternalEffect:
        return self.failed(
            effect_id,
            owner=owner,
            error=PermanentError(error_kind, error_detail),
            at=at,
        )

    def retry(self, effect_id: str, *, at: datetime | None = None) -> ExternalEffect:
        """Return a parked effect to pending; only an explicit operator calls this."""
        now = at or self._clock()
        with transaction(self._store.connection) as connection:
            current = self._require_in(effect_id, connection)
            if current.state is not EffectState.PARKED:
                raise ValueError(f"effect {effect_id!r} is {current.state.value}, not parked")
            connection.execute(
                "UPDATE external_effects SET state = ?, attempts = 0, next_attempt_at = ?, "
                "error_kind = NULL, error_detail = NULL, claim_owner = NULL, "
                "claim_expires_at = NULL, updated_at = ? WHERE id = ?",
                (EffectState.PENDING.value, iso(now), iso(now), effect_id),
            )
            stored = self._require_in(effect_id, connection)
            self._audit(
                EventKind.EXTERNAL_EFFECT_RETRIED,
                stored,
                at=now,
                payload={"effect_id": stored.id, "effect_kind": stored.kind},
            )
            return stored

    def _owned(self, effect_id: str, owner: str, connection: sqlite3.Connection) -> ExternalEffect:
        current = self._require_in(effect_id, connection)
        if current.state is not EffectState.DELIVERING or current.claim_owner != owner:
            raise ValueError(f"effect {effect_id!r} is not claimed by {owner!r}")
        return current

    def _audit(
        self,
        event: EventKind,
        effect: ExternalEffect,
        *,
        at: datetime,
        payload: JsonValue,
    ) -> None:
        self._store.audit.record(
            event,
            at=at,
            run_id=effect.run_id,
            actor=Actor(kind=ActorKind.SYSTEM, id="external-effects"),
            payload=payload,
        )

    @staticmethod
    def _by_key(idempotency_key: str, *, connection: sqlite3.Connection) -> ExternalEffect | None:
        row = connection.execute(
            "SELECT * FROM external_effects WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return None if row is None else _from_row(row)

    @staticmethod
    def _require_in(effect_id: str, connection: sqlite3.Connection) -> ExternalEffect:
        row = connection.execute(
            "SELECT * FROM external_effects WHERE id = ?", (effect_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"no external effect with id {effect_id!r}")
        return _from_row(row)


def _from_row(row: sqlite3.Row) -> ExternalEffect:
    command: Any = codec.loads(row["command"])
    return ExternalEffect(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        run_id=row["run_id"],
        kind=row["kind"],
        command=command,
        state=EffectState(row["state"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        next_attempt_at=parse_iso(row["next_attempt_at"]),
        error_kind=row["error_kind"],
        error_detail=row["error_detail"],
        claim_owner=row["claim_owner"],
        claim_expires_at=(
            None if row["claim_expires_at"] is None else parse_iso(row["claim_expires_at"])
        ),
        created_at=parse_iso(row["created_at"]),
        updated_at=parse_iso(row["updated_at"]),
        delivered_at=None if row["delivered_at"] is None else parse_iso(row["delivered_at"]),
    )
