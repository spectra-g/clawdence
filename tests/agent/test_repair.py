"""Every malformation v1 saw, and the two this deliberately refuses to fix."""

from __future__ import annotations

import pytest

from clawdence.agent import RepairFailed, extract_json


def parse(text: str, *, close_truncated: bool = False) -> tuple[object, tuple[str, ...]]:
    result = extract_json(text, close_truncated=close_truncated)
    return result.value, result.repairs


# --------------------------------------------------------------------------- #
# The happy path costs nothing
# --------------------------------------------------------------------------- #


def test_valid_json_is_reported_as_needing_no_repair() -> None:
    """ "The model complied" and "we fixed it up" are different facts about a
    prompt, and v1 could not tell them apart because it repaired silently."""
    result = extract_json('{"verdict": "approved"}')
    assert result.value == {"verdict": "approved"}
    assert result.repairs == ()
    assert result.clean is True


def test_a_top_level_list_is_a_document_too() -> None:
    assert parse("[1, 2, 3]") == ([1, 2, 3], ())


# --------------------------------------------------------------------------- #
# Framing removals
# --------------------------------------------------------------------------- #


def test_a_code_fence_is_removed() -> None:
    value, repairs = parse('```json\n{"size": "M"}\n```')
    assert value == {"size": "M"}
    assert repairs == ("removed a code fence",)


def test_an_unlabelled_fence_is_removed() -> None:
    assert parse('```\n{"a": 1}\n```')[0] == {"a": 1}


def test_an_unterminated_fence_is_removed() -> None:
    """Truncation cuts the closing fence off before it cuts the document."""
    assert parse('```json\n{"a": 1}')[0] == {"a": 1}


def test_prose_around_the_document_is_discarded() -> None:
    value, repairs = parse('Sure! Here is the JSON:\n{"a": 1}\nLet me know if you need changes.')
    assert value == {"a": 1}
    assert "discarded text around the JSON document" in repairs


def test_a_reasoning_block_is_removed() -> None:
    """The v1 Kimi failure class arrived on this channel."""
    value, repairs = parse('<think>Let me work this out.</think>\n{"a": 1}')
    assert value == {"a": 1}
    assert "removed a reasoning block" in repairs


@pytest.mark.parametrize("tag", ["think", "thinking", "reasoning", "THINKING"])
def test_reasoning_blocks_come_in_several_spellings(tag: str) -> None:
    assert parse(f'<{tag}>hmm</{tag}>{{"a": 1}}')[0] == {"a": 1}


def test_an_unclosed_reasoning_block_is_refused_even_though_it_contains_a_document() -> None:
    """A document found *inside* reasoning was never claimed as the answer — it is
    the model thinking aloud about what it might return. Mining it would report
    speculation as a result, and the message names the actual problem."""
    with pytest.raises(RepairFailed, match="never closed its reasoning block"):
        parse('<think>I will answer in a moment {"a": 1}')


def test_a_closed_block_followed_by_an_unclosed_one_is_still_refused() -> None:
    with pytest.raises(RepairFailed, match="never closed its reasoning block"):
        parse('<think>first</think>{"a": 1}<think>actually, wait')


def test_a_trailing_comma_is_removed() -> None:
    value, repairs = parse('{"a": 1, "b": 2,}')
    assert value == {"a": 1, "b": 2}
    assert "removed a trailing comma" in repairs


def test_a_trailing_comma_inside_a_string_is_left_alone() -> None:
    """A pattern run over the raw text would happily edit this."""
    assert parse('{"note": "first, ]"}')[0] == {"note": "first, ]"}


def test_braces_inside_strings_do_not_confuse_the_scan() -> None:
    """The documents describe code, so braces in string values are routine."""
    text = 'Here you go: {"snippet": "if (x) { y(); }", "ok": true} — done'
    assert parse(text)[0] == {"snippet": "if (x) { y(); }", "ok": True}


def test_the_widest_document_is_taken_not_the_first_balanced_one() -> None:
    """A scan stopping at the first balanced pair takes a nested object out of the
    middle of a valid one."""
    value, _ = parse('Result: {"outer": {"inner": 1}, "also": 2}')
    assert value == {"outer": {"inner": 1}, "also": 2}


def test_repairs_stack_in_order() -> None:
    value, repairs = parse('<think>ok</think>\n```json\n{"a": 1,}\n```')
    assert value == {"a": 1}
    assert repairs == (
        "removed a reasoning block",
        "removed a code fence",
        "removed a trailing comma",
    )


# --------------------------------------------------------------------------- #
# Truncation — off unless asked for
# --------------------------------------------------------------------------- #


def test_a_truncated_document_is_not_closed_unless_the_step_asked() -> None:
    with pytest.raises(RepairFailed):
        parse('{"steps": ["one", "two"')


def test_a_truncated_document_can_be_closed_on_request() -> None:
    value, repairs = parse('{"steps": ["one", "two"', close_truncated=True)
    assert value == {"steps": ["one", "two"]}
    assert "closed a truncated document" in repairs


def test_an_unterminated_string_drops_the_member_rather_than_closing_the_quote() -> None:
    """Closing at the quote would assert that the summary is "the run fai", which
    is a sentence the model did not write."""
    value, _ = parse('{"ok": true, "summary": "the run fai', close_truncated=True)
    assert value == {"ok": True}


def test_a_truncated_list_element_is_dropped_whole() -> None:
    value, _ = parse('{"steps": ["one", "tw', close_truncated=True)
    assert value == {"steps": ["one"]}


def test_a_dangling_key_is_dropped() -> None:
    value, _ = parse('{"a": 1, "b":', close_truncated=True)
    assert value == {"a": 1}


def test_deeply_nested_truncation_closes_every_level() -> None:
    value, _ = parse('{"a": {"b": [{"c": 1', close_truncated=True)
    assert value == {"a": {"b": [{"c": 1}]}}


def test_something_unsalvageable_still_fails() -> None:
    with pytest.raises(RepairFailed):
        parse("I am afraid I cannot help with that.", close_truncated=True)


# --------------------------------------------------------------------------- #
# The repairs this refuses to make
# --------------------------------------------------------------------------- #


def test_single_quotes_are_not_converted() -> None:
    """The transformation that fixes ``{'a': 1}`` corrupts ``{"note": "it's
    fine"}`` — silently, into a document that parses and means something else. A
    parse failure is recoverable by asking again; a wrong value that validated is
    not recoverable at all."""
    with pytest.raises(RepairFailed):
        parse("{'a': 1}")


def test_python_literals_are_not_accepted() -> None:
    """Nothing here evals, so ``None``/``True`` are not silently reinterpreted."""
    with pytest.raises(RepairFailed):
        parse('{"a": None}')


# --------------------------------------------------------------------------- #
# Failure messages
# --------------------------------------------------------------------------- #


def test_empty_text_says_so_plainly() -> None:
    with pytest.raises(RepairFailed, match="returned no text"):
        parse("   \n  ")


def test_the_failure_message_previews_only_a_little_of_the_reply() -> None:
    """Model output quotes the request, and the request is where a pasted
    credential turns up (threat model T11)."""
    with pytest.raises(RepairFailed) as caught:
        parse("no json here " + "x" * 5_000)
    assert len(str(caught.value)) < 500
    assert "…" in str(caught.value)


def test_an_escaped_quote_does_not_end_a_string() -> None:
    """The case a naive scanner gets wrong, and the documents here are full of
    escaped quotes because they quote code."""
    assert parse('{"snippet": "say \\"hello\\" loudly", "ok": true}')[0] == {
        "snippet": 'say "hello" loudly',
        "ok": True,
    }


def test_an_escaped_backslash_does_not_escape_the_next_quote() -> None:
    """``"a\\\\"`` ends the string; a scanner that treated the second backslash as
    an escape would run to the end of the document looking for a closing quote."""
    assert parse('{"path": "C:\\\\", "ok": true}')[0] == {"path": "C:\\", "ok": True}


def test_an_escaped_quote_survives_prose_stripping() -> None:
    text = 'Here: {"note": "it says \\"} \\" in the middle"} and that is all'
    assert parse(text)[0] == {"note": 'it says "} " in the middle'}


def test_a_trailing_comma_after_an_escaped_quote_is_still_found() -> None:
    assert parse('{"a": "x\\"y",}')[0] == {"a": 'x"y'}
