"""``${stage.facet.path}`` expansion into argv elements, env values and stdin.

Three properties, each load-bearing, each the answer to something specific:

**Never into shell text.** Lobster's ``${arg}`` is a raw string replace into the
command string, which is command injection by construction the moment S10b's
untrusted issue text is an argument (ADR-0003). ``ScriptStage.command`` is argv,
so expansion happens *inside one element*. There is no shell, so there is no
metacharacter to escape and no quoting rule to get wrong: an expanded value
containing ``; rm -rf /`` is one argument that happens to contain semicolons.

**Single pass.** The expansion of a placeholder is never rescanned. Values come
from agent output and repo content, both of which an attacker may control, and
a second pass would let ``${a}`` expanding to ``${b}`` reach ``b``. Expand once,
then stop.

**Unresolvable is an error.** A reference that names nothing raises instead of
becoming ``""``. This is the opposite of what conditions do with the same
reference, and the asymmetry is the right way round: a guard reads a field that
may legitimately be absent and should evaluate false, whereas a command line is
being *built* — and an argument that silently vanishes turns a wrong reference
into a command that runs, successfully, meaning something else.

Escaping: ``$${`` is a literal ``${``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

from clawdence.engine.errors import InterpolationError
from clawdence.engine.refs import MISSING, Reference, Resolved, Resolver
from clawdence.engine.refs import parse_reference as _parse_reference

_OPEN = "${"
_ESCAPED_OPEN = "$${"


def contains_placeholder(template: str) -> bool:
    """True if the string has an unescaped ``${``."""
    return next(_placeholders(template), None) is not None


def references(template: str) -> tuple[Reference, ...]:
    """Every reference in a template, in source order.

    Raises ``InterpolationError`` on a malformed placeholder. The loader calls
    this over every interpolatable field so that an unclosed brace or an
    unknown facet is a file that will not load, rather than a stage that fails
    after the ones before it have spent the budget.
    """
    return tuple(reference for _, _, reference in _placeholders(template))


def expand(template: str, resolver: Resolver, *, wrap: Callable[[str], str] | None = None) -> str:
    """Replace every placeholder with the string form of what it names.

    ``wrap``, when given, is applied to each substituted value alone — never to
    the surrounding template text. The template is the caller's own words; what
    a placeholder resolves to is whatever an earlier stage produced, and only
    the runner's plan builder has a reason to tell those apart (marking the
    latter as untrusted content without also marking the former, which is not
    an equivalent claim to make about a workflow author's own instructions).
    """
    out: list[str] = []
    cursor = 0
    for start, end, reference in _placeholders(template):
        out.append(template[cursor:start].replace(_ESCAPED_OPEN, _OPEN))
        value = _stringify(resolver.resolve(reference), reference)
        out.append(value if wrap is None else wrap(value))
        cursor = end
    out.append(template[cursor:].replace(_ESCAPED_OPEN, _OPEN))
    return "".join(out)


def _placeholders(template: str) -> Iterator[tuple[int, int, Reference]]:
    """Yield ``(start, end, reference)`` for each unescaped placeholder.

    A generator rather than a list because ``expand`` and ``references`` want
    the same scan, and running it twice is how the validator and the executor
    would come to disagree about what a template contains.
    """
    index = 0
    length = len(template)
    while index < length:
        start = template.find(_OPEN, index)
        if start == -1:
            return
        if start > 0 and template[start - 1] == "$":
            # Part of "$${", the escape. Step past the whole thing so the "{"
            # cannot start a placeholder on the next pass.
            index = start + len(_OPEN)
            continue

        end = template.find("}", start)
        if end == -1:
            raise InterpolationError(
                f"unclosed '${{' in {template!r}; write '$${{' if you meant the literal characters"
            )

        body = template[start + len(_OPEN) : end]
        try:
            reference = _parse_reference(body, sigil="")
        except ValueError as exc:
            raise InterpolationError(f"in '${{{body}}}': {exc}") from None
        yield start, end + 1, reference
        index = end + 1


def _stringify(value: Resolved, reference: Reference) -> str:
    """Render a resolved value as exactly one argument's worth of text.

    Containers become compact sorted JSON, so passing a whole object to a
    script is one placeholder rather than a per-field template — and sorted, so
    the same object produces the same argument every run, which is what makes a
    step's idempotency key mean anything.
    """
    if value is MISSING:
        raise InterpolationError(
            f"'${{{reference.text}}}' resolves to nothing. "
            "The stage may not have run, or may not emit that field."
        )
    if value is None:
        raise InterpolationError(
            f"'${{{reference.text}}}' is null, and there is no argument that means null. "
            "Guard the stage with a 'when' condition instead."
        )
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
