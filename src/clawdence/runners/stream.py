"""Watching a runner work, and counting what it spends while it does.

v1 ran the coding agent with ``capture_output=True``. Everything the agent said
arrived at once, at the end, if it ended — so a run that was going to take forty
minutes and produce nothing looked identical, for forty minutes, to one that was
about to succeed. That was the single most-felt operational problem in the whole
system, and it is fixed here by structure rather than by a flag: output is read
as it arrives and handed to a ``LogSink``, and everything else in this module
exists because reading output as it arrives raises three questions.

**How much do we keep?** A ``Tail``, and only a tail. v1's processing log reached
300MB because the answer was "all of it". The full stream goes to the sink, which
is somebody else's problem to bound — a file, HQ's log view (S19), a terminal.
What the control plane *retains* is the last few lines, because that is what a
person reads first when something failed.

**What do we do about a line that never ends?** ``StreamReader.readline`` raises
once a line passes its buffer limit, and a model CLI printing a minified bundle
or a base64 blob will do that. So this reads chunks and splits them itself, and
truncates an over-long line rather than failing the run over formatting.

**Where does the token count come from?** Scraped from the same stream (§3.9:
"parse ``tokens used`` from runner stdout"), which is the only channel a CLI
reliably has. Scraping is fragile and this is honest about it: the pattern is
configuration, the accumulation rule is declared rather than assumed, and a CLI
that reports nothing produces a zero that is visibly a zero rather than a
plausible number.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final, TextIO

from clawdence.domain import TokenUsage
from clawdence.ports._common import Clock, utc_now

#: A single line longer than this is cut. Well past any real log line and well
#: short of the memory a runaway ``cat`` of a binary would take.
MAX_LINE_BYTES: Final = 16 * 1024

#: Lines retained for the result's diagnostics. The sink has the whole stream.
DEFAULT_TAIL_LINES: Final = 40


class Stream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class LogLine:
    """One line, with which stream it came from and when it arrived.

    Timestamped at *arrival*, not at parse time, because the interesting
    question a log answers about a stalled runner is "when did it last say
    anything", and that is a property of the moment it was read.
    """

    stream: Stream
    text: str
    at: datetime

    #: Which of a run's two processes this came from — ``"setup"`` or
    #: ``"agent"`` (``runners.agent.Phase``'s values, taken as a plain string
    #: rather than the enum itself: that enum lives in ``agent``, which imports
    #: this module, and a run's two phases are the same shape of fact wherever
    #: they are read from). ``None`` for a line nobody attributed to a phase,
    #: which every caller but the runner is.
    #:
    #: Without this a sink cannot say which process is talking, and a
    #: repository's own install output — which prints its own "BUILD SUCCESS"
    #: banner for a dependency-resolution goal that never touched the agent —
    #: reads exactly like the agent's own work.
    phase: str | None = None


#: Where live output goes. Called once per line, in arrival order.
#:
#: A callable alias rather than a ``Protocol`` with a named parameter, because
#: the most useful sink in a test is ``list.append`` and a protocol that names
#: its argument rejects every function whose parameter is positional-only.
type LogSink = Callable[[LogLine], None]


def write_to(target: TextIO, *, prefix: str = "") -> LogSink:
    """A sink that writes lines to an open text stream, flushing each one.

    Flushing per line is the entire point — a buffered sink reproduces the
    blackout this module exists to remove, just with a smaller buffer.
    """

    def sink(line: LogLine) -> None:
        tag = f"[{line.phase}] " if line.phase else ""
        target.write(f"{prefix}{tag}{line.text}\n")
        target.flush()

    return sink


@dataclass(slots=True)
class Tail:
    """The last ``limit`` lines, and how many there were in total."""

    limit: int = DEFAULT_TAIL_LINES
    _lines: deque[str] = field(init=False, default_factory=deque)
    total: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._lines = deque(maxlen=self.limit)

    def add(self, text: str) -> None:
        self.total += 1
        self._lines.append(text)

    def text(self) -> str:
        return "\n".join(self._lines)

    def last(self) -> str:
        return self._lines[-1] if self._lines else ""


# --------------------------------------------------------------------------- #
# Token accounting
# --------------------------------------------------------------------------- #

#: Matches a labelled per-kind count: ``input tokens: 1234``, ``output_tokens=5``.
#: Named kinds are preferred over a bare total because they are what pricing and
#: the cost ledger are expressed in.
KIND_PATTERN: Final = re.compile(
    r"(?P<kind>input|output|cached[ _-]?input|cached|reasoning)[ _-]?tokens?\s*[:=]\s*"
    r"(?P<count>[\d,_]+)",
    re.IGNORECASE,
)

#: Matches the bare form v1 parsed: ``tokens used: 12345``.
TOTAL_PATTERN: Final = re.compile(
    r"\btokens?\s+used\s*[:=]?\s*(?P<count>[\d,_]+)",
    re.IGNORECASE,
)

#: Keyed on the label with separators normalised to single spaces, so
#: ``cached_input``, ``cached-input`` and ``cached input`` are one entry rather
#: than three that can drift apart.
_FIELDS: Final[dict[str, str]] = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cached": "cached_input_tokens",
    "cached input": "cached_input_tokens",
    "reasoning": "reasoning_tokens",
}


class Accumulation(StrEnum):
    """How successive token reports from one CLI combine.

    ``CUMULATIVE`` — each report restates the running total, so the answer is the
    largest one seen. ``INCREMENTAL`` — each report is a delta, so they add.

    Declared per CLI rather than inferred, because the two are indistinguishable
    from a single run and getting it backwards is unbounded in both directions:
    summing a cumulative counter overcounts badly enough to abort work that was
    within budget, and taking the maximum of deltas undercounts badly enough that
    the cap never fires. A wrong guess here defeats the one cost control the
    system has.
    """

    CUMULATIVE = "cumulative"
    INCREMENTAL = "incremental"


@dataclass(slots=True)
class TokenTally:
    """What has been reported so far, and what it means for a budget.

    ``reported_total`` is kept apart from ``usage`` on purpose. A CLI that prints
    only ``tokens used: N`` has told us a number and *not* told us how it splits,
    and folding it into ``output_tokens`` would invent a split that the cost
    ledger would then report as fact. Budget arithmetic uses whichever is larger;
    the result carries the structured half only.
    """

    accumulation: Accumulation = Accumulation.CUMULATIVE
    usage: TokenUsage = field(default=TokenUsage())
    reported_total: int = 0

    def observe(self, text: str) -> None:
        """Fold any token report on this line into the tally."""
        counts: dict[str, int] = {}
        for match in KIND_PATTERN.finditer(text):
            kind = match["kind"].lower().replace("_", " ").replace("-", " ")
            counts[_FIELDS[kind]] = _number(match["count"])
        if counts:
            self.usage = self._combine(self.usage, counts)
            return

        total = TOTAL_PATTERN.search(text)
        if total is not None:
            value = _number(total["count"])
            self.reported_total = (
                max(self.reported_total, value)
                if self.accumulation is Accumulation.CUMULATIVE
                else self.reported_total + value
            )

    def spent(self) -> int:
        """Tokens to charge against ``Budget.max_tokens``.

        The larger of the two views, because a cap that under-reads its own
        evidence is not a cap.
        """
        structured = (
            self.usage.input_tokens
            + self.usage.output_tokens
            + self.usage.cached_input_tokens
            + self.usage.reasoning_tokens
        )
        return max(structured, self.reported_total)

    def _combine(self, current: TokenUsage, counts: dict[str, int]) -> TokenUsage:
        if self.accumulation is Accumulation.CUMULATIVE:
            merged = {name: max(getattr(current, name), value) for name, value in counts.items()}
        else:
            merged = {name: getattr(current, name) + value for name, value in counts.items()}
        return current.model_copy(update=merged)


def _number(raw: str) -> int:
    return int(raw.replace(",", "").replace("_", ""))


# --------------------------------------------------------------------------- #
# Pumping a subprocess stream
# --------------------------------------------------------------------------- #


async def pump(
    reader: asyncio.StreamReader,
    stream: Stream,
    *,
    on_line: Callable[[LogLine], None],
    clock: Clock = utc_now,
    chunk_size: int = 4096,
    phase: str | None = None,
) -> None:
    """Read a subprocess stream to EOF, delivering it a line at a time.

    Splitting is done here rather than with ``readline`` because
    ``StreamReader.readline`` raises ``ValueError`` once a line exceeds its
    buffer limit, and an agent CLI that echoes a minified file will exceed it.
    Failing a run because its output had no newline in it would be absurd; the
    line is truncated and marked instead.

    A trailing fragment with no newline is delivered at EOF, because a process
    that died mid-sentence said something and that something is usually the
    most interesting line in the log.
    """
    buffer = bytearray()
    while True:
        chunk = await reader.read(chunk_size)
        if not chunk:
            break
        buffer.extend(chunk)
        while True:
            index = buffer.find(b"\n")
            if index != -1 and index <= MAX_LINE_BYTES:
                raw, buffer = bytes(buffer[:index]), buffer[index + 1 :]
                on_line(_line(raw, stream, clock, phase=phase))
            elif len(buffer) > MAX_LINE_BYTES:
                # A newline exists but is past the limit, or there is none at
                # all. Either way the cap wins: the alternative is holding an
                # unbounded line in memory in the hope that one turns up.
                raw, buffer = bytes(buffer[:MAX_LINE_BYTES]), buffer[MAX_LINE_BYTES:]
                on_line(_line(raw, stream, clock, truncated=True, phase=phase))
            else:
                break
    if buffer:
        on_line(_line(bytes(buffer), stream, clock, phase=phase))


def _line(
    raw: bytes, stream: Stream, clock: Clock, *, truncated: bool = False, phase: str | None = None
) -> LogLine:
    text = raw.decode("utf-8", errors="replace").rstrip("\r")
    return LogLine(
        stream=stream,
        text=text + " […]" if truncated else text,
        at=clock(),
        phase=phase,
    )
