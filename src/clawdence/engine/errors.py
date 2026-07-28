"""What the engine raises, and where it raises it.

Two families, and the split is the point:

**Load-time** (``WorkflowLoadError``) — everything knowable from the file alone:
bad YAML, an unparseable condition, a reference to a stage that does not exist
or has not run yet, an interpolation placeholder in an argv[0]. Lobster reports
an unparseable condition at *run* time, which for a workflow whose earlier
stages call an LLM means the failure arrives after the run has already spent
money (ADR-0003). Everything that can be caught before the first stage starts
is caught before the first stage starts.

**Run-time** (``StepFailure`` and the two evaluation errors) — everything that
depends on what a step actually produced.

``WorkflowLoadError`` carries ``origin`` and ``stage_id`` so the message names
the file and the stage rather than leaving the author to grep for a quoted
fragment.
"""

from __future__ import annotations


class EngineError(Exception):
    """Base for everything this package raises."""


class WorkflowLoadError(EngineError):
    """A workflow file cannot be turned into a valid ``Workflow``.

    Raised before any stage runs, which is the whole reason the class exists.
    """

    def __init__(
        self,
        message: str,
        *,
        origin: str | None = None,
        stage_id: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.message = message
        self.origin = origin
        self.stage_id = stage_id
        self.hint = hint
        super().__init__(self._render())

    def _render(self) -> str:
        prefix = self.origin or "<workflow>"
        if self.stage_id is not None:
            prefix = f"{prefix}: stage {self.stage_id!r}"
        text = f"{prefix}: {self.message}"
        if self.hint:
            text = f"{text}\n  hint: {self.hint}"
        return text


class ConditionSyntaxError(EngineError):
    """A ``when:`` expression does not parse.

    Carries the offset into the expression so the loader can point at the
    character rather than restating the expression and leaving the author to
    find it.
    """

    def __init__(self, message: str, *, expression: str, position: int) -> None:
        self.message = message
        self.expression = expression
        self.position = position
        super().__init__(f"{message} (at offset {position} of {expression!r})")


class ConditionEvalError(EngineError):
    """A parsed condition cannot be evaluated against the results at hand.

    Comparing a string to a number is this, not ``False``. A guard that
    silently evaluates false when its operands are nonsense is a guard that
    skips work for reasons nobody can see in the trace.
    """


class InterpolationError(EngineError):
    """A ``${...}`` placeholder cannot be resolved to a string.

    Unresolvable is an error rather than an empty string. The expansion targets
    are argv elements, environment values and stdin; a placeholder that
    silently becomes ``""`` there turns a wrong reference into a command that
    runs with an argument missing.
    """


class StepFailure(EngineError):
    """A handler failed. Raised by handlers, caught by the executor.

    ``retryable`` decides whether the declared ``retry`` policy applies. A
    non-zero exit is worth retrying; a workflow that references a field the
    previous stage never emits will reference it identically on attempt two, so
    retrying it only spends the budget more slowly.
    """

    def __init__(self, kind: str, message: str, *, retryable: bool = False) -> None:
        self.kind = kind
        self.message = message
        self.retryable = retryable
        super().__init__(f"{kind}: {message}")
