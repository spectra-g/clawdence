"""Admission: how many runs at once, and which of them may overlap.

Against a controllable inner port rather than a real tier, because every claim
here is about the *queue* — that a slot is held, that it is released, that a
redelivery does not take one. A runner that started processes would answer those
questions much more slowly and no more convincingly.

The gate is the shape most of these take: an inner dispatch that blocks until the
test lets it go. That is what makes "is this run holding a slot" observable at
all — without it, every dispatch finishes before the next one starts and the
scheduler is untested no matter how many tests are pointed at it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from clawdence.domain import (
    Budget,
    ContractKind,
    DiffStat,
    RepoProfile,
    RunnerOutcome,
    RunnerRequest,
    RunnerResult,
    VerificationContract,
)
from clawdence.ports import PermanentError
from clawdence.runners import Scheduler
from tests.ports import factories as make
from tests.ports.factories import run
from tests.runners.conftest import container_profile


@dataclass
class GatedRunner:
    """An inner ``RunnerPort`` whose dispatches finish when the test says so.

    Idempotent, because the contract requires every adapter to be and because
    the scheduler is entitled to rely on it: the redelivery tests below are
    about the scheduler not *asking* twice, and an inner port that ran twice
    would make them pass for the wrong reason.
    """

    gate: asyncio.Event = field(default_factory=asyncio.Event)
    started: list[str] = field(default_factory=list)
    settled: dict[str, RunnerResult] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    concurrent: int = 0
    peak: int = 0
    per_repo_peak: dict[str, int] = field(default_factory=dict)
    _in_repo: dict[str, int] = field(default_factory=dict)

    async def dispatch(self, request: RunnerRequest) -> RunnerResult:
        existing = self.settled.get(request.idempotency_key)
        if existing is not None:
            return existing

        self.started.append(request.idempotency_key)
        repo = request.profile.id
        self.concurrent += 1
        self._in_repo[repo] = self._in_repo.get(repo, 0) + 1
        self.peak = max(self.peak, self.concurrent)
        self.per_repo_peak[repo] = max(self.per_repo_peak.get(repo, 0), self._in_repo[repo])
        try:
            await self.gate.wait()
        finally:
            self.concurrent -= 1
            self._in_repo[repo] -= 1

        result = succeeded(request)
        self.settled[request.idempotency_key] = result
        return result

    async def cancel(self, request: RunnerRequest) -> bool:
        self.cancelled.append(request.idempotency_key)
        return request.idempotency_key not in self.settled

    def release(self) -> None:
        self.gate.set()


def succeeded(request: RunnerRequest) -> RunnerResult:
    return RunnerResult(
        run_id=request.run_id,
        stage_id=request.stage_id,
        outcome=RunnerOutcome.SUCCEEDED,
        tree_hash=make.commit(2),
        diff=DiffStat(files_changed=1, insertions=1, deletions=0),
        started_at=make.at(0),
        finished_at=make.at(1),
    )


def request_for(
    repo: str = "repo.one",
    *,
    stage: str = "code",
    attempt: int = 1,
    max_concurrent_runs: int = 1,
) -> RunnerRequest:
    profile: RepoProfile = container_profile(id=repo, max_concurrent_runs=max_concurrent_runs)
    return RunnerRequest(
        run_id=f"run.{repo}.{stage}.{attempt}",
        stage_id=stage,
        work_item_id="wi.test",
        worktree_path=f"/clawdence/work/{repo}-{stage}-{attempt}",
        branch=f"clawdence/{stage}",
        base_commit=make.commit(1),
        profile=profile,
        contract=VerificationContract(kind=ContractKind.TEST_AFTER),
        budget=Budget(),
        plan="do the thing",
        idempotency_key=f"{repo}:{stage}:{attempt}",
        created_at=make.at(0),
    )


async def _settle(tasks: Sequence[asyncio.Task[RunnerResult]] = ()) -> None:
    """Let the loop run until nothing is ready. The alternative is a sleep."""
    for _ in range(50):
        await asyncio.sleep(0)
    assert all(not task.done() or task.exception() is None for task in tasks)


# --------------------------------------------------------------------------- #
# The two caps
# --------------------------------------------------------------------------- #


def test_no_more_than_the_limit_run_at_once() -> None:
    """Four runs on four repositories, two slots. The cap is the whole point."""

    async def scenario() -> tuple[int, int]:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=2)
        tasks = [
            asyncio.create_task(scheduler.dispatch(request_for(f"repo.{index}")))
            for index in range(4)
        ]
        await _settle(tasks)
        admitted = inner.peak
        inner.release()
        await asyncio.gather(*tasks)
        return admitted, inner.peak

    while_running, overall = run(scenario())
    assert while_running == 2
    assert overall == 2


def test_runs_on_different_repositories_do_not_wait_for_each_other() -> None:
    """The change from v1, stated directly.

    v1's lock was global: one story in flight anywhere blocked every other. Two
    repositories with a per-repo cap of one apiece must still run in parallel,
    or the per-repo cap has quietly become the global one again.
    """

    async def scenario() -> int:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=4)
        tasks = [
            asyncio.create_task(scheduler.dispatch(request_for("repo.one"))),
            asyncio.create_task(scheduler.dispatch(request_for("repo.two"))),
        ]
        await _settle(tasks)
        peak = inner.peak
        inner.release()
        await asyncio.gather(*tasks)
        return peak

    assert run(scenario()) == 2


def test_one_repository_is_serialised_by_default() -> None:
    """``max_concurrent_runs`` defaults to one, and that default is deliberate:
    two runs on one repository share a warm cache and whatever lock the build
    tool keeps inside it."""

    async def scenario() -> int:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=4)
        tasks = [
            asyncio.create_task(
                scheduler.dispatch(request_for("repo.one", stage="code", attempt=index))
            )
            for index in (1, 2, 3)
        ]
        await _settle(tasks)
        peak = inner.per_repo_peak["repo.one"]
        inner.release()
        await asyncio.gather(*tasks)
        return peak

    assert run(scenario()) == 1


def test_a_repository_that_declares_it_tolerates_parallel_runs_gets_them() -> None:
    async def scenario() -> int:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=4)
        tasks = [
            asyncio.create_task(
                scheduler.dispatch(request_for("repo.one", attempt=index, max_concurrent_runs=3))
            )
            for index in (1, 2, 3)
        ]
        await _settle(tasks)
        peak = inner.per_repo_peak["repo.one"]
        inner.release()
        await asyncio.gather(*tasks)
        return peak

    assert run(scenario()) == 3


def test_a_finished_run_gives_its_slot_back() -> None:
    """Otherwise the queue drains once and then never again."""

    async def scenario() -> int:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=1)
        first = asyncio.create_task(scheduler.dispatch(request_for("repo.one")))
        second = asyncio.create_task(scheduler.dispatch(request_for("repo.two")))
        await _settle([first, second])
        assert len(inner.started) == 1

        inner.release()
        await asyncio.gather(first, second)
        return len(inner.started)

    assert run(scenario()) == 2


def test_a_scheduler_that_admits_nothing_is_refused_at_construction() -> None:
    """A limit of zero is a queue that never drains, and the failure it produces
    is a run that hangs rather than one that says why."""
    with pytest.raises(ValueError, match="admits none"):
        Scheduler(inner=GatedRunner(), limit=0)


# --------------------------------------------------------------------------- #
# Redelivery
# --------------------------------------------------------------------------- #


def test_a_redelivery_of_an_in_flight_attempt_does_not_take_a_second_slot() -> None:
    """The deadlock this class exists to prevent.

    A step times out, the watchdog recovers it, and the run resumes while the
    original is still working — so the same attempt is dispatched again. If that
    queued for a slot of its own, then with N in flight and N redeliveries the
    queue is entirely made of dispatches waiting for the runs that are waiting
    for them.
    """

    async def scenario() -> tuple[int, int, bool]:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=1)
        original = asyncio.create_task(scheduler.dispatch(request_for("repo.one")))
        await _settle([original])

        redelivery = asyncio.create_task(scheduler.dispatch(request_for("repo.one")))
        await _settle([original, redelivery])

        queued, running = scheduler.queued, scheduler.running
        inner.release()
        first, second = await asyncio.gather(original, redelivery)
        return queued, running, first == second and len(inner.started) == 1

    queued, running, joined = run(scenario())
    assert (queued, running) == (0, 1)
    assert joined


def test_a_redelivery_of_a_settled_attempt_never_queues() -> None:
    """A finished attempt's answer is already known, and making a redelivery
    wait behind a full queue to be told so is a slot's worth of latency for a
    lookup."""

    async def scenario() -> RunnerResult:
        inner = GatedRunner()
        inner.release()
        scheduler = Scheduler(inner=inner, limit=1)
        first = await scheduler.dispatch(request_for("repo.one"))

        # Fill every slot with something that will not finish.
        blocker = GatedRunner()
        held = Scheduler(inner=blocker, limit=1)
        holding = asyncio.create_task(held.dispatch(request_for("repo.other")))
        await _settle([holding])

        again = await asyncio.wait_for(scheduler.dispatch(request_for("repo.one")), timeout=1)
        blocker.release()
        await holding
        assert first == again
        return again

    assert run(scenario()).outcome is RunnerOutcome.SUCCEEDED


def test_cancelling_a_redelivery_does_not_stop_the_original() -> None:
    """The same shielding ``AgentRunner.dispatch`` needs, one layer up: a caller
    that gave up is not a reason to abandon work another caller is waiting on."""

    async def scenario() -> RunnerOutcome:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=2)
        original = asyncio.create_task(scheduler.dispatch(request_for("repo.one")))
        await _settle([original])

        redelivery = asyncio.create_task(scheduler.dispatch(request_for("repo.one")))
        await _settle([original, redelivery])
        redelivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await redelivery

        inner.release()
        return (await original).outcome

    assert run(scenario()) is RunnerOutcome.SUCCEEDED


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


def test_cancelling_a_queued_run_stops_it_where_it_stands() -> None:
    """It never reaches the tier, and it reports ``CANCELLED`` rather than an
    error.

    The obvious implementation waits for a slot, starts the work and then stops
    it — spending a slot, and on the container tier an image pull, to reach a
    conclusion that was decided before any of it.
    """

    async def scenario() -> tuple[bool, RunnerResult, Sequence[str]]:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=1)
        holding = asyncio.create_task(scheduler.dispatch(request_for("repo.one")))
        await _settle([holding])

        waiting = request_for("repo.two")
        queued = asyncio.create_task(scheduler.dispatch(waiting))
        await _settle([holding, queued])
        assert scheduler.queued == 1

        stopped = await scheduler.cancel(waiting)
        result = await queued
        inner.release()
        await holding
        return stopped, result, tuple(inner.started)

    stopped, result, started = run(scenario())
    assert stopped is True
    assert result.outcome is RunnerOutcome.CANCELLED
    assert "repo.two:code:1" not in started


def test_cancelling_a_running_run_is_the_tier_s_job() -> None:
    """Delegated, because stopping it properly means walking the collection path
    so whatever the agent already committed survives (§3.11). The scheduler has
    nothing to collect and no idea how to."""

    async def scenario() -> tuple[bool, Sequence[str]]:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=1)
        running = request_for("repo.one")
        task = asyncio.create_task(scheduler.dispatch(running))
        await _settle([task])

        stopped = await scheduler.cancel(running)
        inner.release()
        await task
        return stopped, tuple(inner.cancelled)

    stopped, cancelled = run(scenario())
    assert stopped is True
    assert cancelled == ("repo.one:code:1",)


def test_the_original_dispatcher_giving_up_releases_the_attempt_entirely() -> None:
    """A caller that was cancelled is not the same as a run that was cancelled.

    Nobody asked this scheduler to stop the run — the *waiter* went away, which
    on the executor's side is a step unwinding. So the cancellation propagates
    rather than being answered with a ``CANCELLED`` result, and the attempt is
    forgotten, because remembering a cancelled admission would make every later
    dispatch of that key raise ``CancelledError`` from a task nobody is running.
    """

    async def scenario() -> RunnerOutcome:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=1)
        holding = asyncio.create_task(scheduler.dispatch(request_for("repo.one")))
        await _settle([holding])

        gave_up = asyncio.create_task(scheduler.dispatch(request_for("repo.two")))
        await _settle([holding, gave_up])
        gave_up.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gave_up

        inner.release()
        await holding
        # Forgotten, so the same attempt can genuinely be dispatched again.
        return (await scheduler.dispatch(request_for("repo.two"))).outcome

    assert run(scenario()) is RunnerOutcome.SUCCEEDED


def test_cancelling_something_that_was_never_dispatched_is_not_an_error() -> None:
    """The watchdog deciding a step is overdue races the step reporting, and
    that race must not itself produce a failure."""

    async def scenario() -> bool:
        inner = GatedRunner()
        inner.release()
        scheduler = Scheduler(inner=inner, limit=1)
        request = request_for("repo.one")
        await scheduler.dispatch(request)
        return await scheduler.cancel(request)

    assert run(scenario()) is False


# --------------------------------------------------------------------------- #
# Bookkeeping
# --------------------------------------------------------------------------- #


def test_finished_admissions_are_forgotten_once_there_are_too_many() -> None:
    """Bounded, because a long-lived control plane dispatches forever.

    Forgetting one is not a correctness cost: the next redelivery re-admits and
    the inner port, which the contract requires to be idempotent, answers from
    its own record without running anything.
    """

    async def scenario() -> tuple[int, int]:
        inner = GatedRunner()
        inner.release()
        scheduler = Scheduler(inner=inner, limit=4, max_remembered=3)
        for index in range(1, 8):
            await scheduler.dispatch(request_for("repo.one", attempt=index))
        return len(scheduler._runs), len(inner.started)

    remembered, started = run(scenario())
    assert remembered == 3
    assert started == 7


def test_a_running_admission_is_never_forgotten_however_full_the_map_is() -> None:
    """Evicting one would let the next redelivery of it start a second run,
    which is the single thing this class exists to prevent."""

    async def scenario() -> tuple[int, int]:
        inner = GatedRunner()
        scheduler = Scheduler(inner=inner, limit=4, max_remembered=1)
        tasks = [
            asyncio.create_task(scheduler.dispatch(request_for(f"repo.{index}")))
            for index in range(3)
        ]
        await _settle(tasks)

        again = asyncio.create_task(scheduler.dispatch(request_for("repo.0")))
        await _settle([*tasks, again])
        inner.release()
        await asyncio.gather(*tasks, again)
        return len(inner.started), len(set(inner.started))

    started, unique = run(scenario())
    assert started == unique == 3


def test_a_dispatch_that_fails_is_not_remembered_as_an_answer() -> None:
    """Only results short-circuit a redelivery.

    Remembering the exception instead would mean a data plane that was briefly
    unreachable stayed unreachable *for that attempt* for the life of the
    process, however long ago it recovered — and the caller has no way to ask
    again, because asking again is what it just did.

    The slot has to come back too. A failed dispatch that leaked one would
    shrink the fleet's capacity by one every time the daemon hiccupped.
    """

    @dataclass
    class SometimesRefusing:
        attempts: int = 0

        async def dispatch(self, request: RunnerRequest) -> RunnerResult:
            self.attempts += 1
            if self.attempts == 1:
                raise PermanentError("no-runner", "nothing is wired")
            return succeeded(request)

        async def cancel(self, request: RunnerRequest) -> bool:
            return False

    async def scenario() -> tuple[RunnerOutcome, int]:
        inner = SometimesRefusing()
        scheduler = Scheduler(inner=inner, limit=2)
        request = request_for("repo.one")

        with pytest.raises(PermanentError):
            await scheduler.dispatch(request)
        result = await scheduler.dispatch(request)
        return result.outcome, scheduler.running

    outcome, running = run(scenario())
    assert outcome is RunnerOutcome.SUCCEEDED
    assert running == 0
