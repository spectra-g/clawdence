"""The condition grammar.

Organised around the decisions rather than around the functions: each class is
one thing ADR-0003 or the S0 spike settled, and the tests under it are what
would have to break for that decision to have been reversed by accident.
"""

from __future__ import annotations

import pytest

from clawdence.domain import StepStatus
from clawdence.engine import ConditionEvalError, ConditionSyntaxError
from clawdence.engine import conditions as c
from tests.engine.factories import resolver, resolver_for, step_result


def truth(expression: str, **outputs: object) -> bool:
    return c.evaluate(c.parse(expression), resolver_for(**outputs))  # type: ignore[arg-type]


class TestOperators:
    """The set adopted from Lobster, verbatim."""

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ('$a.json.v == "APPROVED"', True),
            ('$a.json.v != "APPROVED"', False),
            ("$a.json.n < 10", True),
            ("$a.json.n <= 3", True),
            ("$a.json.n > 10", False),
            ("$a.json.n >= 3", True),
            ('$a.json.v == "APPROVED" && $a.json.n == 3', True),
            ('$a.json.v == "REJECTED" || $a.json.n == 3', True),
            ('!($a.json.v == "REJECTED")', True),
            ('($a.json.v == "REJECTED" || $a.json.n == 3) && $a.json.ok', True),
        ],
    )
    def test_evaluates(self, expression: str, expected: bool) -> None:
        assert truth(expression, a={"v": "APPROVED", "n": 3, "ok": True}) is expected

    def test_and_binds_tighter_than_or(self) -> None:
        # false || (true && false) is false; (false || true) && false is also
        # false, so pick operands where the two groupings disagree.
        assert truth("$a.json.t || $a.json.f && $a.json.f", a={"t": True, "f": False}) is True

    def test_comparisons_do_not_chain(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="do not chain"):
            c.parse("$a.json.n < $a.json.m < 10")


class TestSpikeGotchas:
    """The two things the S0 spike lost time to, made into errors."""

    @pytest.mark.parametrize(
        ("word", "replacement"),
        [("and", "&&"), ("or", "||"), ("not", "!")],
    )
    def test_english_operators_name_the_replacement(self, word: str, replacement: str) -> None:
        with pytest.raises(ConditionSyntaxError) as caught:
            c.parse(f'$a.json.x == "A" {word} $a.json.y == "B"')
        assert replacement in str(caught.value)

    def test_single_equals_is_named(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="use '=='"):
            c.parse('$a.json.x = "A"')


class TestTypeStrictness:
    """``True`` is not ``1``, because Lobster uses ``Object.is`` and JSON agrees."""

    def test_true_does_not_equal_one(self) -> None:
        assert truth("$a.json.flag == 1", a={"flag": True}) is False

    def test_one_does_not_equal_true(self) -> None:
        assert truth("$a.json.n == true", a={"n": 1}) is False

    def test_true_equals_true(self) -> None:
        assert truth("$a.json.flag == true", a={"flag": True}) is True

    def test_int_and_float_compare(self) -> None:
        assert truth("$a.json.n == 3", a={"n": 3.0}) is True

    def test_containers_compare_deeply(self) -> None:
        assert c.evaluate(
            c.parse("$a.json.x == $b.json.x"),
            resolver_for(a={"x": [1, {"k": "v"}]}, b={"x": [1, {"k": "v"}]}),
        )

    def test_ordering_across_types_raises(self) -> None:
        # Not `False`. A guard that quietly evaluates false when its operands
        # are nonsense skips work for a reason nobody can see in the trace.
        with pytest.raises(ConditionEvalError, match="cannot order"):
            truth('$a.json.n > "seven"', a={"n": 3})

    def test_booleans_are_not_ordered_as_numbers(self) -> None:
        with pytest.raises(ConditionEvalError):
            truth("$a.json.flag > 0", a={"flag": True})

    def test_strings_order_lexicographically(self) -> None:
        assert truth('$a.json.v < "b"', a={"v": "a"}) is True
        assert truth('$a.json.v >= "b"', a={"v": "a"}) is False

    def test_ordering_an_absent_value_says_so(self) -> None:
        with pytest.raises(ConditionEvalError, match="not present"):
            truth("$a.json.nope > 1", a={"x": 1})

    def test_the_error_names_the_offending_value(self) -> None:
        with pytest.raises(ConditionEvalError, match="list"):
            truth("$a.json.items > 1", a={"items": [1, 2]})


class TestMissingValues:
    """Absent resolves; it does not raise. Conditions are the lenient half."""

    def test_absent_field_is_falsy(self) -> None:
        assert truth("$a.json.nope", a={"x": 1}) is False

    def test_absent_field_compares_unequal_to_everything(self) -> None:
        assert truth('$a.json.nope == "x"', a={"x": 1}) is False
        assert truth("$a.json.nope == null", a={"x": 1}) is False

    def test_present_null_equals_null(self) -> None:
        # The distinction MISSING exists for: "the field is explicitly null" is
        # a different fact from "there is no such field".
        assert truth("$a.json.err == null", a={"err": None}) is True

    def test_unreached_stage_is_falsy(self) -> None:
        assert c.evaluate(c.parse("$later.succeeded"), resolver()) is False

    def test_the_sentinel_is_a_falsy_singleton(self) -> None:
        # Identity is how MISSING is compared, so there must be exactly one.
        from clawdence.engine.refs import MISSING, _MissingType

        assert _MissingType() is MISSING
        assert not MISSING

    def test_and_short_circuits_past_a_failed_stage(self) -> None:
        results = resolver(a=step_result("a", status=StepStatus.FAILED, output=None))
        assert c.evaluate(c.parse("$a.succeeded && $a.json.count > 0"), results) is False


class TestFacets:
    """A closed set. An unknown facet is a typo every time."""

    def test_status_reads_the_status(self) -> None:
        results = resolver(a=step_result("a", status=StepStatus.TIMED_OUT))
        assert c.evaluate(c.parse('$a.status == "timed_out"'), results) is True

    @pytest.mark.parametrize(
        ("status", "facet", "expected"),
        [
            (StepStatus.SUCCEEDED, "succeeded", True),
            (StepStatus.SUCCEEDED, "failed", False),
            (StepStatus.FAILED, "failed", True),
            (StepStatus.TIMED_OUT, "failed", True),
            (StepStatus.SKIPPED, "skipped", True),
            (StepStatus.SKIPPED, "failed", False),
        ],
    )
    def test_boolean_facets(self, status: StepStatus, facet: str, expected: bool) -> None:
        results = resolver(a=step_result("a", status=status))
        assert c.evaluate(c.parse(f"$a.{facet}"), results) is expected

    def test_response_is_separate_from_output(self) -> None:
        # The S2 distinction: what a step produced vs what a human submitted.
        results = resolver(gate=step_result("gate", output={"d": "auto"}, response={"d": "human"}))
        assert c.evaluate(c.parse('$gate.response.d == "human"'), results) is True
        assert c.evaluate(c.parse('$gate.json.d == "human"'), results) is False

    def test_unknown_facet_is_a_syntax_error(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="is not a facet"):
            c.parse("$a.jsonn.x == 1")

    def test_terminal_facet_rejects_a_path(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="has no fields"):
            c.parse('$a.status.value == "ok"')

    def test_facet_is_required(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="names no facet"):
            c.parse("$a == 1")


class TestPaths:
    def test_nested_objects(self) -> None:
        assert truth('$a.json.x.y.z == "deep"', a={"x": {"y": {"z": "deep"}}}) is True

    def test_list_indices(self) -> None:
        assert truth('$a.json.items.1 == "b"', a={"items": ["a", "b"]}) is True

    def test_index_past_the_end_is_missing(self) -> None:
        assert truth("$a.json.items.9", a={"items": ["a"]}) is False

    def test_descending_into_a_scalar_is_missing(self) -> None:
        assert truth("$a.json.x.y", a={"x": 1}) is False


class TestLiterals:
    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ('$a.json.s == "quoted"', True),
            ("$a.json.s == 'single'", False),
            ("$a.json.n == -2", False),
            ("$a.json.f == 1.5", True),
            ("$a.json.nothing == null", True),
        ],
    )
    def test_forms(self, expression: str, expected: bool) -> None:
        assert truth(expression, a={"s": "quoted", "n": 3, "f": 1.5, "nothing": None}) is expected

    def test_escapes_inside_strings(self) -> None:
        assert truth(r'$a.json.s == "say \"hi\""', a={"s": 'say "hi"'}) is True

    def test_unterminated_string(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="unterminated"):
            c.parse('$a.json.s == "open')

    def test_bare_word_is_not_a_value(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="is not a value"):
            c.parse("$a.json.s == APPROVED")


class TestStructure:
    def test_empty_condition_says_to_omit_it(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="omit 'when'"):
            c.parse("   ")

    def test_unclosed_paren(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="unclosed"):
            c.parse("($a.succeeded")

    def test_trailing_garbage(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="after a complete expression"):
            c.parse("$a.succeeded $b.succeeded")

    def test_an_operator_with_no_right_hand_side(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="ends early"):
            c.parse("$a.json.x ==")

    def test_an_operator_where_a_value_belongs(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="expected a value"):
            c.parse("&& $a.succeeded")

    def test_an_unexpected_character(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="unexpected character"):
            c.parse("$a.succeeded @ $b.succeeded")

    def test_a_trailing_backslash_does_not_hang(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="unterminated"):
            c.parse('$a.json.x == "oops\\')

    def test_a_bare_sigil_is_a_bad_reference(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="needs a stage id"):
            c.parse("$ == 1")

    def test_an_uppercase_stage_id_is_rejected(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="not a valid stage id"):
            c.parse("$Plan.succeeded")

    def test_a_bad_path_segment_is_rejected(self) -> None:
        with pytest.raises(ConditionSyntaxError, match="not a usable path segment"):
            c.parse("$a.json.1x == 1")

    def test_error_carries_the_offset(self) -> None:
        with pytest.raises(ConditionSyntaxError) as caught:
            c.parse("$a.succeeded and $b.succeeded")
        assert caught.value.position == len("$a.succeeded ")


class TestReferenceCollection:
    """What the loader walks to prove every reference names an earlier stage."""

    def test_collects_in_source_order(self) -> None:
        node = c.parse('$a.json.x == "1" && ($b.succeeded || !$c.skipped)')
        assert [ref.stage_id for ref in c.references(node)] == ["a", "b", "c"]

    def test_literals_contribute_nothing(self) -> None:
        assert c.references(c.parse("true")) == ()
