"""What the file says, drawn — before it is what the run did.

``runs show`` answers "what happened". This answers "what *would* happen", from
the file alone, and the two readings that question has get one renderer each:

**The tree** is for the author at the terminal. Order, nesting, guards and the
data each stage reads, in the shape the YAML already has — so the thing being
checked is whether the file agrees with the process in the author's head.

**Mermaid** is for the pull request and the design note. It renders in GitHub
and in this project's own planning documents without a toolchain, which is the
whole reason it is the second format rather than DOT: a diagram nobody can see
without installing something is a diagram nobody looks at.

Neither renderer resolves anything. A guard is printed as written, not
evaluated, and no output is invented — that is ``dryrun``'s job, and keeping the
two apart is what lets this one be safe to run against a file you do not trust.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from clawdence.domain import Stage, Workflow
from clawdence.domain.workflow import (
    AgentStage,
    ApprovalStage,
    ForEachStage,
    OnError,
    ParallelStage,
    RepeatStage,
    RunnerStage,
    ScriptStage,
    SubWorkflowStage,
    WorkflowDefinition,
)
from clawdence.engine import conditions, interpolation, refs

#: Mermaid node shapes, one per step type. A reader should be able to tell a
#: human gate from a script at a glance without reading the labels.
_SHAPES = {
    "script": ('["', '"]'),
    "agent": ('(["', '"])'),
    "runner": ('[["', '"]]'),
    "approval": ('{{"', '"}}'),
    "for_each": ('["', '"]'),
    "parallel": ('["', '"]'),
    "repeat": ('["', '"]'),
    "workflow": ('[/"', '"/]'),
}


def render_tree(workflow: Workflow) -> str:
    """The workflow as an indented outline, in execution order."""
    lines = [f"{workflow.name}@{workflow.version}  (schema {workflow.schema_version})"]
    if workflow.description:
        lines.append(workflow.description)
    lines.append("")
    lines.extend(_tree_stages(workflow.stages, depth=1))
    for name, definition in workflow.sub_workflows.items():
        lines.append("")
        inputs = ", ".join(definition.inputs) or "no inputs"
        lines.append(f"  sub-workflow {name}  ({inputs})")
        lines.extend(_tree_stages(definition.stages, depth=2))
    return "\n".join(lines)


def _tree_stages(stages: Sequence[Stage], *, depth: int) -> Iterator[str]:
    pad = "  " * depth
    width = max((len(stage.id) for stage in stages), default=0)
    for stage in stages:
        yield f"{pad}{stage.id.ljust(width)}  {stage.type.value:<8}  {_summary(stage)}".rstrip()
        for note in _annotations(stage):
            yield f"{pad}{' ' * width}  {note}"
        if isinstance(stage, ForEachStage | RepeatStage):
            yield from _tree_stages(stage.stages, depth=depth + 1)
        elif isinstance(stage, ParallelStage):
            for branch in stage.branches:
                yield f"{pad}  branch {branch.id}"
                yield from _tree_stages(branch.stages, depth=depth + 2)


def _summary(stage: Stage) -> str:
    """The one thing about this stage a reader most wants beside its id."""
    match stage:
        case ScriptStage():
            extra = f" (+{len(stage.command) - 1} args)" if len(stage.command) > 1 else ""
            return f"{stage.command[0]}{extra}"
        case AgentStage():
            return f"role {stage.role}, model {stage.model.model}"
        case RunnerStage():
            tier = stage.isolation_tier_override
            return f"tier {tier}" if tier is not None else "repo work in the data plane"
        case ApprovalStage():
            approver = stage.required_approver
            return f"human gate, approver {approver}" if approver else "human gate"
        case ForEachStage():
            serial = f", serial by {stage.serial_key}" if stage.serial_key else ""
            return f"over {stage.items}, up to {stage.max_parallel} at once{serial}"
        case ParallelStage():
            branches = ", ".join(branch.id for branch in stage.branches)
            return f"branches {branches}, up to {stage.max_parallel} at once"
        case SubWorkflowStage():
            return f"calls {stage.workflow}"
        case RepeatStage():
            return f"until {stage.until}, at most {stage.max_iterations} times"


def _annotations(stage: Stage) -> Iterator[str]:
    """The declared behaviour that is not the stage's job — guards and policy."""
    if stage.when is not None:
        yield f"when      {stage.when}"
    reads = _reads(stage)
    if reads:
        yield f"reads     {', '.join(reads)}"
    if stage.retry.max_attempts > 1:
        backoff = f", {stage.retry.backoff_seconds}s apart" if stage.retry.backoff_seconds else ""
        yield f"retry     up to {stage.retry.max_attempts} attempts{backoff}"
    if stage.timeout_seconds is not None:
        yield f"timeout   {stage.timeout_seconds:g}s"
    if stage.on_error is not OnError.FAIL:
        yield f"on_error  {stage.on_error.value}"


def _reads(stage: Stage) -> list[str]:
    """Which stages this one reads, deduplicated and in first-seen order.

    Control flow is the tree's spine; this is the other graph in the same file,
    and it is the one that catches "why is this stage getting nothing" — a stage
    whose ``reads`` is empty when the author thought it took the plan.
    """
    seen: dict[str, None] = {}
    for reference in _references(stage):
        if reference.stage_id != refs.REQUEST:
            seen.setdefault(reference.stage_id, None)
        else:
            seen.setdefault("request", None)
    return list(seen)


def _references(stage: Stage) -> Iterator[refs.Reference]:
    """Every reference a stage makes. The file has already been validated.

    Malformed placeholders and unparseable conditions cannot reach here — the
    loader refuses the file — so this reads the same fields without the error
    handling that would only ever describe an impossible state.
    """
    for text in _condition_texts(stage):
        yield from conditions.references(conditions.parse(text))
    for template in _template_texts(stage):
        yield from interpolation.references(template)
    if isinstance(stage, ForEachStage):
        yield refs.parse_reference(stage.items)
    if isinstance(stage, SubWorkflowStage):
        for value in stage.inputs.values():
            if isinstance(value, str) and value.startswith("$"):
                yield refs.parse_reference(value)


def _condition_texts(stage: Stage) -> Iterator[str]:
    if stage.when is not None:
        yield stage.when
    if isinstance(stage, RepeatStage):
        yield stage.until


def _template_texts(stage: Stage) -> Iterator[str]:
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
        case ForEachStage():
            if stage.serial_key is not None:
                yield stage.serial_key
        case _:
            return


def render_mermaid(workflow: Workflow) -> str:
    """A ``flowchart`` of the same thing, for somewhere a terminal is not."""
    lines = [
        "flowchart TD",
        f"  %% {workflow.name}@{workflow.version}",
    ]
    lines.extend(_mermaid_scope(workflow.stages, prefix="s", indent="  "))
    for name, definition in workflow.sub_workflows.items():
        lines.extend(_mermaid_definition(name, definition))
    return "\n".join(lines)


def _mermaid_definition(name: str, definition: WorkflowDefinition) -> Iterator[str]:
    node = _node_id(f"sub_{name}")
    yield f'  subgraph {node}["sub-workflow {_label(name)}"]'
    yield "    direction TB"
    yield from _mermaid_scope(definition.stages, prefix=f"{node}_", indent="    ")
    yield "  end"


def _mermaid_scope(stages: Sequence[Stage], *, prefix: str, indent: str) -> Iterator[str]:
    """Nodes for one ordered scope, then the edges that put them in order."""
    # The position is in the id because the sanitiser is lossy: `build-item` and
    # `build_item` are distinct stage ids and would otherwise become one node,
    # which is a diagram that is quietly wrong rather than one that fails.
    ids = [_node_id(f"{prefix}{index}_{stage.id}") for index, stage in enumerate(stages)]
    for stage, node in zip(stages, ids, strict=True):
        if isinstance(stage, ForEachStage | RepeatStage):
            yield f'{indent}subgraph {node}["{_composition_label(stage)}"]'
            yield f"{indent}  direction TB"
            yield from _mermaid_scope(stage.stages, prefix=f"{node}_", indent=f"{indent}  ")
            yield f"{indent}end"
        elif isinstance(stage, ParallelStage):
            yield f'{indent}subgraph {node}["{_composition_label(stage)}"]'
            yield f"{indent}  direction TB"
            for branch in stage.branches:
                branch_node = _node_id(f"{node}_{branch.id}")
                yield f'{indent}  subgraph {branch_node}["{_label(branch.id)}"]'
                yield f"{indent}    direction TB"
                yield from _mermaid_scope(
                    branch.stages, prefix=f"{branch_node}_", indent=f"{indent}    "
                )
                yield f"{indent}  end"
            yield f"{indent}end"
        else:
            open_shape, close_shape = _SHAPES[stage.type.value]
            yield f"{indent}{node}{open_shape}{_node_label(stage)}{close_shape}"

    for previous, stage, node in zip(ids[:-1], stages[1:], ids[1:], strict=True):
        # The guard rides on the edge into the stage it guards, because that is
        # where a reader asks "and when does this one run".
        label = f'|"{_label(stage.when)}"|' if stage.when is not None else ""
        yield f"{indent}{previous} -->{label} {node}"

    for stage, node in zip(stages, ids, strict=True):
        if isinstance(stage, SubWorkflowStage):
            yield f"{indent}{node} -.-> {_node_id(f'sub_{stage.workflow}')}"


def _node_label(stage: Stage) -> str:
    return f"{_label(stage.id)}<br/>{stage.type.value}"


def _composition_label(stage: ForEachStage | ParallelStage | RepeatStage) -> str:
    match stage:
        case ForEachStage():
            return f"{_label(stage.id)} · for_each {_label(stage.items)}"
        case ParallelStage():
            return f"{_label(stage.id)} · parallel"
        case RepeatStage():
            return f"{_label(stage.id)} · repeat ≤{stage.max_iterations}"


def _label(text: str) -> str:
    """Escape for a mermaid label.

    Mermaid's own entity spellings rather than HTML's, because a label is not
    HTML and ``&quot;`` renders as the six characters it is. Newlines become
    breaks: a multi-line condition would otherwise end the statement.
    """
    return (
        text.replace("#", "#35;")
        .replace('"', "#quot;")
        .replace("<", "#lt;")
        .replace(">", "#gt;")
        .replace("\n", "<br/>")
    )


def _node_id(raw: str) -> str:
    """A mermaid-safe identifier. Stage ids are slugs; scopes join them."""
    return "".join(char if char.isalnum() or char == "_" else "_" for char in raw)
