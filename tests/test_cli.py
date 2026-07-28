"""Scaffold-level checks: the package imports and the entry point runs.

These exist so CI has something real to fail on from commit 1.

Every test that executes a workflow passes ``--no-state`` or an explicit
``--state``. Neither is optional: ``clawdence run`` records to
``~/.clawdence/state.db`` by default, and a test suite that writes into the
home directory of whoever runs it is a test suite that has a side effect.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clawdence import __version__
from clawdence.cli import main
from clawdence.domain import Run, RunStatus, StepResult, StepStatus, StepType
from clawdence.store import StateStore


def test_version_is_populated() -> None:
    assert __version__
    assert __version__ != "0.0.0+unknown"


def test_main_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "clawdence" in capsys.readouterr().out


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_schema_export_then_check_agree(tmp_path: Path) -> None:
    assert main(["schema", "export", "--out", str(tmp_path)]) == 0
    assert main(["schema", "check", "--out", str(tmp_path)]) == 0


def test_schema_check_fails_on_a_stale_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-zero exit is the whole point: this runs in CI.

    A drift check that reports the problem and exits zero is a drift check
    nobody finds out about.
    """
    assert main(["schema", "check", "--out", str(tmp_path)]) == 1
    assert "out of date" in capsys.readouterr().out


def test_run_executes_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    """Exit 1, because ``toy.yaml`` induces a failure on purpose.

    The distinction the exit codes carry: 1 means the workflow ran and did not
    succeed, 2 means it never ran at all. A script wrapping ``clawdence run``
    needs to tell those apart.
    """
    assert main(["run", "examples/toy.yaml", "--no-state"]) == 1
    out = capsys.readouterr().out
    assert "classify" in out
    assert "status: halted" in out


def test_run_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "examples/toy.yaml", "--json", "--no-state"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow"]["name"] == "toy"


def test_run_reports_a_bad_workflow_on_stderr_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("name: demo\nversion: 1.0.0\nstages: []\n", encoding="utf-8")
    assert main(["run", str(path), "--no-state"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not match the workflow schema" in captured.err
    assert "Traceback" not in captured.err


def test_run_accepts_a_work_item_id(capsys: pytest.CaptureFixture[str]) -> None:
    main(["run", "examples/toy.yaml", "--json", "--no-state", "--work-item", "wi.chosen"])
    assert json.loads(capsys.readouterr().out)["run"]["work_item_id"] == "wi.chosen"


def test_resume_without_a_state_database_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "examples/toy.yaml", "--no-state", "--resume", "run.x"]) == 2
    assert "--resume needs the state database" in capsys.readouterr().err


def test_resuming_a_run_that_was_never_recorded_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state.db"
    assert main(["run", "examples/toy.yaml", "--state", str(state), "--resume", "run.x"]) == 2
    assert "no run with id" in capsys.readouterr().err


class TestRunsCommands:
    """The operator surface. HQ (S19) is the read surface; this is the one for
    when HQ is the thing that is not working."""

    def state(self, tmp_path: Path) -> Path:
        path = tmp_path / "state.db"
        main(["run", "examples/toy.yaml", "--state", str(path)])
        return path

    def test_list_shows_the_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["runs", "list", "--state", str(self.state(tmp_path))]) == 0
        assert "toy@1.0.0" in capsys.readouterr().out

    def test_list_of_an_empty_store_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["runs", "list", "--state", str(tmp_path / "empty.db")]) == 0
        assert "no runs recorded" in capsys.readouterr().out

    def test_list_filters_by_status(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()
        assert main(["runs", "list", "--state", str(state), "--status", "done"]) == 0
        assert "no runs recorded" in capsys.readouterr().out

    def test_show_lists_every_step(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()
        run_id = _only_run_id(state)

        assert main(["runs", "show", run_id, "--state", str(state)]) == 0
        out = capsys.readouterr().out
        assert "classify" in out
        assert "audit records" in out

    def test_show_of_an_unknown_run_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["runs", "show", "run.x", "--state", str(tmp_path / "state.db")]) == 2
        assert "no run with id" in capsys.readouterr().err

    def test_recover_reports_a_quiet_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["runs", "recover", "--state", str(tmp_path / "state.db")]) == 0
        assert "nothing stalled" in capsys.readouterr().out

    def test_recover_dry_run_changes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state.db"
        _abandon_a_step(state)
        capsys.readouterr()

        assert main(["runs", "recover", "--state", str(state), "--dry-run"]) == 0
        assert "would recover" in capsys.readouterr().out

        with StateStore.open(state) as store:
            assert store.running_steps() != ()

    def test_recover_times_the_step_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state.db"
        _abandon_a_step(state)
        capsys.readouterr()

        assert main(["runs", "recover", "--state", str(state)]) == 0
        assert "recovered: " in capsys.readouterr().out

        with StateStore.open(state) as store:
            assert store.running_steps() == ()

    def test_recover_drains_the_dead_letter_queue(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state.db"
        with StateStore.open(state) as store:
            store.audit.submit({"id": "ev.bad", "kind": "no.such.kind"}, at=_now())

        assert main(["runs", "recover", "--state", str(state)]) == 0
        assert "dead letters: 1 parked, 0 replayed" in capsys.readouterr().out


def _now() -> datetime:
    return datetime.now(UTC)


def _only_run_id(state: Path) -> str:
    with StateStore.open(state) as store:
        (run,) = store.list_runs()
        return run.id


def _abandon_a_step(state: Path) -> None:
    """A step row left running long enough to be overdue, as a crash leaves it."""
    with StateStore.open(state) as store:
        run = store.create_run(
            Run(
                id="run.abandoned",
                work_item_id="wi.test",
                workflow="toy",
                workflow_version="1.0.0",
                status=RunStatus.RUNNING,
                created_at=_now() - timedelta(hours=4),
                updated_at=_now() - timedelta(hours=4),
            )
        )
        store.start_step(
            StepResult(
                id="sr.stuck",
                run_id=run.id,
                stage_id="build",
                type=StepType.SCRIPT,
                status=StepStatus.RUNNING,
                attempt=1,
                idempotency_key="run.abandoned:build:1",
                timeout_seconds=60,
                started_at=_now() - timedelta(hours=4),
            )
        )
