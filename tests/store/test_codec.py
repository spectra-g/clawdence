"""Rows and models: they round-trip, and they have not drifted apart.

The drift test is the important one. Mapping column by column is what makes the
tables queryable, and the price of that is a mapping that can silently fall
behind the model — a field added in Python with no column to hold it is data the
source of truth does not record. Here it fails the build instead.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from pydantic import BaseModel

from clawdence.domain import (
    Actor,
    ActorKind,
    Budget,
    Event,
    EventKind,
    Run,
    RunStatus,
    StepError,
    StepResult,
    StepStatus,
)
from clawdence.store import codec
from tests.store.factories import RUN_ID, at, make_run, make_step

#: Columns a table has that the domain model deliberately does not.
STORAGE_ONLY = {
    "runs": {"version"},
    "steps": set[str](),
    "audit": {"seq"},
}


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


@pytest.mark.parametrize(
    ("table", "model"),
    [("runs", Run), ("steps", StepResult), ("audit", Event)],
)
def test_the_table_holds_exactly_what_the_model_has(
    db: sqlite3.Connection, table: str, model: type[BaseModel]
) -> None:
    assert table_columns(db, table) - STORAGE_ONLY[table] == set(model.model_fields)


class TestRoundTrip:
    def insert_and_read(
        self, db: sqlite3.Connection, table: str, row: dict[str, object]
    ) -> sqlite3.Row:
        sql, values = codec.insert_statement(table, row)
        db.execute(sql, values)
        stored: sqlite3.Row = db.execute(
            f"SELECT * FROM {table}"  # noqa: S608 - a literal, from the parametrize list
        ).fetchone()
        return stored

    def test_a_run_survives(self, db: sqlite3.Connection) -> None:
        run = make_run(status=RunStatus.HALTED).model_copy(
            update={
                "repo_id": "repo.one",
                "budget": Budget(max_usd=Decimal("2.50"), max_tokens=1000),
                "finished_at": at(30),
            }
        )
        row = self.insert_and_read(db, "runs", codec.run_to_row(run))
        assert codec.row_to_run(row) == run

    def test_a_step_survives(self, db: sqlite3.Connection) -> None:
        db.execute(*codec.insert_statement("runs", codec.run_to_row(make_run())))
        step = make_step(
            "verify",
            status=StepStatus.FAILED,
            attempt=3,
            timeout=12.5,
            output={"exit_code": 1, "nested": [1, None, "two"]},
            response={"decision": "reject"},
            error=StepError(kind="script-exit", message="exited 1", retryable=True),
        )
        row = self.insert_and_read(db, "steps", codec.step_to_row(step))
        assert codec.row_to_step(row) == step

    def test_an_event_survives(self, db: sqlite3.Connection) -> None:
        event = Event(
            id="ev.one",
            kind=EventKind.STEP_FINISHED,
            at=at(1),
            run_id=RUN_ID,
            stage_id="verify",
            actor=Actor(kind=ActorKind.HUMAN, id="u1", display_name="A Person"),
            payload={"attempt": 1, "status": "succeeded"},
            redacted=False,
        )
        row = self.insert_and_read(db, "audit", codec.event_to_row(event))
        assert codec.row_to_event(row) == event

    def test_absent_and_null_stay_the_same_value(self, db: sqlite3.Connection) -> None:
        """The domain has one absence, so storage must not invent a second."""
        db.execute(*codec.insert_statement("runs", codec.run_to_row(make_run())))
        step = make_step("a", output=None)
        row = self.insert_and_read(db, "steps", codec.step_to_row(step))
        assert row["output"] == "null"
        assert codec.row_to_step(row).output is None


class TestStatements:
    def test_insert_binds_every_value(self) -> None:
        sql, values = codec.insert_statement("runs", {"id": "run.1", "workflow": "toy"})
        assert sql == "INSERT INTO runs (id, workflow) VALUES (?, ?)"
        assert values == ("run.1", "toy")

    def test_update_leaves_the_where_clause_to_the_caller(self) -> None:
        sql, values = codec.update_statement(
            "runs", {"status": "done"}, where="id = ?", also_set=", version = version + 1"
        )
        assert sql == "UPDATE runs SET status = ?, version = version + 1 WHERE id = ?"
        assert values == ("done",)
