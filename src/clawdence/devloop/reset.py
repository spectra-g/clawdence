"""``clawdence reset`` — a dirty environment back to clean, in one command.

The step this ports is v1's ``reset-pipeline.sh``, and the lesson it carries is
one line in the plan: clearing the ``.jsonl`` event files without also clearing
``sessions.json`` left stale session ids behind, and messages sent to them were
**silently dropped**. Nothing failed. The pipeline just stopped hearing.

Generalised, that is the whole design constraint here: **a partial reset leaves
dangling references, and dangling references fail quietly.** So this command's
job is not "delete some things" but "leave nothing pointing at something that is
gone". Three couplings in v2 could each reproduce the bug, and they are handled
in three different ways because they are three different kinds of coupling:

``steering`` / ``cancellations`` → ``runs``
    Structural. Both declare ``REFERENCES runs (id) ON DELETE CASCADE``, so the
    v1 failure is not merely avoided here, it is *unrepresentable* — deleting a
    run takes its inbox with it whether or not this module remembers to.

``intake`` → ``runs``
    Not structural, and deliberately so: a request outlives every run made from
    it, and an FK would be wrong. Which means it is exactly v1's bug — an
    ``acknowledged`` row says "the pipeline has this", and after a reset there
    is no pipeline and no run, so it will sit there forever, collected by
    nothing and re-queued by nothing. The default clears it. ``keep_inbox``
    keeps the requests and puts them **back in the queue**
    (``Intake.unacknowledge``), because keeping them in the state they were in
    is the one option that is quietly wrong.

containers / worktrees / caches → ``runs``
    On the disk and in a daemon, where a foreign key cannot reach. Swept by the
    reaper, below.

**This is a reaper sweep with every protection switched off**, and reusing
``Reaper`` says so honestly rather than growing a second deletion path that
would drift from it. The reaper reclaims what is *unclaimed* and *not recent*;
this passes an empty live set and zero retentions, which turns both those
questions off. That is precisely why the CLI asks for confirmation and why a
live run refuses: with the protections gone, the only thing standing between
this command and a run in flight is the person typing it.

**Caches are kept by default**, the one asymmetry with ``reap``. A cache holds
no state, references no run and lies to nobody — it is content-addressed
downloads. Clearing it makes the next run slow and makes the environment no
cleaner, so it takes an explicit ``--caches``.

**The database is emptied, not deleted.** Removing the file would take the
schema version with it and re-run every migration on the next open, which
converts "reset my environment" into "exercise the migration path", and those
are different things to want. It also means a reset cannot be what fixes a
database this build cannot open — for that, delete the file yourself.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from clawdence.devloop.errors import ResetRefused
from clawdence.domain import RunStatus
from clawdence.runners.cache import Cache
from clawdence.runners.engine import ContainerEngine
from clawdence.runners.reaper import Reaper, Reclaimed
from clawdence.store.intake import Intake
from clawdence.store.schema import transaction
from clawdence.store.state import StateStore

#: Every table in the state database, children before parents. The order is
#: belt and braces — the cascades would handle it — but a delete that depends on
#: a cascade reads as if the dependency were not there.
TABLES: Final[tuple[str, ...]] = (
    "intake_turns",
    "intake",
    "steering",
    "cancellations",
    "steps",
    "audit",
    "dead_letters",
    "runs",
)

#: The two that hold submitted requests rather than executions. ``keep_inbox``
#: is the difference between "throw away what I ran" and "throw away everything
#: including what I asked for".
INBOX_TABLES: Final[tuple[str, ...]] = ("intake_turns", "intake")

#: Everything, with no floor under it. What makes this a reset rather than a
#: reap: age stops being evidence that something is still in use.
NO_RETENTION: Final = timedelta(0)


@dataclass(frozen=True, slots=True)
class Reset:
    """What one reset removed, or would have removed under ``dry_run``."""

    #: Table name → rows removed. Tables with nothing in them are still listed,
    #: because "the audit log was already empty" and "the audit log was not
    #: touched" are different facts and a report that omits zeroes conflates them.
    rows: Mapping[str, int] = field(default_factory=dict)

    #: Acknowledged requests put back in the queue. Non-zero only with
    #: ``keep_inbox``, and the number nobody would think to look for.
    requeued: int = 0

    #: Containers, worktrees and caches. The reaper's own report type, because
    #: it is the reaper that produced it.
    debris: Reclaimed = field(default_factory=Reclaimed)

    #: Runs the store still called ``running``. Empty unless ``force`` was
    #: given, or this was a dry run — see ``reset``.
    abandoned: tuple[str, ...] = ()

    #: Whether the inbox was spared. Carried rather than inferred from which
    #: tables are missing from ``rows``: a reset that removed nothing has no
    #: rows either, and inferring would make it claim to have kept something.
    kept_inbox: bool = False

    dry_run: bool = False

    @property
    def records(self) -> int:
        return sum(self.rows.values())

    def __bool__(self) -> bool:
        return bool(self.records or self.requeued or self.debris)


async def reset(
    store: StateStore,
    *,
    at: datetime,
    work_root: Path | None = None,
    cache: Cache | None = None,
    engine: ContainerEngine | None = None,
    keep_inbox: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> Reset:
    """Empty the state database and reclaim what runs left on the machine.

    Refuses while anything is still ``running``, unless ``force``. The check is
    not paternalism: this deletes the container a live run is writing into and
    the row it is about to update, and the run does not find out — it fails
    later, somewhere else, for a reason that does not mention this command. A
    dry run never refuses, because refusing to *describe* something is not a
    protection.
    """
    live = live_runs(store)
    if live and not force and not dry_run:
        raise ResetRefused(refusal(live))

    tables = tuple(name for name in TABLES if not (keep_inbox and name in INBOX_TABLES))
    counts = _counts(store.connection, tables)
    requeued = 0

    if not dry_run:
        with transaction(store.connection) as connection:
            for table in tables:
                connection.execute(f"DELETE FROM {table}")  # noqa: S608 - from TABLES
            # AUTOINCREMENT keeps its high-water mark in ``sqlite_sequence``
            # after the last row is gone, so an emptied database would hand out
            # ``seq`` 4001 to the first event of a fresh environment. Only for
            # tables actually emptied: a kept inbox keeps its counter, or the
            # next request would collide with one still in the table.
            for table in tables:
                connection.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
        if keep_inbox:
            requeued = Intake(store).unacknowledge(at=at)
    elif keep_inbox:
        requeued = len(_acknowledged(store))

    reaper = Reaper(
        engine=engine if engine is not None else ContainerEngine(),
        work_root=work_root,
        cache=cache,
        # The three that make this a reset. An empty live set means nothing is
        # protected by ownership, and zero retention means nothing is protected
        # by age — which together are the whole difference from ``reap``.
        grace=NO_RETENTION,
        worktree_retention=NO_RETENTION,
        cache_retention=NO_RETENTION,
    )
    debris = await reaper.sweep((), dry_run=dry_run)

    return Reset(
        rows=counts,
        requeued=requeued,
        debris=debris,
        abandoned=live,
        kept_inbox=keep_inbox,
        dry_run=dry_run,
    )


def live_runs(store: StateStore) -> tuple[str, ...]:
    """Runs the store still calls ``running``. The reason to refuse."""
    return tuple(run.id for run in store.list_runs(status=RunStatus.RUNNING, limit=1000))


def refusal(live: Sequence[str]) -> str:
    """Why a reset stopped, and the three ways out of it.

    One function rather than a message built where it is raised, because a
    surface that wants to refuse *before* prompting — which is every surface
    that offers a confirmation — would otherwise write its own wording, and two
    wordings for one rule is how they stop agreeing.
    """
    shown = ", ".join(live[:3]) + (", …" if len(live) > 3 else "")
    return (
        f"{len(live)} run(s) are still running ({shown}). Stop them; or run "
        f"`clawdence runs recover` if their processes are already gone and only the "
        f"rows are left; or pass --force to reset anyway and abandon them"
    )


def _counts(connection: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    """How many rows each table holds. Read before the delete, for the report.

    Counted rather than taken from ``rowcount`` so that a dry run and a real one
    produce the same numbers by the same means — the property ``Reaper.sweep``
    keeps for the same reason.
    """
    counts: dict[str, int] = {}
    for table in tables:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608 - TABLES
        counts[table] = int(row[0])
    return counts


def _acknowledged(store: StateStore) -> list[sqlite3.Row]:
    return list(
        store.connection.execute("SELECT seq FROM intake WHERE state = 'acknowledged'").fetchall()
    )
