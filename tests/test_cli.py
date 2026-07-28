"""Scaffold-level checks: the package imports and the entry point runs.

These exist so CI has something real to fail on from commit 1.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawdence import __version__
from clawdence.cli import main


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
    assert main(["run", "examples/toy.yaml"]) == 1
    out = capsys.readouterr().out
    assert "classify" in out
    assert "status: halted" in out


def test_run_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "examples/toy.yaml", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow"]["name"] == "toy"


def test_run_reports_a_bad_workflow_on_stderr_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("name: demo\nversion: 1.0.0\nstages: []\n", encoding="utf-8")
    assert main(["run", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not match the workflow schema" in captured.err
    assert "Traceback" not in captured.err


def test_run_accepts_a_work_item_id(capsys: pytest.CaptureFixture[str]) -> None:
    main(["run", "examples/toy.yaml", "--json", "--work-item", "wi.chosen"])
    assert json.loads(capsys.readouterr().out)["run"]["work_item_id"] == "wi.chosen"
