"""S3's acceptance criterion, executed.

    "clawdence run examples/toy.yaml executes a 3-stage workflow with one
     conditional branch and one induced failure hitting on_error: skip_rest.
     Output is inspectable."

Run end to end against the real file with real subprocesses — no stubs — because
the criterion is about the shipped artifact rather than about the executor's
internals. It also means the example in the repo cannot rot: a change that
breaks it breaks the build.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import JsonValue

from clawdence.domain import RunStatus, StepStatus
from clawdence.engine import RunReport, execute, load_workflow, render_json, render_text
from tests.engine.factories import run

TOY = Path("examples/toy.yaml")


def report() -> RunReport:
    workflow = load_workflow(TOY)
    return run(execute(workflow, run_id="run.toy", work_item_id="wi.toy"))


def envelope(result: RunReport, stage_id: str) -> dict[str, JsonValue]:
    """A script step's output, narrowed — see ``ScriptHandler``'s one shape."""
    output = result.final[stage_id].output
    assert isinstance(output, dict)
    return output


class TestToyWorkflow:
    def test_it_runs_end_to_end(self) -> None:
        result = report()
        statuses = {stage_id: r.status for stage_id, r in result.final.items()}
        assert statuses == {
            "classify": StepStatus.SUCCEEDED,
            "implement": StepStatus.SUCCEEDED,
            # The conditional branch that was not taken.
            "split": StepStatus.SKIPPED,
            # The induced failure.
            "verify": StepStatus.FAILED,
            # skip_rest reached this one.
            "publish": StepStatus.SKIPPED,
        }

    def test_the_run_is_halted_not_done(self) -> None:
        result = report()
        assert result.run.status is RunStatus.HALTED
        assert result.failed_stages == ("verify",)

    def test_json_output_flows_into_the_next_stage(self) -> None:
        # classify emits JSON; implement interpolates a field of it into argv.
        result = report()
        assert envelope(result, "classify")["parsed"] == {"verdict": "APPROVED", "size": "M"}
        assert "implementing a M change" in str(envelope(result, "implement")["stdout"])

    def test_the_failing_stage_exhausted_its_retries(self) -> None:
        result = report()
        verify = [r for r in result.attempts if r.stage_id == "verify"]
        assert [r.attempt for r in verify] == [1, 2]


class TestInspectability:
    def test_the_text_trace_shows_every_stage_including_skipped(self) -> None:
        text = render_text(report())
        for stage_id in ("classify", "implement", "split", "verify", "publish"):
            assert stage_id in text
        assert "halted" in text
        # A trace that listed only what ran could not distinguish "the branch
        # was not taken" from "the engine forgot".
        assert "condition was false" in text

    def test_the_text_trace_marks_the_retry(self) -> None:
        assert "attempt 2" in render_text(report())

    def test_the_json_report_holds_every_attempt(self) -> None:
        payload = json.loads(render_json(report()))
        assert payload["succeeded"] is False
        assert payload["failed_stages"] == ["verify"]
        assert len(payload["attempts"]) == 6  # five stages, one retried once
        assert payload["run"]["workflow"] == "toy"

    def test_the_json_report_is_deterministic_in_key_order(self) -> None:
        # Sorted keys, so a diff of two runs shows what changed rather than
        # what moved.
        text = render_json(report())
        assert text.index('"attempts"') < text.index('"failed_stages"') < text.index('"run"')
