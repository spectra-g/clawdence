"""Operator operations on the state system of record.

Backups use SQLite's online backup API, not a filesystem copy: the latter can
miss committed pages still held in a WAL file. Restores only create a clean
destination, validate the embedded schema version and integrity both before
and after copying, and publish the completed file atomically.

``tombstone_and_rewrite`` is the deliberately rare exception to the audit
log's append-only policy. It replaces one exact missed secret in content
columns, then appends a metadata-only tombstone describing the repair. The
secret itself is never accepted as audit metadata.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from clawdence.domain import Actor, ActorKind, EventKind
from clawdence.ports.secrets import REDACTED
from clawdence.store.errors import StateOperationError
from clawdence.store.schema import SCHEMA_VERSION, transaction
from clawdence.store.state import StateStore


@dataclass(frozen=True, slots=True)
class DatabaseCopy:
    source: Path
    destination: Path
    schema_version: int
    bytes: int


@dataclass(frozen=True, slots=True)
class RewriteReport:
    rows: Mapping[str, int] = field(default_factory=dict)
    occurrences: int = 0

    @property
    def changed_rows(self) -> int:
        return sum(self.rows.values())


# Content-bearing columns only. Identifiers and state-machine columns are not
# rewritten: changing either could break foreign keys or invent transitions.
REWRITABLE_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    "runs": ("budget",),
    "steps": ("output", "response", "error"),
    "audit": ("actor", "payload"),
    "dead_letters": ("origin", "reason", "body"),
    "steering": ("body", "sender", "reason"),
    "cancellations": ("requested_by", "reason"),
    "intake": ("item", "reason"),
    "intake_turns": ("author", "body"),
    "publications": ("item", "workflow", "last_error"),
    "external_effects": ("command", "error_detail"),
}


def backup(store: StateStore, destination: Path | str) -> DatabaseCopy:
    """Take a consistent online backup and refuse to overwrite a file."""
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise StateOperationError(f"backup destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    _validate_connection(store.connection, expected=SCHEMA_VERSION, label="source database")
    temporary = _temporary_database(target)
    try:
        copied = sqlite3.connect(temporary)
        try:
            store.connection.backup(copied)
            _validate_connection(copied, expected=SCHEMA_VERSION, label="completed backup")
        finally:
            copied.close()
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return DatabaseCopy(
        source=_connection_path(store.connection),
        destination=target,
        schema_version=SCHEMA_VERSION,
        bytes=target.stat().st_size,
    )


def restore(source: Path | str, destination: Path | str) -> DatabaseCopy:
    """Restore a checked backup into a clean, absent destination."""
    backup_path = Path(source).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not backup_path.is_file():
        raise StateOperationError(f"backup does not exist or is not a file: {backup_path}")
    if target.exists():
        raise StateOperationError(f"restore destination already exists: {target}")
    if backup_path == target:
        raise StateOperationError("backup and restore destination must be different")
    target.parent.mkdir(parents=True, exist_ok=True)

    source_connection = _read_only(backup_path)
    temporary = _temporary_database(target)
    try:
        _validate_connection(source_connection, expected=SCHEMA_VERSION, label="backup")
        copied = sqlite3.connect(temporary)
        try:
            source_connection.backup(copied)
            _validate_connection(copied, expected=SCHEMA_VERSION, label="restored database")
        finally:
            copied.close()
        temporary.replace(target)
    finally:
        source_connection.close()
        temporary.unlink(missing_ok=True)
    return DatabaseCopy(
        source=backup_path,
        destination=target,
        schema_version=SCHEMA_VERSION,
        bytes=target.stat().st_size,
    )


def tombstone_and_rewrite(
    store: StateStore,
    secret: str,
    *,
    reason: str,
    requested_by: str,
    at: datetime | None = None,
) -> RewriteReport:
    """Replace an exact missed secret and append an audited repair tombstone."""
    if not secret:
        raise StateOperationError("the secret to rewrite must not be empty")
    if secret == REDACTED:
        raise StateOperationError("the redaction marker itself cannot be rewritten")
    reason = store.screen_text(reason.strip().replace(secret, REDACTED))
    requested_by = store.screen_text(requested_by.strip().replace(secret, REDACTED))
    if not reason:
        raise StateOperationError("an operator reason is required for an audited rewrite")
    if not requested_by:
        raise StateOperationError("an operator identity is required for an audited rewrite")

    rows: dict[str, int] = {}
    occurrences = 0
    with transaction(store.connection) as connection:
        for table, columns in REWRITABLE_COLUMNS.items():
            changed: set[int] = set()
            for column in columns:
                selected = connection.execute(
                    f"SELECT rowid AS __rowid__, {column} FROM {table} "  # noqa: S608
                    f"WHERE instr({column}, ?) > 0",
                    (secret,),
                ).fetchall()
                for row in selected:
                    value = row[column]
                    if isinstance(value, str):
                        occurrences += value.count(secret)
                    changed.add(int(row["__rowid__"]))
                connection.execute(
                    f"UPDATE {table} SET {column} = replace({column}, ?, ?) "  # noqa: S608
                    f"WHERE instr({column}, ?) > 0",
                    (secret, REDACTED, secret),
                )
            if changed:
                rows[table] = len(changed)

        report = RewriteReport(rows=rows, occurrences=occurrences)
        store.audit.record(
            EventKind.STATE_SECRET_REWRITTEN,
            at=at or datetime.now(UTC),
            actor=Actor(kind=ActorKind.HUMAN, id=requested_by),
            payload={
                "reason": reason,
                "changed_rows": report.changed_rows,
                "occurrences": report.occurrences,
                "tables": dict(report.rows),
            },
        )
    return report


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _temporary_database(destination: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    return Path(name)


def _validate_connection(connection: sqlite3.Connection, *, expected: int, label: str) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != expected:
        raise StateOperationError(
            f"{label} has schema version {version}; this build requires exactly {expected}"
        )
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise StateOperationError(f"{label} failed SQLite integrity check: {integrity}")


def _connection_path(connection: sqlite3.Connection) -> Path:
    row = connection.execute("PRAGMA database_list").fetchone()
    raw = row[2] if row is not None else ""
    return Path(raw).resolve() if raw else Path(":memory:")
