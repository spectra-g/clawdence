"""Recorded LLM interactions, so agent tests are fast, free and offline.

Without this, every test touching an agent step is slow, flaky and billable —
and a suite that is slow, flaky and billable is a suite that CI quietly stops
running, which is how a project ends up with a green badge and no coverage of
the part that spends money. S12 plugs its real transport into ``play``; nothing
here knows what a model is.

Five rules, and four of them exist because the obvious implementation gets them
wrong:

**A miss is an error.** Replay never falls through to the network. That single
rule is what makes "zero LLM spend" a property rather than an intention: a new
prompt does not quietly cost money on somebody's laptop and then fail in CI,
it fails immediately and says what to re-record.

**Recording is opt-in, through the environment.** ``CLAWDENCE_CASSETTE=record``
and nothing else. A mode that could be reached by a default, a fixture argument
or a missing file is a mode CI can reach by accident.

**Lookup is by key, not by order.** Sequential cassettes — record calls 1, 2, 3
and replay them in that order — break the moment the code makes two independent
calls in the other order, and the failure looks like a logic bug. The key is a
digest of the request, so order is irrelevant and a *changed* request is a
visible miss rather than a silently wrong replay.

**Redaction happens on the way in.** Cassettes are committed. The request that
produced one carries the system prompt, the issue text and, in the headers, the
API key. Every one of those is a path from a live credential into git history,
where ``git rm`` does not remove it. Same marker as the rest of the system
(``ports.REDACTED``), so one grep finds all of it.

**The file is deterministic.** Sorted keys, one entry per line group, trailing
newline — so re-recording an unchanged interaction produces no diff, and a diff
means something actually changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from clawdence.store.redaction import redact_value

#: The one way to switch recording on.
MODE_ENV: Final = "CLAWDENCE_CASSETTE"


class CassetteError(RuntimeError):
    """A cassette could not answer, and refused to guess."""


class Mode(StrEnum):
    """How a cassette behaves when asked for an interaction."""

    #: Answer from the file; a miss is an error. The default, always.
    REPLAY = "replay"

    #: Call the live transport and write the result down. Opt-in only.
    RECORD = "record"

    #: Call the live transport and write nothing. For a one-off manual check
    #: against a real provider; never in CI.
    OFF = "off"


def mode_from_env(environ: Mapping[str, str] | None = None) -> Mode:
    """The mode this process runs in. ``REPLAY`` unless told otherwise."""
    raw = (environ if environ is not None else os.environ).get(MODE_ENV, "").strip().lower()
    if not raw:
        return Mode.REPLAY
    try:
        return Mode(raw)
    except ValueError as exc:
        options = ", ".join(item.value for item in Mode)
        raise CassetteError(f"{MODE_ENV}={raw!r} is not one of: {options}") from exc


def redact(value: JsonValue) -> JsonValue:
    """Replace anything credential-shaped, everywhere, recursively.

    Two passes over every value because the two leaks are different: a field
    *named* ``authorization`` is redacted whatever it holds, and a string that
    *looks like* a key is redacted whatever it is called — the second being the
    one that catches a key pasted into a prompt by a user.
    """
    return redact_value(value)


def key_for(request: JsonValue) -> str:
    """A stable digest of a request.

    Canonical JSON — sorted keys, no incidental whitespace — so two requests
    that differ only in field order share a key, and any real difference does
    not. Truncated to 16 hex characters: enough that a collision is not a
    practical concern for a few thousand fixtures, short enough to read in a
    filename and in an error message.
    """
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


Transport = Callable[[JsonValue], Awaitable[JsonValue]]


class Cassette:
    """Recorded request/response pairs for one test or one suite.

    Not a mock of an LLM: it stores whatever JSON the transport exchanges, so
    the same object works for a chat completion, an embedding call, or anything
    else S12 and S14 end up speaking.
    """

    __slots__ = ("_dirty", "_interactions", "_mode", "_path", "_played")

    def __init__(self, path: Path, *, mode: Mode | None = None) -> None:
        self._path = path
        self._mode = mode if mode is not None else mode_from_env()
        self._interactions: dict[str, JsonValue] = {}
        self._played: set[str] = set()
        self._dirty = False
        if path.exists():
            self._interactions = dict(_load(path))

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def path(self) -> Path:
        return self._path

    @property
    def played(self) -> frozenset[str]:
        """Keys answered from the file. Used to spot fixtures nothing reads."""
        return frozenset(self._played)

    @property
    def unused(self) -> frozenset[str]:
        """Recorded interactions no test asked for.

        Worth surfacing: a stale entry is a prompt that no longer exists, and
        it will otherwise sit in the repository for years looking like coverage.
        """
        return frozenset(self._interactions) - self._played

    async def play(self, request: JsonValue, live: Transport | None = None) -> JsonValue:
        """Answer a request from the recording, or record it.

        ``live`` is the real transport and is only ever called in ``record`` or
        ``off``. In ``replay`` it is not called even if it was supplied, which
        is deliberate — a test that passes a live transport and then runs in CI
        must not reach it.
        """
        key = key_for(request)

        if self._mode is Mode.REPLAY:
            if key not in self._interactions:
                raise CassetteError(
                    f"no recorded interaction {key} in {self._path}.\n"
                    f"  Re-record with {MODE_ENV}=record, and commit the result.\n"
                    f"  request: {_preview(request)}"
                )
            self._played.add(key)
            return self._interactions[key]

        if live is None:
            raise CassetteError(f"mode is {self._mode.value!r} but no live transport was supplied")

        response = redact(await live(request))
        if self._mode is Mode.RECORD:
            self._interactions[key] = response
            self._played.add(key)
            self._dirty = True
        return response

    def save(self) -> bool:
        """Write the cassette if recording changed it. ``True`` if it wrote.

        Nothing is written in ``replay``, so a test run can never modify a
        committed fixture as a side effect of reading it.
        """
        if self._mode is not Mode.RECORD or not self._dirty:
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(_render(self._interactions), encoding="utf-8")
        self._dirty = False
        return True


def _render(interactions: Mapping[str, JsonValue]) -> str:
    """Deterministic JSON, so re-recording something unchanged is a no-op diff."""
    return (
        json.dumps(dict(sorted(interactions.items())), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    )


def _load(path: Path) -> Mapping[str, JsonValue]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CassetteError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise CassetteError(f"{path} should hold an object keyed by request digest")
    return loaded


def _preview(request: JsonValue, limit: int = 200) -> str:
    """Enough of the request to recognise it, redacted, and never the whole thing."""
    text = json.dumps(redact(request), sort_keys=True, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"
