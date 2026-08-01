"""Telling a human something — progress, a question, a failure.

Notification is the one outbound channel that is *never* worth failing a run
over. If Slack is down, the sprint should still finish; a system that halts
because it could not announce that it was working is worse than one that works
quietly. That policy lives in ``outbox``, not here — this port's job is to
either deliver or raise, and the caller decides how much it cares. A port that
swallows its own failures cannot be composed with one that does not, and cannot
be tested for what it does when the service is down.

Two fields carry the weight:

``idempotency_key``
    Redelivery is normal — a retried step, an outbox flush that raced a
    successful send, a control plane restarting mid-flush. Without a key,
    "retry until it works" and "post it three times" are the same code. The
    adapter makes a repeat of a delivered key a no-op returning the original
    receipt, and the contract suite checks that it does.

``thread``
    v1's ``slackTs``. A question and its answer are one conversation; a system
    that posts every message at top level makes eight concurrent runs
    indistinguishable in a channel, which is how nobody answers the
    clarification the BA asked for.

The text is written by us but frequently *quotes* untrusted input — an issue
body, a discovery note. It leaves as data: nothing on this path builds markup a
chat client would act on, and nothing round-trips a notification back into a
prompt.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime

from clawdence.domain import DomainModel
from clawdence.domain.ids import Identifier, RunId, WorkItemId
from clawdence.ports._common import NULL_PREFIX, Clock, utc_now


class NotificationKind(StrEnum):
    """What the message is for. Adapters route on this.

    Distinct values because the routing differs and always will: progress can
    be batched or rate-limited, a question must reach someone who can answer,
    and a failure should be loud even in a channel that has muted the rest.
    """

    PROGRESS = "progress"
    QUESTION = "question"
    APPROVAL = "approval"
    FAILURE = "failure"
    SUMMARY = "summary"


class Notification(DomainModel):
    """One message, addressed."""

    kind: NotificationKind

    #: Adapter-specific destination — a Slack channel id, a Discord webhook
    #: name, an address. Opaque here on purpose: the control plane routes by
    #: ``kind`` and configuration, not by knowing what a channel is.
    channel: str

    text: str

    #: Conversation to reply into, taken from an earlier receipt. ``None``
    #: starts a new one.
    thread: str | None = None

    run_id: RunId | None = None
    work_item_id: WorkItemId | None = None

    #: Stable across redelivery. Derived from what the message is *about* — run,
    #: stage, attempt — never generated per send. A fresh key per send would
    #: make every retry a new message, which is the bug it exists to prevent.
    idempotency_key: Identifier

    #: Where to go to act on this. The reason ``kind`` alone is not enough: an
    #: approval notification with no way to approve produces a thread asking
    #: where the button is.
    links: tuple[str, ...] = ()


class Receipt(DomainModel):
    """What came back from a delivery."""

    id: str

    #: Reply into this to continue the conversation.
    thread: str | None = None

    delivered_at: AwareDatetime

    #: True when the adapter recognised the idempotency key and did not send
    #: again. Not an error, and worth distinguishing — "we sent it twice" and
    #: "we correctly declined to" are otherwise identical in a log.
    duplicate: bool = False


class NotifyPort(Protocol):
    """Delivers messages to humans."""

    async def send(self, notification: Notification) -> Receipt:
        """Deliver, or raise ``PortError``.

        Repeating a delivered ``idempotency_key`` returns the original receipt
        with ``duplicate=True`` rather than sending again.
        """
        ...


class NullNotifier:
    """Delivers nothing, and its receipts say so.

    For ``--quiet``, for dry runs, and as the default when nothing is
    configured. It returns a receipt because the caller's control flow should
    not depend on whether notification is switched on — but the id carries the
    ``null:`` prefix, so no trace ever claims a message reached a person.
    """

    __slots__ = ("_clock",)

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock

    async def send(self, notification: Notification) -> Receipt:
        return Receipt(
            id=f"{NULL_PREFIX}{notification.idempotency_key}", delivered_at=self._clock()
        )


class RecordingNotifier:
    """Keeps every message in a list. The fake.

    Tests assert on ``sent`` rather than on a mock's call args, because the
    assertion anyone actually wants to write is "the failure notification named
    the stage", and that reads better against a list of ``Notification``s than
    against ``call_args_list[0][0][0].text``.
    """

    __slots__ = ("_by_key", "_clock", "_fail_with", "_sent", "_threads")

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._sent: list[Notification] = []
        self._by_key: dict[str, Receipt] = {}
        self._threads = 0
        self._fail_with: BaseException | None = None

    def fail_with(self, error: BaseException | None) -> None:
        """Make subsequent sends raise. How a test takes the channel down."""
        self._fail_with = error

    async def send(self, notification: Notification) -> Receipt:
        if self._fail_with is not None:
            raise self._fail_with

        existing = self._by_key.get(notification.idempotency_key)
        if existing is not None:
            return existing.model_copy(update={"duplicate": True})

        if notification.thread is None:
            self._threads += 1
            thread = f"thread.{self._threads}"
        else:
            thread = notification.thread

        receipt = Receipt(
            id=f"msg.{len(self._sent) + 1}",
            thread=thread,
            delivered_at=self._clock(),
        )
        self._sent.append(notification)
        self._by_key[notification.idempotency_key] = receipt
        return receipt

    @property
    def sent(self) -> Sequence[Notification]:
        return tuple(self._sent)

    def of_kind(self, kind: NotificationKind) -> Sequence[Notification]:
        return tuple(item for item in self._sent if item.kind is kind)
