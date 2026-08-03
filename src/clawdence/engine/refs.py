"""References into prior stages — one grammar, two syntaxes.

``$plan.json.confidence`` in a condition and ``${plan.json.confidence}`` in an
argv element name the same thing. Both are parsed here so the two cannot drift,
and so the loader can walk every reference in a file before running any of it.

The shape is adopted from Lobster (ADR-0003), including the ``json`` /
``response`` split that S2 wrote into ``StepResult``: ``json`` is what a step
*produced*, ``response`` is what a *human* submitted at a gate. A workflow
branching on a human decision is doing something categorically different from
one branching on a computed verdict.

Facets are a **closed set**. An unknown one is a load error rather than a null,
because ``$plan.jsonn.x`` is a typo every time and the alternative is a guard
that silently never fires.

Two sentinels, deliberately distinct:

``MISSING``
    The path is not present. Distinct from a JSON ``null`` that *is* present —
    ``$plan.json.error == null`` should be true when the step emitted an
    explicit null, and that is a different fact from the step never having had
    the field at all.

Resolution is total for conditions and strict for interpolation, and that
asymmetry is intentional; see ``conditions`` and ``interpolation``.

**One name is not a stage.** ``$request.json.text`` reads the work item the run
is for, and it resolves against a value the pipeline (S11) seeded rather than
against anything the workflow declares. It exists because a process has to be
able to see what it was asked to do, and every alternative was worse: a first
``script`` stage echoing the text — which is what ``examples/`` shipped until
S11 — puts attacker-controlled text in an argv and makes every workflow carry a
stage that does no work, and a handler that reached for the work item itself
would give each step type its own private channel to the request.

It is deliberately read-only, deliberately only navigable through ``json``, and
deliberately reserved: the loader refuses a *stage* called ``request``, because
a workflow that declared one would silently shadow this and change what every
reference below it meant.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import JsonValue

from clawdence.domain import StepResult, StepStatus


class Facet(StrEnum):
    """The addressable views of one stage's result."""

    #: ``StepResult.output`` — what the step produced. Addressable further.
    JSON = "json"
    #: ``StepResult.response`` — what a human submitted. Addressable further.
    RESPONSE = "response"
    #: ``StepResult.status`` as a string. Terminal.
    STATUS = "status"
    #: Terminal booleans, replacing Lobster's ``$step.approved`` /
    #: ``$step.skipped``. Present because branching on "did it work" is the
    #: common case and ``$x.status == "succeeded"`` is the noisy way to say it.
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


#: Facets that address a value a path can descend into. The rest are terminal,
#: and a path after them is a load error rather than a silent ``MISSING``.
NAVIGABLE: Final = frozenset({Facet.JSON, Facet.RESPONSE})

#: Statuses that count as a failure for the ``failed`` facet. A stage that timed
#: out did not succeed, and a workflow asking "did this fail" means to catch
#: that too.
_FAILED_STATUSES: Final = frozenset({StepStatus.FAILED, StepStatus.TIMED_OUT})


class _MissingType:
    """The absence of a value. A singleton so identity comparison works."""

    _instance: _MissingType | None = None

    def __new__(cls) -> _MissingType:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _MissingType()

#: What a resolved reference can be: any JSON value, or absent.
Resolved = JsonValue | _MissingType

#: The one name a reference may use without a stage declaring it: the work item
#: the run is for, seeded by the pipeline. See the module docstring.
REQUEST: Final = "request"

#: A stage id, per ``domain.ids.Slug``.
_STAGE = r"[a-z][a-z0-9_-]*"
#: A path segment: an object key, or an integer list index.
_SEGMENT = r"[A-Za-z_][A-Za-z0-9_-]*|[0-9]+"

#: How the condition tokenizer recognises a reference: greedy over the
#: characters a reference can contain, so ``$plan.json.x=="y"`` splits at the
#: operator. Whether what it matched is *valid* is ``parse_reference``'s job —
#: the tokenizer's only duty is to take the whole of it, so that a typo is
#: reported as a bad reference rather than as a stray token after a good one.
REFERENCE_TOKEN: Final = re.compile(r"\$[A-Za-z0-9_.-]*")


@dataclass(frozen=True, slots=True)
class Reference:
    """One parsed reference, with the text it came from for error messages."""

    stage_id: str
    facet: Facet
    path: tuple[str, ...]
    text: str


def parse_reference(text: str, *, sigil: str = "$") -> Reference:
    """Parse ``<sigil>stage.facet[.path...]``.

    Raises ``ValueError`` with a message naming what was wrong. Callers wrap it
    in whichever of ``ConditionSyntaxError`` / ``WorkflowLoadError`` fits their
    context — the grammar has no opinion about which syntax it was written in.
    """
    body = text.removeprefix(sigil) if sigil and text.startswith(sigil) else text
    parts = body.split(".")

    if not parts[0]:
        raise ValueError("a reference needs a stage id")
    if not re.fullmatch(_STAGE, parts[0]):
        raise ValueError(
            f"{parts[0]!r} is not a valid stage id "
            "(lowercase, starting with a letter, then letters, digits, '_' or '-')"
        )
    if len(parts) == 1:
        raise ValueError(
            f"reference to stage {parts[0]!r} names no facet; expected one of {_facet_list()}"
        )

    try:
        facet = Facet(parts[1])
    except ValueError:
        raise ValueError(
            f"{parts[1]!r} is not a facet of stage {parts[0]!r}; expected one of {_facet_list()}"
        ) from None

    path = tuple(parts[2:])
    if path and facet not in NAVIGABLE:
        raise ValueError(
            f"{facet.value!r} is a single value and has no fields, "
            f"so {'.'.join(path)!r} cannot be read from it"
        )
    for segment in path:
        if not re.fullmatch(_SEGMENT, segment):
            raise ValueError(
                f"{segment!r} is not a usable path segment "
                "(an object key, or a digit index into a list)"
            )

    return Reference(stage_id=parts[0], facet=facet, path=path, text=text)


def _facet_list() -> str:
    return ", ".join(sorted(f.value for f in Facet))


class Resolver:
    """Reads references against the step results a run has produced so far.

    Holds results rather than a store: S3 has no persistence, and when S4
    arrives the executor hands it a resolver built from rows instead. Nothing
    below this line needs to know which it was.

    ``request`` is the work item the run is for, or ``None`` for a run that has
    no work item behind it — ``clawdence run`` against a file, which is every
    ad-hoc run. ``None`` and "the request has no such field" both resolve to
    ``MISSING``, which is right: a workflow reading ``${request.json.text}``
    outside a pipeline has not been given one, and that is the same absence.
    """

    __slots__ = ("_request", "_results", "_variables")

    def __init__(
        self,
        results: Mapping[str, StepResult],
        *,
        request: JsonValue = None,
        variables: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self._results = results
        self._request = request
        self._variables = variables if variables is not None else {}

    def resolve(self, ref: Reference) -> Resolved:
        """The value a reference names, or ``MISSING``.

        A stage that has not run yet resolves to ``MISSING`` rather than
        raising: the loader has already proved every reference names an
        *earlier* stage, so the only way to get here is a stage the run never
        reached, and "not there" is the honest answer for that.
        """
        if ref.stage_id == REQUEST:
            # Only ``json``. The loader rejects the others, so reaching here
            # with one means a ``Resolver`` built in Python without going
            # through it — answered rather than raised, because this is a read
            # of something absent and that is what ``MISSING`` is for.
            return _descend(self._request, ref.path) if ref.facet is Facet.JSON else MISSING

        if ref.stage_id in self._variables:
            return (
                _descend(self._variables[ref.stage_id], ref.path)
                if ref.facet is Facet.JSON
                else MISSING
            )

        result = self._results.get(ref.stage_id)
        if result is None:
            return MISSING

        match ref.facet:
            case Facet.STATUS:
                return result.status.value
            case Facet.SUCCEEDED:
                return result.status is StepStatus.SUCCEEDED
            case Facet.FAILED:
                return result.status in _FAILED_STATUSES
            case Facet.SKIPPED:
                return result.status is StepStatus.SKIPPED
            case Facet.JSON:
                root: JsonValue = result.output
            case Facet.RESPONSE:
                root = result.response

        return _descend(root, ref.path)


def _descend(value: JsonValue, path: tuple[str, ...]) -> Resolved:
    """Walk a dotted path, yielding ``MISSING`` the moment it leaves the data.

    Numeric segments index lists; everything else keys objects. A segment that
    is numeric *and* names an object key reads the key — objects win, because
    JSON object keys are strings and a workflow addressing ``$x.json.2`` of an
    object means the key.
    """
    current: Resolved = value
    for segment in path:
        if isinstance(current, dict):
            if segment not in current:
                return MISSING
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current
