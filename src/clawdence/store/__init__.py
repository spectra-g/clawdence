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
                └─ audit ─ state ─ control · intake ─ ledger ─ watchdog · effects

``ledger`` is the seam with the engine: it satisfies ``engine.Ledger``, so the
executor persists a run without knowing that it does. ``control`` is the same
kind of seam facing the other way (S6c): it satisfies ``ports.ControlPort``, so
a runner can be steered and stopped mid-flight without importing this package.
``intake`` is the third (S10) and satisfies ``ports.IngestPort`` — durable
because the CLI adapter's arrival and the pipeline's collection are two
different processes, so an in-memory dedupe guard would guard nothing.
``effects`` closes the transaction gap between state transitions and external
adapters. Its commands are immutable and generic lifecycle owns claim, retry,
parking and delivery; adapters still own effect-specific idempotency.
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
from clawdence.store.effects import (
    BASE_BACKOFF_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    EffectKind,
    EffectState,
    ExternalEffect,
    ExternalEffects,
    new_effect_id,
)
from clawdence.store.errors import (
    ConcurrentUpdateError,
    DuplicateAttemptError,
    MessageRejectedError,
    StateOperationError,
    StoreError,
    SubmissionRejectedError,
    UnknownConversationError,
    UnknownRunError,
    UnknownSubmissionError,
    UnsupportedDatabaseError,
)
from clawdence.store.intake import (
    Admission,
    ArrivalState,
    Disposition,
    Intake,
    StoreIngest,
    Turn,
)
from clawdence.store.ledger import SqliteLedger
from clawdence.store.operations import (
    DatabaseCopy,
    RewriteReport,
    backup,
    restore,
    tombstone_and_rewrite,
)
from clawdence.store.publication import Publication, Publications, PublicationState
from clawdence.store.redaction import redact, redact_text, redact_value
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
    "BASE_BACKOFF_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_SILENCE_SECONDS",
    "IN_MEMORY",
    "MAX_CLAIM",
    "SCHEMA_VERSION",
    "Admission",
    "ArrivalState",
    "AuditLog",
    "Cancellations",
    "ConcurrentUpdateError",
    "DatabaseCopy",
    "DeadLetter",
    "Disposition",
    "DuplicateAttemptError",
    "EffectKind",
    "EffectState",
    "ExternalEffect",
    "ExternalEffects",
    "Inbox",
    "Intake",
    "MessageRejectedError",
    "MessageState",
    "Publication",
    "PublicationState",
    "Publications",
    "Redactor",
    "ReplayReport",
    "RewriteReport",
    "SqliteLedger",
    "Stall",
    "StallKind",
    "StateOperationError",
    "StateStore",
    "SteeringMessage",
    "StoreControl",
    "StoreError",
    "StoreIngest",
    "SubmissionRejectedError",
    "Turn",
    "UnknownConversationError",
    "UnknownRunError",
    "UnknownSubmissionError",
    "UnsupportedDatabaseError",
    "backup",
    "connect",
    "detect",
    "migrate",
    "new_effect_id",
    "recover",
    "redact",
    "redact_text",
    "redact_value",
    "restore",
    "sweep",
    "tombstone_and_rewrite",
    "transaction",
    "unscreened",
]
