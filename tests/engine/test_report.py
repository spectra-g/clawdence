"""Rendering a run.

Thin, but the trace is the only thing a person sees when a workflow does
something surprising, so the cases where a stage has nothing quotable to show
matter as much as the ones where it does.
"""

from __future__ import annotations

import json

from clawdence.domain import AgentStage, ModelSelector, StepType, Workflow
from clawdence.engine import (
    HandlerRegistry,
    RunReport,
    StubHandler,
    execute,
    render_json,
    render_text,
)
from tests.engine.factories import RUN_ID, run, script, ticking_clock, workflow


def go(wf: Workflow, handler: object) -> RunReport:
    return run(
        execute(
            wf,
            run_id=RUN_ID,
            work_item_id="wi.test",
            registry=HandlerRegistry(dict.fromkeys(StepType, handler)),  # type: ignore[arg-type]
            clock=ticking_clock(),
        )
    )


class TestText:
    def test_a_stage_with_no_quotable_detail_renders_one_line(self) -> None:
        # An agent step's output is not a script envelope, so there is no
        # stdout to echo — the line should still be there.
        stage = AgentStage(id="plan", role="ba", model=ModelSelector(model="claude-opus-5"))
        text = render_text(go(workflow(stage), StubHandler(output={"epic": "…"})))
        assert "plan  [agent]" in text

    def test_a_script_that_printed_nothing_renders_one_line(self) -> None:
        text = render_text(go(workflow(script("quiet")), StubHandler(output={"exit_code": 0})))
        assert "quiet  [script]" in text

    def test_the_header_names_the_workflow_and_version(self) -> None:
        text = render_text(go(workflow(script("a"), name="demo", version="2.0.1"), StubHandler()))
        assert "workflow demo@2.0.1" in text

    def test_the_duration_is_reported(self) -> None:
        text = render_text(go(workflow(script("a")), StubHandler()))
        assert "s)" in text.splitlines()[-1]

    def test_a_clean_run_lists_no_failures(self) -> None:
        assert "failed:" not in render_text(go(workflow(script("a")), StubHandler()))


class TestJson:
    def test_round_trips_through_json(self) -> None:
        payload = json.loads(render_json(go(workflow(script("a")), StubHandler())))
        assert payload["run"]["id"] == RUN_ID
        assert payload["succeeded"] is True
        assert payload["attempts"][0]["stage_id"] == "a"

    def test_datetimes_are_serialised_not_repr(self) -> None:
        payload = json.loads(render_json(go(workflow(script("a")), StubHandler())))
        assert payload["run"]["created_at"].startswith("2026-07-28T")
