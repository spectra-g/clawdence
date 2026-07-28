"""The ``when:`` expression language — tokenizer, parser, evaluator.

Adopted from Lobster wholesale (ADR-0003): ``==`` ``!=`` ``<`` ``<=`` ``>``
``>=``, ``&&`` ``||`` ``!``, parens, string / number / boolean / null literals,
and dotted references into a prior stage's result. The spike found this grammar
well-judged and reimplementing it differently would buy nothing.

Three deliberate differences, each from something the spike hit:

**Parsed at load time.** Lobster raises ``Unsupported condition`` mid-run, after
the earlier stages have already called an LLM. Every condition in a file is
parsed before the first stage starts.

**``and`` / ``or`` / ``not`` are named in the error.** The spike lost time to
``when: $a.json.x == "A" and $a.json.y == "B"`` failing at run time, because the
grammar is C-style and the English keywords are not keywords at all. Writing
them produces an error that says which operator to use instead.

**Nonsense comparisons raise rather than evaluating false.** ``$plan.json.count
> "seven"`` is a mistake, and a guard that silently evaluates false when its
operands are nonsense skips work for reasons nobody can see in the trace.

Where it is deliberately *lenient*: a reference whose path is not present
resolves to ``MISSING``, which compares unequal to everything and is falsy.
Guards run against results whose shape varies with what an agent emitted, and
making every absent optional field an error would make conditions unwritable.
Interpolation takes the opposite line — see that module for why the asymmetry
is the right way round.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from pydantic import JsonValue

from clawdence.engine.errors import ConditionEvalError, ConditionSyntaxError
from clawdence.engine.refs import MISSING, REFERENCE_TOKEN, Reference, Resolved, Resolver
from clawdence.engine.refs import parse_reference as _parse_reference

type ComparisonOp = Literal["==", "!=", "<", "<=", ">", ">="]

#: Longest first, so ``<=`` is not read as ``<`` followed by a stray ``=``.
_OPERATORS: Final[tuple[str, ...]] = ("==", "!=", "<=", ">=", "&&", "||", "<", ">", "!")

_KEYWORDS: Final[dict[str, JsonValue]] = {"true": True, "false": False, "null": None}

#: The English spellings, and what to use instead. Kept as data because the
#: error message is the entire value of knowing about them.
_WRONG_OPERATORS: Final[dict[str, str]] = {"and": "&&", "or": "||", "not": "!"}


# --------------------------------------------------------------------------
# AST


@dataclass(frozen=True, slots=True)
class Const:
    value: JsonValue


@dataclass(frozen=True, slots=True)
class Ref:
    reference: Reference


@dataclass(frozen=True, slots=True)
class Not:
    operand: Node


@dataclass(frozen=True, slots=True)
class And:
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Or:
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Compare:
    op: ComparisonOp
    left: Node
    right: Node


type Node = Const | Ref | Not | And | Or | Compare


# --------------------------------------------------------------------------
# Tokenizer


@dataclass(frozen=True, slots=True)
class _Token:
    kind: Literal["op", "paren", "const", "ref"]
    text: str
    position: int
    value: JsonValue = None
    reference: Reference | None = None


def _tokenize(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    length = len(expression)

    while index < length:
        char = expression[index]

        if char.isspace():
            index += 1
            continue

        if char in "()":
            tokens.append(_Token(kind="paren", text=char, position=index))
            index += 1
            continue

        operator = next((op for op in _OPERATORS if expression.startswith(op, index)), None)
        if operator is not None:
            tokens.append(_Token(kind="op", text=operator, position=index))
            index += len(operator)
            continue

        if char == "=":
            raise ConditionSyntaxError(
                "'=' is assignment, not comparison; use '=='",
                expression=expression,
                position=index,
            )

        if char in "\"'":
            text, index = _read_string(expression, index)
            tokens.append(_Token(kind="const", text=text, position=index, value=text))
            continue

        if char.isdigit() or (
            char == "-" and index + 1 < length and expression[index + 1].isdigit()
        ):
            number, index = _read_number(expression, index)
            tokens.append(_Token(kind="const", text=str(number), position=index, value=number))
            continue

        if char == "$":
            match = REFERENCE_TOKEN.match(expression, index)
            assert match is not None  # noqa: S101 - the pattern matches "$" alone
            raw = match.group(0)
            try:
                reference = _parse_reference(raw)
            except ValueError as exc:
                raise ConditionSyntaxError(
                    str(exc), expression=expression, position=index
                ) from None
            tokens.append(_Token(kind="ref", text=raw, position=index, reference=reference))
            index = match.end()
            continue

        word, end = _read_word(expression, index)
        if word in _KEYWORDS:
            tokens.append(_Token(kind="const", text=word, position=index, value=_KEYWORDS[word]))
            index = end
            continue
        if word in _WRONG_OPERATORS:
            raise ConditionSyntaxError(
                f"{word!r} is not an operator here; use {_WRONG_OPERATORS[word]!r}",
                expression=expression,
                position=index,
            )
        if word:
            raise ConditionSyntaxError(
                f"{word!r} is not a value; reference a stage as '$stage.json.field', "
                "or quote it to mean the literal string",
                expression=expression,
                position=index,
            )
        raise ConditionSyntaxError(
            f"unexpected character {char!r}", expression=expression, position=index
        )

    return tokens


def _read_string(expression: str, start: int) -> tuple[str, int]:
    quote = expression[start]
    out: list[str] = []
    index = start + 1
    while index < len(expression):
        char = expression[index]
        if char == "\\":
            if index + 1 >= len(expression):
                break
            out.append(expression[index + 1])
            index += 2
            continue
        if char == quote:
            return "".join(out), index + 1
        out.append(char)
        index += 1
    raise ConditionSyntaxError(
        f"unterminated string literal (opened with {quote})",
        expression=expression,
        position=start,
    )


def _read_number(expression: str, start: int) -> tuple[int | float, int]:
    index = start + 1 if expression[start] == "-" else start
    seen_dot = False
    while index < len(expression):
        char = expression[index]
        if char.isdigit():
            index += 1
        elif (
            char == "."
            and not seen_dot
            and index + 1 < len(expression)
            and expression[index + 1].isdigit()
        ):
            seen_dot = True
            index += 1
        else:
            break
    text = expression[start:index]
    return (float(text) if seen_dot else int(text)), index


def _read_word(expression: str, start: int) -> tuple[str, int]:
    index = start
    while index < len(expression) and (expression[index].isalnum() or expression[index] == "_"):
        index += 1
    return expression[start:index], index


# --------------------------------------------------------------------------
# Parser
#
# Recursive descent, lowest precedence outermost:
#
#     or      := and ( '||' and )*
#     and     := unary ( '&&' unary )*
#     unary   := '!' unary | comparison
#     compare := primary ( op primary )?      -- no chaining, by design
#     primary := '(' or ')' | const | ref


class _Parser:
    __slots__ = ("_expression", "_index", "_tokens")

    def __init__(self, expression: str, tokens: list[_Token]) -> None:
        self._expression = expression
        self._tokens = tokens
        self._index = 0

    def parse(self) -> Node:
        if not self._tokens:
            raise ConditionSyntaxError(
                "empty condition; omit 'when' to mean 'always run'",
                expression=self._expression,
                position=0,
            )
        node = self._parse_or()
        if self._peek() is not None:
            token = self._tokens[self._index]
            raise ConditionSyntaxError(
                f"unexpected {token.text!r} after a complete expression",
                expression=self._expression,
                position=token.position,
            )
        return node

    def _peek(self) -> _Token | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _take(self) -> _Token:
        token = self._peek()
        if token is None:
            raise ConditionSyntaxError(
                "expression ends early", expression=self._expression, position=len(self._expression)
            )
        self._index += 1
        return token

    def _match(self, kind: str, text: str) -> bool:
        token = self._peek()
        if token is not None and token.kind == kind and token.text == text:
            self._index += 1
            return True
        return False

    def _parse_or(self) -> Node:
        node = self._parse_and()
        while self._match("op", "||"):
            node = Or(left=node, right=self._parse_and())
        return node

    def _parse_and(self) -> Node:
        node = self._parse_unary()
        while self._match("op", "&&"):
            node = And(left=node, right=self._parse_unary())
        return node

    def _parse_unary(self) -> Node:
        if self._match("op", "!"):
            return Not(operand=self._parse_unary())
        return self._parse_comparison()

    def _parse_comparison(self) -> Node:
        left = self._parse_primary()
        token = self._peek()
        if (
            token is None
            or token.kind != "op"
            or token.text
            not in {
                "==",
                "!=",
                "<",
                "<=",
                ">",
                ">=",
            }
        ):
            return left

        self._index += 1
        op: ComparisonOp = token.text  # type: ignore[assignment]
        right = self._parse_primary()

        following = self._peek()
        if (
            following is not None
            and following.kind == "op"
            and following.text
            in {
                "==",
                "!=",
                "<",
                "<=",
                ">",
                ">=",
            }
        ):
            raise ConditionSyntaxError(
                f"comparisons do not chain; write 'a {op} b && b {following.text} c'",
                expression=self._expression,
                position=following.position,
            )
        return Compare(op=op, left=left, right=right)

    def _parse_primary(self) -> Node:
        token = self._take()
        if token.kind == "paren" and token.text == "(":
            node = self._parse_or()
            if not self._match("paren", ")"):
                raise ConditionSyntaxError(
                    "unclosed '('", expression=self._expression, position=token.position
                )
            return node
        if token.kind == "const":
            return Const(value=token.value)
        if token.kind == "ref":
            assert token.reference is not None  # noqa: S101 - set with kind="ref"
            return Ref(reference=token.reference)
        raise ConditionSyntaxError(
            f"expected a value, got {token.text!r}",
            expression=self._expression,
            position=token.position,
        )


def parse(expression: str) -> Node:
    """Parse a condition, or raise ``ConditionSyntaxError``."""
    return _Parser(expression, _tokenize(expression)).parse()


def references(node: Node) -> tuple[Reference, ...]:
    """Every reference in an expression, in source order.

    The loader walks these to prove each one names a stage that is declared
    *earlier* in the file — the check that turns a typo'd stage id from a guard
    that silently never fires into a workflow that will not load.
    """
    found: list[Reference] = []
    _collect(node, found)
    return tuple(found)


def _collect(node: Node, into: list[Reference]) -> None:
    match node:
        case Ref(reference=reference):
            into.append(reference)
        case Not(operand=operand):
            _collect(operand, into)
        case And(left=left, right=right) | Or(left=left, right=right):
            _collect(left, into)
            _collect(right, into)
        case Compare(left=left, right=right):
            _collect(left, into)
            _collect(right, into)
        case Const():
            pass


# --------------------------------------------------------------------------
# Evaluation


def evaluate(node: Node, resolver: Resolver) -> bool:
    """Evaluate a parsed condition to a decision about whether to run."""
    return _truthy(_eval(node, resolver))


def _eval(node: Node, resolver: Resolver) -> Resolved:
    match node:
        case Const(value=value):
            return value
        case Ref(reference=reference):
            return resolver.resolve(reference)
        case Not(operand=operand):
            return not _truthy(_eval(operand, resolver))
        case And(left=left, right=right):
            # Short-circuits, so `$a.succeeded && $a.json.x == 1` is safe to
            # write when a failed `a` has no output at all.
            return _truthy(_eval(left, resolver)) and _truthy(_eval(right, resolver))
        case Or(left=left, right=right):
            return _truthy(_eval(left, resolver)) or _truthy(_eval(right, resolver))
        case Compare(op=op, left=left, right=right):
            return _compare(op, _eval(left, resolver), _eval(right, resolver))


def _truthy(value: Resolved) -> bool:
    """JSON truthiness: absent, null, false, 0, "", [] and {} are false."""
    if value is MISSING or value is None:
        return False
    return bool(value)


def _compare(op: ComparisonOp, left: Resolved, right: Resolved) -> bool:
    if op == "==":
        return _equal(left, right)
    if op == "!=":
        return not _equal(left, right)
    return _order(op, left, right)


def _equal(left: Resolved, right: Resolved) -> bool:
    """Deep equality, with booleans and numbers held apart.

    Python says ``True == 1``; JSON does not, and Lobster uses ``Object.is`` for
    scalars for the same reason. ``$gate.json.approved == 1`` should be false
    when the field is ``true``, or the condition is testing something other
    than what it reads as.
    """
    if left is MISSING or right is MISSING:
        return left is right
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return bool(left == right)


def _order(op: ComparisonOp, left: Resolved, right: Resolved) -> bool:
    numbers = _as_number(left), _as_number(right)
    if None not in numbers:
        first, second = numbers
        assert first is not None and second is not None  # noqa: S101 - checked above
        return _apply_order(op, first, second)

    if isinstance(left, str) and isinstance(right, str):
        return _apply_order(op, left, right)

    raise ConditionEvalError(
        f"cannot order {_describe(left)} against {_describe(right)} with {op!r}; "
        "ordering compares two numbers or two strings"
    )


def _apply_order(op: ComparisonOp, left: float | str, right: float | str) -> bool:
    match op:
        case "<":
            return left < right  # type: ignore[operator]
        case "<=":
            return left <= right  # type: ignore[operator]
        case ">":
            return left > right  # type: ignore[operator]
        case ">=":
            return left >= right  # type: ignore[operator]
        case _:  # pragma: no cover - _compare handles equality before this
            raise ConditionEvalError(f"{op!r} is not an ordering operator")


def _as_number(value: Resolved) -> float | None:
    """Numbers only. ``True`` is not 1 here, for the reason ``_equal`` gives."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _describe(value: Resolved) -> str:
    if value is MISSING:
        return "a value that is not present"
    return f"{type(value).__name__} {value!r}"
