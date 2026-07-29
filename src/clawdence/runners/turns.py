"""Reading the agent's own event stream, to see what the exit status cannot.

§3.7a's finding in one sentence: **an agent CLI can exit 0 having done nothing**,
because the thing that went wrong went wrong at the provider and the CLI reported
it the way it reports everything else — as an event — and then exited normally.
A taxonomy anchored on the exit status calls that ``SUCCEEDED``, opens a pull
request, and advances the workflow. That is a *false success*, and a false
success is worse than the single-value taxonomy v1 had, because v1's at least
made you look.

Two signals come out of here and each answers a question the process could not:

**Did the stream end on an error?** Not "did an error appear" — an agent that
hits a rate limit, waits, and carries on has emitted an error and then recovered,
and failing that run would be inventing a failure. So a model turn *clears* a
pending error, and only an error with no model turn after it is terminal. That
one rule is most of this module.

**Was there a model turn at all?** Events flowing with no model among them is the
rejected credential: the CLI started, printed its banner, asked, was refused, and
stopped. It exits like a startup failure and reads like one, which sends somebody
looking at the image instead of at the key.

**Three-valued, deliberately.** Most CLIs emit prose, not events, and for those
this can say nothing — so ``model_turn_seen`` is ``None`` rather than a
``False`` that would report ``NO_MODEL_RESPONSE`` for every run of a CLI that
simply does not emit JSON. Absence of evidence gets its own value, and the
classifier falls through to the exit status exactly as S6 did.

**Only structured events, never prose.** Matching "credit balance" against
stdout would fire on an agent that *printed* an error it went on to handle, and
a false ``PROVIDER_ERROR`` on a run that worked costs as much as the false
success this exists to stop. The line for a CLI that emits nothing readable is
"we cannot tell", and it is a better line than a guess.

**What the markers are.** Configuration in the same sense as ``AgentCommand``:
the CLIs move faster than this system will, and a new one is a name added to a
frozenset rather than a new code path. What is *not* configuration is the
terminal rule above — that is the finding, and it is the same for every CLI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

#: Capped hard, because this string is persisted with the step result and it is
#: written by the provider — which means it can quote the request that was
#: rejected, and a rejected request contains whatever was in it. Kept rather
#: than dropped because a ``provider-error`` that does not say which provider
#: error is a value nobody can act on; bounded because "the provider said so" is
#: not a reason to persist a kilobyte of it. The residue is real and is the same
#: one ``AgentCommand.include_stderr_tail`` names.
MAX_ERROR_CHARS: Final = 200

#: ``type`` values that name a turn the model took. The shapes in the wild are a
#: message with a role and an envelope with a type, so both are read. Kept
#: narrow: ``message`` and ``response`` are envelope names that a user turn and
#: a tool result wear too, and a marker that matches the prompt going *in* makes
#: every run that started look like a run that answered.
_TURN_TYPES: Final[frozenset[str]] = frozenset(
    {
        "assistant",
        "assistant_message",
        "agent_message",
        "agent_reasoning",
    }
)

#: Roles that mean the model spoke. A user turn is the prompt going *in*, and
#: counting it would make every run that started look like a run that answered.
_TURN_ROLES: Final[frozenset[str]] = frozenset({"assistant", "model"})

#: Values of ``stop_reason``, ``status`` or ``subtype`` that mean the turn
#: carried a failure rather than an answer.
_ERROR_REASONS: Final[frozenset[str]] = frozenset(
    {
        "error",
        "errored",
        "error_during_execution",
        "error_max_turns",
        "failed",
        "failure",
        "overloaded",
        "rate_limit",
        "rate_limited",
        "refusal",
    }
)

#: Keys whose value, when it is a string, is the provider's own complaint.
_MESSAGE_KEYS: Final[tuple[str, ...]] = (
    "message",
    "error_message",
    "detail",
    "reason",
    "result",
    "text",
)

#: Keys an event nests its real content under. One level, not a recursive walk:
#: a walk would find a ``role: assistant`` inside a transcript the agent quoted
#: back, and start reporting turns that a previous run took.
_NESTED_KEYS: Final[tuple[str, ...]] = ("message", "item", "response", "turn", "data")


@dataclass(slots=True)
class TurnTracker:
    """What the stream said about the agent's turns, so far.

    Fed the same lines as the token tally, from the same callback, because both
    are answers to "what has this run said" and reading the stream twice would
    mean two chances to disagree about what arrived.
    """

    #: Whether anything on this stream parsed as an agent event. What separates
    #: "the agent never responded" from "this CLI does not emit events".
    saw_event: bool = False

    #: Whether a model turn was among them.
    saw_model_turn: bool = False

    #: An error with no model turn after it yet. Cleared when one arrives.
    pending_error: str | None = None

    def observe(self, text: str) -> None:
        """Fold one line of stdout into the record."""
        event = _parse(text)
        if event is None:
            return
        error = _error_in(event)
        turn = _is_model_turn(event)
        if error is None and not turn:
            # A tool call, a token count, a heartbeat. Real, and says nothing
            # about either question — but it does prove this CLI emits events,
            # which is what makes an *absent* model turn mean something.
            self.saw_event = True
            return

        self.saw_event = True
        if error is not None:
            self.pending_error = error
            return
        self.saw_model_turn = True
        # The model answered after the error, so the CLI recovered from it. An
        # agent that hits a rate limit, waits and carries on is a working agent.
        self.pending_error = None

    @property
    def terminal_error(self) -> str | None:
        """The error the stream ended on, if it ended on one."""
        return self.pending_error

    @property
    def model_turn_seen(self) -> bool | None:
        """Whether the model spoke. ``None`` when this CLI cannot say."""
        return self.saw_model_turn if self.saw_event else None


def _parse(text: str) -> dict[str, Any] | None:
    """One line as an event object, or ``None``.

    The cheap check first: a CLI printing a megabyte of prose should not be
    handing every line of it to a JSON parser, and a line that is an event
    always starts with a brace.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        # Not JSON, or nested past the parser's limit — which is a thing a
        # process running model-generated output can print at us.
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_model_turn(event: dict[str, Any]) -> bool:
    """Whether the model itself spoke in this event.

    A ``role`` settles the scope on its own, either way: an event that says
    ``role: user`` is the prompt going in, and falling through to the type
    markers after reading that would let an envelope name overrule the one
    field that was unambiguous.
    """
    for scope in _scopes(event):
        role = _text(scope.get("role"))
        if role:
            if role in _TURN_ROLES:
                return True
            continue
        if _type_of(scope) in _TURN_TYPES:
            return True
    return False


def _error_in(event: dict[str, Any]) -> str | None:
    """The provider's complaint, if this event carries one.

    ``is_error`` before the reason fields, because a CLI that has a boolean for
    it is being unambiguous and the reason strings are where the guessing is.
    """
    for scope in _scopes(event):
        if scope.get("is_error") is True:
            return _render(scope, fallback="the agent reported an error")

        nested = scope.get("error")
        if isinstance(nested, dict):
            return _render(nested, fallback="the provider returned an error")
        if isinstance(nested, str) and nested.strip():
            return _clip(nested)

        for key in ("stop_reason", "status", "subtype", "type"):
            value = _text(scope.get(key))
            if value in _ERROR_REASONS:
                return _render(scope, fallback=value)
    return None


def _scopes(event: dict[str, Any]) -> list[dict[str, Any]]:
    """The event, then one level of whatever it wraps.

    One level rather than a walk: an agent that echoes a transcript back has a
    ``role: assistant`` somewhere inside it, and a walk would read that as this
    run taking a turn.
    """
    scopes = [event]
    for key in _NESTED_KEYS:
        nested = event.get(key)
        if isinstance(nested, dict):
            scopes.append(nested)
    return scopes


def _render(scope: dict[str, Any], *, fallback: str) -> str:
    for key in _MESSAGE_KEYS:
        value = scope.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value)
    return _clip(fallback)


def _type_of(scope: dict[str, Any]) -> str:
    """``type``, with any envelope prefix dropped: ``item.completed`` → ``completed``
    is useless, but ``response.output_text`` → ``output_text`` is not, so the
    *last* segment is the one asked about."""
    return _text(scope.get("type")).rsplit(".", 1)[-1]


def _text(value: object) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _clip(value: str) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= MAX_ERROR_CHARS:
        return collapsed
    return collapsed[: MAX_ERROR_CHARS - 1] + "…"
