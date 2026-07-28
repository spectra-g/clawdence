"""Small things every module in this package needs, and no consumer does.

Private because the alternative is worse in both directions: duplicating a
five-character prefix across three modules is how two of them eventually
disagree, and exporting it would put an implementation detail of the fakes into
the package's public surface.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

#: Prefix on every identifier a *null* adapter mints. It has to be obviously
#: unreal: an id shaped like ``PROJ-14`` or ``msg.7`` ends up in a notification
#: telling somebody to go and read something that was never written.
NULL_PREFIX: Final = "null:"

#: One clock type, injected everywhere, for the reason the engine already does
#: it (``executor.Clock``): a fake that stamps ``datetime.now()`` makes every
#: assertion about ordering, duration or "is this overdue" depend on how fast
#: the machine running the test happens to be.
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def counting_clock(start: datetime, step_seconds: float = 1.0) -> Clock:
    """Advances a fixed amount per call. For fakes and tests."""
    ticks = 0

    def clock() -> datetime:
        nonlocal ticks
        now = start + timedelta(seconds=step_seconds * ticks)
        ticks += 1
        return now

    return clock
