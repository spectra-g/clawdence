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
                └─ audit ─ state ─ control ─ ledger ─ watchdog

``ledger`` is the seam with the engine: it satisfies ``engine.Ledger``, so the
executor persists a run without knowing that it does. ``control`` is the same
kind of seam facing the other way (S6c): it satisfies ``ports.ControlPort``, so
a runner can be steered and stopped mid-flight without importing this package.
"""

from __future__ import annotations

from clawdence.store.audit import (
    AuditLog,
    DeadLetter,
    Redactor,
    ReplayReport,
    unscreened,
)
from clawdence.store.control import (
    MAX_CLAIM,
    Cancellations,
    Inbox,
    MessageState,
    SteeringMessage,
    StoreControl,
)
from clawdence.store.errors import (
    ConcurrentUpdateError,
    DuplicateAttemptError,
    MessageRejectedError,
    StoreError,
    UnknownRunError,
    UnsupportedDatabaseError,
)
from clawdence.store.ledger import SqliteLedger
from clawdence.store.schema import IN_MEMORY, SCHEMA_VERSION, connect, migrate, transaction
from clawdence.store.state import StateStore
from clawdence.store.watchdog import (
    DEFAULT_SILENCE_SECONDS,
    Stall,
    StallKind,
    detect,
    recover,
    sweep,
)

__all__ = [
    "DEFAULT_SILENCE_SECONDS",
    "IN_MEMORY",
    "MAX_CLAIM",
    "SCHEMA_VERSION",
    "AuditLog",
    "Cancellations",
    "ConcurrentUpdateError",
    "DeadLetter",
    "DuplicateAttemptError",
    "Inbox",
    "MessageRejectedError",
    "MessageState",
    "Redactor",
    "ReplayReport",
    "SqliteLedger",
    "Stall",
    "StallKind",
    "StateStore",
    "SteeringMessage",
    "StoreControl",
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
