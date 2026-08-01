"""Record/replay — and the rules that keep CI free and offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from clawdence.ports import REDACTED
from tests.conftest import CassetteFactory
from tests.harness.cassette import (
    MODE_ENV,
    Cassette,
    CassetteError,
    Mode,
    key_for,
    mode_from_env,
    redact,
)
from tests.ports.factories import run

REQUEST: JsonValue = {"model": "a-model", "messages": [{"role": "user", "content": "hello"}]}
RESPONSE: JsonValue = {"content": "hi", "usage": {"input_tokens": 3, "output_tokens": 1}}


class Live:
    """A transport that answers, and counts how often it was asked.

    A class rather than a closure with an attribute bolted on, because the
    assertion these tests keep making is "the live transport was not reached",
    and that reads better as ``transport.calls == []``.
    """

    def __init__(self, response: JsonValue = RESPONSE) -> None:
        self.response = response
        self.calls: list[JsonValue] = []

    async def __call__(self, request: JsonValue) -> JsonValue:
        self.calls.append(request)
        return self.response


def live(response: JsonValue = RESPONSE) -> Live:
    return Live(response)


# --------------------------------------------------------------------------- #
# The rule that matters
# --------------------------------------------------------------------------- #


def test_a_miss_is_an_error_not_a_network_call(tmp_path: Path) -> None:
    """The single rule that makes "zero LLM spend" a property rather than an
    intention. A new prompt fails immediately and says what to re-record,
    instead of quietly costing money on a laptop and failing in CI."""
    cassette = Cassette(tmp_path / "c.json", mode=Mode.REPLAY)
    with pytest.raises(CassetteError) as caught:
        run(cassette.play(REQUEST))
    assert MODE_ENV in str(caught.value)
    assert "record" in str(caught.value)


def test_replay_ignores_a_live_transport_it_was_handed(tmp_path: Path) -> None:
    """A test that supplies a live transport and then runs in CI must not
    reach it. Replay does not call it even when it is there."""
    cassette = Cassette(tmp_path / "c.json", mode=Mode.REPLAY)
    transport = live()
    with pytest.raises(CassetteError):
        run(cassette.play(REQUEST, transport))
    assert transport.calls == []


def test_record_then_replay(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    recorder = Cassette(path, mode=Mode.RECORD)
    transport = live()
    assert run(recorder.play(REQUEST, transport)) == RESPONSE
    assert recorder.save() is True

    player = Cassette(path, mode=Mode.REPLAY)
    assert run(player.play(REQUEST)) == RESPONSE
    assert len(transport.calls) == 1


def test_replay_never_writes(tmp_path: Path) -> None:
    """A test run must not modify a committed fixture as a side effect of
    reading it."""
    path = tmp_path / "c.json"
    Cassette(path, mode=Mode.RECORD)
    player = Cassette(path, mode=Mode.REPLAY)
    assert player.save() is False
    assert not path.exists()


def test_off_calls_through_and_records_nothing(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    cassette = Cassette(path, mode=Mode.OFF)
    assert run(cassette.play(REQUEST, live())) == RESPONSE
    assert cassette.save() is False
    assert not path.exists()


def test_recording_without_a_transport_says_so(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "c.json", mode=Mode.RECORD)
    with pytest.raises(CassetteError, match="live transport"):
        run(cassette.play(REQUEST))


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


def test_the_key_ignores_field_order() -> None:
    """Two requests differing only in serialisation order are one request."""
    assert key_for({"a": 1, "b": 2}) == key_for({"b": 2, "a": 1})


def test_a_changed_request_is_a_different_key() -> None:
    """Which is the point: a prompt change is a visible miss you have to
    re-record deliberately, not a silently wrong replay."""
    assert key_for({"prompt": "one"}) != key_for({"prompt": "two"})


def test_lookup_is_by_key_not_by_order(tmp_path: Path) -> None:
    """Sequential cassettes break the moment the code makes two independent
    calls in the other order, and the failure looks like a logic bug."""
    path = tmp_path / "c.json"
    recorder = Cassette(path, mode=Mode.RECORD)
    run(recorder.play({"n": 1}, live({"answer": 1})))
    run(recorder.play({"n": 2}, live({"answer": 2})))
    recorder.save()

    player = Cassette(path, mode=Mode.REPLAY)
    assert run(player.play({"n": 2})) == {"answer": 2}
    assert run(player.play({"n": 1})) == {"answer": 1}


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_named_secret_fields_are_replaced() -> None:
    """Cassettes are committed, and ``git rm`` does not remove a key from
    history."""
    cleaned = redact({"Authorization": "Bearer abc", "api_key": "xyz", "model": "a-model"})
    assert cleaned == {"Authorization": REDACTED, "api_key": REDACTED, "model": "a-model"}


def test_key_shaped_values_are_replaced_wherever_they_appear() -> None:
    """The one that catches a credential pasted into a prompt by a user — the
    field is called ``content``, so a name-based rule would miss it."""
    pasted = "here is my key sk-abcdefghijklmnopqrstuvwx please use it"
    cleaned = redact({"content": pasted})
    assert isinstance(cleaned, dict)
    assert "sk-abcdefghij" not in str(cleaned["content"])
    assert REDACTED in str(cleaned["content"])


@pytest.mark.parametrize(
    "value",
    [
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAA",
        "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "xoxb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_common_credential_shapes(value: str) -> None:
    assert redact(f"token is {value}") == f"token is {REDACTED}"


def test_redaction_reaches_into_lists_and_nesting() -> None:
    cleaned = redact({"messages": [{"role": "system", "api_key": "abc"}]})
    assert cleaned == {"messages": [{"role": "system", "api_key": REDACTED}]}


def test_non_strings_pass_through() -> None:
    assert redact({"n": 1, "ok": True, "nothing": None}) == {"n": 1, "ok": True, "nothing": None}


def test_recorded_responses_are_redacted_on_the_way_in(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    recorder = Cassette(path, mode=Mode.RECORD)
    run(recorder.play(REQUEST, live({"echo": "your key sk-abcdefghijklmnopqrstuvwx"})))
    recorder.save()
    assert "sk-abcdefghij" not in path.read_text(encoding="utf-8")


def test_the_miss_message_is_redacted_too(tmp_path: Path) -> None:
    """The error prints part of the request so you can recognise it. That
    request is exactly where a pasted key lives."""
    cassette = Cassette(tmp_path / "c.json", mode=Mode.REPLAY)
    with pytest.raises(CassetteError) as caught:
        run(cassette.play({"content": "sk-abcdefghijklmnopqrstuvwx"}))
    assert "sk-abcdefghij" not in str(caught.value)


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #


def test_the_file_is_deterministic(tmp_path: Path) -> None:
    """Re-recording something unchanged produces no diff, so a diff means
    something actually changed."""
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    one: JsonValue = {"n": 1}
    two: JsonValue = {"n": 2}
    for path, order in ((first, (one, two)), (second, (two, one))):
        recorder = Cassette(path, mode=Mode.RECORD)
        for request in order:
            run(recorder.play(request, live({"answer": request})))
        recorder.save()
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_a_corrupt_cassette_says_which_file(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CassetteError, match="valid JSON"):
        Cassette(path, mode=Mode.REPLAY)


def test_a_cassette_holding_the_wrong_shape_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(CassetteError, match="keyed by request digest"):
        Cassette(path, mode=Mode.REPLAY)


def test_unused_entries_are_reported(tmp_path: Path) -> None:
    """A stale entry is a prompt that no longer exists. Left alone it sits in
    the repository for years looking like coverage."""
    path = tmp_path / "c.json"
    recorder = Cassette(path, mode=Mode.RECORD)
    run(recorder.play({"n": 1}, live()))
    run(recorder.play({"n": 2}, live()))
    recorder.save()

    player = Cassette(path, mode=Mode.REPLAY)
    run(player.play({"n": 1}))
    assert player.unused == frozenset({key_for({"n": 2})})
    assert player.played == frozenset({key_for({"n": 1})})


def test_saving_an_unchanged_recording_does_not_rewrite(tmp_path: Path) -> None:
    recorder = Cassette(tmp_path / "c.json", mode=Mode.RECORD)
    run(recorder.play(REQUEST, live()))
    assert recorder.save() is True
    assert recorder.save() is False


# --------------------------------------------------------------------------- #
# Mode selection
# --------------------------------------------------------------------------- #


def test_the_default_is_replay() -> None:
    """A mode reachable by a default, a fixture argument or a missing file is a
    mode CI can reach by accident."""
    assert mode_from_env({}) is Mode.REPLAY
    assert mode_from_env({MODE_ENV: ""}) is Mode.REPLAY


def test_recording_is_opt_in_through_the_environment() -> None:
    assert mode_from_env({MODE_ENV: "record"}) is Mode.RECORD
    assert mode_from_env({MODE_ENV: "  RECORD "}) is Mode.RECORD
    assert mode_from_env({MODE_ENV: "off"}) is Mode.OFF


def test_an_unknown_mode_is_rejected_with_the_options() -> None:
    with pytest.raises(CassetteError, match="replay, record, off"):
        mode_from_env({MODE_ENV: "yes-please"})


def test_the_fixture_defaults_to_replay_under_a_temporary_path(
    cassettes: CassetteFactory,
) -> None:
    """Temporary rather than the committed fixture directory, so a test cannot
    rewrite a fixture by accident."""
    cassette = cassettes("anything")
    assert cassette.mode is Mode.REPLAY
    assert not cassette.path.exists()
