"""The state store — SQLite tables as the source of truth.

ADR-0005 chose a state table over event sourcing, and this package is that
decision made concrete. ``runs`` and ``steps`` hold what is true; ``audit``
records what happened and is explicitly *not* replayed to recover
state. What falls out of the inversion is the point: crash-resume is a
``SELECT``, idempotency is a unique constraint, a schema change is an ordinary
migration, and there are no upcasters, snapshots, compaction or projection
rebuilds to keep working forever.

The layering, as elsewhere, is one-directional::

    errors ─ schema ─ codec
                └─ audit ─ state ─ ledger ─ watchdog

``ledger`` is the seam with the engine: it satisfies ``engine.Ledger``, so the
executor persists a run without knowing that it does.
"""

from __future__ import annotations

from clawdence.store.audit import (
    AuditLog,
    DeadLetter,
    Redactor,
    ReplayReport,
    unscreened,
)
from clawdence.store.errors import (
    ConcurrentUpdateError,
    DuplicateAttemptError,
    StoreError,
    UnknownRunError,
    UnsupportedDatabaseError,
)
from clawdence.store.ledger import SqliteLedger
from clawdence.store.schema import IN_MEMORY, SCHEMA_VERSION, connect, migrate, transaction
from clawdence.store.state import StateStore
from clawdence.store.watchdog import Stall, StallKind, detect, recover, sweep

__all__ = [
    "IN_MEMORY",
    "SCHEMA_VERSION",
    "AuditLog",
    "ConcurrentUpdateError",
    "DeadLetter",
    "DuplicateAttemptError",
    "Redactor",
    "ReplayReport",
    "SqliteLedger",
    "Stall",
    "StallKind",
    "StateStore",
    "StoreError",
    "UnknownRunError",
    "UnsupportedDatabaseError",
    "connect",
    "detect",
    "migrate",
    "recover",
    "sweep",
    "transaction",
    "unscreened",
]
