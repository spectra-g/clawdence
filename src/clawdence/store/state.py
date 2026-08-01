"""The source of truth: ``runs`` and ``steps``.

Everything the system knows about what has happened is read from these two
tables. Nothing is replayed to get here (ADR-0005), so crash-resume is a
``SELECT``, idempotency is a unique constraint, and the watchdog's input is a
column rather than a reconstruction.

The write model is **optimistic concurrency on the run aggregate**. A run row
carries a ``version``; a writer reads it, computes the new state, and writes
conditionally on that version still being current. A loser retries against what
it now sees rather than overwriting it. ADR-0005 deferred this decision on the
grounds that M1 executes one run at a time and recorded that S7's scheduler must
not be built until it is settled — so it is settled here, at the cost of a
column and a loop, rather than left for the step that would be building on top
of it. The forced-interleaving test is the part that proves it: "it worked once"
is not evidence about concurrency.

Two write paths for a step, and the difference is the point. ``start_step``
inserts and *fails* on collision, because a second write of the same
(run, stage, attempt) is a redelivered dispatch and letting it through is v1's
duplicate-event bug. ``finish_step`` writes over the row its own start created,
because a result superseding its own in-flight record is the normal case.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

from pydantic import JsonValue

from clawdence.domain import Run, RunStatus, StepResult, StepStatus
from clawdence.store import codec
from clawdence.store.audit import AuditLog, Redactor
from clawdence.store.errors import (
    ConcurrentUpdateError,
    DuplicateAttemptError,
    UnknownRunError,
)
from clawdence.store.redaction import redact
from clawdence.store.schema import connect, iso, transaction

#: How many times a writer re-reads and retries before declaring real
#: contention. Generous: each retry is a read and a conditional write against a
#: local file, and the alternative is failing a run over a lock.
OPTIMISTIC_RETRIES: Final = 8


def _no_window() -> None:
    """The default conflict window: nothing happens in it."""


class StateStore:
    """Runs, steps, and the audit log that describes them."""

    __slots__ = ("_audit", "_connection", "_redactor", "_window")

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        redactor: Redactor = redact,
        conflict_window: Callable[[], None] = _no_window,
    ) -> None:
        """
        ``conflict_window`` is called between the read and the conditional write
        of ``update_run``. It exists so a test can force the interleaving that
        optimistic concurrency is supposed to survive — the race is otherwise
        unreachable from a single process, and an unreachable race is one nobody
        has actually tested.
        """
        self._connection = connection
        self._redactor = redactor
        self._audit = AuditLog(connection, redactor=redactor)
        self._window = conflict_window

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        redactor: Redactor = redact,
        conflict_window: Callable[[], None] = _no_window,
    ) -> Self:
        return cls(connect(path), redactor=redactor, conflict_window=conflict_window)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def audit(self) -> AuditLog:
        return self._audit

    @property
    def connection(self) -> sqlite3.Connection:
        """For ``transaction(...)``, so a caller can make writes land together."""
        return self._connection

    def screen(self, value: JsonValue) -> JsonValue:
        """Screen a JSON value before another store component persists it."""
        screened, _ = self._redactor(value)
        return screened

    def screen_text(self, value: str) -> str:
        """The text-specialised form, with a defensive contract check."""
        screened = self.screen(value)
        if not isinstance(screened, str):
            raise TypeError("a state-store redactor must return text for text input")
        return screened

    # ------------------------------------------------------------------ runs

    def create_run(self, run: Run) -> Run:
        sql, values = codec.insert_statement("runs", codec.run_to_row(run))
        with transaction(self._connection) as connection:
            connection.execute(sql, values)
        return run

    def get_run(self, run_id: str) -> Run | None:
        row = self._run_row(run_id)
        return None if row is None else codec.row_to_run(row)

    def require_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise UnknownRunError(f"no run with id {run_id!r}")
        return run

    def list_runs(
        self,
        *,
        status: RunStatus | None = None,
        limit: int | None = None,
    ) -> tuple[Run, ...]:
        """Newest first — the order someone looking for their run wants."""
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status.value)
        sql += " ORDER BY created_at DESC, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return tuple(
            codec.row_to_run(row) for row in self._connection.execute(sql, params).fetchall()
        )

    def update_run(
        self,
        run_id: str,
        mutate: Callable[[Run], Run],
        *,
        retries: int = OPTIMISTIC_RETRIES,
    ) -> Run:
        """Read, mutate, write — or start over if someone else got there first.

        ``mutate`` is called with the *current* stored run and may be called
        more than once, so it has to be a function of what it is given rather
        than of what the caller read earlier. That constraint is the whole
        discipline of optimistic concurrency, and writing it as a callback is
        how the interface enforces it instead of documenting it.
        """
        for _ in range(retries):
            row = self._run_row(run_id)
            if row is None:
                raise UnknownRunError(f"no run with id {run_id!r}")
            updated = mutate(codec.row_to_run(row))

            self._window()

            row_values = codec.run_to_row(updated)
            del row_values["id"]
            sql, values = codec.update_statement(
                "runs",
                row_values,
                where="id = ? AND version = ?",
                also_set=", version = version + 1",
            )
            with transaction(self._connection) as connection:
                cursor = connection.execute(sql, (*values, run_id, row["version"]))
            if cursor.rowcount == 1:
                return updated
        raise ConcurrentUpdateError(
            f"run {run_id!r} was modified by another writer {retries} times running"
        )

    def touch_run(self, run_id: str, *, at: datetime) -> None:
        """Move the heartbeat forward. No version check, and none needed.

        The version exists to stop a lost update — one writer's read-modify-write
        silently discarding another's. A monotone maximum has nothing to lose:
        ``MAX`` keeps whichever instant is later regardless of who wrote it or
        in what order, which is exactly what "something is still working on
        this" means to the watchdog.
        """
        with transaction(self._connection) as connection:
            connection.execute(
                "UPDATE runs SET updated_at = MAX(updated_at, ?) WHERE id = ?",
                (iso(at), run_id),
            )

    # ----------------------------------------------------------------- steps

    def start_step(self, result: StepResult) -> None:
        """Record an attempt as started. Collides rather than duplicates."""
        result = self._screen_step(result)
        sql, values = codec.insert_statement("steps", codec.step_to_row(result))
        try:
            with transaction(self._connection) as connection:
                connection.execute(sql, values)
        except sqlite3.IntegrityError as exc:
            raise DuplicateAttemptError(
                f"attempt {result.attempt} of stage {result.stage_id!r} in run "
                f"{result.run_id!r} is already recorded"
            ) from exc

    def finish_step(self, result: StepResult) -> None:
        """Record how an attempt ended.

        Upserts, because not every result has a start: a stage skipped by a
        false guard never ran, and a run record with holes in it cannot answer
        "why is there no PR" months later.
        """
        result = self._screen_step(result)
        row = codec.step_to_row(result)
        key = row["idempotency_key"]
        changes = {name: value for name, value in row.items() if name != "idempotency_key"}
        sql, values = codec.update_statement("steps", changes, where="idempotency_key = ?")
        with transaction(self._connection) as connection:
            cursor = connection.execute(sql, (*values, key))
            if cursor.rowcount == 0:
                insert_sql, insert_values = codec.insert_statement("steps", row)
                connection.execute(insert_sql, insert_values)

    def steps_for(self, run_id: str) -> tuple[StepResult, ...]:
        """Every attempt in this run, in the order it was written."""
        rows = self._connection.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return tuple(codec.row_to_step(row) for row in rows)

    def running_steps(self, *, run_id: str | None = None) -> tuple[StepResult, ...]:
        """Steps that still claim to be executing. The watchdog's query."""
        sql = "SELECT * FROM steps WHERE status = ?"
        params: list[Any] = [StepStatus.RUNNING.value]
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY started_at, rowid"
        return tuple(
            codec.row_to_step(row) for row in self._connection.execute(sql, params).fetchall()
        )

    def _run_row(self, run_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._connection.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return row

    def _screen_step(self, result: StepResult) -> StepResult:
        data = result.model_dump(mode="json")
        for field in ("output", "response", "error"):
            data[field] = self.screen(data[field])
        return StepResult.model_validate(data)
