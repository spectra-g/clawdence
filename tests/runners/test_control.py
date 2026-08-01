"""Mid-run control against real processes, on both tiers (§3.11).

Every claim here is about a run that is *already going*, which is why nothing in
this file sets something up and then dispatches: the message is sent, and the
cancel is issued, from a second task while the agent is running — with a real
``StateStore`` in between, because "from outside" is the entire point of the
feature and a fake handed to the runner at construction is inside.

The agent-side half is a real subprocess too. It waits for a file to appear in
the steering directory and writes back what it found, so a test that passes is
one where the store, the runner's poll loop, the worktree and a separate process
all agreed — rather than one where a mock was called.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest

from clawdence.domain import RunnerOutcome, RunnerRequest, RunnerResult
from clawdence.ports import InMemoryControl, NoControl
from clawdence.runners import STEERING_DIR, ContainerRunner, HostRunner
from clawdence.store import (
    IN_MEMORY,
    Cancellations,
    Inbox,
    MessageState,
    StateStore,
    StoreControl,
)
from tests.harness.agent import FakeAgent
from tests.harness.engine import FakeEngine
from tests.harness.repos import FixtureRepo
from tests.ports.factories import run as await_
from tests.runners.conftest import PINNED_IMAGE, RequestFactory, container_profile
from tests.store.factories import RUN_ID, at, make_run

#: Fast enough that a test does not wait on it, slow enough that the loop is a
#: loop rather than a spin. The product default is three seconds.
POLL = 0.02

#: Where the fake agent writes what it saw. Under the runner's own directory so
#: it never counts as the agent leaving work uncommitted.
SEEN = ".clawdence/seen.txt"

#: Written by the agent once its commit has landed. Same directory, same reason.
DID_THE_WORK = ".clawdence/committed"

CHANGED = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TESTS_PASSED = {"reporter": "pytest-json-report", "total": 4, "passed": 4}


@pytest.fixture
def state_with_run() -> Iterator[StateStore]:
    """A store that already knows about ``run.test`` — the id every request uses.

    Opened here rather than borrowed from ``tests/conftest``: that one is
    scoped to the store's own suite, and the project runs with warnings as
    errors, so an unclosed connection fails the build.
    """
    with StateStore.open(IN_MEMORY) as store:
        store.create_run(make_run(RUN_ID))
        yield store


@pytest.fixture
def control(state_with_run: StateStore) -> StoreControl:
    return StoreControl(state_with_run, clock=lambda: at(1))


def listens(**verdict: Any) -> FakeAgent:
    """An agent that waits for a message, records it, then finishes normally."""
    verdict.setdefault("tests", TESTS_PASSED)
    return (
        FakeAgent()
        .say("starting")
        .await_steering(SEEN)
        .write("app.py", CHANGED)
        .commit()
        .verdict(**verdict)
    )


async def _while_running(
    dispatch: Coroutine[Any, Any, RunnerResult],
    action: Callable[[], object],
    *,
    when: Callable[[], bool] | None = None,
) -> RunnerResult:
    """Do something to a run that is already in flight.

    ``when`` waits for a condition the agent creates rather than for a duration,
    for the reason ``test_container._until`` gives: a fixed sleep long enough on
    an idle machine is the flake that only shows up in a full suite run.

    ``action`` may return anything — ``send`` and ``request`` both hand back the
    row they wrote — and what it returns is discarded here: the assertion is
    always about what the *run* did afterwards.
    """

    async def meanwhile() -> None:
        deadline = time.monotonic() + 20.0
        while when is not None and not when():
            assert time.monotonic() < deadline, "the agent never got going"
            await asyncio.sleep(POLL)
        action()

    result, _ = await asyncio.gather(dispatch, meanwhile())
    assert isinstance(result, RunnerResult)
    return result


def started(repo: FixtureRepo) -> Callable[[], bool]:
    """True once the runner has set the worktree up for this attempt."""
    return lambda: (repo.path / STEERING_DIR).is_dir()


def committed(repo: FixtureRepo) -> Callable[[], bool]:
    """True once the agent has committed and is into its long sleep.

    Waiting for the *setup* is not enough for the cancel tests: the point of
    them is that work already done survives being stopped, and a cancel that
    landed before the agent got going would prove nothing about that.
    """
    return lambda: (repo.path / DID_THE_WORK).is_file()


# --------------------------------------------------------------------------- #
# Steering
# --------------------------------------------------------------------------- #


class TestSteering:
    def test_a_message_sent_in_flight_reaches_the_agent(
        self,
        request_for: RequestFactory,
        repo: FixtureRepo,
        control: StoreControl,
    ) -> None:
        """The headline claim, end to end and through three processes."""
        runner = HostRunner(listens().command(), control=control, poll_seconds=POLL)
        request = request_for()

        result = await_(
            _while_running(
                runner.dispatch(request),
                lambda: control.inbox.send(RUN_ID, "use the existing parser", at=at(0)),
                when=started(repo),
            )
        )

        assert result.outcome is RunnerOutcome.SUCCEEDED
        assert "use the existing parser" in (repo.path / SEEN).read_text(encoding="utf-8")

    def test_the_agent_is_told_where_to_look_before_anything_is_there(
        self, request_for: RequestFactory, repo: FixtureRepo
    ) -> None:
        """The plan names the directory on every run, so it has to exist on
        every run — including the ones nobody ever steers."""
        runner = HostRunner(FakeAgent().say("nothing to see").command())
        await_(runner.dispatch(request_for()))

        assert (repo.path / STEERING_DIR).is_dir()

    def test_a_delivered_message_is_recorded_as_delivered(
        self,
        request_for: RequestFactory,
        repo: FixtureRepo,
        control: StoreControl,
        state_with_run: StateStore,
    ) -> None:
        runner = HostRunner(listens().command(), control=control, poll_seconds=POLL)

        await_(
            _while_running(
                runner.dispatch(request_for()),
                lambda: control.inbox.send(RUN_ID, "narrow the change", at=at(0)),
                when=started(repo),
            )
        )

        (message,) = Inbox(state_with_run).messages_for(RUN_ID)
        assert message.state is MessageState.DELIVERED
        assert message.ordinal == 1

    def test_the_agent_sees_each_message_exactly_once(
        self,
        request_for: RequestFactory,
        repo: FixtureRepo,
        control: StoreControl,
    ) -> None:
        """Polls keep happening after a message is claimed. If the claim were
        not the delivery, the same instruction would land again every ``POLL``
        seconds for as long as the run lasted."""
        agent = (
            FakeAgent()
            .say("starting")
            .await_steering(SEEN)
            .sleep(POLL * 20)
            .count_steering(SEEN + ".count")
            .commit()
            .verdict(status="passed", tests=TESTS_PASSED)
        )
        runner = HostRunner(agent.command(), control=control, poll_seconds=POLL)

        await_(
            _while_running(
                runner.dispatch(request_for()),
                lambda: control.inbox.send(RUN_ID, "only once please", at=at(0)),
                when=started(repo),
            )
        )

        assert (repo.path / (SEEN + ".count")).read_text(encoding="utf-8").strip() == "1"

    def test_a_steering_message_is_not_the_agents_uncommitted_work(
        self,
        request_for: RequestFactory,
        repo: FixtureRepo,
        control: StoreControl,
    ) -> None:
        """Otherwise every steered run would report a dropped commit.

        The file is under ``.clawdence/``, which git's exclude covers and
        ``Installed`` owns by prefix — so this is really a check that the
        directory chosen in ``steering`` is the one those two already protect.
        """
        runner = HostRunner(listens().command(), control=control, poll_seconds=POLL)

        result = await_(
            _while_running(
                runner.dispatch(request_for()),
                lambda: control.inbox.send(RUN_ID, "a message", at=at(0)),
                when=started(repo),
            )
        )

        assert result.outcome is RunnerOutcome.SUCCEEDED
        assert result.dirty is False
        assert result.dirty_paths == ()

    def test_messages_arrive_in_claim_order(
        self,
        request_for: RequestFactory,
        repo: FixtureRepo,
        control: StoreControl,
    ) -> None:
        """Priority is invisible to the agent, so it has to be baked into the
        order the files sort in."""

        def send_both() -> None:
            control.inbox.send(RUN_ID, "queued suggestion", at=at(0))
            control.inbox.send(RUN_ID, "urgent correction", at=at(1), priority=10)

        runner = HostRunner(listens().command(), control=control, poll_seconds=POLL)
        await_(_while_running(runner.dispatch(request_for()), send_both, when=started(repo)))

        seen = (repo.path / SEEN).read_text(encoding="utf-8")
        assert seen.index("urgent correction") < seen.index("queued suggestion")

    def test_a_runner_with_no_control_source_still_runs(
        self, request_for: RequestFactory, repo: FixtureRepo
    ) -> None:
        """``NoControl`` is a real configuration, not a misconfiguration: it is
        exactly what S6 shipped."""
        runner = HostRunner(
            FakeAgent().write("app.py", CHANGED).commit().verdict(tests=TESTS_PASSED).command(),
            control=NoControl(),
            poll_seconds=POLL,
        )
        assert await_(runner.dispatch(request_for())).outcome is RunnerOutcome.SUCCEEDED


# --------------------------------------------------------------------------- #
# Cancel from outside
# --------------------------------------------------------------------------- #


def stopper(control: StoreControl) -> HostRunner:
    """A host runner over ``never_finishes``, wired to a real store."""
    return HostRunner(never_finishes().command(), control=control, poll_seconds=POLL)


def never_finishes() -> FakeAgent:
    """Does its work, commits, and then would run past the end of the test.

    The marker is written *after* the commit, so a test waiting on it knows the
    work it is about to assert survived is already in the tree.
    """
    return (
        FakeAgent()
        .say("working")
        .write("app.py", CHANGED)
        .commit()
        .write(DID_THE_WORK, "committed\n")
        .sleep(120)
    )


class TestCancel:
    def test_a_cancel_from_outside_stops_a_host_run(
        self,
        request_for: RequestFactory,
        repo: FixtureRepo,
        control: StoreControl,
    ) -> None:
        result = await_(
            _while_running(
                stopper(control).dispatch(request_for()),
                lambda: control.cancellations.request(RUN_ID, at=at(0), reason="wrong branch"),
                when=committed(repo),
            )
        )

        assert result.outcome is RunnerOutcome.CANCELLED

    def test_a_cancel_is_not_reported_as_a_timeout(
        self,
        request_for: RequestFactory,
        repo: FixtureRepo,
        control: StoreControl,
    ) -> None:
        """Both are true of the process; only one is true of the *run*, and they
        are handled oppositely — a timeout is worth asking why, a cancel is not
        worth anything at all."""
        result = await_(
            _while_running(
                stopper(control).dispatch(request_for(wall_clock_seconds=60)),
                lambda: control.cancellations.request(RUN_ID, at=at(0), reason="stop"),
                when=committed(repo),
            )
        )

        assert result.outcome is RunnerOutcome.CANCELLED
        assert result.message is not None
        assert "stop" in result.message

    def test_partial_work_is_still_collected(
        self,
        request_for: RequestFactory,
        repo: FixtureRepo,
        control: StoreControl,
    ) -> None:
        """§3.11's requirement, and what makes the watchdog able to use this: a
        silenced run fails through the normal collection path, so whatever the
        agent committed before it hung is preserved rather than abandoned."""
        result = await_(
            _while_running(
                stopper(control).dispatch(request_for()),
                lambda: control.cancellations.request(RUN_ID, at=at(0), reason="stop"),
                when=committed(repo),
            )
        )

        assert result.outcome is RunnerOutcome.CANCELLED
        assert result.commits_ahead == 1
        assert result.diff is not None
        assert result.diff.files_changed == 1

    def test_the_cancel_is_acknowledged_so_an_unattended_run_is_visible(
        self,
        request_for: RequestFactory,
        repo: FixtureRepo,
        control: StoreControl,
        state_with_run: StateStore,
    ) -> None:
        """A request nobody acknowledged means no process is attending the run,
        which is a different problem from a run refusing to die."""
        await_(
            _while_running(
                stopper(control).dispatch(request_for()),
                lambda: control.cancellations.request(RUN_ID, at=at(0), reason="stop"),
                when=committed(repo),
            )
        )

        assert Cancellations(state_with_run).acknowledged_at(RUN_ID) is not None

    def test_a_cancel_from_outside_stops_a_container_run(
        self,
        request_for: RequestFactory,
        repo: FixtureRepo,
        control: StoreControl,
        fake_engine: FakeEngine,
    ) -> None:
        """The same verb on the other tier. It matters that this is separately
        asserted: ``_halt`` is overridden there — the container is removed
        before the client is killed — and a cancel that killed only the client
        would leave the agent working and the run's stdout open."""
        runner = ContainerRunner(
            never_finishes().command(),
            image=PINNED_IMAGE,
            engine=fake_engine.engine,
            control=control,
            poll_seconds=POLL,
        )

        result = await_(
            _while_running(
                runner.dispatch(request_for(profile=container_profile())),
                lambda: control.cancellations.request(RUN_ID, at=at(0), reason="stop"),
                when=committed(repo),
            )
        )

        assert result.outcome is RunnerOutcome.CANCELLED
        assert result.commits_ahead == 1


# --------------------------------------------------------------------------- #
# The heartbeat the silence detector reads
# --------------------------------------------------------------------------- #


class TestHeartbeat:
    def test_a_talkative_run_reports_what_it_heard_and_when(
        self, request_for: RequestFactory
    ) -> None:
        """The instant is the arrival time of a line, not the time of the poll —
        a heartbeat stamped ``now`` would be a timer, and a timer would report a
        wedged run as healthy for as long as it stayed wedged."""
        fake = InMemoryControl()
        agent = (
            FakeAgent()
            .say("one")
            .sleep(POLL * 4)
            .say("two")
            .sleep(POLL * 4)
            .commit()
            .verdict(status="passed", tests=TESTS_PASSED)
        )
        runner = HostRunner(agent.command(), control=fake, poll_seconds=POLL)

        await_(runner.dispatch(request_for()))

        assert fake.beats, "a run that said something reported nothing"
        assert {run_id for run_id, _ in fake.beats} == {RUN_ID}

    def test_the_same_instant_is_never_reported_twice(self, request_for: RequestFactory) -> None:
        """A run that goes quiet stops beating, which is the whole signal. If an
        unchanged instant were re-sent every poll, silence would look identical
        to health.
        """
        fake = InMemoryControl()
        agent = (
            FakeAgent()
            .say("the only thing I will ever say")
            .sleep(POLL * 15)
            .commit()
            .verdict(status="passed", tests=TESTS_PASSED)
        )
        runner = HostRunner(agent.command(), control=fake, poll_seconds=POLL)

        await_(runner.dispatch(request_for()))

        instants = [instant for _, instant in fake.beats]
        assert len(instants) == len(set(instants))

    def test_a_run_that_never_says_anything_never_beats(
        self, request_for: RequestFactory, repo: FixtureRepo
    ) -> None:
        """The case the detector exists for, from the reporting side."""
        fake = InMemoryControl()
        runner = HostRunner(FakeAgent().sleep(POLL * 10).command(), control=fake, poll_seconds=POLL)

        await_(runner.dispatch(request_for()))

        assert fake.beats == []


# --------------------------------------------------------------------------- #
# What a broken control plane may not do
# --------------------------------------------------------------------------- #


class _Broken:
    """A control source that fails every call. Not hypothetical: a busy SQLite
    file, a database on a full disk, a store opened read-only."""

    def __init__(self) -> None:
        self.calls = 0

    async def poll(self, run_id: str) -> Any:
        self.calls += 1
        raise OSError("the store is unreachable")

    async def heartbeat(self, run_id: str, *, at: Any) -> None:
        raise OSError("the store is unreachable")


def test_an_unreachable_control_plane_does_not_fail_the_run(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """Steering improves a run that is otherwise working. Killing work in
    progress because the database was busy would make the feature more dangerous
    than its absence."""
    broken = _Broken()
    agent = (
        FakeAgent()
        .say("working")
        .sleep(POLL * 8)
        .write("app.py", CHANGED)
        .commit()
        .verdict(status="passed", tests=TESTS_PASSED)
    )
    runner = HostRunner(agent.command(), control=broken, poll_seconds=POLL)

    result = await_(runner.dispatch(request_for()))

    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert broken.calls > 0, "the loop stopped polling instead of carrying on"


def test_the_worktree_a_message_lands_in_is_the_one_the_request_named(
    request_for: RequestFactory, repo: FixtureRepo, control: StoreControl, tmp_path: Path
) -> None:
    """Guards the one substitution that would be silent: writing into a path
    derived from anywhere other than the request would still produce a passing
    run, with the message in a directory nobody reads."""
    runner = HostRunner(listens().command(), control=control, poll_seconds=POLL)
    request: RunnerRequest = request_for()

    await_(
        _while_running(
            runner.dispatch(request),
            lambda: control.inbox.send(RUN_ID, "look here", at=at(0)),
            when=started(repo),
        )
    )

    delivered = list((Path(request.worktree_path) / STEERING_DIR).glob("*.md"))
    assert len(delivered) == 1
    assert not (tmp_path / STEERING_DIR).exists()
