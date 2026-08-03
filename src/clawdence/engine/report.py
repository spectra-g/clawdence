"""Making a run inspectable.

"Output is inspectable" is S3's own acceptance criterion, and it is a real
requirement rather than a nicety: every step's result is persisted precisely
because Lobster surfacing only the final step's output is a structural mismatch
for a system whose premise is observable multi-stage work (ADR-0003).

Two renderings, for the two readers. The text form is for a person watching a
run and answers "what happened, and why did that stage not run". The JSON form
is for everything else — S19's timeline, S20's replay, and a test that would
rather assert on a structure than on a paragraph.

Skipped stages are shown, not omitted. A trace that lists only what ran cannot
distinguish "the branch was not taken" from "the engine forgot".
"""

from __future__ import annotations

import json
from typing import Any

from clawdence.domain import StepResult, StepStatus
from clawdence.engine.executor import RunReport

#: Aligned so the status column is scannable down the left.
_MARKS = {
    StepStatus.SUCCEEDED: "ok",
    StepStatus.FAILED: "FAIL",
    StepStatus.TIMED_OUT: "TIME",
    StepStatus.SKIPPED: "skip",
    StepStatus.PENDING: "....",
    StepStatus.RUNNING: "....",
    StepStatus.AWAITING_APPROVAL: "gate",
    StepStatus.CANCELLED: "canc",
}


def render_text(report: RunReport) -> str:
    """A per-stage trace, in execution order, including retries."""
    lines = [
        f"run {report.run.id}  workflow {report.workflow.name}@{report.workflow.version}",
        "",
    ]
    for result in report.attempts:
        lines.append(_line(result))
        detail = _detail(result)
        if detail:
            lines.append(f"        {detail}")

    lines.append("")
    lines.append(f"status: {report.run.status.value}  ({_duration(report)})")
    if report.failed_stages:
        lines.append(f"failed: {', '.join(report.failed_stages)}")
    return "\n".join(lines)


def _line(result: StepResult) -> str:
    attempt = f" (attempt {result.attempt})" if result.attempt > 1 else ""
    if result.scope:
        declared = result.definition_id if result.definition_id is not None else result.stage_id
        label = f"{' / '.join(result.scope)} / {declared}"
    else:
        label = result.stage_id
    return f"  {_MARKS[result.status]:>4}  {label}  [{result.type.value}]{attempt}"


def _detail(result: StepResult) -> str:
    if result.error is not None:
        return f"{result.error.kind}: {result.error.message}"
    if isinstance(result.output, dict) and "exit_code" in result.output:
        stdout = result.output.get("stdout")
        if isinstance(stdout, str) and stdout.strip():
            return stdout.strip().splitlines()[0]
    return ""


def _duration(report: RunReport) -> str:
    started, finished = report.run.created_at, report.run.finished_at
    if finished is None:  # pragma: no cover - execute always sets it
        return "unfinished"
    return f"{(finished - started).total_seconds():.2f}s"


def to_dict(report: RunReport) -> dict[str, Any]:
    """The structured form. Every attempt, not just the surviving one."""
    return {
        "run": report.run.model_dump(mode="json"),
        "workflow": {"name": report.workflow.name, "version": report.workflow.version},
        "succeeded": report.succeeded,
        "failed_stages": list(report.failed_stages),
        "attempts": [result.model_dump(mode="json") for result in report.attempts],
    }


def render_json(report: RunReport) -> str:
    return json.dumps(to_dict(report), indent=2, sort_keys=True, ensure_ascii=False)
