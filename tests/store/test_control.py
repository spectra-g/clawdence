"""The steering inbox, the cancel latch, and the adapter over both (§3.11).

The claim rule and the delivery lifecycle are the two things worth testing
hardest, and for opposite reasons. The claim rule is *stated* — priority-desc,
then FIFO within a class — so it is testable by construction. The lifecycle is
about what happens when nobody is around to observe it: the interesting cases
are all "the process that was holding this died", which is why they are written
as two stores over one file rather than as one store told to pretend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawdence.domain import EventKind
from clawdence.ports import MAX_STEERING_CHARS
from clawdence.store import (
    Cancellations,
    Inbox,
    MessageRejectedError,
    MessageState,
    StateStore,
    SteeringMessage,
    StoreControl,
    UnknownRunError,
)
from tests.ports.factories import run as await_
from tests.store.conftest import StoreFactory
from tests.store.factories import RUN_ID, at, make_run


@pytest.fixture
def inbox(state: StateStore) -> Inbox:
    state.create_run(make_run())
    return Inbox(state)


@pytest.fixture
def cancels(state: StateStore) -> Cancellations:
    state.create_run(make_run())
    return Cancellations(state)


def bodies(messages: tuple[SteeringMessage, ...]) -> list[str]:
    return [message.body for message in messages]


class TestSending:
    def test_a_message_starts_unread(self, inbox: Inbox) -> None:
        message = inbox.send(RUN_ID, "use the existing parser", at=at(0))

        assert message.state is MessageState.UNREAD
        assert message.body == "use the existing parser"
        assert message.sender == "operator"
        assert message.created_at == at(0)
        assert message.ordinal is None

    def test_a_message_to_a_run_that_does_not_exist_says_so(self, inbox: Inbox) -> None:
        """The foreign key would also stop it, and would not say which run."""
        with pytest.raises(UnknownRunError, match=r"run\.nope"):
            inbox.send("run.nope", "hello", at=at(0))

    def test_an_empty_message_is_refused(self, inbox: Inbox) -> None:
        with pytest.raises(MessageRejectedError, match="says nothing"):
            inbox.send(RUN_ID, "   \n  ", at=at(0))

    def test_an_oversized_message_is_refused_rather_than_trimmed(self, inbox: Inbox) -> None:
        """Half of 'do not touch the migration, only the model' is the opposite."""
        with pytest.raises(MessageRejectedError, match="truncated instruction"):
            inbox.send(RUN_ID, "x" * (MAX_STEERING_CHARS + 1), at=at(0))

    def test_a_message_at_the_limit_is_accepted(self, inbox: Inbox) -> None:
        assert inbox.send(RUN_ID, "x" * MAX_STEERING_CHARS, at=at(0)).state is MessageState.UNREAD


class TestClaimOrder:
    def test_same_priority_is_first_in_first_out(self, inbox: Inbox) -> None:
        for text in ("first", "second", "third"):
            inbox.send(RUN_ID, text, at=at(0))

        assert bodies(inbox.claim(RUN_ID, at=at(1))) == ["first", "second", "third"]

    def test_priority_wins_over_arrival(self, inbox: Inbox) -> None:
        """The point of a priority: put something in front without cancelling."""
        inbox.send(RUN_ID, "queued suggestion", at=at(0))
        inbox.send(RUN_ID, "stop touching the database layer", at=at(1), priority=10)

        assert bodies(inbox.claim(RUN_ID, at=at(2)))[0] == "stop touching the database layer"

    def test_arrival_still_orders_within_a_priority_class(self, inbox: Inbox) -> None:
        inbox.send(RUN_ID, "low", at=at(0))
        inbox.send(RUN_ID, "urgent one", at=at(1), priority=5)
        inbox.send(RUN_ID, "urgent two", at=at(2), priority=5)

        assert bodies(inbox.claim(RUN_ID, at=at(3))) == ["urgent one", "urgent two", "low"]

    def test_a_negative_priority_goes_last(self, inbox: Inbox) -> None:
        """Signed, so 'after everything queued' needs no renumbering either."""
        inbox.send(RUN_ID, "whenever", at=at(0), priority=-1)
        inbox.send(RUN_ID, "normal", at=at(1))

        assert bodies(inbox.claim(RUN_ID, at=at(2))) == ["normal", "whenever"]

    def test_the_ordinal_counts_deliveries_not_arrivals(self, inbox: Inbox) -> None:
        """It names the file the agent reads, so it has to follow claim order."""
        inbox.send(RUN_ID, "queued", at=at(0))
        inbox.send(RUN_ID, "urgent", at=at(1), priority=1)

        first, second = inbox.claim(RUN_ID, at=at(2))
        assert (first.body, first.ordinal) == ("urgent", 1)
        assert (second.body, second.ordinal) == ("queued", 2)

    def test_ordinals_keep_counting_across_claims(self, inbox: Inbox) -> None:
        inbox.send(RUN_ID, "one", at=at(0))
        inbox.claim(RUN_ID, at=at(1))
        inbox.send(RUN_ID, "two", at=at(2))

        (second,) = inbox.claim(RUN_ID, at=at(3))
        assert second.ordinal == 2

    def test_a_claim_is_bounded(self, inbox: Inbox) -> None:
        for index in range(5):
            inbox.send(RUN_ID, f"message {index}", at=at(index))

        assert len(inbox.claim(RUN_ID, at=at(9), limit=2)) == 2
        assert len(inbox.pending(RUN_ID)) == 3

    def test_one_run_never_sees_another_run_s_inbox(self, state: StateStore) -> None:
        state.create_run(make_run("run.a"))
        state.create_run(make_run("run.b"))
        inbox = Inbox(state)
        inbox.send("run.a", "for a", at=at(0))

        assert inbox.claim("run.b", at=at(1)) == ()
        assert bodies(inbox.claim("run.a", at=at(1))) == ["for a"]


class TestDeliveryLifecycle:
    def test_claiming_is_delivering(self, inbox: Inbox) -> None:
        inbox.send(RUN_ID, "hello", at=at(0))

        (claimed,) = inbox.claim(RUN_ID, at=at(5))

        assert claimed.state is MessageState.DELIVERED
        assert claimed.delivered_at == at(5)

    def test_a_delivered_message_is_never_claimed_again(self, inbox: Inbox) -> None:
        inbox.send(RUN_ID, "hello", at=at(0))
        inbox.claim(RUN_ID, at=at(1))

        assert inbox.claim(RUN_ID, at=at(2)) == ()

    def test_looking_is_free(self, inbox: Inbox) -> None:
        """``pending`` is the ``detect`` to ``claim``'s ``recover``."""
        inbox.send(RUN_ID, "hello", at=at(0))

        assert bodies(inbox.pending(RUN_ID)) == ["hello"]
        assert bodies(inbox.pending(RUN_ID)) == ["hello"]
        assert bodies(inbox.claim(RUN_ID, at=at(1))) == ["hello"]

    def test_abandon_fails_the_delivered_and_leaves_the_unread(self, inbox: Inbox) -> None:
        """The crash rule, in one assertion.

        A message the dead process was holding is closed out — it may already
        have been acted on, and an instruction followed twice is worse than one
        that visibly never arrived. A message nobody has seen is untouched,
        because the resumed run is exactly the reader it was waiting for.
        """
        delivered = inbox.send(RUN_ID, "already handed over", at=at(0))
        inbox.claim(RUN_ID, at=at(1))
        waiting = inbox.send(RUN_ID, "still queued", at=at(2))

        assert inbox.abandon(RUN_ID, at=at(3), reason="the process is gone") == 1

        after = {message.id: message for message in inbox.messages_for(RUN_ID)}
        assert after[delivered.id].state is MessageState.FAILED
        assert after[delivered.id].reason == "the process is gone"
        assert after[delivered.id].closed_at == at(3)
        assert after[waiting.id].state is MessageState.UNREAD

    def test_close_fails_everything_still_open(self, inbox: Inbox) -> None:
        inbox.send(RUN_ID, "delivered", at=at(0))
        inbox.claim(RUN_ID, at=at(1))
        inbox.send(RUN_ID, "never read", at=at(2))

        assert inbox.close(RUN_ID, at=at(3), reason="the run finished") == 2
        assert {m.state for m in inbox.messages_for(RUN_ID)} == {MessageState.FAILED}

    def test_closing_twice_changes_nothing(self, inbox: Inbox) -> None:
        inbox.send(RUN_ID, "never read", at=at(0))
        inbox.close(RUN_ID, at=at(1), reason="first")

        assert inbox.close(RUN_ID, at=at(2), reason="second") == 0
        assert inbox.messages_for(RUN_ID)[0].reason == "first"

    def test_an_unknown_message_is_none_rather_than_an_error(self, inbox: Inbox) -> None:
        assert inbox.get("st.nothing") is None


class TestCrashResume:
    def test_an_unread_message_survives_the_process_that_was_going_to_read_it(
        self, stores: StoreFactory, tmp_path: Path
    ) -> None:
        """Two stores over one file, because 'it survived' is a claim about the
        file and not about an object that was never dropped."""
        path = tmp_path / "state.db"
        first = stores(path)
        first.create_run(make_run())
        Inbox(first).send(RUN_ID, "use the existing parser", at=at(0))
        first.close()

        assert bodies(Inbox(stores(path)).claim(RUN_ID, at=at(1))) == ["use the existing parser"]

    def test_a_message_claimed_before_the_crash_is_not_redelivered(
        self, stores: StoreFactory, tmp_path: Path
    ) -> None:
        """The property the whole lifecycle exists for."""
        path = tmp_path / "state.db"
        first = stores(path)
        first.create_run(make_run())
        Inbox(first).send(RUN_ID, "revert the change you just made", at=at(0))
        Inbox(first).claim(RUN_ID, at=at(1))
        first.close()

        resumed = Inbox(stores(path))
        assert resumed.claim(RUN_ID, at=at(2)) == ()
        assert resumed.messages_for(RUN_ID)[0].state is MessageState.DELIVERED


class TestCancellation:
    def test_a_request_is_readable_by_anyone_who_asks(self, cancels: Cancellations) -> None:
        cancels.request(RUN_ID, at=at(0), reason="wrong branch", requested_by="ana")

        request = cancels.pending(RUN_ID)
        assert request is not None
        assert request.run_id == RUN_ID
        assert request.reason == "wrong branch"
        assert request.requested_by == "ana"
        assert request.at == at(0)

    def test_no_request_is_none(self, cancels: Cancellations) -> None:
        assert cancels.pending(RUN_ID) is None

    def test_the_first_request_wins(self, cancels: Cancellations) -> None:
        """A second ask is the same ask; two rows would make 'who' unanswerable."""
        cancels.request(RUN_ID, at=at(0), reason="first", requested_by="ana")
        second = cancels.request(RUN_ID, at=at(5), reason="second", requested_by="bo")

        assert second.reason == "first"
        assert second.requested_by == "ana"

    def test_only_the_request_that_won_is_audited(self, state: StateStore) -> None:
        state.create_run(make_run())
        cancels = Cancellations(state)
        cancels.request(RUN_ID, at=at(0), reason="first")
        cancels.request(RUN_ID, at=at(5), reason="second")

        (event,) = state.audit.read(kinds=[EventKind.RUN_CANCELLED])
        assert event.payload == {"reason": "first", "requested_by": "operator"}

    def test_cancelling_a_run_that_does_not_exist_says_so(self, cancels: Cancellations) -> None:
        with pytest.raises(UnknownRunError, match=r"run\.nope"):
            cancels.request("run.nope", at=at(0), reason="whatever")

    def test_acknowledgement_happens_once(self, cancels: Cancellations) -> None:
        """A request nobody has acknowledged means no process is attending the
        run, which is a different problem from one refusing to die."""
        cancels.request(RUN_ID, at=at(0), reason="stop")

        assert cancels.acknowledged_at(RUN_ID) is None
        assert cancels.acknowledge(RUN_ID, at=at(1)) is True
        assert cancels.acknowledge(RUN_ID, at=at(2)) is False
        assert cancels.acknowledged_at(RUN_ID) == at(1)

    def test_acknowledging_does_not_clear_the_request(self, cancels: Cancellations) -> None:
        """It is still the reason the run is stopping."""
        cancels.request(RUN_ID, at=at(0), reason="stop")
        cancels.acknowledge(RUN_ID, at=at(1))

        assert cancels.pending(RUN_ID) is not None

    def test_acknowledging_nothing_is_false(self, cancels: Cancellations) -> None:
        assert cancels.acknowledge(RUN_ID, at=at(1)) is False
        assert cancels.acknowledged_at(RUN_ID) is None

    def test_clearing_removes_the_outstanding_request(self, cancels: Cancellations) -> None:
        cancels.request(RUN_ID, at=at(0), reason="stop")

        assert cancels.clear(RUN_ID) is True
        assert cancels.pending(RUN_ID) is None
        assert cancels.clear(RUN_ID) is False

    def test_clearing_keeps_the_timeline(self, state: StateStore) -> None:
        """The table holds what is outstanding; the log holds what happened."""
        state.create_run(make_run())
        cancels = Cancellations(state)
        cancels.request(RUN_ID, at=at(0), reason="stop")
        cancels.clear(RUN_ID)

        assert len(state.audit.read(kinds=[EventKind.RUN_CANCELLED])) == 1


class TestStoreControl:
    """The adapter, which is what the runner is actually handed.

    ``await_`` rather than a ``pytest-asyncio`` decorator, for the reason
    ``tests.ports.factories`` gives: every dependency here is pinned exactly and
    is therefore a standing maintenance obligation, and this one would buy a
    decorator.
    """

    @pytest.fixture
    def control(self, state: StateStore) -> StoreControl:
        state.create_run(make_run())
        return StoreControl(state, clock=lambda: at(100))

    def test_an_empty_inbox_polls_to_nothing(self, control: StoreControl) -> None:
        signal = await_(control.poll(RUN_ID))
        assert signal.messages == ()
        assert signal.cancel is None

    def test_a_poll_claims_in_order_and_only_once(self, control: StoreControl) -> None:
        control.inbox.send(RUN_ID, "queued", at=at(0))
        control.inbox.send(RUN_ID, "urgent", at=at(1), priority=3)

        signal = await_(control.poll(RUN_ID))

        assert [message.body for message in signal.messages] == ["urgent", "queued"]
        assert [message.ordinal for message in signal.messages] == [1, 2]
        assert await_(control.poll(RUN_ID)).messages == ()

    def test_polling_an_unknown_run_is_empty_rather_than_an_error(
        self, control: StoreControl
    ) -> None:
        """A runner asking about a run the store has never heard of is a
        misconfiguration to surface elsewhere, not a reason to kill its work."""
        assert await_(control.poll("run.nope")).messages == ()

    def test_a_cancel_is_returned_and_acknowledged(self, control: StoreControl) -> None:
        control.cancellations.request(RUN_ID, at=at(0), reason="wrong branch")

        signal = await_(control.poll(RUN_ID))

        assert signal.cancel is not None
        assert signal.cancel.reason == "wrong branch"
        assert control.cancellations.acknowledged_at(RUN_ID) == at(100)

    def test_nothing_is_claimed_alongside_a_stop(self, control: StoreControl) -> None:
        """A message marked delivered by the poll that ends the run is one the
        agent could never have read — the lifecycle exists to make that
        visible, not to manufacture it."""
        control.inbox.send(RUN_ID, "still worth saying", at=at(0))
        control.cancellations.request(RUN_ID, at=at(1), reason="stop")

        signal = await_(control.poll(RUN_ID))

        assert signal.messages == ()
        assert control.inbox.messages_for(RUN_ID)[0].state is MessageState.UNREAD

    def test_a_heartbeat_moves_the_run_forward(
        self, state: StateStore, control: StoreControl
    ) -> None:
        await_(control.heartbeat(RUN_ID, at=at(50)))
        assert state.require_run(RUN_ID).updated_at == at(50)

    def test_a_heartbeat_never_moves_backwards(
        self, state: StateStore, control: StoreControl
    ) -> None:
        """``MAX``, so two writers in any order leave the later instant."""
        await_(control.heartbeat(RUN_ID, at=at(50)))
        await_(control.heartbeat(RUN_ID, at=at(20)))
        assert state.require_run(RUN_ID).updated_at == at(50)
