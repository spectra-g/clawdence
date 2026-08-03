"""Run the workflow, execute none of it.

The engine is real here: the same ``execute``, the same guards, the same
fan-out, the same barriers. What is replaced is the four leaf handlers, so no
model is called, no repository is touched, no state is written. That is the only
way a dry run can be worth anything — a second implementation that walked the
stages itself would be testing the walker, not the workflow.

**The dry run takes the happy path, and it does that by inventing values.** Stub
handlers alone are not enough, and the reason is worth stating because it is not
obvious: a guard reading ``$plan.json.result.confidence`` against a stub's empty
output resolves to ``MISSING``, compares false, and skips the stage. Every
interesting stage in a real file is behind a guard like that, so a dry run
without invented values reports a run in which almost nothing happened — the
exact failure mode ``loader`` exists to prevent, reproduced in the tool meant to
catch it.

So before running, the file is read for every reference it makes, and each one
is given a value that satisfies the comparison it appears in:
``$assessment.json.result.size != "L"`` gets something that is not ``"L"``,
``... .confidence >= 0.5`` gets ``0.5``, a ``for_each`` over
``$decompose.json.parsed.items`` gets a list of two, shaped by whatever the loop
body reads out of ``item``. A reference nobody compares gets a visible
placeholder — ``<plan.json.result>`` — which is legible in the output as
something that was made up rather than produced.

Two consequences to keep in view, both stated in the report rather than hidden:

* **Only one path is walked.** The happy one. ``--output`` overrides a stage's
  invented result, which is how the other branch gets exercised.
* **Success here means the file hangs together, not that the work will work.**
  A dry run cannot tell you the model will answer usefully or the tests will
  pass. It tells you the stages connect, the guards can fire, the references
  resolve, and the fan-out has something to fan out over.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import JsonValue

from clawdence.domain import Stage, StepStatus, StepType, Workflow
from clawdence.domain.workflow import (
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
from clawdence.engine import report as trace
from clawdence.engine.conditions import And, Compare, ComparisonOp, Const, Node, Not, Or, Ref
from clawdence.engine.executor import RunReport, execute
from clawdence.engine.handlers import HandlerOutcome, HandlerRegistry, StepContext
from clawdence.engine.refs import MISSING, Facet, Reference, Resolved, Resolver

#: How many items an invented fan-out produces. Two, because one cannot show
#: that a concurrency cap or a serial key does anything, and more only makes the
#: output longer — a dry run is read, not measured.
FAN_OUT_SAMPLE: Final = 2

#: The run id a dry run records against. Fixed rather than generated: nothing is
#: persisted, and a stable id makes two dry runs of one file comparable.
DRY_RUN_ID: Final = "dry"


# --------------------------------------------------------------------------
# Inventing the values


class _Unset:
    """No value has been chosen for this path yet. Distinct from JSON null."""


_UNSET: Final = _Unset()


@dataclass(frozen=True, slots=True)
class _Forbidden:
    """What a path must *not* be, from a ``!=`` the author wrote."""

    value: JsonValue


#: What one reference tells us about the value behind it.
type _Hint = JsonValue | _Unset | _Forbidden


@dataclass(slots=True)
class _Shape:
    """A tree of the paths one facet of one stage is read at."""

    children: dict[str, _Shape] = field(default_factory=dict)
    value: JsonValue | _Unset = _UNSET
    forbidden: list[JsonValue] = field(default_factory=list)

    @property
    def untouched(self) -> bool:
        return not self.children and isinstance(self.value, _Unset) and not self.forbidden

    def at(self, path: Sequence[str]) -> _Shape:
        node = self
        for segment in path:
            node = node.children.setdefault(segment, _Shape())
        return node

    def assign(self, path: Sequence[str], value: _Hint) -> None:
        """First writer wins, and nobody overrules a ``!=``.

        Two guards routinely constrain one field in ways no single value
        satisfies — ``size != "L"`` on the stage that implements and
        ``size == "L"`` on the stage that splits, which is ``examples/toy.yaml``
        exactly. Something has to give, and what gives is the later guard: the
        earlier branch is the one the file is written to take, and a dry run
        that skipped it in favour of the fallback would be demonstrating the
        path nobody asked about. The report prints every chosen value, so the
        branch that did not fire is explained rather than mysterious.
        """
        node = self.at(path)
        if isinstance(value, _Forbidden):
            node.forbidden.append(value.value)
            return
        if isinstance(value, _Unset) or not isinstance(node.value, _Unset):
            return
        if any(value == banned for banned in node.forbidden):
            return
        node.value = value

    def materialise(self, prefix: str) -> JsonValue:
        # A list wins over the children beneath it: the only way a path acquires
        # one is a `for_each` reading it, and a fan-out handed an object fails
        # the stage — a failure invented by the dry run rather than found in the
        # file, which is the one thing this must never report.
        if isinstance(self.value, list):
            return self.value
        if self.children:
            return {
                name: child.materialise(f"{prefix}.{name}") for name, child in self.children.items()
            }
        if isinstance(self.value, _Unset):
            return f"<{prefix}>"
        return self.value


@dataclass(frozen=True, slots=True)
class Synthesis:
    """Everything the dry run made up, before anything ran."""

    outputs: Mapping[str, JsonValue]
    responses: Mapping[str, JsonValue]
    request: JsonValue

    def flattened(self) -> Iterator[tuple[str, JsonValue]]:
        """Leaf by leaf, addressed the way the workflow addresses it."""
        if self.request is not None:
            yield from _flatten("request.json", self.request)
        for stage_id, output in sorted(self.outputs.items()):
            yield from _flatten(f"{stage_id}.json", output)
        for stage_id, response in sorted(self.responses.items()):
            yield from _flatten(f"{stage_id}.response", response)


def _flatten(prefix: str, value: JsonValue) -> Iterator[tuple[str, JsonValue]]:
    if isinstance(value, dict) and value:
        for name, child in value.items():
            yield from _flatten(f"{prefix}.{name}", child)
    else:
        yield prefix, value


class _Collector:
    """Walks the file collecting what each reference needs to be worth."""

    def __init__(self) -> None:
        self.facets: dict[tuple[str, Facet], _Shape] = {}
        self.request = _Shape()
        #: Composition stages produce their result from the executor, not from a
        #: handler, so anything invented for one would be discarded silently.
        self.compositions: set[str] = set()

    # -- collection ------------------------------------------------------

    def scope(self, stages: Sequence[Stage], variables: Mapping[str, _Shape]) -> None:
        for stage in stages:
            self.stage(stage, variables)

    def stage(self, stage: Stage, variables: Mapping[str, _Shape]) -> None:
        if stage.when is not None:
            self.condition(conditions.parse(stage.when), variables)

        for template in _templates(stage):
            for reference in interpolation.references(template):
                self.reference(reference, variables, _UNSET)

        match stage:
            case ForEachStage():
                self.compositions.add(stage.id)
                item, index = _Shape(), _Shape()
                nested = {**variables, stage.item_var: item, stage.index_var: index}
                if stage.serial_key is not None:
                    for reference in interpolation.references(stage.serial_key):
                        self.reference(reference, nested, _UNSET)
                self.scope(stage.stages, nested)
                # After the body, because the body is what says what an item
                # looks like. Each element is materialised separately so two
                # branches never share one mutable object.
                self.reference(
                    refs.parse_reference(stage.items),
                    variables,
                    [item.materialise(f"{stage.item_var}.json") for _ in range(FAN_OUT_SAMPLE)],
                )
            case ParallelStage():
                self.compositions.add(stage.id)
                for branch in stage.branches:
                    self.scope(branch.stages, variables)
            case RepeatStage():
                self.compositions.add(stage.id)
                loop = {**variables, "iteration": _Shape(), "previous": _Shape()}
                self.scope(stage.stages, loop)
                # The loop's exit condition is satisfied on purpose: a dry run
                # that spun `max_iterations` times and then failed would report
                # the bound as broken when it is doing its job.
                self.condition(conditions.parse(stage.until), loop)
            case SubWorkflowStage():
                self.compositions.add(stage.id)
                for value in stage.inputs.values():
                    if isinstance(value, str) and value.startswith("$"):
                        self.reference(refs.parse_reference(value), variables, _UNSET)
            case _:
                return

    def condition(
        self, node: Node, variables: Mapping[str, _Shape], *, positive: bool = True
    ) -> None:
        """Give the references in a guard values that make it fire."""
        match node:
            case Const():
                return
            case Ref(reference):
                self.reference(reference, variables, positive)
            case Not(operand):
                self.condition(operand, variables, positive=not positive)
            case And(left, right) | Or(left, right):
                # Both sides of an `||` are satisfied, not one. Satisfying both
                # is still satisfying either, and choosing a side would make the
                # invented values depend on which one was written first.
                self.condition(left, variables, positive=positive)
                self.condition(right, variables, positive=positive)
            case Compare(op, left, right):
                self.comparison(op, left, right, variables, positive=positive)

    def comparison(
        self,
        op: ComparisonOp,
        left: Node,
        right: Node,
        variables: Mapping[str, _Shape],
        *,
        positive: bool,
    ) -> None:
        # `!($x == "L")` constrains `$x` exactly as `$x != "L"` does, so the
        # negation is pushed into the operator instead of being a second set of
        # rules that would have to agree with the first.
        effective = op if positive else _complement(op)
        match (left, right):
            case (Ref(reference), Const(literal)):
                self.reference(reference, variables, _satisfying(effective, literal))
            case (Const(literal), Ref(reference)):
                self.reference(reference, variables, _satisfying(_mirror(effective), literal))
            case _:
                for node in (left, right):
                    if isinstance(node, Ref):
                        self.reference(node.reference, variables, _UNSET)

    def reference(
        self,
        reference: Reference,
        variables: Mapping[str, _Shape],
        value: _Hint,
    ) -> None:
        """Record what one reference reads, and what would satisfy it."""
        if reference.facet not in refs.NAVIGABLE:
            # `succeeded`, `failed`, `skipped` and `status` come from what the
            # stub actually did. Inventing them would be inventing the run.
            return
        if reference.stage_id == refs.REQUEST:
            self.request.assign(reference.path, value)
            return
        if reference.stage_id in variables:
            # A loop item, an index, or a sub-workflow input: supplied by the
            # executor from something else this collector already shaped.
            variables[reference.stage_id].assign(reference.path, value)
            return
        key = (reference.stage_id, reference.facet)
        self.facets.setdefault(key, _Shape()).assign(reference.path, value)

    # -- result ----------------------------------------------------------

    def finish(self) -> Synthesis:
        outputs: dict[str, JsonValue] = {}
        responses: dict[str, JsonValue] = {}
        for (stage_id, facet), shape in self.facets.items():
            if stage_id in self.compositions:
                continue
            target = outputs if facet is Facet.JSON else responses
            target[stage_id] = shape.materialise(f"{stage_id}.{facet.value}")
        return Synthesis(
            outputs=outputs,
            responses=responses,
            # A workflow that never reads the request gets none, which is what
            # an ad-hoc `clawdence run` gives it. Inventing one there would put
            # a value in the report that no stage could ever have seen.
            request=None if self.request.untouched else self.request.materialise("request.json"),
        )


def synthesise(workflow: Workflow) -> Synthesis:
    """Invent one result per stage, from what the rest of the file reads."""
    collector = _Collector()
    collector.scope(workflow.stages, {})
    for definition in workflow.sub_workflows.values():
        collector.scope(definition.stages, {name: _Shape() for name in definition.inputs})
    return collector.finish()


def _satisfying(op: ComparisonOp, literal: JsonValue) -> _Hint:
    """A value that makes ``<something> op literal`` true, if one is obvious."""
    if op == "==":
        return literal
    if op == "!=":
        # Recorded as a prohibition rather than as a value: the placeholder
        # already satisfies this, and remembering the literal stops a later
        # `== literal` from quietly turning this guard false.
        return _Forbidden(literal)
    if isinstance(literal, bool) or not isinstance(literal, int | float):
        # Ordering against a string or a boolean: only the non-strict forms have
        # an answer that needs no ordering of our own.
        return literal if op in ("<=", ">=") else _UNSET
    match op:
        case ">":
            return literal + 1
        case ">=":
            return literal
        case "<":
            return literal - 1
        case "<=":
            return literal


#: The two rewrites a comparison needs: read from the other side, and negated.
_MIRRORED: Final[Mapping[str, ComparisonOp]] = {
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
    "==": "==",
    "!=": "!=",
}
_COMPLEMENT: Final[Mapping[str, ComparisonOp]] = {
    "<": ">=",
    "<=": ">",
    ">": "<=",
    ">=": "<",
    "==": "!=",
    "!=": "==",
}


def _mirror(op: ComparisonOp) -> ComparisonOp:
    """The same comparison read from the other side."""
    return _MIRRORED[op]


def _complement(op: ComparisonOp) -> ComparisonOp:
    """The comparison that is true exactly when this one is not."""
    return _COMPLEMENT[op]


def _templates(stage: Stage) -> Iterator[str]:
    match stage:
        case ScriptStage():
            yield from stage.command[1:]
            yield from stage.env.values()
            if stage.cwd is not None:
                yield stage.cwd
            if stage.stdin is not None:
                yield stage.stdin
        case AgentStage():
            yield stage.task
        case RunnerStage():
            if stage.plan is not None:
                yield stage.plan
        case ApprovalStage():
            yield stage.prompt
        case _:
            return


# --------------------------------------------------------------------------
# Running against the invented values


@dataclass(frozen=True, slots=True)
class PlannedStep:
    """What one stage would have done, with everything expanded."""

    stage_id: str
    definition_id: str
    attempt: int
    type: StepType
    detail: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Unresolved:
    """A placeholder that had nothing behind it even after synthesis."""

    stage_id: str
    field: str
    reference: str


class _LenientResolver(Resolver):
    """Resolves like the real thing, but never refuses.

    Interpolation treats an unresolvable placeholder as an error, and that is
    right at run time: an argv element that silently vanishes runs a command
    meaning something else. In a dry run it is the finding itself — the author
    wants to be told which reference is empty, not to have the run stop at the
    first one. So the leniency lives in the resolver rather than in a second
    expander: ``interpolation`` scans a template in exactly one place, and a
    copy of that scanner here is how this and the executor would come to
    disagree about what a template contains.
    """

    __slots__ = ("_found", "_inner")

    def __init__(self, inner: Resolver, found: list[str]) -> None:
        self._inner = inner
        self._found = found

    def resolve(self, ref: Reference) -> Resolved:
        value = self._inner.resolve(ref)
        if value is MISSING or value is None:
            self._found.append(ref.text)
            return f"<{ref.text}>"
        return value


@dataclass(slots=True)
class _DryRunHandler:
    """The one handler every step type gets. Runs nothing, records everything."""

    synthesis: Synthesis
    steps: list[PlannedStep] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)

    async def __call__(self, ctx: StepContext) -> HandlerOutcome:
        stage = ctx.stage
        self.steps.append(
            PlannedStep(
                stage_id=ctx.idempotency_stage_id,
                definition_id=stage.id,
                attempt=ctx.attempt,
                type=stage.type,
                detail=self._detail(stage, ctx.resolver),
            )
        )
        output = _merge(_base_output(stage), self.synthesis.outputs.get(stage.id))
        response = (
            self.synthesis.responses.get(stage.id, {}) if isinstance(stage, ApprovalStage) else None
        )
        return HandlerOutcome(output=output, response=response)

    def _detail(self, stage: Stage, resolver: Resolver) -> dict[str, JsonValue]:
        match stage:
            case ScriptStage():
                argv: list[JsonValue] = [
                    stage.command[0],
                    *(
                        self._expand(element, resolver, stage.id, f"command[{index}]")
                        for index, element in enumerate(stage.command[1:], start=1)
                    ),
                ]
                # Names only. A workflow's `env:` values expand from step output,
                # and a rehearsal that printed them would put whatever the last
                # stage produced into a terminal and a CI log.
                names: list[JsonValue] = [*sorted(stage.env)]
                return {
                    "argv": argv,
                    "cwd": self._maybe(stage.cwd, resolver, stage.id, "cwd"),
                    "stdin": self._maybe(stage.stdin, resolver, stage.id, "stdin"),
                    "env": names,
                }
            case AgentStage():
                return {
                    "role": stage.role,
                    "model": stage.model.model,
                    "max_turns": stage.max_turns,
                    "response_schema": stage.response_schema,
                    "task": self._expand(stage.task, resolver, stage.id, "task"),
                    "budget": _budget(stage.budget),
                }
            case RunnerStage():
                return {
                    "isolation_tier_override": stage.isolation_tier_override,
                    "plan": self._maybe(stage.plan, resolver, stage.id, "plan"),
                    "budget": _budget(stage.budget),
                }
            case ApprovalStage():
                return {
                    "prompt": self._expand(stage.prompt, resolver, stage.id, "prompt"),
                    "required_approver": stage.required_approver,
                    "require_different_approver": stage.require_different_approver,
                }
            case _:  # pragma: no cover - the registry only routes leaf types here
                return {}

    def _maybe(
        self, template: str | None, resolver: Resolver, stage_id: str, field_name: str
    ) -> str | None:
        """Expand an optional field, keeping "not set" distinct from "empty"."""
        return None if template is None else self._expand(template, resolver, stage_id, field_name)

    def _expand(self, template: str, resolver: Resolver, stage_id: str, field_name: str) -> str:
        found: list[str] = []
        expanded = interpolation.expand(template, _LenientResolver(resolver, found))
        self.unresolved.extend(
            Unresolved(stage_id=stage_id, field=field_name, reference=text) for text in found
        )
        return expanded


def _base_output(stage: Stage) -> JsonValue:
    """What this step type always emits, before anything invented is layered on.

    Only ``script`` has one: ``ScriptHandler`` promises the same five fields
    whatever the command did, so a workflow may read ``exit_code`` without any
    stage referring to it. The other handlers' shapes are theirs to change, and
    a copy of them here would be a second declaration that goes stale quietly.
    """
    if isinstance(stage, ScriptStage):
        return {"exit_code": 0, "stdout": "", "stderr": "", "parsed": None, "truncated": False}
    return {}


def _merge(base: JsonValue, invented: JsonValue | None) -> JsonValue:
    """Layer invented values over a base, deepest first. Invented wins."""
    if invented is None:
        return base
    if not isinstance(base, dict) or not isinstance(invented, dict):
        return invented
    merged = dict(base)
    for name, value in invented.items():
        merged[name] = _merge(base.get(name), value) if name in base else value
    return merged


def _budget(budget: object) -> JsonValue:
    if budget is None:
        return None
    dumped = getattr(budget, "model_dump", None)
    return (
        None if dumped is None else {k: v for k, v in dumped(mode="json").items() if v is not None}
    )


@dataclass(frozen=True, slots=True)
class DryRunReport:
    """The run that did not happen, and everything invented to get it there."""

    run: RunReport
    steps: tuple[PlannedStep, ...]
    unresolved: tuple[Unresolved, ...]
    synthesis: Synthesis

    @property
    def succeeded(self) -> bool:
        return self.run.succeeded


async def dry_run(
    workflow: Workflow,
    *,
    request: JsonValue | None = None,
    outputs: Mapping[str, JsonValue] | None = None,
    run_id: str = DRY_RUN_ID,
) -> DryRunReport:
    """Execute the workflow against invented results and report what it would do.

    ``request`` and ``outputs`` are the steering wheel: without them the happy
    path is walked, and with them any other path is. Both are layered over the
    invented values rather than replacing them, so overriding one field of one
    stage does not leave the rest of that stage empty.
    """
    synthesis = _override(synthesise(workflow), request=request, outputs=outputs)
    handler = _DryRunHandler(synthesis)
    registry = HandlerRegistry(
        {
            StepType.SCRIPT: handler,
            StepType.AGENT: handler,
            StepType.RUNNER: handler,
            StepType.APPROVAL: handler,
        }
    )
    report = await execute(
        workflow,
        run_id=run_id,
        work_item_id=f"wi.{run_id}",
        registry=registry,
        request=synthesis.request,
        sleep=_no_sleep,
    )
    return DryRunReport(
        run=report,
        steps=tuple(handler.steps),
        unresolved=tuple(handler.unresolved),
        synthesis=synthesis,
    )


def _override(
    synthesis: Synthesis,
    *,
    request: JsonValue | None,
    outputs: Mapping[str, JsonValue] | None,
) -> Synthesis:
    if request is None and not outputs:
        return synthesis
    merged = dict(synthesis.outputs)
    for stage_id, value in (outputs or {}).items():
        merged[stage_id] = _merge(merged.get(stage_id, {}), value)
    return Synthesis(
        outputs=merged,
        responses=synthesis.responses,
        request=synthesis.request if request is None else _merge(synthesis.request, request),
    )


async def _no_sleep(seconds: float) -> None:
    """Retry backoff is wall-clock the author already declared; do not spend it."""
    return None


# --------------------------------------------------------------------------
# Saying what would have happened


#: Long text is one line in the trace. The whole of it is in ``--json``, which
#: is where a reader who wants the entire prompt goes.
_EXCERPT: Final = 78


def render_dry_run(report: DryRunReport) -> str:
    """The trace, the invented values, and anything still empty."""
    workflow = report.run.workflow
    lines = [
        f"dry run of {workflow.name}@{workflow.version} — no model call, no repository, no state",
        "",
    ]
    planned = {(step.stage_id, step.attempt): step for step in report.steps}
    for result in report.run.attempts:
        lines.append(trace.render_line(result))
        step = planned.get((result.stage_id, result.attempt))
        if step is not None:
            lines.extend(f"        {line}" for line in _detail_lines(step))
        elif result.status is StepStatus.SKIPPED and result.error is not None:
            lines.append(f"        {result.error.message}")

    unsuccessful = report.run.failed_stages
    lines.extend(
        [
            "",
            f"status: {report.run.run.status.value}  "
            f"({len(report.run.attempts)} stages, {len(unsuccessful)} unsuccessful)",
        ]
    )
    if unsuccessful:
        lines.append(f"failed: {', '.join(unsuccessful)}")

    invented = list(report.synthesis.flattened())
    if invented:
        lines.extend(["", "values invented for this run, because nothing produced any:"])
        width = max(len(path) for path, _ in invented)
        lines.extend(f"  {path.ljust(width)}  {_compact(value)}" for path, value in invented)

    if report.unresolved:
        lines.extend(["", "references that resolved to nothing even so:"])
        lines.extend(
            f"  {found.reference} in {found.stage_id}.{found.field}" for found in report.unresolved
        )
    return "\n".join(lines)


def _detail_lines(step: PlannedStep) -> Iterator[str]:
    """The one or two things about a step worth reading in a trace."""
    detail = step.detail
    match step.type:
        case StepType.SCRIPT:
            argv = detail.get("argv")
            if isinstance(argv, list):
                yield f"argv  {_excerpt(shlex.join(str(part) for part in argv))}"
        case StepType.AGENT:
            yield f"role  {detail.get('role')} · {detail.get('model')}"
            yield f"task  {_excerpt(str(detail.get('task', '')))}"
        case StepType.RUNNER:
            plan = detail.get("plan")
            yield f"plan  {_excerpt(str(plan)) if plan is not None else "the handler's default"}"
        case StepType.APPROVAL:
            yield f"ask   {_excerpt(str(detail.get('prompt', '')))}"
        case _:  # pragma: no cover - only leaf types reach a handler
            return


def _excerpt(text: str) -> str:
    """One line of it. Newlines become spaces so a prompt cannot break the trace."""
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= _EXCERPT else flattened[: _EXCERPT - 1] + "…"


def _compact(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def to_dict(report: DryRunReport) -> dict[str, Any]:
    """The structured form: the run, the plan, and what was made up."""
    return {
        "run": trace.to_dict(report.run),
        "planned": [
            {
                "stage_id": step.stage_id,
                "definition_id": step.definition_id,
                "attempt": step.attempt,
                "type": step.type.value,
                "detail": dict(step.detail),
            }
            for step in report.steps
        ],
        "invented": {
            "request": report.synthesis.request,
            "outputs": dict(report.synthesis.outputs),
            "responses": dict(report.synthesis.responses),
        },
        "unresolved": [
            {"stage_id": found.stage_id, "field": found.field, "reference": found.reference}
            for found in report.unresolved
        ],
    }


def render_dry_run_json(report: DryRunReport) -> str:
    return json.dumps(to_dict(report), indent=2, sort_keys=True, ensure_ascii=False)
