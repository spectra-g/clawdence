"""``${...}`` expansion.

The three properties in the module docstring are the three classes here. The
injection ones are worth reading as security tests rather than formatting
tests: they are what stands between S10b's untrusted issue text and a command
line.
"""

from __future__ import annotations

import pytest

from clawdence.engine import InterpolationError
from clawdence.engine import interpolation as interp
from tests.engine.factories import resolver, resolver_for, step_result


def expand(template: str, **outputs: object) -> str:
    return interp.expand(template, resolver_for(**outputs))  # type: ignore[arg-type]


class TestSubstitution:
    def test_whole_element(self) -> None:
        assert expand("${a.json.size}", a={"size": "M"}) == "M"

    def test_within_an_element(self) -> None:
        assert expand("size=${a.json.size}!", a={"size": "M"}) == "size=M!"

    def test_several_placeholders(self) -> None:
        assert expand("${a.json.x}-${a.json.y}", a={"x": "1", "y": "2"}) == "1-2"

    def test_no_placeholder_is_returned_unchanged(self) -> None:
        assert expand("plain text", a={}) == "plain text"

    def test_response_facet(self) -> None:
        results = resolver(gate=step_result("gate", response={"feedback": "try again"}))
        assert interp.expand("${gate.response.feedback}", results) == "try again"

    def test_status_facet(self) -> None:
        results = resolver(a=step_result("a"))
        assert interp.expand("${a.status}", results) == "succeeded"


class TestStringification:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("text", "text"),
            (True, "true"),
            (False, "false"),
            (3, "3"),
            (1.5, "1.5"),
        ],
    )
    def test_scalars(self, value: object, expected: str) -> None:
        assert expand("${a.json.v}", a={"v": value}) == expected

    def test_containers_become_sorted_json(self) -> None:
        # Sorted so the same object yields the same argument every run, which
        # is what makes a step's idempotency key mean anything.
        assert expand("${a.json.v}", a={"v": {"b": 2, "a": 1}}) == '{"a":1,"b":2}'

    def test_lists_keep_their_order(self) -> None:
        assert expand("${a.json.v}", a={"v": [3, 1, 2]}) == "[3,1,2]"

    def test_non_ascii_is_not_escaped(self) -> None:
        assert expand("${a.json.v}", a={"v": ["café"]}) == '["café"]'


class TestUnresolvable:
    """The strict half of the asymmetry with conditions."""

    def test_missing_field_raises(self) -> None:
        with pytest.raises(InterpolationError, match="resolves to nothing"):
            expand("${a.json.nope}", a={"x": 1})

    def test_unreached_stage_raises(self) -> None:
        with pytest.raises(InterpolationError, match="resolves to nothing"):
            interp.expand("${later.json.x}", resolver())

    def test_null_raises_rather_than_becoming_the_word_null(self) -> None:
        with pytest.raises(InterpolationError, match="is null"):
            expand("${a.json.v}", a={"v": None})

    def test_unclosed_brace_suggests_the_escape(self) -> None:
        with pytest.raises(InterpolationError, match=r"write '\$\$\{'"):
            expand("${a.json.x", a={"x": 1})

    def test_unknown_facet(self) -> None:
        with pytest.raises(InterpolationError, match="is not a facet"):
            expand("${a.nope.x}", a={"x": 1})


class TestInjectionResistance:
    """What keeps untrusted step output from becoming syntax."""

    def test_expansion_is_not_rescanned(self) -> None:
        # An agent that emits "${secret.json.token}" gets that text back, not
        # the value it names. A second pass would make agent output a way to
        # address any stage in the run.
        template = "${a.json.text}"
        assert expand(template, a={"text": "${b.json.token}"}) == "${b.json.token}"

    def test_shell_metacharacters_survive_as_text(self) -> None:
        # There is no shell in the path, so this is one argument that happens
        # to contain semicolons. The assertion is that nothing here tries to
        # quote, escape, or strip it.
        hostile = "; rm -rf / #$(whoami)`id`"
        assert expand("${a.json.text}", a={"text": hostile}) == hostile

    def test_newlines_survive(self) -> None:
        assert expand("${a.json.text}", a={"text": "one\ntwo"}) == "one\ntwo"


class TestWrapping:
    """``wrap`` marks a substituted value alone — never the surrounding
    template, which is the caller's own words and not something this function
    should treat as data. ``runners.handler`` is the one caller that passes
    this, to fence a plan's untrusted spans without also fencing the workflow
    author's own instructions around them."""

    def test_wrap_applies_to_the_substituted_value_only(self) -> None:
        wrapped = interp.expand(
            "before ${a.json.text} after",
            resolver_for(a={"text": "middle"}),
            wrap=lambda value: f"<<{value}>>",
        )
        assert wrapped == "before <<middle>> after"

    def test_wrap_is_not_applied_when_there_is_no_placeholder(self) -> None:
        wrapped = interp.expand("plain text", resolver_for(), wrap=lambda value: f"<<{value}>>")
        assert wrapped == "plain text"

    def test_each_placeholder_is_wrapped_independently(self) -> None:
        wrapped = interp.expand(
            "${a.json.x}-${a.json.y}",
            resolver_for(a={"x": "1", "y": "2"}),
            wrap=lambda value: f"[{value}]",
        )
        assert wrapped == "[1]-[2]"


class TestEscaping:
    def test_double_dollar_is_a_literal_brace(self) -> None:
        assert expand("$${a.json.x}", a={"x": 1}) == "${a.json.x}"

    def test_escape_beside_a_real_placeholder(self) -> None:
        assert expand("$${literal} ${a.json.x}", a={"x": 1}) == "${literal} 1"


class TestInspection:
    """What the loader calls to validate a file without running it."""

    def test_references_in_source_order(self) -> None:
        found = interp.references("${a.json.x}/${b.response.y}")
        assert [(ref.stage_id, ref.facet.value) for ref in found] == [
            ("a", "json"),
            ("b", "response"),
        ]

    def test_contains_placeholder(self) -> None:
        assert interp.contains_placeholder("${a.json.x}") is True
        assert interp.contains_placeholder("plain") is False
        assert interp.contains_placeholder("$${a.json.x}") is False
