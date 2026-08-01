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


class MessageRejectedError(StoreError):
    """A steering message the inbox will not take (§3.11).

    Refused rather than trimmed. The message is pasted whole into the agent's
    context, and half of "do not touch the migration, only the model" is an
    instruction to do the opposite of what was meant — so the operator, who is
    at an interactive surface and can retype it, is told.
    """


class SubmissionRejectedError(StoreError):
    """An arrival the intake will not take (S10).

    Refused at the door rather than stored and dealt with later. Everything
    reaching this is a fact about the *envelope* — nothing in it, too much in
    it, or our own output coming back round — and each of those is cheaper to
    answer at the submitting surface, which still has a person in front of it,
    than three steps downstream where the only evidence left is a work item
    nobody meant to create.
    """


class UnknownSubmissionError(StoreError):
    """No arrival with that key or work item id.

    Distinct from a withdrawn one: "there is nothing to amend" and "you already
    withdrew that" are different mistakes and need different answers.
    """


class UnknownConversationError(StoreError):
    """A reply arrived for a conversation nothing was submitted under.

    Raised rather than opening a new request under it. A follow-up whose parent
    is missing is either a routing bug or a stray message, and inventing a work
    item out of half a conversation is how v1's threading failures turned into
    duplicate epics.
    """


class ConcurrentUpdateError(StoreError):
    """A run was modified by someone else and the retries ran out.

    Optimistic concurrency: readers do not lock, writers check the version they
    read is still current. Reaching this means the contention is real rather
    than incidental, which is information worth surfacing rather than absorbing.
    """
