"""Provisional durable publication intents: execution once, delivery may retry.

A run can finish and create a local commit while the forge is unavailable. That
is not a reason to run the coding agent again: the expensive, non-idempotent
part already happened, while create-branch, push-the-same-hash and open-or-find
the pull request are deliberately idempotent. This table is the seam between
those two lifetimes.

This is the immediate single-publication bridge found while testing M1, not the
settled S4b.1 architecture. S4b.1 is the next planned step and is expected to
replace or substantially rework this into generic durable external effects with
minimal immutable commands, transient retry versus permanent parking, bounded
backoff, expiring claims, audit events and several effects per run. Keeping that
warning here matters: an integration author must not copy this table and create
one private retry queue per port merely because publication happened to expose
the deferred dual-write problem first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from clawdence.domain import Workflow, WorkItem
from clawdence.ports._common import Clock, utc_now
from clawdence.store import codec
from clawdence.store.schema import iso, parse_iso, transaction

if TYPE_CHECKING:
    from datetime import datetime

    from clawdence.store.state import StateStore


class PublicationState(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class Publication:
    run_id: str
    work_item: WorkItem
    workflow: Workflow
    repo_id: str
    branch: str
    base_commit: str
    head_commit: str
    state: PublicationState = PublicationState.PENDING
    attempts: int = 0
    last_error: str | None = None
    updated_at: datetime | None = None


class Publications:
    """The provisional bridge for branch and pull-request publication.

    S4b.1 is the next private-plan step; consult it before extending this API.
    """

    __slots__ = ("_clock", "_store")

    def __init__(self, store: StateStore, *, clock: Clock = utc_now) -> None:
        self._store = store
        self._clock = clock

    def enqueue(self, publication: Publication) -> Publication:
        """Record intent before the first forge side effect; idempotent by run."""
        at = publication.updated_at or self._clock()
        with transaction(self._store.connection) as connection:
            connection.execute(
                "INSERT INTO publications "
                "(run_id, work_item_id, repo_id, branch, base_commit, head_commit, item, state, "
                "workflow, attempts, last_error, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO NOTHING",
                (
                    publication.run_id,
                    publication.work_item.id,
                    publication.repo_id,
                    publication.branch,
                    publication.base_commit,
                    publication.head_commit,
                    codec.dumps(publication.work_item.model_dump(mode="json")),
                    publication.state.value,
                    codec.dumps(publication.workflow.model_dump(mode="json")),
                    publication.attempts,
                    publication.last_error,
                    iso(at),
                ),
            )
        stored = self.require(publication.run_id)
        immutable = (
            "work_item",
            "workflow",
            "repo_id",
            "branch",
            "base_commit",
            "head_commit",
        )
        if any(getattr(stored, field) != getattr(publication, field) for field in immutable):
            raise ValueError(f"run {publication.run_id!r} already has a different publication")
        return stored

    def get(self, run_id: str) -> Publication | None:
        row = self._store.connection.execute(
            "SELECT * FROM publications WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _from_row(row)

    def require(self, run_id: str) -> Publication:
        found = self.get(run_id)
        if found is None:  # pragma: no cover - enqueue writes and reads in one process
            raise LookupError(f"no publication for run {run_id!r}")
        return found

    def pending(self, *, limit: int | None = None) -> tuple[Publication, ...]:
        sql = "SELECT * FROM publications WHERE state = ? ORDER BY updated_at, run_id"
        params: list[object] = [PublicationState.PENDING.value]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return tuple(_from_row(row) for row in self._store.connection.execute(sql, params))

    def attempting(self, run_id: str) -> Publication:
        with transaction(self._store.connection) as connection:
            connection.execute(
                "UPDATE publications SET attempts = attempts + 1, last_error = NULL, "
                "updated_at = ? WHERE run_id = ? AND state = ?",
                (iso(self._clock()), run_id, PublicationState.PENDING.value),
            )
        return self.require(run_id)

    def failed(self, run_id: str, error: str) -> Publication:
        return self._set(run_id, PublicationState.PENDING, error)

    def refused(self, run_id: str, reason: str) -> Publication:
        return self._set(run_id, PublicationState.REFUSED, reason)

    def published(self, run_id: str) -> Publication:
        return self._set(run_id, PublicationState.PUBLISHED, None)

    def _set(self, run_id: str, state: PublicationState, error: str | None) -> Publication:
        with transaction(self._store.connection) as connection:
            connection.execute(
                "UPDATE publications SET state = ?, last_error = ?, updated_at = ? "
                "WHERE run_id = ? AND state = ?",
                (
                    state.value,
                    error,
                    iso(self._clock()),
                    run_id,
                    PublicationState.PENDING.value,
                ),
            )
        return self.require(run_id)


def _from_row(row: sqlite3.Row) -> Publication:
    return Publication(
        run_id=row["run_id"],
        work_item=WorkItem.model_validate(codec.loads(row["item"])),
        workflow=Workflow.model_validate(codec.loads(row["workflow"])),
        repo_id=row["repo_id"],
        branch=row["branch"],
        base_commit=row["base_commit"],
        head_commit=row["head_commit"],
        state=PublicationState(row["state"]),
        attempts=row["attempts"],
        last_error=row["last_error"],
        updated_at=parse_iso(row["updated_at"]),
    )
