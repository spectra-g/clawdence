"""The database itself: migrations, pragmas, and the transaction helper."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from clawdence.store import SCHEMA_VERSION, UnsupportedDatabaseError, connect, migrate, transaction
from clawdence.store.schema import IN_MEMORY, iso, parse_iso
from tests.conftest import ConnectionFactory
from tests.store.factories import at

PARK = "INSERT INTO dead_letters (at, origin, reason, body) VALUES (?, ?, ?, ?)"

#: Tables the newest migration creates. Dropped and re-applied by the
#: downgrade-then-upgrade test below, which is what makes it a test of the
#: migration this build added rather than of one that has worked for months.
#: Children first, so the foreign key does not object to the order.
LATEST_TABLES = ("publications",)


def park(connection: sqlite3.Connection) -> None:
    connection.execute(PARK, (iso(at(0)), "test", "because", "{}"))


def parked(connection: sqlite3.Connection) -> int:
    count: int = connection.execute("SELECT count(*) FROM dead_letters").fetchone()[0]
    return count


def user_version(connection: sqlite3.Connection) -> int:
    version: int = connection.execute("PRAGMA user_version").fetchone()[0]
    return version


class TestMigrations:
    def test_a_new_database_is_at_the_current_version(self, db: sqlite3.Connection) -> None:
        assert user_version(db) == SCHEMA_VERSION

    def test_every_table_is_created(self, db: sqlite3.Connection) -> None:
        names = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "runs",
            "steps",
            "audit",
            "dead_letters",
            "steering",
            "cancellations",
            "intake",
            "intake_turns",
            "publications",
        } <= names

    def test_a_database_from_an_older_build_is_migrated_forward(
        self, connections: ConnectionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The newest migration's tables arrive on a database that already has
        runs in it — currently S10's ``intake``, as it was S6c's ``steering``.

        Written as a real downgrade-then-upgrade rather than a hand-carved older
        file: what a migration has to survive is a database that was *written*
        by the earlier build, and a fixture typed out by hand is a guess about
        what that looked like. The cost of doing it this way is that the step
        adding a migration also updates ``LATEST_TABLES``, which is the right
        thing to have to notice.
        """
        path = tmp_path / "state.db"
        first = connections(path)
        park(first)
        for table in LATEST_TABLES:
            first.execute(f"DROP TABLE {table}")
        first.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        first.close()

        second = connections(path)

        assert user_version(second) == SCHEMA_VERSION
        assert parked(second) == 1
        for table in LATEST_TABLES:
            assert second.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608

    def test_migrating_again_changes_nothing(self, db: sqlite3.Connection) -> None:
        park(db)
        assert migrate(db) == SCHEMA_VERSION
        assert parked(db) == 1

    def test_reopening_a_file_keeps_its_contents(
        self, connections: ConnectionFactory, tmp_path: Path
    ) -> None:
        path = tmp_path / "nested" / "state.db"
        first = connections(path)
        park(first)
        first.close()

        assert parked(connections(path)) == 1

    def test_a_database_from_a_newer_build_is_refused(self, db: sqlite3.Connection) -> None:
        """Better to stop than to write rows the newer schema will not accept."""
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with pytest.raises(UnsupportedDatabaseError, match="Upgrade clawdence"):
            migrate(db)

    def test_a_failed_migration_leaves_nothing_behind(
        self, connections: ConnectionFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-applied schema is the one outcome a migration must not have."""
        connection = connections()
        monkeypatch.setattr(
            "clawdence.store.schema._MIGRATIONS",
            _MIGRATIONS_THAT_FAIL_HALFWAY,
        )
        monkeypatch.setattr("clawdence.store.schema.SCHEMA_VERSION", SCHEMA_VERSION + 1)
        with pytest.raises(sqlite3.OperationalError):
            migrate(connection)
        assert user_version(connection) == SCHEMA_VERSION
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'half_applied'"
        ).fetchall()


#: A migration whose second statement is invalid. Used by the test above.
_MIGRATIONS_THAT_FAIL_HALFWAY = (
    *[""] * SCHEMA_VERSION,
    "CREATE TABLE half_applied (a TEXT) STRICT; CREATE TABLE half_applied (a TEXT) STRICT;",
)


class TestPragmas:
    def test_columns_are_typed(self, db: sqlite3.Connection) -> None:
        """STRICT: without it SQLite would store anything in any column."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO dead_letters (at, origin, reason, body, tries) VALUES (?,?,?,?,?)",
                (iso(at(0)), "test", "because", "{}", "not-a-number"),
            )

    def test_a_step_cannot_point_at_a_run_that_does_not_exist(self, db: sqlite3.Connection) -> None:
        """Foreign keys are off by default in SQLite, per connection, forever."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO steps (id, run_id, stage_id, type, status, attempt, "
                "idempotency_key, output, response, error) "
                "VALUES ('sr.x', 'run.nope', 'a', 'script', 'succeeded', 1, 'k', "
                "'null', 'null', 'null')"
            )

    def test_a_file_database_uses_wal(self, connections: ConnectionFactory, tmp_path: Path) -> None:
        """So the watchdog can read a run while a live process writes it."""
        connection = connections(tmp_path / "state.db")
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


class TestTimestamps:
    def test_round_trip(self) -> None:
        assert parse_iso(iso(at(1.5))) == at(1.5)

    def test_precision_is_fixed_so_text_order_is_time_order(self) -> None:
        """The heartbeat compares instants as strings, in SQL, via MAX()."""
        assert iso(at(0)) < iso(at(0.000001)) < iso(at(1)) < iso(at(61))
        assert len({len(iso(at(offset))) for offset in (0, 0.5, 1, 61)}) == 1


class TestTransaction:
    def test_a_failure_discards_the_whole_group(self, db: sqlite3.Connection) -> None:
        with pytest.raises(RuntimeError), transaction(db):
            park(db)
            raise RuntimeError("something went wrong afterwards")
        assert parked(db) == 0

    def test_an_inner_transaction_joins_the_outer_one(self, db: sqlite3.Connection) -> None:
        """SQLite has no nesting, so the outermost group owns the commit."""
        with pytest.raises(RuntimeError), transaction(db):
            with transaction(db):
                park(db)
            assert db.in_transaction, "the inner group must not have committed on its own"
            raise RuntimeError("the outer group failed")
        assert parked(db) == 0

    def test_a_group_that_succeeds_is_visible_afterwards(self, db: sqlite3.Connection) -> None:
        with transaction(db):
            park(db)
        assert not db.in_transaction
        assert parked(db) == 1


def test_an_old_sqlite_is_refused_at_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refusing beats creating a database the STRICT tables cannot live in."""
    monkeypatch.setattr("clawdence.store.schema.sqlite3.sqlite_version_info", (3, 36, 0))
    monkeypatch.setattr("clawdence.store.schema.sqlite3.sqlite_version", "3.36.0")
    with pytest.raises(UnsupportedDatabaseError, match=r"3\.37"):
        connect(IN_MEMORY)
