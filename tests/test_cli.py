"""Scaffold-level checks: the package imports and the entry point runs.

These exist so CI has something real to fail on from commit 1.
"""

from __future__ import annotations

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
