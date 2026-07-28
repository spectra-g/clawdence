"""The in-memory tracker against the contract; the null one against its own."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from clawdence.ports import InMemoryTracker, NullTracker, TicketState, TransientError
from clawdence.ports._common import counting_clock
from tests.ports.contract import Call, NullAdapterContract, TrackerContract
from tests.ports.factories import START, run


class TestInMemoryTracker(TrackerContract):
    @pytest.fixture
    def tracker(self) -> InMemoryTracker:
        return InMemoryTracker(clock=counting_clock(START))


class TestNullTracker(NullAdapterContract):
    @pytest.fixture
    def calls(self) -> Sequence[Call]:
        tracker = NullTracker(clock=counting_clock(START))
        return (
            lambda: tracker.ensure(work_item_id="wi.1", title="A task", body="details"),
            lambda: tracker.transition("null:wi.1", TicketState.CLOSED),
            lambda: tracker.comment("null:wi.1", "progress"),
            lambda: tracker.find("wi.1"),
        )

    @pytest.fixture
    def minting(self) -> Sequence[Call]:
        tracker = NullTracker(clock=counting_clock(START))
        return (lambda: tracker.ensure(work_item_id="wi.1", title="A task", body="details"),)


def test_the_null_tracker_stores_nothing_and_says_so() -> None:
    """The honest half of "no tracker configured is a supported deployment".

    It cannot satisfy ``TrackerContract`` — an adapter that claims to store
    things has to return what it stored — and pretending otherwise would mean
    the contract suite passes for something that persists nothing.
    """
    tracker = NullTracker(clock=counting_clock(START))
    run(tracker.ensure(work_item_id="wi.1", title="A task", body="details"))
    assert run(tracker.find("wi.1")) is None


def test_comments_are_kept_per_ticket() -> None:
    tracker = InMemoryTracker(clock=counting_clock(START))
    first = run(tracker.ensure(work_item_id="wi.1", title="One", body=""))
    second = run(tracker.ensure(work_item_id="wi.2", title="Two", body=""))
    run(tracker.comment(first.id, "started"))
    run(tracker.comment(first.id, "still going"))
    run(tracker.comment(second.id, "unrelated"))

    assert tracker.comments_on(first.id) == ("started", "still going")
    assert tracker.comments_on(second.id) == ("unrelated",)


def test_ensure_records_scope_and_labels() -> None:
    tracker = InMemoryTracker(clock=counting_clock(START))
    ticket = run(
        tracker.ensure(
            work_item_id="wi.1",
            title="A task",
            body="details",
            repo_id="repo.test",
            labels=["clawdence", "automated"],
        )
    )
    assert ticket.repo_id == "repo.test"
    assert ticket.labels == ("clawdence", "automated")
    assert ticket.url is not None


def test_the_fake_can_be_taken_down() -> None:
    tracker = InMemoryTracker(clock=counting_clock(START))
    tracker.fail_with(TransientError("unavailable", "503"))
    with pytest.raises(TransientError):
        run(tracker.ensure(work_item_id="wi.1", title="A task", body=""))
    assert tracker.tickets == ()
