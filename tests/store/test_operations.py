"""S4b.2: checked state copies and the audited missed-secret repair."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from clawdence.domain import IngestSource, SourceRef, Submitter, WorkItem, WorkItemType
from clawdence.ports import PermanentError
from clawdence.store import (
    ExternalEffects,
    Intake,
    StateOperationError,
    StateStore,
    backup,
    restore,
    tombstone_and_rewrite,
)
from tests.conftest import StoreFactory
from tests.store.factories import RUN_ID, TEST_CREDENTIAL, at, make_run, make_step


def _item(text: str = "Fix the checkout") -> WorkItem:
    return WorkItem(
        id="wi.backup",
        type=WorkItemType.TASK,
        title="Checkout fix",
        raw_text=text,
        submitter=Submitter(source=IngestSource.CLI, external_id="operator", trusted=True),
        source_ref=SourceRef(
            source=IngestSource.CLI,
            external_id="backup-test",
            conversation_id="conversation.backup",
        ),
        created_at=at(0),
    )


def _populate(store: StateStore) -> None:
    store.create_run(make_run())
    store.finish_step(make_step("plan", output={"plan": "ready"}))
    intake = Intake(store)
    intake.submit(_item(), at=at(1))
    intake.reply(
        source=IngestSource.CLI,
        conversation_id="conversation.backup",
        body="One more detail",
        author="operator",
        at=at(2),
    )
    effects = ExternalEffects(store, clock=lambda: at(3))
    effects.enqueue(
        effect_id="fx.pending",
        idempotency_key="effect.pending",
        run_id=RUN_ID,
        kind="publish_pull_request",
        command={"title": "Pending"},
    )
    effects.enqueue(
        effect_id="fx.parked",
        idempotency_key="effect.parked",
        run_id=RUN_ID,
        kind="publish_pull_request",
        command={"title": "Parked"},
    )
    assert effects.claim("fx.parked", owner="worker", at=at(4)) is not None
    effects.failed(
        "fx.parked",
        owner="worker",
        error=PermanentError("rejected", "provider rejected it"),
        at=at(5),
    )


def _domain_boundary(store: StateStore) -> tuple[object, ...]:
    intake = Intake(store)
    return (
        store.list_runs(),
        store.steps_for(RUN_ID),
        intake.list(),
        intake.turns("cli:backup-test"),
        ExternalEffects(store).list(),
        store.audit.read(),
    )


def test_restore_into_a_clean_environment_reproduces_the_domain_boundary(
    stores: StoreFactory, tmp_path: Path
) -> None:
    source = stores(tmp_path / "source.db")
    _populate(source)
    expected = _domain_boundary(source)

    copy = backup(source, tmp_path / "state.backup.db")
    restored_path = tmp_path / "clean" / "state.db"
    restored = restore(copy.destination, restored_path)
    reopened = stores(restored.destination)

    assert copy.schema_version == restored.schema_version
    assert _domain_boundary(reopened) == expected


def test_backup_and_restore_refuse_overwriting_existing_files(
    stores: StoreFactory, tmp_path: Path
) -> None:
    source = stores(tmp_path / "source.db")
    destination = tmp_path / "already-there.db"
    destination.write_text("keep me")

    with pytest.raises(StateOperationError, match="already exists"):
        backup(source, destination)
    with pytest.raises(StateOperationError, match="already exists"):
        restore(source.connection.execute("PRAGMA database_list").fetchone()[2], destination)
    assert destination.read_text() == "keep me"


def test_restore_requires_this_builds_exact_schema(tmp_path: Path) -> None:
    old = tmp_path / "old.db"
    connection = sqlite3.connect(old)
    connection.execute("PRAGMA user_version = 4")
    connection.close()

    with pytest.raises(StateOperationError, match="schema version 4"):
        restore(old, tmp_path / "restored.db")
    assert not (tmp_path / "restored.db").exists()


def test_a_missed_secret_can_be_rewritten_with_an_audit_tombstone(
    stores: StoreFactory, tmp_path: Path
) -> None:
    state = stores(tmp_path / "state.db")
    _populate(state)
    # Simulate an old writer that predated write-time screening.
    state.connection.execute(
        "UPDATE steps SET output = ? WHERE stage_id = 'plan'",
        (f'{{"echo":"{TEST_CREDENTIAL}"}}',),
    )
    state.connection.execute(
        "UPDATE intake SET item = replace(item, 'Fix the checkout', ?)",
        (f"Fix with {TEST_CREDENTIAL}",),
    )

    report = tombstone_and_rewrite(
        state,
        TEST_CREDENTIAL,
        reason=f"remove {TEST_CREDENTIAL}; it predates screening",
        requested_by="security-operator",
        at=at(10),
    )

    assert report.occurrences == 2
    assert report.rows == {"steps": 1, "intake": 1}
    assert TEST_CREDENTIAL not in "\n".join(state.connection.iterdump())
    event = state.audit.read()[-1]
    assert event.kind.value == "state.secret_rewritten"
    assert event.actor is not None and event.actor.id == "security-operator"
    assert event.payload == {
        "reason": "remove [redacted]; it predates screening",
        "changed_rows": 2,
        "occurrences": 2,
        "tables": {"steps": 1, "intake": 1},
    }
