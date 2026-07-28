"""Kill the process mid-workflow. Restart. It picks up where it left off.

S4's headline acceptance criterion, tested the way it is written: a *real*
child process, killed with ``SIGKILL`` while a step is in flight, and a second
invocation that has to work out what survived. Nothing here is simulated —
mocking the crash would test the mock, and the failure this is written against
(a row that says ``running`` because the process that wrote it is gone) only
exists because processes really do die without unwinding.

The first stage appends one character to a marker file, so "was it run again"
is a fact on disk rather than an inference from the report.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from clawdence.domain import StepStatus
from clawdence.store import StateStore

#: The child. ``python -c`` rather than a console script so the test runs the
#: same interpreter (and the same installed package) as the test process.
CHILD = "import sys; from clawdence.cli import main; sys.exit(main(sys.argv[1:]))"

#: How long to wait for the child to reach the blocking stage before giving up.
STARTUP_TIMEOUT_SECONDS = 30.0

#: Appends one character per execution, so "did this run twice" is a fact on
#: disk rather than an inference from the report.
FIRST_STAGE = """
import pathlib, sys

marker = pathlib.Path(sys.argv[1])
marker.write_text((marker.read_text() if marker.exists() else "") + "x")
"""

#: Blocks until the test releases it — which the first incarnation never does,
#: because it gets killed instead.
SECOND_STAGE = """
import pathlib, sys, time

gate = pathlib.Path(sys.argv[1])
deadline = time.time() + 60
while not gate.exists() and time.time() < deadline:
    time.sleep(0.02)
"""

WORKFLOW = """
schema_version: 1
name: crash
version: 1.0.0
stages:
  - id: first
    type: script
    command: ["{python}", "{first}", "{marker}"]
  - id: second
    type: script
    command: ["{python}", "{second}", "{gate}"]
"""


def clawdence(*args: str) -> list[str]:
    return [sys.executable, "-c", CHILD, *args]


def write_workflow(tmp_path: Path, *, marker: Path, gate: Path) -> Path:
    first = tmp_path / "first.py"
    first.write_text(FIRST_STAGE, encoding="utf-8")
    second = tmp_path / "second.py"
    second.write_text(SECOND_STAGE, encoding="utf-8")

    workflow = tmp_path / "crash.yaml"
    workflow.write_text(
        WORKFLOW.format(
            python=sys.executable, first=first, second=second, marker=marker, gate=gate
        ),
        encoding="utf-8",
    )
    return workflow


def wait_for_running_step(state: Path, stage_id: str) -> str:
    """Block until the child has a step in flight, and say which run it is."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if state.exists():
            with StateStore.open(state) as store:
                for step in store.running_steps():
                    if step.stage_id == stage_id:
                        return step.run_id
        time.sleep(0.05)
    raise AssertionError(f"the child never started stage {stage_id!r}")


def test_a_killed_run_resumes_without_repeating_finished_work(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    marker = tmp_path / "marker"
    gate = tmp_path / "gate"
    workflow = write_workflow(tmp_path, marker=marker, gate=gate)

    with subprocess.Popen(  # noqa: S603 - argv built here, no shell
        clawdence("run", str(workflow), "--state", str(state), "--work-item", "wi.crash"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        run_id = wait_for_running_step(state, "second")
        child.kill()
        child.wait()

    assert marker.read_text(encoding="utf-8") == "x", "the first stage ran once"

    with StateStore.open(state) as store:
        # What a killed process leaves behind, and the reason the watchdog and
        # the resume path both exist: a row that still claims to be running.
        assert [step.stage_id for step in store.running_steps()] == ["second"]

    gate.touch()
    resumed = subprocess.run(  # noqa: S603 - argv built here, no shell
        clawdence("run", str(workflow), "--state", str(state), "--resume", run_id),
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT_SECONDS,
        check=False,
    )

    assert resumed.returncode == 0, resumed.stderr
    assert marker.read_text(encoding="utf-8") == "x", "'first' had succeeded and must not re-run"

    with StateStore.open(state) as store:
        run = store.require_run(run_id)
        assert run.status.value == "done"
        assert run.work_item_id == "wi.crash"

        by_stage = [(step.stage_id, step.attempt, step.status) for step in store.steps_for(run_id)]
        assert by_stage == [
            ("first", 1, StepStatus.SUCCEEDED),
            # Attempt 1 of 'second' was reconciled: the process that started it
            # never came back to say how it ended.
            ("second", 1, StepStatus.CANCELLED),
            ("second", 2, StepStatus.SUCCEEDED),
        ]
        assert store.running_steps() == ()


def test_resuming_against_a_changed_workflow_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run keeps the definition it started with. That is what pinning means."""
    from clawdence.cli import main

    state = tmp_path / "state.db"
    workflow = tmp_path / "wf.yaml"
    workflow.write_text(
        "schema_version: 1\nname: pinned\nversion: 1.0.0\nstages:\n"
        "  - id: a\n    type: script\n    command: [python3, -c, 'print(1)']\n",
        encoding="utf-8",
    )
    assert main(["run", str(workflow), "--state", str(state)]) == 0

    with StateStore.open(state) as store:
        (run,) = store.list_runs()

    workflow.write_text(
        "schema_version: 1\nname: pinned\nversion: 2.0.0\nstages:\n"
        "  - id: a\n    type: script\n    command: [python3, -c, 'print(1)']\n",
        encoding="utf-8",
    )
    assert main(["run", str(workflow), "--state", str(state), "--resume", run.id]) == 2
    assert "was started against pinned@1.0.0" in capsys.readouterr().err
