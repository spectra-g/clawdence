"""How many runs happen at once, and which of them may overlap.

v1 allowed one story in flight *globally*: ``find_next_assigned_story`` returned
``None`` if anything at all was active. That is a correct lock around the wrong
thing. What two runs actually contend for is a repository — its worktrees, its
warm dependency cache, and whatever lock its build tool keeps inside that cache —
and two runs on two different repositories contend for nothing but the machine.
So §3.4 replaces the global lock with two caps: **N in flight overall**, and
**``RepoProfile.max_concurrent_runs`` per repository**, which defaults to one and
therefore keeps v1's serialisation exactly where it was earning its keep.

This is a decorator over a ``RunnerPort`` rather than something inside
``AgentRunner``, and the split is the usual one: admission is a property of the
*fleet*, and a tier is a property of one run. Wrapping also means the scheduler
applies to every adapter, including the fakes, so the queue is exercised by tests
that never start a process.

Four things here are less obvious than the semaphores, and each is a way the
naive version is wrong:

**A redelivery must never take a slot.** Redelivery is not hypothetical (see
``ports.runner``): a step times out, the watchdog recovers it, the run resumes,
and the same attempt is dispatched again while the first is still working. If
that second dispatch queued for a slot of its own, then with N in flight and N
redeliveries the queue is full of dispatches waiting for runs that are waiting
for them to stop waiting — a deadlock made entirely of duplicates. So an
attempt's admission happens once, and every later dispatch of the same
idempotency key joins it.

**A settled attempt must not queue either.** Once an attempt has finished, its
answer is already known, and making a redelivery wait behind a full queue to be
told so is a slot's worth of latency for a lookup. Finished admissions are
therefore remembered — bounded, and evicted oldest-first, because the *durable*
answer to "has this attempt happened" is the ledger's unique constraint and this
map is only an optimisation over asking the inner port again.

**Cancelling a queued run must not run it.** The obvious implementation waits
for a slot, starts the work, and then stops it — which spends a slot, and on the
container tier an image pull, to produce a result that was decided before any of
it. A queued run is cancelled where it stands and reports ``CANCELLED`` without
ever reaching the tier.

**The repository slot is taken before the global one.** Both orders are
deadlock-free, since every caller takes them in the same order; the difference is
head-of-line blocking. Taking the global slot first means a run blocked on a busy
repository is holding capacity that runs on *idle* repositories could have used,
so one hot repository throttles the whole fleet. Taking the repository's first
means a run that cannot proceed is holding nothing.

**What still holds a slot forever is a hung run**, and that is deliberately not
solved here. S6c's silence detector is what notices a run that is alive, inside
its declared timeout, and saying nothing, and its recovery walks the ordinary
cancellation path — which releases the slot through the same code as any other
cancel. A scheduler that grew its own liveness heuristic would be a second
detector disagreeing with the first.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from clawdence.domain import RepoProfile, RunnerOutcome, RunnerRequest, RunnerResult
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.runner import RunnerPort, validate_result

#: Runs in flight across the whole fleet, unless configured otherwise. Three
#: rather than one, because the point of the step is that runs on different
#: repositories no longer wait for each other; and rather than "the core count",
#: because what a run costs is a container's worth of memory and an LLM
#: subscription's worth of rate limit, neither of which this can measure.
DEFAULT_LIMIT: Final = 3

#: How many finished admissions are remembered. Each is one small record, and
#: forgetting one costs a slot's worth of latency on a redelivery that arrives
#: after a thousand other attempts — which is not a correctness cost, because
#: the inner port is idempotent and the ledger is durable.
DEFAULT_REMEMBERED: Final = 1024


@dataclass(slots=True)
class _Admission:
    """One attempt's passage through the queue."""

    task: asyncio.Task[RunnerResult]

    #: Whether the inner dispatch has begun. The whole of what ``cancel`` needs
    #: to decide between stopping a queued run itself and asking the tier to
    #: stop a running one — and it is read and acted on without an ``await``
    #: between, which is what makes the decision atomic.
    started: bool = False

    #: Set only when *we* cancelled a queued run, so the coroutine can tell that
    #: cancellation from the caller's own and answer with a result instead of
    #: propagating.
    stopped: bool = False


@dataclass(slots=True)
class Scheduler:
    """Bounded concurrency in front of any ``RunnerPort``.

    ``limit`` is the fleet's; the per-repository cap comes from each request's
    own profile, so a repository that has declared it tolerates parallel installs
    gets them without the scheduler being reconfigured. The first request seen
    for a repository fixes that repository's cap for the life of this instance:
    a semaphore's size cannot change, and re-reading it per request would mean
    the cap in force depended on which run happened to arrive first anyway.
    """

    inner: RunnerPort
    limit: int = DEFAULT_LIMIT
    clock: Clock = utc_now
    max_remembered: int = DEFAULT_REMEMBERED

    _slots: asyncio.Semaphore = field(init=False)
    _per_repo: dict[str, asyncio.Semaphore] = field(init=False, default_factory=dict)
    _runs: OrderedDict[str, _Admission] = field(init=False, default_factory=OrderedDict)
    _running: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"a scheduler that admits {self.limit} runs admits none")
        self._slots = asyncio.Semaphore(self.limit)

    # ------------------------------------------------------------------ port

    async def dispatch(self, request: RunnerRequest) -> RunnerResult:
        key = request.idempotency_key
        entry = self._runs.get(key)
        mine = entry is None
        if entry is None:
            entry = _Admission(task=asyncio.create_task(self._admit(request)))
            self._remember(key, entry)

        try:
            # Shielded for the same reason ``AgentRunner.dispatch`` shields: a
            # *redelivery* being cancelled must not stop work the original
            # dispatcher is still waiting on.
            return await asyncio.shield(entry.task)
        except asyncio.CancelledError:
            if mine and not entry.task.done():
                entry.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry.task
                self._runs.pop(key, None)
            raise
        except BaseException:
            # A dispatch that *failed* is not a remembered answer. Only results
            # are worth short-circuiting a redelivery with; keeping an exception
            # would mean a daemon that was briefly unreachable stayed
            # unreachable for that attempt for the life of the process, however
            # long ago it recovered.
            self._runs.pop(key, None)
            raise

    async def cancel(self, request: RunnerRequest) -> bool:
        """Stop a run, wherever in the queue it is.

        Delegated when the work has started, because stopping it properly means
        walking the tier's collection path so whatever the agent already
        committed survives (§3.11). Handled here when it has not, because there
        is nothing to collect and nothing to ask.
        """
        entry = self._runs.get(request.idempotency_key)
        if entry is None or entry.task.done() or entry.started:
            return await self.inner.cancel(request)
        entry.stopped = True
        entry.task.cancel()
        return True

    # ------------------------------------------------------------- the queue

    async def _admit(self, request: RunnerRequest) -> RunnerResult:
        entry = self._runs[request.idempotency_key]
        started_at = self.clock()
        try:
            async with self._admitted(request.profile):
                entry.started = True
                return await self.inner.dispatch(request)
        except asyncio.CancelledError:
            if not entry.stopped:
                raise
            # Cancelled while queued, so nothing ran and there is nothing to
            # collect — but the caller asked for a run and is owed an answer
            # about it, and ``CANCELLED`` is that answer. Reporting it as a
            # failure to dispatch would send it through a retry policy written
            # for a data plane that was unreachable.
            return validate_result(
                request,
                RunnerResult(
                    run_id=request.run_id,
                    stage_id=request.stage_id,
                    outcome=RunnerOutcome.CANCELLED,
                    started_at=started_at,
                    finished_at=self.clock(),
                    message="cancelled while queued — the run never started",
                ),
            )

    @asynccontextmanager
    async def _admitted(self, profile: RepoProfile) -> AsyncIterator[None]:
        """Hold this repository's slot and one of the fleet's. In that order."""
        repo = self._repo_slots(profile)
        await repo.acquire()
        try:
            await self._slots.acquire()
        except BaseException:
            # Cancelled while waiting for the fleet. Releasing the repository's
            # slot here is not tidiness: the ``finally`` below never runs,
            # because the body was never entered, and the repository would be
            # permanently one run smaller.
            repo.release()
            raise
        self._running += 1
        try:
            yield
        finally:
            self._running -= 1
            self._slots.release()
            repo.release()

    def _repo_slots(self, profile: RepoProfile) -> asyncio.Semaphore:
        existing = self._per_repo.get(profile.id)
        if existing is None:
            existing = asyncio.Semaphore(profile.max_concurrent_runs)
            self._per_repo[profile.id] = existing
        return existing

    def _remember(self, key: str, entry: _Admission) -> None:
        self._runs[key] = entry
        while len(self._runs) > self.max_remembered:
            # Oldest first, and only if it is finished. Evicting a *running*
            # admission would let the next redelivery of it start a second one,
            # which is the one thing this class exists to prevent.
            oldest, candidate = next(iter(self._runs.items()))
            if not candidate.task.done():
                return
            del self._runs[oldest]

    # ------------------------------------------------------------ inspection

    @property
    def running(self) -> int:
        """Runs currently holding a slot."""
        return self._running

    @property
    def queued(self) -> int:
        """Admissions waiting for one."""
        return sum(
            1 for entry in self._runs.values() if not entry.started and not entry.task.done()
        )
