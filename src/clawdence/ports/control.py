"""Talking to a run that has already started (§3.11).

Everything else the runner does is settled before the agent is spawned: the plan
is built, the environment is assembled, the container is started, and from then
until the process exits a run is **write-only**. Output comes out — S6 fixed
that — and nothing goes in. Two operational failures come straight out of that
shape:

*You watch a run go the wrong way for thirty minutes.* The agent misread the
plan in its second paragraph and everything after it is wasted, and there is no
way to say one sentence to it.

*You cannot stop a run from anywhere except the process that started it.*
``RunnerPort.cancel`` exists and works, and it works only for the caller holding
the ``asyncio.Task``. An operator at a terminal, a watchdog in another process,
and HQ are all somewhere else.

Both are the same missing thing — a channel *into* a live run — so both are one
port. The third piece of §3.11, the heartbeat, is here for a related reason
rather than the same one: the detector that finds a run which is alive, inside
its timeout, and silent needs somebody to tell it what "heard from" means, and
the only process that knows is the one reading the agent's output.

**The runner does not know it is a database.** This is a ``Protocol`` for the
same reason ``engine.Ledger`` is one: the runner is in the data plane's half of
the code and importing ``store`` there would make the tier depend on how the
control plane persists things. ``store.control.StoreControl`` is the adapter;
``NoControl`` is what a runner nobody wired one to gets.

**Polling, and not a subscription.** The runner is already running an event loop
around a subprocess and a container it may have to kill; a second inbound
transport — a socket, a signal handler, a watched file — is a second thing that
can wedge. A ``SELECT`` every few seconds against a local SQLite file costs
nothing next to the agent it is supervising, and the latency it buys is bounded
by ``DEFAULT_POLL_SECONDS`` rather than by whether a notification was missed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final, Protocol

#: How long a steering message may be. It is pasted straight into the agent's
#: context, so an unbounded message is an unbounded prompt — and the operator
#: sending it is at an interactive surface where a refusal is readable. Enforced
#: at ``send``, because truncating an instruction is a worse failure than
#: refusing one: the half that survives can invert the meaning of the half that
#: does not.
MAX_STEERING_CHARS: Final = 4_000

#: Gap between polls, and so the worst case for how long an operator waits
#: between saying something and the agent being able to see it. Seconds rather
#: than sub-second because the agent picks messages up *on its next turn*, and a
#: turn is tens of seconds at best.
DEFAULT_POLL_SECONDS: Final = 3.0


@dataclass(frozen=True, slots=True)
class Steer:
    """One thing somebody said to a run in flight."""

    id: str
    body: str

    #: Higher goes first. The claim rule is priority-desc, then arrival order
    #: within a class — so "stop touching the database layer" can be put in
    #: front of three queued suggestions without cancelling them.
    priority: int = 0

    sender: str = "operator"

    #: Claim order within the run: 1 is the first message this run was ever
    #: handed. It names the file the agent reads, which is how the order the
    #: inbox chose survives into a directory listing.
    ordinal: int = 0

    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Cancellation:
    """Somebody outside this process wants the run stopped."""

    run_id: str
    reason: str
    requested_by: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Signal:
    """What the outside world has said since the last poll.

    Both halves in one answer because the runner asks one question — "anything
    for me?" — and a cancel that arrived in the same poll as a steering message
    should not depend on which of two calls the runner made first.
    """

    messages: tuple[Steer, ...] = ()
    cancel: Cancellation | None = None


class ControlPort(Protocol):
    """The channel into a run that is already going."""

    async def poll(self, run_id: str) -> Signal:
        """Claim whatever is waiting for this run.

        **Claiming is delivering.** A message this returns is already recorded
        as delivered, and no later poll — in this process or in one that
        replaces it after a crash — will return it again. That is the direction
        the ambiguity is resolved in on purpose: a steering message the agent
        acts on twice is an instruction followed twice, while one that is lost
        because the control plane died between claiming it and writing it out is
        recorded as ``failed`` and visible to the person who sent it.

        Must not raise for an unknown or finished run; there is nothing waiting
        for one, which is an empty ``Signal``.
        """
        ...

    async def heartbeat(self, run_id: str, *, at: datetime) -> None:
        """Report that something was heard from this run at ``at``.

        Called only when there was something to report. The silence detector
        keys on the absence of these, so a heartbeat sent on a timer regardless
        of whether the agent said anything would make every hung run look
        healthy — which is exactly the reporting a wedged run produces.
        """
        ...


class NoControl:
    """The default: nothing gets in, nothing is recorded.

    Deliberately not a refusal, unlike ``RefusingRunner``. A runner with no
    control source is a runner nobody can steer or stop from outside, which is
    precisely what S6 shipped and is a real configuration rather than a
    misconfiguration. Nothing here reports work that did not happen, so the
    reasoning that makes a stub runner dangerous does not apply.
    """

    __slots__ = ()

    async def poll(self, run_id: str) -> Signal:
        return Signal()

    async def heartbeat(self, run_id: str, *, at: datetime) -> None:
        return None


@dataclass(slots=True)
class InMemoryControl:
    """The fake. Holds messages until a run polls for them.

    Keeps the same claim discipline as the real one — priority-desc, then the
    order things were sent, and a message goes out exactly once — because a fake
    that redelivers is a fake that hides the bug the durable one exists to
    prevent.
    """

    #: Every heartbeat that arrived, as ``(run_id, at)``. What a test reads to
    #: assert that a talkative run reports and a silent one does not.
    beats: list[tuple[str, datetime]] = field(default_factory=list)

    #: Messages handed out, in claim order, per run.
    claimed: list[Steer] = field(default_factory=list)

    _runs: dict[str, list[Steer]] = field(default_factory=dict, init=False)
    _cancels: dict[str, Cancellation] = field(default_factory=dict, init=False)
    _sent: int = field(default=0, init=False)
    _ordinals: dict[str, int] = field(default_factory=dict, init=False)

    def send(
        self,
        run_id: str,
        body: str,
        *,
        priority: int = 0,
        sender: str = "operator",
        at: datetime | None = None,
    ) -> Steer:
        self._sent += 1
        message = Steer(
            id=f"st.{self._sent:04d}",
            body=body,
            priority=priority,
            sender=sender,
            at=at,
        )
        self._runs.setdefault(run_id, []).append(message)
        return message

    def cancel(
        self,
        run_id: str,
        *,
        reason: str = "an operator asked",
        requested_by: str = "operator",
        at: datetime,
    ) -> Cancellation:
        request = Cancellation(run_id=run_id, reason=reason, requested_by=requested_by, at=at)
        self._cancels.setdefault(run_id, request)
        return self._cancels[run_id]

    async def poll(self, run_id: str) -> Signal:
        claimed: list[Steer] = []
        for message in in_claim_order(self._runs.pop(run_id, [])):
            self._ordinals[run_id] = self._ordinals.get(run_id, 0) + 1
            claimed.append(replace(message, ordinal=self._ordinals[run_id]))
        self.claimed.extend(claimed)
        return Signal(messages=tuple(claimed), cancel=self._cancels.get(run_id))

    async def heartbeat(self, run_id: str, *, at: datetime) -> None:
        self.beats.append((run_id, at))


def in_claim_order(messages: Sequence[Steer]) -> tuple[Steer, ...]:
    """Priority first, then the order they arrived in.

    Shared so the fake and the durable inbox cannot disagree about what the
    claim rule is — the ``ORDER BY`` and this sort are the same sentence written
    twice otherwise, and the one that drifts is the one nobody tested.
    """
    return tuple(
        message
        for _, message in sorted(enumerate(messages), key=lambda pair: (-pair[1].priority, pair[0]))
    )
