"""What the store raises.

Each of these names a distinct operator-visible situation. A single
``StoreError`` would be cheaper to write and would make "someone else got there
first" indistinguishable from "that run does not exist", which is the difference
between retrying and giving up.
"""

from __future__ import annotations


class StoreError(Exception):
    """Base for everything this package raises."""


class UnsupportedDatabaseError(StoreError):
    """The SQLite build underneath is too old for the schema.

    Raised at connect time rather than at the first write, because a partially
    created database is a worse thing to hand someone than a refusal.
    """


class UnknownRunError(StoreError):
    """No run with that id. Distinct from a run with nothing in it."""


class DuplicateAttemptError(StoreError):
    """A step attempt was written twice.

    The unique constraint on ``idempotency_key`` is the structural form of v1's
    hand-written duplicate-event guards — those were per-handler and drifted
    from each other, so a redelivered dispatch got through wherever the guard
    had been forgotten. Here there is one rule and the database enforces it.
    """


class ConcurrentUpdateError(StoreError):
    """A run was modified by someone else and the retries ran out.

    Optimistic concurrency: readers do not lock, writers check the version they
    read is still current. Reaching this means the contention is real rather
    than incidental, which is information worth surfacing rather than absorbing.
    """
