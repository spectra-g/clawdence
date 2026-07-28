"""The recording notifier against the contract, and the null one against its own."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from clawdence.ports import (
    NotificationKind,
    NullNotifier,
    RecordingNotifier,
    TransientError,
)
from clawdence.ports._common import counting_clock
from tests.ports import factories as make
from tests.ports.contract import Call, NotifyContract, NullAdapterContract
from tests.ports.factories import START, run


class TestRecordingNotifier(NotifyContract):
    @pytest.fixture
    def notifier(self) -> RecordingNotifier:
        return RecordingNotifier(clock=counting_clock(START))


class TestNullNotifier(NullAdapterContract):
    @pytest.fixture
    def calls(self) -> Sequence[Call]:
        notifier = NullNotifier(clock=counting_clock(START))
        return (lambda: notifier.send(make.notification("run.1:plan:1")),)

    @pytest.fixture
    def minting(self) -> Sequence[Call]:
        notifier = NullNotifier(clock=counting_clock(START))
        return (lambda: notifier.send(make.notification("run.1:plan:1")),)


def test_recorded_messages_are_readable_as_notifications() -> None:
    """The reason this is a recorder rather than a mock: the assertion anyone
    wants to write is about the message, not about call args."""
    notifier = RecordingNotifier(clock=counting_clock(START))
    run(notifier.send(make.notification("k1", kind=NotificationKind.FAILURE, text="plan failed")))
    run(notifier.send(make.notification("k2", kind=NotificationKind.PROGRESS)))

    failures = notifier.of_kind(NotificationKind.FAILURE)
    assert [item.text for item in failures] == ["plan failed"]
    assert len(notifier.sent) == 2


def test_a_deduplicated_send_is_not_recorded_twice() -> None:
    notifier = RecordingNotifier(clock=counting_clock(START))
    run(notifier.send(make.notification("k1")))
    run(notifier.send(make.notification("k1")))
    assert len(notifier.sent) == 1


def test_the_fake_can_be_taken_down() -> None:
    """How a test makes a channel unreachable without needing a channel."""
    notifier = RecordingNotifier(clock=counting_clock(START))
    notifier.fail_with(TransientError("rate-limited", "slow down"))
    with pytest.raises(TransientError):
        run(notifier.send(make.notification("k1")))

    notifier.fail_with(None)
    assert run(notifier.send(make.notification("k1"))).duplicate is False


def test_null_receipts_do_not_claim_delivery() -> None:
    """Control flow should not change when notification is switched off — but
    nothing downstream may report that a person was told."""
    receipt = run(NullNotifier(clock=counting_clock(START)).send(make.notification("k1")))
    assert receipt.id.startswith("null:")
    assert receipt.thread is None
