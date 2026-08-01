"""Streaming, the tail, and the token scraper."""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime

import pytest

from clawdence.domain import TokenUsage
from clawdence.runners.stream import (
    MAX_LINE_BYTES,
    Accumulation,
    LogLine,
    Stream,
    Tail,
    TokenTally,
    pump,
    write_to,
)
from tests.ports.factories import run

AT = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def collect(payload: bytes, *, chunk_size: int = 4096) -> list[str]:
    """Run the pump over a fixed payload and return the lines it produced.

    The reader is built *inside* the coroutine: ``StreamReader`` binds to the
    running loop at construction, and one made beforehand attaches to whatever
    loop happens to be current — which under pytest is none.
    """
    lines: list[LogLine] = []

    async def drive() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(payload)
        reader.feed_eof()
        await pump(
            reader,
            Stream.STDOUT,
            on_line=lines.append,
            clock=lambda: AT,
            chunk_size=chunk_size,
        )

    run(drive())
    return [line.text for line in lines]


# --------------------------------------------------------------------------- #
# pump
# --------------------------------------------------------------------------- #


def test_lines_arrive_split() -> None:
    assert collect(b"one\ntwo\nthree\n") == ["one", "two", "three"]


def test_a_line_split_across_reads_is_still_one_line() -> None:
    """The whole reason this buffers rather than treating a read as a line."""
    assert collect(b"hello world\n", chunk_size=3) == ["hello world"]


def test_a_final_fragment_without_a_newline_is_delivered() -> None:
    """A process that died mid-sentence said something, and it is usually the
    most interesting line in the log."""
    assert collect(b"finished\nsegmentation f") == ["finished", "segmentation f"]


def test_carriage_returns_are_dropped() -> None:
    assert collect(b"windows\r\nline\r\n") == ["windows", "line"]


def test_an_endless_line_is_truncated_rather_than_raising() -> None:
    """``StreamReader.readline`` raises past its buffer limit, and a CLI that
    echoes a minified bundle will pass it. Failing a run over the absence of a
    newline would be absurd."""
    lines = collect(b"x" * (MAX_LINE_BYTES + 10) + b"\n")
    assert lines[0].endswith("[…]")
    assert len(lines[0]) == MAX_LINE_BYTES + len(" […]")


def test_invalid_utf8_does_not_stop_the_stream() -> None:
    assert collect(b"before\n\xff\xfe\nafter\n") == ["before", "��", "after"]


# --------------------------------------------------------------------------- #
# Tail and sinks
# --------------------------------------------------------------------------- #


def test_the_tail_keeps_the_last_lines_and_counts_them_all() -> None:
    """v1's processing log reached 300MB because the answer to "how much do we
    keep" was "all of it"."""
    tail = Tail(limit=3)
    for index in range(10):
        tail.add(f"line {index}")
    assert tail.text() == "line 7\nline 8\nline 9"
    assert tail.last() == "line 9"
    assert tail.total == 10


def test_an_empty_tail_has_no_last_line() -> None:
    assert Tail().last() == ""


def test_write_to_flushes_every_line() -> None:
    """A buffered sink reproduces the blackout this exists to remove, just with
    a smaller buffer."""
    target = io.StringIO()
    sink = write_to(target, prefix="runner| ")
    sink(LogLine(stream=Stream.STDOUT, text="working", at=AT))
    assert target.getvalue() == "runner| working\n"


def test_write_to_tags_the_phase_when_one_is_given() -> None:
    """Without this, a repository's own install output — which prints its own
    success banner for a step that never touched the agent — reads exactly
    like the agent's own work."""
    target = io.StringIO()
    sink = write_to(target, prefix="runner| ")
    sink(LogLine(stream=Stream.STDOUT, text="BUILD SUCCESS", at=AT, phase="setup"))
    assert target.getvalue() == "runner| [setup] BUILD SUCCESS\n"


def test_pump_attributes_every_line_to_the_phase_it_was_given() -> None:
    lines: list[LogLine] = []

    async def drive() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"one\ntwo\n")
        reader.feed_eof()
        await pump(reader, Stream.STDOUT, on_line=lines.append, clock=lambda: AT, phase="agent")

    run(drive())
    assert [line.phase for line in lines] == ["agent", "agent"]


# --------------------------------------------------------------------------- #
# TokenTally
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("input tokens: 100", TokenUsage(input_tokens=100)),
        ("output_tokens=42", TokenUsage(output_tokens=42)),
        ("  cached input tokens : 7 ", TokenUsage(cached_input_tokens=7)),
        ("cached_input_tokens: 7", TokenUsage(cached_input_tokens=7)),
        ("reasoning tokens: 3", TokenUsage(reasoning_tokens=3)),
        ("input tokens: 1,024", TokenUsage(input_tokens=1024)),
        ("Input Tokens: 5", TokenUsage(input_tokens=5)),
    ],
)
def test_labelled_counts_are_read(line: str, expected: TokenUsage) -> None:
    tally = TokenTally()
    tally.observe(line)
    assert tally.usage == expected


def test_a_line_with_both_kinds_reads_both() -> None:
    tally = TokenTally()
    tally.observe("done. input tokens: 10, output tokens: 20")
    assert tally.usage == TokenUsage(input_tokens=10, output_tokens=20)


def test_prose_without_numbers_changes_nothing() -> None:
    tally = TokenTally()
    tally.observe("thinking about the tokens used by this function")
    assert tally.spent() == 0


def test_the_bare_total_is_kept_apart_from_the_breakdown() -> None:
    """Folding it into ``output_tokens`` would invent a split, and the cost
    ledger would then report that invention as fact."""
    tally = TokenTally()
    tally.observe("tokens used: 900")
    assert tally.usage == TokenUsage()
    assert tally.reported_total == 900
    assert tally.spent() == 900


def test_cumulative_reports_take_the_largest() -> None:
    tally = TokenTally(accumulation=Accumulation.CUMULATIVE)
    tally.observe("tokens used: 100")
    tally.observe("tokens used: 250")
    tally.observe("tokens used: 250")
    assert tally.spent() == 250


def test_incremental_reports_add_up() -> None:
    tally = TokenTally(accumulation=Accumulation.INCREMENTAL)
    tally.observe("tokens used: 100")
    tally.observe("tokens used: 250")
    assert tally.spent() == 350


def test_cumulative_labelled_counts_take_the_largest() -> None:
    tally = TokenTally(accumulation=Accumulation.CUMULATIVE)
    tally.observe("input tokens: 100 output tokens: 10")
    tally.observe("input tokens: 140 output tokens: 30")
    assert tally.usage == TokenUsage(input_tokens=140, output_tokens=30)


def test_incremental_labelled_counts_add_up() -> None:
    tally = TokenTally(accumulation=Accumulation.INCREMENTAL)
    tally.observe("input tokens: 100")
    tally.observe("input tokens: 40")
    assert tally.usage == TokenUsage(input_tokens=140)


def test_spent_is_the_larger_of_the_two_views() -> None:
    """A cap that under-reads its own evidence is not a cap."""
    tally = TokenTally()
    tally.observe("input tokens: 10 output tokens: 10")
    tally.observe("tokens used: 500")
    assert tally.spent() == 500
