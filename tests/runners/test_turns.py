"""Reading the agent's event stream: what it says, and what it refuses to say.

Two questions, and the interesting tests are all about the boundary between
"this stream told us something" and "this stream cannot be asked". A reader that
guesses in that gap either invents failures on every plain-text CLI or reports
false successes on every structured one, and both are worse than the taxonomy
this replaces.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from clawdence.runners.turns import MAX_ERROR_CHARS, TurnTracker


def feed(*lines: str) -> TurnTracker:
    tracker = TurnTracker()
    for line in lines:
        tracker.observe(line)
    return tracker


def event(**fields: Any) -> str:
    return json.dumps(fields)


# --------------------------------------------------------------------------- #
# Was there a model turn at all
# --------------------------------------------------------------------------- #


def test_a_cli_that_says_nothing_structured_cannot_be_asked() -> None:
    """The default, and the reason the answer is three-valued.

    Most CLIs emit prose. A reader that returned ``False`` here would report
    ``no-model-response`` for every run of every one of them.
    """
    tracker = feed("working on it", "wrote app.py", "done")
    assert tracker.model_turn_seen is None
    assert tracker.terminal_error is None


def test_events_with_no_model_among_them_is_the_rejected_credential() -> None:
    """§3.7a's third failure. Reported as ``startup-failed`` it is
    indistinguishable from a missing image, which is a different repair."""
    tracker = feed(
        event(type="system", subtype="init", model="some-model"),
        event(type="tool_use", name="bash"),
    )
    assert tracker.model_turn_seen is False


def test_a_model_turn_is_recognised_by_its_role() -> None:
    tracker = feed(event(type="message", message={"role": "assistant", "content": "hello"}))
    assert tracker.model_turn_seen is True


def test_a_model_turn_is_recognised_by_its_type_when_there_is_no_role() -> None:
    """codex's shape: an envelope whose item names what it is."""
    tracker = feed(event(type="item.completed", item={"type": "assistant_message", "text": "hi"}))
    assert tracker.model_turn_seen is True


def test_the_prompt_going_in_is_not_a_turn_coming_out() -> None:
    """A user message counts as an event and not as an answer. Counting it would
    make every run that *started* look like a run that responded."""
    tracker = feed(event(type="message", message={"role": "user", "content": "the plan"}))
    assert tracker.model_turn_seen is False


def test_a_role_settles_its_scope_even_when_the_type_disagrees() -> None:
    """Within one object the unambiguous field wins. ``role`` says who spoke;
    ``type`` is the marker set doing its best, and it does not get to overrule
    the field that was not guessing."""
    tracker = feed(event(role="user", type="assistant_message", content="the plan"))
    assert tracker.model_turn_seen is False


# --------------------------------------------------------------------------- #
# Did the stream end on an error
# --------------------------------------------------------------------------- #


def test_a_terminal_error_frame_is_reported() -> None:
    tracker = feed(
        event(type="assistant", message={"role": "assistant", "content": "starting"}),
        event(type="result", subtype="error_during_execution", message="credit balance too low"),
    )
    assert tracker.terminal_error == "credit balance too low"
    assert tracker.model_turn_seen is True


def test_an_error_the_agent_recovered_from_is_not_terminal() -> None:
    """The rule the whole module is built on. An agent that hits a rate limit,
    waits, and carries on is a working agent — failing that run would be
    inventing a failure out of a retry that succeeded."""
    tracker = feed(
        event(type="error", error={"message": "rate limited, retrying"}),
        event(type="assistant", message={"role": "assistant", "content": "done"}),
    )
    assert tracker.terminal_error is None


def test_the_last_error_is_the_one_reported() -> None:
    tracker = feed(
        event(type="error", message="first"),
        event(type="assistant", message={"role": "assistant"}),
        event(type="error", message="second"),
    )
    assert tracker.terminal_error == "second"


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ({"type": "result", "is_error": True, "result": "overloaded"}, "overloaded"),
        ({"type": "error", "error": "a bare string"}, "a bare string"),
        ({"type": "error", "error": {"detail": "a 400"}}, "a 400"),
        ({"type": "turn", "stop_reason": "refusal"}, "refusal"),
        ({"type": "result", "status": "failed", "reason": "no capacity"}, "no capacity"),
        ({"type": "result", "is_error": True}, "the agent reported an error"),
    ],
)
def test_the_shapes_a_provider_failure_arrives_in(frame: dict[str, Any], expected: str) -> None:
    """Each of these is a real CLI's spelling of the same thing. Marker sets
    rather than a parser per vendor: a new CLI is a name added to a frozenset."""
    assert feed(json.dumps(frame)).terminal_error == expected


def test_a_successful_result_frame_is_not_an_error() -> None:
    """The frame every run ends on. Reading it as a failure would invert the
    bug this module exists to fix."""
    tracker = feed(
        event(type="assistant", message={"role": "assistant"}),
        event(type="result", subtype="success", is_error=False, result="all done"),
    )
    assert tracker.terminal_error is None
    assert tracker.model_turn_seen is True


# --------------------------------------------------------------------------- #
# Untrusted input, because that is what a stream is
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "not json at all",
        "{not valid json",
        '["a", "list"]',
        "{}",
        '{"type": null}',
        "42",
    ],
)
def test_a_line_that_is_not_an_event_is_ignored_rather_than_fatal(line: str) -> None:
    """A CLI printing a JSON fragment, a progress bar or a stack trace must not
    be able to make the reader raise: it runs inside the loop that is reading a
    live process, and an exception there loses the whole run."""
    tracker = feed(line)
    assert tracker.terminal_error is None
    assert tracker.model_turn_seen in (None, False)


def test_a_quoted_transcript_does_not_count_as_a_turn() -> None:
    """One level of nesting, not a walk. An agent that echoes a conversation
    back has a ``role: assistant`` deep inside it, and a recursive reader would
    report a previous run's turn as this one's."""
    tracker = feed(
        event(
            type="tool_result",
            output={"history": {"messages": [{"role": "assistant", "content": "old"}]}},
        )
    )
    assert tracker.model_turn_seen is False


def test_a_provider_message_is_clipped() -> None:
    """It is persisted with the step result and it is written by the provider,
    which means it can quote the request that was rejected."""
    tracker = feed(event(type="error", message="x" * (MAX_ERROR_CHARS * 3)))
    assert tracker.terminal_error is not None
    assert len(tracker.terminal_error) == MAX_ERROR_CHARS
    assert tracker.terminal_error.endswith("…")


def test_whitespace_in_a_provider_message_is_collapsed() -> None:
    """Newlines in the middle of the result's ``message`` field turn one record
    into something that reads like several."""
    assert feed(event(type="error", message="a\n  b\tc")).terminal_error == "a b c"
