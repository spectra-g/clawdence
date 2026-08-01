"""The control port: the fake, the durable adapter, and the one that does nothing.

Both real implementations are held to the same contract, which is the point of
having one. ``InMemoryControl`` is what the runner tests are written against and
``StoreControl`` is what production runs, so a divergence in the claim rule
between them would be a divergence between what is tested and what happens.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from clawdence.ports import ControlPort, InMemoryControl, NoControl, Signal
from clawdence.store import IN_MEMORY, StateStore, StoreControl
from tests.ports import factories as make
from tests.ports.contract import Call, ControlContract, NullAdapterContract
from tests.ports.factories import run
from tests.store.factories import make_run


class TestInMemoryControl(ControlContract):
    @pytest.fixture
    def control(self) -> InMemoryControl:
        return InMemoryControl()

    def send(self, control: ControlPort, body: str, *, priority: int = 0) -> None:
        assert isinstance(control, InMemoryControl)
        control.send(make.RUN_ID, body, priority=priority, at=make.at(0))


class TestStoreControl(ControlContract):
    @pytest.fixture
    def control(self) -> Iterator[StoreControl]:
        with StateStore.open(IN_MEMORY) as store:
            store.create_run(make_run(make.RUN_ID))
            yield StoreControl(store, clock=lambda: make.at(1))

    def send(self, control: ControlPort, body: str, *, priority: int = 0) -> None:
        assert isinstance(control, StoreControl)
        control.inbox.send(make.RUN_ID, body, priority=priority, at=make.at(0))


class TestNoControl(NullAdapterContract):
    """Not configured, and honest about it. Never raises, records nothing."""

    @pytest.fixture
    def calls(self) -> list[Call]:
        control = NoControl()
        return [
            lambda: control.poll(make.RUN_ID),
            lambda: control.heartbeat(make.RUN_ID, at=make.at(0)),
        ]


def test_nothing_reaches_a_run_through_the_null_adapter() -> None:
    """The distinction from ``RefusingRunner``: this is a real configuration
    rather than a misconfiguration, so it answers rather than raising — and what
    it answers is that nobody said anything, which is true."""
    assert run(NoControl().poll(make.RUN_ID)) == Signal()


def test_the_fake_records_heartbeats_so_a_test_can_assert_on_silence() -> None:
    fake = InMemoryControl()
    run(fake.heartbeat(make.RUN_ID, at=make.at(3)))
    assert fake.beats == [(make.RUN_ID, make.at(3))]


def test_the_fake_latches_a_cancel_the_way_the_table_does() -> None:
    """First writer wins, in both, so a test cannot pass against a fake that is
    more forgiving than the row it stands in for."""
    fake = InMemoryControl()
    fake.cancel(make.RUN_ID, reason="first", at=make.at(0))
    fake.cancel(make.RUN_ID, reason="second", at=make.at(1))

    signal = run(fake.poll(make.RUN_ID))
    assert signal.cancel is not None
    assert signal.cancel.reason == "first"


def test_the_fake_keeps_one_run_out_of_another_s_inbox() -> None:
    fake = InMemoryControl()
    fake.send("run.a", "for a")
    assert run(fake.poll("run.b")).messages == ()
    assert [m.body for m in run(fake.poll("run.a")).messages] == ["for a"]
