"""Getting JSON out of what a model actually said.

v1's ``_repair_json`` existed because models emit malformed JSON often enough that
repair is load-bearing rather than a nicety, and the shape of the malformation is
predictable: a code fence, a sentence of preamble, a trailing comma, a document
cut off mid-string because the output limit arrived first. Every one of those is
recoverable without guessing at content.

Two rules decide what belongs here.

**A repair may remove framing; it may never change content.** Stripping a fence
is safe — the fence was never part of the document. Converting single quotes to
double quotes is *not* safe, and this deliberately does not do it: the same
transformation that fixes ``{'a': 1}`` corrupts ``{"note": "it's fine"}``, and it
does so silently, producing a document that parses and means something different.
A parse failure is recoverable by asking again. A wrong value that validated is
not recoverable at all, because nothing downstream has any reason to doubt it.

**Every repair is named.** The step output carries the list, so a role whose
responses always need the same repair is visible as a prompt problem rather than
absorbed as normal. v1 repaired silently, so nobody knew which agents produced
clean output and which were being patched up on every call.

The truncation case is the one worth pointing at. ``StopReason.MAX_TOKENS`` and
``ABORTED`` leave a document with unclosed braces, and v1 observed that aborted
sessions retain usable partial content. Closing those is a repair, it is recorded
as one, and it is only ever attempted for a step that asked for partial output to
be salvaged — because a document completed by machine is a document with fields
nobody wrote.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

#: A fenced block, with or without a language tag. Models add these when asked
#: for JSON roughly half the time, whatever the prompt says.
_FENCED: Final = re.compile(r"```[A-Za-z0-9_+-]*\s*\n(?P<body>.*?)(?:\n\s*```|\s*$)", re.DOTALL)

#: A *closed* reasoning block. Removed as framing: the model thought, then
#: answered, and only the answer is the answer.
_THINK: Final = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)

#: An opener left over after the closed blocks are removed — the v1 Kimi failure
#: class, where tool calls were emitted *inside* ``<think>`` and the session ended
#: with no reply. Refused rather than mined for a document; see ``extract_json``.
_THINK_OPEN: Final = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)

#: A comma before a closing brace or bracket. JSON forbids it; every model emits
#: it eventually. Only matched outside strings — see ``_strip_trailing_commas``.
_TRAILING_COMMA: Final = re.compile(r",(\s*[}\]])")

#: How much of a failing document goes into an error message. Short, because the
#: document is model output that may quote the request, and the request is where
#: a pasted credential lives (threat model T11).
PREVIEW_CHARS: Final = 200


class RepairFailed(ValueError):
    """The text could not be read as JSON without guessing at its content."""


@dataclass(frozen=True, slots=True)
class Repaired:
    """A parsed document, and what had to be done to it.

    ``repairs`` is empty for text that was already valid JSON, which is the
    distinction the step output exists to record: "the model complied" and "we
    fixed it up" are different facts about a prompt.
    """

    value: JsonValue
    repairs: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.repairs


def extract_json(text: str, *, close_truncated: bool = False) -> Repaired:
    """Read a JSON document out of a model's reply.

    Applies the framing removals in order, stopping the moment the document
    parses, so a well-behaved reply costs one ``json.loads`` and reports no
    repairs. ``close_truncated`` permits the last and least safe step — balancing
    a document that was cut off — and defaults to off.

    Raises ``RepairFailed`` when nothing parses.
    """
    if not text.strip():
        raise RepairFailed("the model returned no text")

    repairs: list[str] = []
    candidate = text

    attempts: list[tuple[str, str]] = [("", candidate)]

    stripped_thinking = _THINK.sub("", candidate).strip()
    if stripped_thinking != candidate.strip():
        candidate = stripped_thinking
        attempts.append(("removed a reasoning block", candidate))

    if _THINK_OPEN.search(candidate) is not None:
        # An opener with no closer means everything after it is reasoning, and a
        # document found *inside* reasoning was never claimed as the answer — it is
        # the model thinking aloud about what it might return. Extracting it would
        # report speculation as a result, which is the false-success class this
        # project refuses everywhere else. This is v1's Kimi failure exactly, and
        # the useful thing to do about it is name it.
        raise RepairFailed(
            "the model never closed its reasoning block, so it emitted no answer "
            f"(the opener is at character {_THINK_OPEN.search(candidate).start()})"  # type: ignore[union-attr]
        )

    fenced = _FENCED.search(candidate)
    if fenced is not None:
        candidate = fenced.group("body").strip()
        attempts.append(("removed a code fence", candidate))

    span = _outermost_span(candidate)
    if span is not None and span != candidate.strip():
        candidate = span
        attempts.append(("discarded text around the JSON document", candidate))

    without_commas = _strip_trailing_commas(candidate)
    if without_commas != candidate:
        candidate = without_commas
        attempts.append(("removed a trailing comma", candidate))

    if close_truncated:
        closed = _close_unbalanced(candidate)
        if closed != candidate:
            candidate = closed
            attempts.append(("closed a truncated document", candidate))

    # Each attempt is the cumulative result of every repair up to it, so trying
    # them in order and keeping the repairs made so far is the same as trying
    # every subset — without the combinatorics, and in an order where the least
    # invasive repair is preferred.
    for repair, document in attempts:
        if repair:
            repairs.append(repair)
        try:
            parsed: JsonValue = json.loads(document)
        except ValueError:
            continue
        return Repaired(value=parsed, repairs=tuple(repairs))

    raise RepairFailed(
        f"the reply is not JSON and could not be repaired into any "
        f"({len(attempts) - 1} repair(s) attempted): {_preview(text)}"
    )


def _outermost_span(text: str) -> str | None:
    """The widest ``{...}`` or ``[...]`` in the text, string-aware.

    Widest rather than first-balanced, because the failure this fixes is prose
    around a document ("Here is the JSON: {...} Let me know if..."), and a scan
    that stopped at the first balanced pair would take a nested object out of the
    middle of a valid one whose opening brace it had already skipped.

    String-aware because braces inside string values are extremely common in this
    application — the documents describe code — and a bracket counter that reads
    them stops in the wrong place.
    """
    start = None
    for index, char in enumerate(text):
        if char in "{[":
            start = index
            break
    if start is None:
        return None

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    end = None

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                end = index + 1

    return text[start:end] if end is not None else None


def _strip_trailing_commas(text: str) -> str:
    """Remove ``,`` before ``}`` or ``]``, outside strings.

    Done by masking string contents, running the pattern over the mask, and
    applying the resulting cuts to the original — rather than by running the
    pattern over the text, which would happily edit ``{"a": "x, ]"}``.
    """
    masked = _mask_strings(text)
    cuts = [match.start(0) for match in _TRAILING_COMMA.finditer(masked)]
    if not cuts:
        return text
    keep = [char for index, char in enumerate(text) if index not in set(cuts)]
    return "".join(keep)


def _mask_strings(text: str) -> str:
    """Replace the contents of every JSON string with spaces, keeping length.

    Length-preserving so that offsets found in the mask are offsets in the
    original. The quotes themselves are kept, which is what makes an unterminated
    final string visible to ``_close_unbalanced``.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
                out.append(" ")
            elif char == "\\":
                escaped = True
                out.append(" ")
            elif char == '"':
                in_string = False
                out.append('"')
            else:
                out.append(" ")
            continue
        out.append(char)
        if char == '"':
            in_string = True
    return "".join(out)


def _close_unbalanced(text: str) -> str:
    """Close a document that stopped early.

    Three cuts, in this order: drop an unterminated final string *and the key or
    element it belonged to*, drop a dangling comma, then close every open
    bracket. Dropping the incomplete member rather than closing its quote is the
    difference between a salvaged document and an invented one — ``{"summary":
    "the run fai`` closed at the quote asserts that the summary is ``the run
    fai``, which is a sentence the model did not write.
    """
    masked = _mask_strings(text)
    body = text

    if masked.count('"') % 2:
        # An unterminated string. Cut back to the last structural boundary before
        # it, which discards the partial member entirely.
        opening = masked.rindex('"')
        boundary = max(
            body.rfind(",", 0, opening),
            body.rfind("{", 0, opening),
            body.rfind("[", 0, opening),
        )
        if boundary < 0:
            return text
        body = body[: boundary + 1] if body[boundary] in "{[" else body[:boundary]
        masked = _mask_strings(body)

    body = body.rstrip()
    dropped_key = False
    while body and body[-1] in ",:":
        dropped_key = dropped_key or body[-1] == ":"
        body = body[:-1].rstrip()
    if dropped_key:
        # A colon at the end means the last thing in the document is a key whose
        # value never arrived. Closing here would produce ``{"a": 1, "b"}``, which
        # is not JSON; keeping the key with a made-up value would be worse than
        # both. Cut the key out and let the schema report the field as missing,
        # which is a true statement about what the model managed to say.
        masked = _mask_strings(body)
        boundary = max(masked.rfind(","), masked.rfind("{"), masked.rfind("["))
        if boundary < 0:
            return text
        body = (body[: boundary + 1] if body[boundary] in "{[" else body[:boundary]).rstrip()
    if not body:
        return text

    stack: list[str] = []
    in_string = False
    escaped = False
    for char in body:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack:
            stack.pop()

    return body + "".join(reversed(stack))


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    collapsed = " ".join(text.split())
    return repr(collapsed if len(collapsed) <= limit else collapsed[:limit] + "…")
