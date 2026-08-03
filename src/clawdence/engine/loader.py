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
  **earlier** in the file, or the reserved ``request``;
* every ``${...}`` placeholder in an argv element, env value, ``cwd``, ``stdin``,
  agent task, runner plan or approval prompt does the same;
* nested scopes obey the same reference rules, composition variables do not
  shadow stages, and sub-workflow inputs match their declarations;
* the complete embedded sub-workflow call graph is acyclic;
* no stage is called ``request``, and nothing reads a facet of it but ``json``;
* no placeholder appears in ``command[0]``.

The earlier-only rule is stricter than "the stage exists" and deliberately so.
Stages run in order, so a reference forward is a reference to nothing — but as a
runtime lookup it would resolve to ``MISSING``, compare unequal, and leave a
guard that silently never fires. That is the failure mode this whole module is
written against: the workflow that runs, reports success, and quietly did less
than it was asked to.

Every check carries a **document path** — ``("stages", 3, "command", 1)`` — down
with it, so ``source`` can turn the failure into a line number. The path is
threaded rather than reconstructed: the loader already walks the file's shape,
and a second walk that guessed at paths would drift from the first the moment a
composition type was added (S3c, after S3b added four of them).

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
from clawdence.domain.workflow import (
    WORKFLOW_SCHEMA_VERSION,
    AgentStage,
    ApprovalStage,
    ForEachStage,
    ParallelStage,
    RepeatStage,
    RunnerStage,
    ScriptStage,
    SubWorkflowStage,
)
from clawdence.engine import conditions, interpolation, refs
from clawdence.engine.errors import (
    ConditionSyntaxError,
    InterpolationError,
    WorkflowLoadError,
)
from clawdence.engine.refs import Reference
from clawdence.engine.source import DocumentPath, SourceMap

#: Where the versioning policy is written down. Quoted in the error a workflow
#: from a different release gets, because "migrate the file" without saying what
#: changed between the versions is an instruction nobody can act on.
SCHEMA_POLICY_DOC = "docs/workflow-schema.md"


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
    source = SourceMap.from_text(text)
    _check_schema_version(document, origin, source)
    _check_root_ids(document, origin, source)
    workflow = _build(document, origin, source)
    validate_references(workflow, origin=origin, source=source)
    return workflow


def _parse_yaml(text: str, origin: str) -> dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowLoadError(
            _yaml_message(exc), origin=origin, hint=_yaml_hint(exc), line=_yaml_line(exc)
        ) from exc

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
        return f"column {mark.column + 1}: {problem}"
    return str(problem)  # pragma: no cover - every parser error we have seen carries a mark


def _yaml_line(exc: yaml.YAMLError) -> int | None:
    mark = getattr(exc, "problem_mark", None)
    return None if mark is None else int(mark.line) + 1


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


def _check_schema_version(document: dict[str, Any], origin: str, source: SourceMap) -> None:
    """Reject a version this build does not understand, with a hint.

    Ahead of the model because pydantic's ``ge=1`` would accept a version from a
    future release and then half-interpret it — reading the fields it recognises
    and ignoring the meaning it does not have.
    """
    declared = document.get("schema_version", WORKFLOW_SCHEMA_VERSION)
    if declared == WORKFLOW_SCHEMA_VERSION:
        return
    line = source.line(("schema_version",))
    if not isinstance(declared, int):
        raise WorkflowLoadError(
            f"schema_version must be an integer, not {type(declared).__name__}",
            origin=origin,
            line=line,
        )
    direction = "newer" if declared > WORKFLOW_SCHEMA_VERSION else "older"
    raise WorkflowLoadError(
        f"declares workflow schema_version {declared}, "
        f"which is {direction} than this build understands ({WORKFLOW_SCHEMA_VERSION})",
        origin=origin,
        line=line,
        hint=(
            f"upgrade clawdence to run this workflow; see {SCHEMA_POLICY_DOC}"
            if declared > WORKFLOW_SCHEMA_VERSION
            else (
                f"migrate the file to schema_version {WORKFLOW_SCHEMA_VERSION}; "
                f"{SCHEMA_POLICY_DOC} lists what changed"
            )
        ),
    )


def _check_root_ids(document: dict[str, Any], origin: str, source: SourceMap) -> None:
    """Duplicate ids in the top-level scope, reported with both lines.

    The domain model refuses these too, and has to: a ``Workflow`` built in
    Python has no file behind it. But a model validator's error carries no path,
    so it resolves to the first line of the document — which for "duplicate
    stage id" is the least useful line in the file. Nested scopes need no
    equivalent, because no model validator pre-empts ``_validate_sequence``
    there and that one has known both lines since it was written.
    """
    stages = document.get("stages")
    if not isinstance(stages, list):
        return
    seen: dict[str, int] = {}
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            continue
        stage_id = stage.get("id")
        if not isinstance(stage_id, str):
            continue
        if stage_id in seen:
            first = source.line(("stages", seen[stage_id]))
            raise WorkflowLoadError(
                f"declares duplicate stage id {stage_id!r}",
                origin=origin,
                stage_id=stage_id,
                line=source.line(("stages", index)),
                hint=(
                    f"first declared on line {first}; "
                    "duplicate ids make '$stage.json' references ambiguous"
                    if first is not None
                    else "duplicate ids make '$stage.json' references ambiguous"
                ),
            )
        seen[stage_id] = index


def _build(document: dict[str, Any], origin: str, source: SourceMap) -> Workflow:
    try:
        return Workflow.model_validate(document)
    except ValidationError as exc:
        raise WorkflowLoadError(
            "does not match the workflow schema:\n" + _format_validation(exc, source),
            origin=origin,
            line=_first_error_line(exc, source),
        ) from exc


def _format_validation(exc: ValidationError, source: SourceMap) -> str:
    """One line per problem, addressed by path and by line.

    pydantic's own rendering repeats the whole input for discriminated unions,
    which for a workflow means the entire file per error.
    """
    lines: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        line = source.line(_document_path(error["loc"]))
        where = f"line {line}: " if line is not None else ""
        lines.append(f"  {where}{location}: {error['msg']}")
    return "\n".join(lines)


def _first_error_line(exc: ValidationError, source: SourceMap) -> int | None:
    for error in exc.errors():
        line = source.line(_document_path(error["loc"]))
        if line is not None:
            return line
    return None  # pragma: no cover - a composed document always answers at the root


def _document_path(location: tuple[int | str, ...]) -> DocumentPath:
    """A pydantic error location as a path into the document.

    A tagged union puts the tag in the middle of the path
    (``stages.0.script.command``); ``SourceMap`` resolves by longest known
    prefix, so the tag simply stops the walk one segment early rather than
    needing to be stripped by name.
    """
    return tuple(location)


def validate_references(
    workflow: Workflow, *, origin: str = "<workflow>", source: SourceMap | None = None
) -> None:
    """Validate data flow and the complete embedded workflow call graph.

    ``source`` is optional because a ``Workflow`` built in Python has no file
    behind it — the executor validates one of those on every run — and an
    absent map simply means errors carry no line.
    """
    positions = source if source is not None else SourceMap(lines={})
    _validate_sequence(
        workflow.stages,
        available={refs.REQUEST},
        variables={refs.REQUEST},
        workflow=workflow,
        origin=origin,
        location="root",
        path=("stages",),
        source=positions,
    )
    for name, definition in workflow.sub_workflows.items():
        if refs.REQUEST in definition.inputs:
            raise WorkflowLoadError(
                f"sub-workflow {name!r} declares reserved input {refs.REQUEST!r}",
                origin=origin,
                line=positions.line(("sub_workflows", name, "inputs")),
                hint="the work item is already available as '$request.json'",
            )
        variables = {refs.REQUEST, *definition.inputs}
        _validate_sequence(
            definition.stages,
            available=set(variables),
            variables=variables,
            workflow=workflow,
            origin=origin,
            location=f"sub_workflows.{name}",
            path=("sub_workflows", name, "stages"),
            source=positions,
        )
    _validate_call_graph(workflow, origin=origin, source=positions)


def _validate_sequence(
    stages: tuple[Stage, ...],
    *,
    available: set[str],
    variables: set[str],
    workflow: Workflow,
    origin: str,
    location: str,
    path: DocumentPath,
    source: SourceMap,
) -> set[str]:
    """Validate one ordered scope and return everything it declares."""
    ids = [stage.id for stage in stages]
    if len(ids) != len(set(ids)):
        duplicate = next(stage_id for stage_id in ids if ids.count(stage_id) > 1)
        second = len(ids) - 1 - ids[::-1].index(duplicate)
        raise WorkflowLoadError(
            f"{location} declares duplicate stage id {duplicate!r}",
            origin=origin,
            line=source.line((*path, second)),
        )

    declared = set(available)
    for index, stage in enumerate(stages):
        here: DocumentPath = (*path, index)
        if stage.id in variables:
            if stage.id == refs.REQUEST:
                raise WorkflowLoadError(
                    f"declares a stage called {refs.REQUEST!r}, which is the name of the work "
                    "item this run is for",
                    origin=origin,
                    stage_id=stage.id,
                    line=source.line((*here, "id")),
                    hint=(
                        f"every '${{{refs.REQUEST}.json...}}' below it would silently read the "
                        "stage's output instead of the request; rename the stage"
                    ),
                )
            raise WorkflowLoadError(
                f"declares a stage called {stage.id!r}, which is a value provided to this scope",
                origin=origin,
                stage_id=stage.id,
                line=source.line((*here, "id")),
                hint="rename the stage so references cannot silently change meaning",
            )

        for reference, where, field in _references(stage, origin, here, source):
            _check_reference(
                reference,
                where=where,
                stage=stage,
                declared=declared,
                variables=variables,
                all_ids=set(ids),
                origin=origin,
                line=source.line((*here, *field)),
            )

        if isinstance(stage, ForEachStage):
            collisions = {stage.item_var, stage.index_var} & declared
            if collisions:
                raise WorkflowLoadError(
                    "fan-out variables shadow values already available here: "
                    + ", ".join(sorted(collisions)),
                    origin=origin,
                    stage_id=stage.id,
                    line=source.line(here),
                )
            nested_variables = variables | {stage.item_var, stage.index_var}
            if stage.serial_key is not None:
                for reference in _template_references(
                    stage.serial_key, stage.id, "serial_key", origin, source.line((*here, "id"))
                ):
                    _check_reference(
                        reference,
                        where=f"serial_key placeholder '${{{reference.text}}}'",
                        stage=stage,
                        declared=declared | {stage.item_var, stage.index_var},
                        variables=nested_variables,
                        all_ids=set(ids),
                        origin=origin,
                        line=source.line((*here, "serial_key")),
                    )
            _validate_sequence(
                stage.stages,
                available=declared | {stage.item_var, stage.index_var},
                variables=nested_variables,
                workflow=workflow,
                origin=origin,
                location=f"{location}.{stage.id}.stages",
                path=(*here, "stages"),
                source=source,
            )
        elif isinstance(stage, ParallelStage):
            for branch_index, branch in enumerate(stage.branches):
                _validate_sequence(
                    branch.stages,
                    available=set(declared),
                    variables=set(variables),
                    workflow=workflow,
                    origin=origin,
                    location=f"{location}.{stage.id}.branches.{branch.id}",
                    path=(*here, "branches", branch_index, "stages"),
                    source=source,
                )
        elif isinstance(stage, RepeatStage):
            collisions = {"iteration", "previous"} & declared
            if collisions:
                raise WorkflowLoadError(
                    "loop variables shadow values already available here: "
                    + ", ".join(sorted(collisions)),
                    origin=origin,
                    stage_id=stage.id,
                    line=source.line(here),
                )
            nested_variables = variables | {"iteration", "previous"}
            body_ids = _validate_sequence(
                stage.stages,
                available=declared | {"iteration", "previous"},
                variables=nested_variables,
                workflow=workflow,
                origin=origin,
                location=f"{location}.{stage.id}.stages",
                path=(*here, "stages"),
                source=source,
            )
            for reference in _condition_references(
                stage.until, stage.id, "until", origin, source.line((*here, "until"))
            ):
                _check_reference(
                    reference,
                    where=f"'until' condition {reference.text!r}",
                    stage=stage,
                    declared=body_ids,
                    variables=nested_variables,
                    all_ids={child.id for child in stage.stages},
                    origin=origin,
                    line=source.line((*here, "until")),
                    allow_self=True,
                )
        elif isinstance(stage, SubWorkflowStage):
            definition = workflow.sub_workflows.get(stage.workflow)
            if definition is None:
                raise WorkflowLoadError(
                    f"calls sub-workflow {stage.workflow!r}, which is not defined",
                    origin=origin,
                    stage_id=stage.id,
                    line=source.line((*here, "workflow")),
                    hint=(
                        "available sub-workflows: "
                        + (", ".join(sorted(workflow.sub_workflows)) or "none")
                    ),
                )
            expected, supplied = set(definition.inputs), set(stage.inputs)
            if expected != supplied:
                missing = sorted(expected - supplied)
                extra = sorted(supplied - expected)
                details = []
                if missing:
                    details.append(f"missing {', '.join(missing)}")
                if extra:
                    details.append(f"unknown {', '.join(extra)}")
                raise WorkflowLoadError(
                    f"inputs for sub-workflow {stage.workflow!r} do not match: "
                    + "; ".join(details),
                    origin=origin,
                    stage_id=stage.id,
                    line=source.line((*here, "inputs")),
                )
        declared.add(stage.id)
    return declared


def _check_reference(
    reference: Reference,
    *,
    where: str,
    stage: Stage,
    declared: set[str],
    variables: set[str],
    all_ids: set[str],
    origin: str,
    line: int | None = None,
    allow_self: bool = False,
) -> None:
    if reference.stage_id == stage.id and not allow_self:
        raise WorkflowLoadError(
            f"{where} refers to {reference.stage_id!r}, which is the stage itself",
            origin=origin,
            stage_id=stage.id,
            line=line,
        )
    if reference.stage_id in variables and reference.facet is not refs.Facet.JSON:
        kind = "work item" if reference.stage_id == refs.REQUEST else "scope variable"
        if reference.stage_id == refs.REQUEST:
            raise WorkflowLoadError(
                f"{where} reads {reference.facet.value!r} of {refs.REQUEST!r}, which is "
                "a work item rather than a step and so never ran, succeeded or failed",
                origin=origin,
                stage_id=stage.id,
                line=line,
                hint=f"'${{{refs.REQUEST}.json.text}}' is the request itself",
            )
        raise WorkflowLoadError(
            f"{where} reads {reference.facet.value!r} of {reference.stage_id!r}, which is a "
            f"{kind} rather than a step",
            origin=origin,
            stage_id=stage.id,
            line=line,
            hint=f"use '${{{reference.stage_id}.json}}' or a path beneath it",
        )
    if reference.stage_id not in declared:
        placement = (
            "which is declared later — stages run in order, so it has no result yet"
            if reference.stage_id in all_ids
            else "which no stage declares (and no scope variable provides)"
        )
        raise WorkflowLoadError(
            f"{where} refers to stage {reference.stage_id!r}, {placement}",
            origin=origin,
            stage_id=stage.id,
            line=line,
            hint=f"values available here: {', '.join(sorted(declared)) or 'none'}",
        )


def _validate_call_graph(workflow: Workflow, *, origin: str, source: SourceMap) -> None:
    graph: dict[str, set[str]] = {"<root>": _called_workflows(workflow.stages)}
    graph.update(
        {
            name: _called_workflows(definition.stages)
            for name, definition in workflow.sub_workflows.items()
        }
    )
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            cycle = [*visiting[visiting.index(name) :], name]
            raise WorkflowLoadError(
                "contains a cyclic sub-workflow call: " + " -> ".join(cycle),
                origin=origin,
                line=source.line(("sub_workflows", name)),
            )
        if name in visited:
            return
        visiting.append(name)
        for called in graph.get(name, set()):
            visit(called)
        visiting.pop()
        visited.add(name)

    visit("<root>")
    for name in graph:
        visit(name)


def _called_workflows(stages: tuple[Stage, ...]) -> set[str]:
    called: set[str] = set()
    for stage in stages:
        if isinstance(stage, SubWorkflowStage):
            called.add(stage.workflow)
        elif isinstance(stage, ForEachStage | RepeatStage):
            called.update(_called_workflows(stage.stages))
        elif isinstance(stage, ParallelStage):
            for branch in stage.branches:
                called.update(_called_workflows(branch.stages))
    return called


def _references(
    stage: Stage, origin: str, here: DocumentPath, source: SourceMap
) -> Iterator[tuple[Reference, str, DocumentPath]]:
    """Every reference a stage makes, with where it was written — twice.

    ``where`` is the phrase a person reads; the path is what turns into a line
    number. Both, because "the 'when' condition" and ``("when",)`` answer
    different halves of "which one did you mean".
    """
    if stage.when is not None:
        for reference in _condition_references(
            stage.when, stage.id, "when", origin, source.line((*here, "when"))
        ):
            yield reference, f"'when' condition {reference.text!r}", ("when",)

    if isinstance(stage, ForEachStage):
        yield (
            _exact_reference(stage.items, stage.id, "items", origin, source.line((*here, "items"))),
            "'items' reference",
            ("items",),
        )

    if isinstance(stage, SubWorkflowStage):
        for name, value in stage.inputs.items():
            if isinstance(value, str) and value.startswith("$"):
                yield (
                    _exact_reference(
                        value,
                        stage.id,
                        f"inputs[{name}]",
                        origin,
                        source.line((*here, "inputs", name)),
                    ),
                    f"inputs[{name}] reference",
                    ("inputs", name),
                )

    for template, where, field in _templates(stage):
        try:
            found = interpolation.references(template)
        except InterpolationError as exc:
            raise WorkflowLoadError(
                f"has an invalid placeholder in {where}: {exc}",
                origin=origin,
                stage_id=stage.id,
                line=source.line((*here, *field)),
            ) from None
        for reference in found:
            yield reference, f"{where} placeholder '${{{reference.text}}}'", field

    if isinstance(stage, ScriptStage) and interpolation.contains_placeholder(stage.command[0]):
        raise WorkflowLoadError(
            f"interpolates command[0] ({stage.command[0]!r})",
            origin=origin,
            stage_id=stage.id,
            line=source.line((*here, "command", 0)),
            hint=(
                "which executable runs is the workflow author's decision; "
                "a value from an earlier step must not choose it"
            ),
        )


def _templates(stage: Stage) -> Iterator[tuple[str, str, DocumentPath]]:
    """The fields a value may be interpolated into, and their names.

    Deliberately a closed list. Anything reachable by an expansion is a place
    step output influences what runs, and that set should be reviewable in one
    screen rather than inferred from whichever fields happen to be strings.
    """
    if isinstance(stage, ScriptStage):
        for index, element in enumerate(stage.command[1:], start=1):
            yield element, f"command[{index}]", ("command", index)
        for name, value in stage.env.items():
            yield value, f"env[{name}]", ("env", name)
        if stage.cwd is not None:
            yield stage.cwd, "cwd", ("cwd",)
        if stage.stdin is not None:
            yield stage.stdin, "stdin", ("stdin",)
    elif isinstance(stage, AgentStage):
        yield stage.task, "task", ("task",)
    elif isinstance(stage, RunnerStage):
        if stage.plan is not None:
            yield stage.plan, "plan", ("plan",)
    elif isinstance(stage, ApprovalStage):
        yield stage.prompt, "prompt", ("prompt",)


def _template_references(
    template: str, stage_id: str, field: str, origin: str, line: int | None = None
) -> tuple[Reference, ...]:
    try:
        return interpolation.references(template)
    except InterpolationError as exc:
        raise WorkflowLoadError(
            f"has an invalid placeholder in {field}: {exc}",
            origin=origin,
            stage_id=stage_id,
            line=line,
        ) from None


def _condition_references(
    expression: str, stage_id: str, field: str, origin: str, line: int | None = None
) -> tuple[Reference, ...]:
    try:
        node = conditions.parse(expression)
    except ConditionSyntaxError as exc:
        raise WorkflowLoadError(
            f"has an invalid {field!r} condition: {exc.message}",
            origin=origin,
            stage_id=stage_id,
            line=line,
            hint=_caret(exc.expression, exc.position),
        ) from None
    return conditions.references(node)


def _exact_reference(
    value: str, stage_id: str, field: str, origin: str, line: int | None = None
) -> Reference:
    try:
        reference = refs.parse_reference(value)
    except ValueError as exc:
        raise WorkflowLoadError(
            f"has an invalid {field!r} reference: {exc}",
            origin=origin,
            stage_id=stage_id,
            line=line,
        ) from None
    if not value.startswith("$"):
        raise WorkflowLoadError(
            f"{field!r} must be an exact '$stage.json.path' reference",
            origin=origin,
            stage_id=stage_id,
            line=line,
        )
    return reference


def _caret(expression: str, position: int) -> str:
    """Point at the offending character rather than restating the expression."""
    return f"{expression}\n        {' ' * position}^"
