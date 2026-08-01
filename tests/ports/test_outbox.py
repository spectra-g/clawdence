"""The outbox — "non-fatal" as one object rather than a convention.

Every test here is a way v1 got this wrong at one call site and right at
another: a permanent failure retried five times, a queue that grew without
bound, one dead destination blocking every other message, and a flush that
re-sent what had already gone out.
"""

from __future__ import annotations

from clawdence.ports import Outbox, PermanentError, TransientError
from clawdence.ports._common import counting_clock
from clawdence.ports.outbox import DEFAULT_CAPACITY, DEFAULT_MAX_ATTEMPTS
from tests.ports.factories import START, run


class Sink:
    """A delivery target that can be told to fail. The whole test double."""

    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.error: Exception | None = None
        self.attempts = 0

    async def __call__(self, message: str) -> None:
        self.attempts += 1
        if self.error is not None:
            raise self.error
        self.delivered.append(message)


def outbox(
    sink: Sink,
    *,
    capacity: int = DEFAULT_CAPACITY,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Outbox[str]:
    return Outbox(sink, capacity=capacity, max_attempts=max_attempts, clock=counting_clock(START))


def test_a_working_destination_delivers_immediately() -> None:
    sink = Sink()
    box = outbox(sink)
    assert run(box.send("hello", key="k1")) is True
    assert sink.delivered == ["hello"]
    assert box.pending == ()


def test_a_transient_failure_is_held_not_raised() -> None:
    """The property the whole object exists for: Slack being down must not fail
    a run. A system that halts because it could not announce that it was
    working is worse than one that works quietly."""
    sink = Sink()
    sink.error = TransientError("unavailable", "503")
    box = outbox(sink)

    assert run(box.send("hello", key="k1")) is False
    assert [held.key for held in box.pending] == ["k1"]
    assert box.dead_letters == ()


def test_a_permanent_failure_skips_the_retries() -> None:
    """Retrying a 404 spends five attempts confirming it. The adapter already
    said which kind of failure this is, so nothing has to guess."""
    sink = Sink()
    sink.error = PermanentError("not-found", "no such channel")
    box = outbox(sink)

    assert run(box.send("hello", key="k1")) is False
    assert box.pending == ()
    assert [dead.key for dead in box.dead_letters] == ["k1"]
    assert sink.attempts == 1


def test_flushing_delivers_what_was_held() -> None:
    sink = Sink()
    sink.error = TransientError("unavailable", "503")
    box = outbox(sink)
    run(box.send("hello", key="k1"))

    sink.error = None
    report = run(box.flush())
    assert report.ok
    assert report.delivered == ("k1",)
    assert sink.delivered == ["hello"]
    assert box.pending == ()


def test_a_message_is_parked_once_the_attempts_run_out() -> None:
    """A destination that has refused five times is down, not slow, and the
    queue is more useful reporting that than continuing to hope."""
    sink = Sink()
    sink.error = TransientError("unavailable", "503")
    box = outbox(sink, max_attempts=3)

    run(box.send("hello", key="k1"))
    run(box.flush())
    report = run(box.flush())

    assert report.parked == ("k1",)
    assert box.pending == ()
    assert [dead.tries for dead in box.dead_letters] == [3]


def test_one_dead_message_does_not_block_the_rest() -> None:
    """No head-of-line blocking. A flush attempts every held message, including
    the ones queued behind a failure."""

    class Selective:
        def __init__(self) -> None:
            self.delivered: list[str] = []

        async def __call__(self, message: str) -> None:
            if message == "poison":
                raise TransientError("unavailable", "503")
            self.delivered.append(message)

    sink = Selective()
    box: Outbox[str] = Outbox(sink, clock=counting_clock(START))
    run(box.send("poison", key="bad"))
    run(box.send("fine", key="good"))

    assert sink.delivered == ["fine"]
    assert [held.key for held in box.pending] == ["bad"]


def test_a_delivered_key_is_never_sent_again() -> None:
    """Both a resumed run replaying its notifications and a flush racing a
    successful send land on this."""
    sink = Sink()
    box = outbox(sink)
    run(box.send("hello", key="k1"))
    assert run(box.send("hello again", key="k1")) is True
    assert sink.delivered == ["hello"]


def test_a_pending_key_is_not_queued_twice() -> None:
    sink = Sink()
    sink.error = TransientError("unavailable", "503")
    box = outbox(sink)
    run(box.send("hello", key="k1"))
    assert run(box.send("hello", key="k1")) is False
    assert len(box.pending) == 1


def test_the_queue_is_bounded() -> None:
    """An unbounded buffer in front of a service that has been down for a day
    is v1's 300MB processing log with extra steps: the symptom is memory
    exhaustion, and every message that could have said why is inside it."""
    sink = Sink()
    sink.error = TransientError("unavailable", "503")
    box = outbox(sink, capacity=2)

    for index in range(4):
        run(box.send(f"m{index}", key=f"k{index}"))

    assert len(box.pending) == 2
    assert [dead.reason for dead in box.dead_letters] == ["outbox-full", "outbox-full"]


def test_a_full_queue_still_does_not_raise() -> None:
    """Bounded, but still non-fatal. The loss becomes *visible* as a dead
    letter rather than becoming an exception in the middle of a run."""
    sink = Sink()
    sink.error = TransientError("unavailable", "503")
    box = outbox(sink, capacity=1)
    run(box.send("m0", key="k0"))
    assert run(box.send("m1", key="k1")) is False


def test_pending_messages_still_retry_after_the_queue_filled() -> None:
    """Capacity refuses *new* keys; it must not stop the held ones retrying,
    or a full queue would stay full forever."""
    sink = Sink()
    sink.error = TransientError("unavailable", "503")
    box = outbox(sink, capacity=1)
    run(box.send("m0", key="k0"))
    run(box.send("m1", key="k1"))

    sink.error = None
    assert run(box.flush()).delivered == ("k0",)


def test_dead_letters_describe_themselves() -> None:
    sink = Sink()
    sink.error = PermanentError("not-found", "no such channel")
    box = outbox(sink)
    run(box.send("hello", key="k1"))

    dead = box.dead_letters[0]
    assert dead.describe() == "k1: not-found after 1 try"
    assert dead.detail == "no such channel"
    assert dead.message == "hello"
    assert dead.first_failed_at == dead.last_failed_at


def test_draining_clears_the_dead_letters() -> None:
    sink = Sink()
    sink.error = PermanentError("not-found", "gone")
    box = outbox(sink)
    run(box.send("hello", key="k1"))

    assert len(box.drain_dead_letters()) == 1
    assert box.dead_letters == ()


def test_repeated_failures_count_up() -> None:
    sink = Sink()
    sink.error = TransientError("unavailable", "503")
    box = outbox(sink, max_attempts=5)
    run(box.send("hello", key="k1"))
    run(box.flush())

    held = box.pending[0]
    assert held.tries == 2
    assert held.describe() == "k1: unavailable after 2 tries"
    assert held.first_failed_at < held.last_failed_at
