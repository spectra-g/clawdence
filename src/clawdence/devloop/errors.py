"""What the dev loop refuses to do."""

from __future__ import annotations


class DevLoopError(Exception):
    """Base for everything this package raises."""


class ResetRefused(DevLoopError):
    """Something is still running, and a reset would pull the floor out from it.

    Separate from ``store.UnknownRunError`` and friends because it is not a
    store failure: the database is fine, and what has been refused is a
    judgement about the world outside it.
    """
