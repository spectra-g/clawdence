"""Making a port's failures non-fatal — once, rather than at every call site.

ARCHITECTURE's failure table says tracker and notify are "degraded, queued,
**non-fatal** — work continues". That is a policy, and policies applied by
convention at each call site drift: v1 wrapped some Slack calls in
``try/except`` and not others, so a channel outage failed roughly a third of the
runs that touched it, unpredictably. This is that policy as one object.

The division of labour is the design decision. The **ports raise**; the
**outbox decides that raising does not matter**. A port that swallowed its own
failures could not be composed with one whose failures do matter — and could not
be tested for what it does when the service is down, because it would never
say.

Four behaviours, each answering a specific way this goes wrong:

**Transient failures are held; permanent ones are not.** Retrying a 500 is worth
doing and retrying a 404 is not, and the adapter already declared which it is
(``ports.errors``). A permanent failure goes straight to the dead letters, where
it is visible, rather than consuming five retries first.

**There is no head-of-line blocking.** One undeliverable message must not hold
up the rest. A flush attempts every pending message and leaves the ones that
fail in place — the ordering that matters (a run's own updates) is preserved
because they share a destination and fail together anyway.

**The queue is bounded.** An unbounded buffer in front of a service that has
been down for a day is v1's 300MB processing log with extra steps: the symptom
is memory exhaustion, hours after the cause, and every message that could have
said "the tracker is down" is inside the buffer. At capacity, new messages are
parked as dead letters immediately — still non-fatal, but *visible*, and
bounded.

**Delivery is idempotent on the caller's key.** The same key twice is delivered
once. Both a resumed run replaying its notifications and a flush racing a
successful send land on this.

What this does not do is persist. A control plane that dies loses its pending
messages, which is the correct trade for something explicitly non-fatal: giving
progress updates the durability of the state store would mean paying for
crash-safety on the one thing the system is allowed to lose. S4b's dual-write is
where anything that genuinely must survive a restart belongs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from clawdence.ports._common import Clock, utc_now
from clawdence.ports.errors import OutboxFullError, PortError

#: Enough to absorb an outage of minutes across several concurrent runs, small
#: enough that a longer one shows up as dead letters rather than as memory.
DEFAULT_CAPACITY = 256

#: Attempts *including* the first. Past this a message is parked: a destination
#: that has refused five times is down, not slow, and the queue is more useful
#: reporting that than continuing to hope.
DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class Undelivered[T]:
    """A message the outbox is still holding, or has given up on.

    Field names match ``store.DeadLetter`` — ``reason``, ``tries`` — because
    they are the same concept at a different layer, and an operator reading two
    queues should not have to learn two vocabularies.
    """

    key: str
    message: T
    reason: str
    detail: str
    tries: int
    first_failed_at: datetime
    last_failed_at: datetime

    def describe(self) -> str:
        return f"{self.key}: {self.reason} after {self.tries} " + (
            "try" if self.tries == 1 else "tries"
        )


@dataclass(frozen=True, slots=True)
class FlushReport:
    """What one drain achieved."""

    delivered: tuple[str, ...] = ()
    still_pending: tuple[str, ...] = ()
    parked: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.still_pending and not self.parked


@dataclass(slots=True)
class Outbox[T]:
    """Holds messages a port could not deliver, and tries again later.

    ``deliver`` is a coroutine taking one message — usually a bound
    ``NotifyPort.send`` or a closure over ``TrackerPort.comment``. A callable
    rather than a port, because the two ports this wraps have different method
    names and the queueing behaviour is identical for both; typing it as the
    union of their interfaces would buy nothing and constrain the third one.
    """

    deliver: Callable[[T], Awaitable[object]]
    capacity: int = DEFAULT_CAPACITY
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    clock: Clock = utc_now

    _pending: dict[str, Undelivered[T]] = field(default_factory=dict, init=False)
    _parked: list[Undelivered[T]] = field(default_factory=list, init=False)
    _delivered: set[str] = field(default_factory=set, init=False)

    async def send(self, message: T, *, key: str) -> bool:
        """Try to deliver. ``True`` if it went out; never raises.

        A key already delivered returns ``True`` without delivering again. A key
        already pending is dropped rather than queued twice — the message it
        would replace is the same message.
        """
        if key in self._delivered:
            return True
        if key in self._pending:
            return False

        try:
            await self.deliver(message)
        except PortError as exc:
            self._hold(key, message, exc)
            return False
        else:
            self._delivered.add(key)
            return True

    async def flush(self) -> FlushReport:
        """Retry everything held, oldest first.

        Every message is attempted, including ones after a failure — see the
        module docstring on head-of-line blocking.
        """
        delivered: list[str] = []
        pending: list[str] = []
        parked: list[str] = []

        for held in tuple(self._pending.values()):
            try:
                await self.deliver(held.message)
            except PortError as exc:
                retained = self._hold(held.key, held.message, exc)
                (pending if retained else parked).append(held.key)
            else:
                del self._pending[held.key]
                self._delivered.add(held.key)
                delivered.append(held.key)

        return FlushReport(
            delivered=tuple(delivered),
            still_pending=tuple(pending),
            parked=tuple(parked),
        )

    def _hold(self, key: str, message: T, error: PortError) -> bool:
        """Record a failure. ``True`` if the message is still worth retrying."""
        now = self.clock()
        previous = self._pending.get(key)
        record = Undelivered(
            key=key,
            message=message,
            reason=error.kind,
            detail=error.message,
            tries=1 if previous is None else previous.tries + 1,
            first_failed_at=now if previous is None else previous.first_failed_at,
            last_failed_at=now,
        )

        # Three ways a message stops being worth holding, and they are checked
        # in this order because each is a stronger statement than the last:
        # the adapter says repeating changes nothing; we have already repeated
        # enough; there is no room to hold anything.
        if not error.retryable or record.tries >= self.max_attempts:
            self._park(key, record)
            return False
        if previous is None and len(self._pending) >= self.capacity:
            full = OutboxFullError(self.capacity)
            self._park(key, replace(record, reason=full.kind, detail=full.message))
            return False

        self._pending[key] = record
        return True

    def _park(self, key: str, record: Undelivered[T]) -> None:
        self._pending.pop(key, None)
        self._parked.append(record)

    @property
    def pending(self) -> Sequence[Undelivered[T]]:
        """Messages still being retried, oldest failure first."""
        return tuple(self._pending.values())

    @property
    def dead_letters(self) -> Sequence[Undelivered[T]]:
        """Messages given up on. Nothing removes these but an operator."""
        return tuple(self._parked)

    def drain_dead_letters(self) -> Sequence[Undelivered[T]]:
        """Take the dead letters, clearing them. For a reporting sweep."""
        taken = tuple(self._parked)
        self._parked.clear()
        return taken
