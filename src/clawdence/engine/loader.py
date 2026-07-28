"""YAML in, validated ``Workflow`` out — and everything knowable checked here.

The rule this module exists to enforce: **a workflow that will fail should fail
before it costs anything.** Lobster reports an unparseable condition when
execution reaches it, by which time the stages ahead have called an LLM
(ADR-0003). Every check that needs only the file is done here, in one pass,
before the executor sees it:

* the YAML parses, and the two ways it usually does not are named specifically;
* the declared ``schema_version`` is one this build understands;
* the domain model accepts it — types, ranges, ``extra="forbid"``, unique ids;
* every ``$stage.facet`` reference in a condition names a stage declared
  **earlier** in the file;
* every ``${...}`` placeholder in an argv element, env value, ``cwd``, ``stdin``
  or approval prompt does the same;
* no placeholder appears in ``command[0]``.

The earlier-only rule is stricter than "the stage exists" and deliberately so.
Stages run in order, so a reference forward is a reference to nothing — but as a
runtime lookup it would resolve to ``MISSING``, compare unequal, and leave a
guard that silently never fires. That is the failure mode this whole module is
written against: the workflow that runs, reports success, and quietly did less
than it was asked to.

``yaml.safe_load`` only. The full loader constructs arbitrary Python objects,
which for a file format this system executes would make a workflow file a
remote code execution vector by design.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from clawdence.domain import Stage, Workflow
from clawdence.domain.workflow import WORKFLOW_SCHEMA_VERSION, ApprovalStage, ScriptStage
from clawdence.engine import conditions, interpolation
from clawdence.engine.errors import (
    ConditionSyntaxError,
    InterpolationError,
    WorkflowLoadError,
)
from clawdence.engine.refs import Reference


def load_workflow(path: Path) -> Workflow:
    """Read and validate a workflow file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowLoadError(f"cannot be read: {exc.strerror or exc}", origin=str(path)) from exc
    return parse_workflow(text, origin=str(path))


def parse_workflow(text: str, *, origin: str = "<workflow>") -> Workflow:
    """Validate a workflow document that is already in hand."""
    document = _parse_yaml(text, origin)
    _check_schema_version(document, origin)
    workflow = _build(document, origin)
    validate_references(workflow, origin=origin)
    return workflow


def _parse_yaml(text: str, origin: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowLoadError(_yaml_message(exc), origin=origin, hint=_yaml_hint(exc)) from exc

    if document is None:
        raise WorkflowLoadError("is empty", origin=origin)
    if not isinstance(document, dict):
        raise WorkflowLoadError(
            f"must be a mapping at the top level, not {type(document).__name__}", origin=origin
        )
    return document


def _yaml_message(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or "is not valid YAML"
    if mark is not None:
        return f"line {mark.line + 1}, column {mark.column + 1}: {problem}"
    return str(problem)  # pragma: no cover - every parser error we have seen carries a mark


def _yaml_hint(exc: yaml.YAMLError) -> str | None:
    """Name the two mistakes that actually happen, rather than all of them.

    The unquoted ``!`` is the one the S0 spike lost time to: ``when: !$a.failed``
    is parsed by YAML as a *tag*, not as negation, and Lobster's own reaction to
    it was a warning and silently wrong behaviour.
    """
    text = str(exc)
    if "could not determine a constructor" in text or "found unknown tag" in text:
        return (
            "a value starting with '!' is a YAML tag, not negation — "
            'quote the whole condition: when: "!$stage.succeeded"'
        )
    if "found character '\\t'" in text:
        return "YAML does not allow tabs for indentation; use spaces"
    return None


def _check_schema_version(document: dict[str, Any], origin: str) -> None:
    """Reject a version this build does not understand, with a hint.

    Ahead of the model because pydantic's ``ge=1`` would accept a version from a
    future release and then half-interpret it — reading the fields it recognises
    and ignoring the meaning it does not have.
    """
    declared = document.get("schema_version", WORKFLOW_SCHEMA_VERSION)
    if declared == WORKFLOW_SCHEMA_VERSION:
        return
    if not isinstance(declared, int):
        raise WorkflowLoadError(
            f"schema_version must be an integer, not {type(declared).__name__}", origin=origin
        )
    direction = "newer" if declared > WORKFLOW_SCHEMA_VERSION else "older"
    raise WorkflowLoadError(
        f"declares workflow schema_version {declared}, "
        f"which is {direction} than this build understands ({WORKFLOW_SCHEMA_VERSION})",
        origin=origin,
        hint=(
            "upgrade clawdence to run this workflow"
            if declared > WORKFLOW_SCHEMA_VERSION
            else f"migrate the file to schema_version {WORKFLOW_SCHEMA_VERSION}"
        ),
    )


def _build(document: dict[str, Any], origin: str) -> Workflow:
    try:
        return Workflow.model_validate(document)
    except ValidationError as exc:
        raise WorkflowLoadError(
            "does not match the workflow schema:\n" + _format_validation(exc), origin=origin
        ) from exc


def _format_validation(exc: ValidationError) -> str:
    """One line per problem, addressed by path.

    pydantic's own rendering repeats the whole input for discriminated unions,
    which for a workflow means the entire file per error.
    """
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


def validate_references(workflow: Workflow, *, origin: str = "<workflow>") -> None:
    """Prove every reference names an earlier stage. Raises, or returns nothing.

    Separate from ``parse_workflow`` because S3c's ``workflow validate`` runs it
    over workflows built any way at all, and because a caller constructing a
    ``Workflow`` in Python deserves the same check a YAML author gets.
    """
    declared: set[str] = set()
    for stage in workflow.stages:
        for reference, where in _references(stage, origin):
            if reference.stage_id == stage.id:
                raise WorkflowLoadError(
                    f"{where} refers to {reference.stage_id!r}, which is the stage itself",
                    origin=origin,
                    stage_id=stage.id,
                )
            if reference.stage_id not in declared:
                raise WorkflowLoadError(
                    f"{where} refers to stage {reference.stage_id!r}, "
                    + _placement(reference.stage_id, declared, workflow),
                    origin=origin,
                    stage_id=stage.id,
                    hint=(f"stages available here: {', '.join(sorted(declared)) or 'none'}"),
                )
        declared.add(stage.id)


def _placement(stage_id: str, declared: set[str], workflow: Workflow) -> str:
    later = any(stage.id == stage_id for stage in workflow.stages if stage.id not in declared)
    if later:
        return "which is declared later — stages run in order, so it has no result yet"
    return "which no stage declares"


def _references(stage: Stage, origin: str) -> Iterator[tuple[Reference, str]]:
    """Every reference a stage makes, paired with where it was written."""
    if stage.when is not None:
        try:
            node = conditions.parse(stage.when)
        except ConditionSyntaxError as exc:
            raise WorkflowLoadError(
                f"has an invalid 'when' condition: {exc.message}",
                origin=origin,
                stage_id=stage.id,
                hint=_caret(exc.expression, exc.position),
            ) from None
        for reference in conditions.references(node):
            yield reference, f"'when' condition {reference.text!r}"

    for template, where in _templates(stage):
        try:
            found = interpolation.references(template)
        except InterpolationError as exc:
            raise WorkflowLoadError(
                f"has an invalid placeholder in {where}: {exc}", origin=origin, stage_id=stage.id
            ) from None
        for reference in found:
            yield reference, f"{where} placeholder '${{{reference.text}}}'"

    if isinstance(stage, ScriptStage) and interpolation.contains_placeholder(stage.command[0]):
        raise WorkflowLoadError(
            f"interpolates command[0] ({stage.command[0]!r})",
            origin=origin,
            stage_id=stage.id,
            hint=(
                "which executable runs is the workflow author's decision; "
                "a value from an earlier step must not choose it"
            ),
        )


def _templates(stage: Stage) -> Iterator[tuple[str, str]]:
    """The fields a value may be interpolated into, and their names.

    Deliberately a closed list. Anything reachable by an expansion is a place
    step output influences what runs, and that set should be reviewable in one
    screen rather than inferred from whichever fields happen to be strings.
    """
    if isinstance(stage, ScriptStage):
        for index, element in enumerate(stage.command[1:], start=1):
            yield element, f"command[{index}]"
        for name, value in stage.env.items():
            yield value, f"env[{name}]"
        if stage.cwd is not None:
            yield stage.cwd, "cwd"
        if stage.stdin is not None:
            yield stage.stdin, "stdin"
    elif isinstance(stage, ApprovalStage):
        yield stage.prompt, "prompt"


def _caret(expression: str, position: int) -> str:
    """Point at the offending character rather than restating the expression."""
    return f"{expression}\n        {' ' * position}^"
