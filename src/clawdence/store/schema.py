"""The database: how to open one, and what is in it.

SQLite, per ADR-0005 — single node, transactional, and already a dependency
the memory layer will bring anyway. The tables are the
source of truth; ``audit`` is explicitly *not*, which is the whole content of
that decision. State is read from ``runs`` and ``steps``, never rebuilt from the
log, so there are no upcasters, no snapshots, no compaction and no projection
rebuild to maintain — and the log can be rotated or lost without losing the
system of record.

Four connection choices, all load-bearing:

``isolation_level=None``
    Autocommit, with every write wrapped in an explicit ``BEGIN IMMEDIATE``.
    The default implicit-transaction behaviour opens a *deferred* transaction on
    the first write, which takes a read lock and then tries to upgrade — two
    writers doing that deadlock one of themselves into ``SQLITE_BUSY`` no matter
    how long the busy timeout is. Taking the write lock up front makes the
    second writer wait rather than fail.

``journal_mode=WAL`` with ``synchronous=FULL``
    WAL so a reader (the watchdog, ``clawdence runs list``) never blocks the
    process executing a run. ``FULL`` rather than WAL's usual ``NORMAL`` because
    this is the system of record: ``NORMAL`` can lose the most recent commits to
    an OS-level crash, and "the run finished but the database forgot" is exactly
    the class of inconsistency this store exists to prevent. The cost is one
    fsync per step transition, which is nothing next to running a step.

``foreign_keys=ON``
    Off by default in SQLite, per connection, forever. A step row pointing at a
    run that does not exist is a bug that should fail where it is made.

``STRICT`` tables
    SQLite otherwise stores whatever it is given, so a status column would
    happily hold an integer. Requires SQLite 3.37 (2021); the version is checked
    at connect time and refused clearly rather than failing on the first DDL.

Timestamps are ISO-8601 UTC strings at fixed microsecond precision. Fixed
precision matters: it makes lexicographic comparison chronological, which is
what lets the watchdog and the ``updated_at`` heartbeat compare instants in SQL
without a date function. Datetimes are never handed to the driver directly —
its default adapters are deprecated in 3.12, and this project runs with
warnings as errors.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from clawdence.store.errors import UnsupportedDatabaseError

#: STRICT tables landed in 3.37.0.
MINIMUM_SQLITE: Final = (3, 37, 0)

#: How long a writer waits for another writer's transaction before giving up.
BUSY_TIMEOUT_MS: Final = 5_000

#: ``:memory:`` is spelled out so callers can ask for an ephemeral store without
#: knowing SQLite's spelling of it.
IN_MEMORY: Final = ":memory:"

_MIGRATIONS: Final[tuple[str, ...]] = (
    # 1 — the S4 schema.
    """
    CREATE TABLE runs (
        id               TEXT    NOT NULL PRIMARY KEY,
        work_item_id     TEXT    NOT NULL,
        workflow         TEXT    NOT NULL,
        workflow_version TEXT    NOT NULL,
        status           TEXT    NOT NULL,
        repo_id          TEXT,
        budget           TEXT    NOT NULL,
        created_at       TEXT    NOT NULL,
        updated_at       TEXT    NOT NULL,
        finished_at      TEXT,
        -- Optimistic concurrency. Not part of the domain ``Run``: it describes
        -- how the row is written, not what a run is.
        version          INTEGER NOT NULL DEFAULT 1
    ) STRICT;

    CREATE INDEX runs_status ON runs (status, updated_at);

    CREATE TABLE steps (
        id              TEXT    NOT NULL PRIMARY KEY,
        run_id          TEXT    NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        stage_id        TEXT    NOT NULL,
        type            TEXT    NOT NULL,
        status          TEXT    NOT NULL,
        attempt         INTEGER NOT NULL,
        -- Idempotency, structurally. Two writers racing the same attempt of the
        -- same stage collide here instead of both succeeding.
        idempotency_key TEXT    NOT NULL UNIQUE,
        timeout_seconds REAL,
        started_at      TEXT,
        finished_at     TEXT,
        output          TEXT    NOT NULL,
        response        TEXT    NOT NULL,
        error           TEXT    NOT NULL,
        UNIQUE (run_id, stage_id, attempt)
    ) STRICT;

    -- The watchdog's query: every step still claiming to be running.
    CREATE INDEX steps_running ON steps (status, started_at);

    CREATE TABLE audit (
        -- Total order across the whole log, assigned by the database. Wall-clock
        -- ``at`` is not an ordering: two events in the same microsecond, or a
        -- clock that steps backwards, would make the timeline ambiguous exactly
        -- when it is being read to answer what happened first.
        seq            INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        id             TEXT    NOT NULL UNIQUE,
        schema_version INTEGER NOT NULL,
        kind           TEXT    NOT NULL,
        at             TEXT    NOT NULL,
        run_id         TEXT,
        work_item_id   TEXT,
        stage_id       TEXT,
        actor          TEXT    NOT NULL,
        payload        TEXT    NOT NULL,
        redacted       INTEGER NOT NULL
    ) STRICT;

    CREATE INDEX audit_run ON audit (run_id, seq);

    CREATE TABLE dead_letters (
        id      INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        at      TEXT    NOT NULL,
        origin  TEXT    NOT NULL,
        reason  TEXT    NOT NULL,
        body    TEXT    NOT NULL,
        tries   INTEGER NOT NULL DEFAULT 1
    ) STRICT;
    """,
    # 2 — S6c: what the outside world can say to a run that is already going.
    """
    CREATE TABLE steering (
        -- Total order of arrival, assigned by the database, for the same reason
        -- ``audit`` has one: FIFO within a priority class has to be anchored on
        -- something a clock cannot make ambiguous.
        seq          INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        id           TEXT    NOT NULL UNIQUE,
        run_id       TEXT    NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        -- Higher first. Signed, so "before everything already queued" needs no
        -- renumbering of what is queued.
        priority     INTEGER NOT NULL DEFAULT 0,
        state        TEXT    NOT NULL,
        body         TEXT    NOT NULL,
        sender       TEXT    NOT NULL,
        created_at   TEXT    NOT NULL,
        delivered_at TEXT,
        closed_at    TEXT,
        -- Claim order within the run, assigned when the message is claimed. It
        -- is what names the file the agent reads, so the order the inbox chose
        -- survives into a directory listing.
        ordinal      INTEGER,
        reason       TEXT
    ) STRICT;

    -- The claim: one run's unread messages, already in the order they go out.
    CREATE INDEX steering_claim ON steering (run_id, state, priority DESC, seq);

    CREATE TABLE cancellations (
        -- One per run, and the primary key is what says so: a second request to
        -- stop the same run is the same request, not another one.
        run_id          TEXT NOT NULL PRIMARY KEY REFERENCES runs (id) ON DELETE CASCADE,
        requested_at    TEXT NOT NULL,
        requested_by    TEXT NOT NULL,
        reason          TEXT NOT NULL,
        acknowledged_at TEXT
    ) STRICT;
    """,
    # 3 — S10: what arrived, and what has happened to it since.
    """
    CREATE TABLE intake (
        -- Arrival order, assigned by the database, for the reason ``audit`` and
        -- ``steering`` have one: a backlog has to be worked through in an order
        -- a clock cannot make ambiguous.
        seq                   INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,

        -- ``source:external_id`` — ``ports.ingest.dedupe_key``. The identity of
        -- a *request*, not of a delivery, which is what makes a redelivered
        -- webhook collide here instead of becoming a second work item. UNIQUE
        -- rather than checked in Python: the guard has to hold across the two
        -- processes that a CLI submission and a running control plane are.
        dedupe_key            TEXT    NOT NULL UNIQUE,

        -- The id we minted, on first arrival, and never again. Amendments keep
        -- it: everything downstream refers to a work item by this, and an edit
        -- that renamed it would strand every reference.
        work_item_id          TEXT    NOT NULL UNIQUE,

        source                TEXT    NOT NULL,
        conversation_id       TEXT,
        state                 TEXT    NOT NULL,

        -- Bumps on every amendment. What tells a reader "the third version of
        -- one request" from "three requests".
        revision              INTEGER NOT NULL DEFAULT 1,

        -- The revision that was handed to the pipeline, or NULL. Kept after an
        -- amendment re-queues the item, because "we ran revision 1 and they are
        -- now on revision 3" is the thing somebody debugging needs to see.
        acknowledged_revision INTEGER,

        item                  TEXT    NOT NULL,
        received_at           TEXT    NOT NULL,
        updated_at            TEXT    NOT NULL,
        acknowledged_at       TEXT,
        closed_at             TEXT,
        reason                TEXT
    ) STRICT;

    -- The collect query: what has not been dealt with, in arrival order.
    CREATE INDEX intake_pending ON intake (state, seq);

    -- Reply routing: a source plus a conversation identifies the request a
    -- follow-up belongs to.
    CREATE INDEX intake_conversation ON intake (source, conversation_id);

    CREATE TABLE intake_turns (
        seq        INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
        id         TEXT    NOT NULL UNIQUE,
        dedupe_key TEXT    NOT NULL REFERENCES intake (dedupe_key) ON DELETE CASCADE,
        author     TEXT    NOT NULL,
        body       TEXT    NOT NULL,
        at         TEXT    NOT NULL
    ) STRICT;

    CREATE INDEX intake_turns_request ON intake_turns (dedupe_key, seq);
    """,
    # 4 — provisional M1 publication bridge. S4b.1 deliberately owns replacing
    # or reshaping this into the generic durable-effects schema; do not add a
    # second adapter-specific queue beside it.
    """
    CREATE TABLE publications (
        run_id        TEXT    NOT NULL PRIMARY KEY REFERENCES runs (id) ON DELETE CASCADE,
        work_item_id  TEXT    NOT NULL,
        repo_id       TEXT    NOT NULL,
        branch        TEXT    NOT NULL,
        base_commit   TEXT    NOT NULL,
        head_commit   TEXT    NOT NULL,
        item          TEXT    NOT NULL,
        state         TEXT    NOT NULL,
        workflow      TEXT    NOT NULL,
        attempts      INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT,
        updated_at    TEXT    NOT NULL
    ) STRICT;

    CREATE INDEX publications_pending ON publications (state, updated_at);
    """,
)

#: The schema version this build writes and expects.
SCHEMA_VERSION: Final = len(_MIGRATIONS)


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write, or several that must land together.

    Re-entrant by design: an inner ``transaction`` inside an outer one joins it
    rather than starting a second, because SQLite has no nested transactions and
    the alternative is every caller knowing who else is on the stack. What it
    buys is the property the ledger depends on — a step row and the audit entry
    describing it commit together or not at all, so the log cannot end up
    describing a state change that was rolled back. (The *external* dual-write
    problem — appending an event and then opening a PR — is not this, and is
    S4b's.)
    """
    if connection.in_transaction:
        yield connection
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def iso(moment: datetime) -> str:
    """UTC, fixed precision. The only way a timestamp enters the database."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) and migrate a state database."""
    if sqlite3.sqlite_version_info < MINIMUM_SQLITE:
        wanted = ".".join(str(part) for part in MINIMUM_SQLITE)
        raise UnsupportedDatabaseError(
            f"clawdence needs SQLite {wanted} or newer for STRICT tables, "
            f"and this Python is linked against {sqlite3.sqlite_version}"
        )

    if isinstance(path, Path):
        path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(path), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if str(path) != IN_MEMORY:
        # WAL is meaningless for an in-memory database and setting it there is a
        # no-op that reports success, which is worse than not asking.
        connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    migrate(connection)
    return connection


def migrate(connection: sqlite3.Connection) -> int:
    """Bring a connection's database up to ``SCHEMA_VERSION``.

    ``user_version`` is SQLite's own four bytes of header, so the version
    travels with the file and needs no table of its own — which means the
    bootstrap case has no chicken-and-egg problem.

    Migrations are ordinary DDL against current-shaped rows. That is the payoff
    of ADR-0005: with the tables as the source of truth rather than a projection
    of a log, a schema change is a migration, not a rewrite of history.

    **Two processes may bootstrap the same file at once**, and until S7 made
    concurrency real that was rare enough to look impossible: ``clawdence run``
    in two terminals against a fresh database, or a run and a ``runs recover``.
    Both read ``user_version`` as 0, both decide to apply migration 0, and the
    second one to reach the write lock finds the first one's tables already
    there. The version is only readable *outside* the write lock — sqlite3's
    ``executescript`` commits any open transaction before it runs, so there is
    no way to hold the lock across the check and the DDL — so the check is
    repeated after a failure instead: a version that has moved past the
    migration we were attempting means somebody else applied it, which is the
    outcome we wanted and not an error.
    """
    current = _version(connection)
    if current > SCHEMA_VERSION:
        raise UnsupportedDatabaseError(
            f"this database was written at schema version {current}; "
            f"this build understands {SCHEMA_VERSION}. Upgrade clawdence."
        )
    for version in range(current, SCHEMA_VERSION):
        # The transaction lives *inside* the script. ``executescript`` commits
        # any open transaction before it runs, so a migration wrapped from out
        # here would run uncovered and then fail to commit — and a half-applied
        # schema is the one failure a migration must not have. ``user_version``
        # takes a literal rather than a parameter, and it moves inside the same
        # transaction as the DDL it describes.
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{_MIGRATIONS[version]}\n"
            f"PRAGMA user_version = {version + 1};\n"
            "COMMIT;"
        )
        try:
            connection.executescript(script)
        except Exception:
            # ``execute``, not ``executescript`` — the latter would commit the
            # half-applied transaction it is being asked to discard. The suppress
            # covers a failure that already rolled itself back.
            with suppress(sqlite3.OperationalError):
                connection.execute("ROLLBACK")
            if _version(connection) > version:
                # Somebody else applied it while we were queueing for the write
                # lock. Their transaction moved the version and ours failed on
                # tables they had already created, which is a race we lost and
                # not a database we cannot open.
                continue
            raise
    return SCHEMA_VERSION


def _version(connection: sqlite3.Connection) -> int:
    value: int = connection.execute("PRAGMA user_version").fetchone()[0]
    return value
