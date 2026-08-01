"""Rows to models and back.

Written out column by column rather than dumping a model into one JSON blob.
ADR-0005 makes ``runs`` and ``steps`` the source of truth, and a source of truth
you cannot query is a file with extra steps — the watchdog's "which steps are
still running" and HQ's "what is queued" are SQL, not a scan-and-decode. The
cost is that the table and the model can drift; ``test_codec`` pins them
together by comparing field names against columns, so drift is a red build
rather than a column nobody writes.

Structured fields (``budget``, ``output``, ``response``, ``error``, ``actor``,
``payload``) are JSON text. They are never queried *into*, so they lose nothing
by being opaque — and ``json`` rather than ``NULL`` for absence keeps one
decoding path: a step whose output is an explicit JSON ``null`` and a step with
no output are the same value in the domain, and inventing a distinction in the
storage layer that the domain does not have is how a round trip stops being one.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import JsonValue

from clawdence.domain import Event, Run, StepResult
from clawdence.store.schema import iso


def dumps(value: Any) -> str:
    """Canonical JSON for a structured column."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def loads(text: str) -> JsonValue:
    decoded: JsonValue = json.loads(text)
    return decoded


def run_to_row(run: Run) -> dict[str, Any]:
    data = run.model_dump(mode="json")
    return {
        "id": data["id"],
        "work_item_id": data["work_item_id"],
        "workflow": data["workflow"],
        "workflow_version": data["workflow_version"],
        "status": data["status"],
        "repo_id": data["repo_id"],
        "budget": dumps(data["budget"]),
        "created_at": iso(run.created_at),
        "updated_at": iso(run.updated_at),
        "finished_at": iso(run.finished_at) if run.finished_at is not None else None,
    }


def row_to_run(row: sqlite3.Row) -> Run:
    return Run.model_validate(
        {
            "id": row["id"],
            "work_item_id": row["work_item_id"],
            "workflow": row["workflow"],
            "workflow_version": row["workflow_version"],
            "status": row["status"],
            "repo_id": row["repo_id"],
            "budget": loads(row["budget"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }
    )


def step_to_row(result: StepResult) -> dict[str, Any]:
    data = result.model_dump(mode="json")
    return {
        "id": data["id"],
        "run_id": data["run_id"],
        "stage_id": data["stage_id"],
        "type": data["type"],
        "status": data["status"],
        "attempt": data["attempt"],
        "idempotency_key": data["idempotency_key"],
        "timeout_seconds": data["timeout_seconds"],
        "started_at": iso(result.started_at) if result.started_at is not None else None,
        "finished_at": iso(result.finished_at) if result.finished_at is not None else None,
        "output": dumps(data["output"]),
        "response": dumps(data["response"]),
        "error": dumps(data["error"]),
    }


def row_to_step(row: sqlite3.Row) -> StepResult:
    return StepResult.model_validate(
        {
            "id": row["id"],
            "run_id": row["run_id"],
            "stage_id": row["stage_id"],
            "type": row["type"],
            "status": row["status"],
            "attempt": row["attempt"],
            "idempotency_key": row["idempotency_key"],
            "timeout_seconds": row["timeout_seconds"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "output": loads(row["output"]),
            "response": loads(row["response"]),
            "error": loads(row["error"]),
        }
    )


def event_to_row(event: Event) -> dict[str, Any]:
    data = event.model_dump(mode="json")
    return {
        "id": data["id"],
        "schema_version": data["schema_version"],
        "kind": data["kind"],
        "at": iso(event.at),
        "run_id": data["run_id"],
        "work_item_id": data["work_item_id"],
        "stage_id": data["stage_id"],
        "actor": dumps(data["actor"]),
        "payload": dumps(data["payload"]),
        "redacted": int(data["redacted"]),
    }


def row_to_event(row: sqlite3.Row) -> Event:
    """Rebuild an audit record.

    ``schema_version`` is taken from the row, not from the current constant, and
    nothing here rejects a version it does not recognise. The log outlives the
    build that wrote it; a reader that refuses old records is a reader that
    cannot answer questions about last month.
    """
    return Event.model_validate(
        {
            "id": row["id"],
            "schema_version": row["schema_version"],
            "kind": row["kind"],
            "at": row["at"],
            "run_id": row["run_id"],
            "work_item_id": row["work_item_id"],
            "stage_id": row["stage_id"],
            "actor": loads(row["actor"]),
            "payload": loads(row["payload"]),
            "redacted": bool(row["redacted"]),
        }
    )


def insert_statement(table: str, row: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """``(sql, parameters)`` for inserting one row.

    Built here, once, rather than at each call site, so there is one place to
    check the property the linter is right to ask about: **the only thing
    interpolated into these statements is column names, and those come from the
    ``*_to_row`` functions above — a fixed set, derived from the domain model,
    never from a caller and never from stored data.** Every *value* is a bound
    parameter. ``table`` and ``where`` are literals written in this package.
    """
    names = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    return (
        f"INSERT INTO {table} ({names}) VALUES ({placeholders})",  # noqa: S608 - see docstring
        tuple(row.values()),
    )


def update_statement(
    table: str,
    row: dict[str, Any],
    *,
    where: str,
    also_set: str = "",
) -> tuple[str, tuple[Any, ...]]:
    """``(sql, parameters)`` for updating one row. See ``insert_statement``.

    Parameters cover the assignments only; anything the ``where`` clause binds
    is the caller's to append.
    """
    assignments = ", ".join(f"{name} = ?" for name in row)
    return (
        f"UPDATE {table} SET {assignments}{also_set} WHERE {where}",  # noqa: S608 - see above
        tuple(row.values()),
    )
