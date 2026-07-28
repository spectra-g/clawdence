"""Reading a file written by a process that ran model-generated code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Aliased: pytest tries to collect any module-level name starting with `Test`,
# and then warns that it cannot, which fails a suite that treats warnings as
# errors.
from clawdence.domain import TestReporter as Reporter
from clawdence.runners import verdict as vd


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    (tmp_path / ".clawdence").mkdir()
    return tmp_path


def write(worktree: Path, contents: str) -> None:
    vd.path_in(worktree).write_text(contents, encoding="utf-8")


def test_no_verdict_is_an_absence_not_an_error(worktree: Path) -> None:
    """What the absence *means* is the contract's business, not this reader's."""
    assert vd.read(worktree) is None


def test_a_verdict_is_parsed(worktree: Path) -> None:
    write(
        worktree,
        json.dumps(
            {
                "status": "passed",
                "summary": "added a branch for strings",
                "tests": {"reporter": "pytest-json-report", "total": 12, "passed": 12},
                "discovery_notes": ["the parser is generated"],
                "unresolved_stubs": ["error path for empty input"],
            }
        ),
    )
    parsed = vd.read(worktree)
    assert parsed is not None
    assert parsed.status is vd.VerdictStatus.PASSED
    assert parsed.tests is not None
    assert parsed.tests.reporter is Reporter.PYTEST_JSON_REPORT
    assert parsed.discovery_notes == ("the parser is generated",)


def test_a_symlink_is_refused_rather_than_followed(worktree: Path) -> None:
    """Otherwise a link at the verdict path has the control plane read whatever
    it points at, and put it in a run record."""
    secret = worktree.parent / "credentials"
    secret.write_text("token = hunter2\n")
    vd.path_in(worktree).symlink_to(secret)

    with pytest.raises(vd.VerdictError, match="symlink"):
        vd.read(worktree)


def test_a_directory_is_refused(worktree: Path) -> None:
    vd.path_in(worktree).mkdir()
    with pytest.raises(vd.VerdictError, match="regular file"):
        vd.read(worktree)


def test_an_oversized_verdict_is_refused_before_it_is_parsed(worktree: Path) -> None:
    """The process that would parse a gigabyte of JSON is the control plane."""
    write(worktree, json.dumps({"status": "passed", "summary": "x" * vd.MAX_VERDICT_BYTES}))
    with pytest.raises(vd.VerdictError, match="over the"):
        vd.read(worktree)


def test_malformed_json_is_a_verdict_error(worktree: Path) -> None:
    write(worktree, "{not json")
    with pytest.raises(vd.VerdictError, match="did not validate"):
        vd.read(worktree)


def test_an_unknown_field_is_refused(worktree: Path) -> None:
    """A verdict from a newer protocol is a parse failure rather than a record
    we half understand."""
    write(worktree, json.dumps({"status": "passed", "confidence": 0.9}))
    with pytest.raises(vd.VerdictError, match="confidence"):
        vd.read(worktree)


def test_an_unknown_status_is_refused(worktree: Path) -> None:
    write(worktree, json.dumps({"status": "probably fine"}))
    with pytest.raises(vd.VerdictError):
        vd.read(worktree)


def test_the_error_does_not_quote_the_file(worktree: Path) -> None:
    """pydantic's own message includes the input, and the input came from a
    process we do not trust. What propagates is field names, which are ours."""
    write(worktree, json.dumps({"status": "passed", "summary": "SECRET-a1b2c3"}, indent=1) + "x")
    with pytest.raises(vd.VerdictError) as caught:
        vd.read(worktree)
    assert "SECRET" not in str(caught.value)


def test_a_note_longer_than_the_cap_is_refused(worktree: Path) -> None:
    """The caps are what stop a repository deciding how much of the next
    prompt it occupies."""
    write(worktree, json.dumps({"status": "passed", "discovery_notes": ["x" * 2001]}))
    with pytest.raises(vd.VerdictError, match="discovery_notes"):
        vd.read(worktree)


def test_too_many_notes_are_refused(worktree: Path) -> None:
    write(worktree, json.dumps({"status": "passed", "discovery_notes": ["note"] * 101}))
    with pytest.raises(vd.VerdictError, match="discovery_notes"):
        vd.read(worktree)


def test_clearing_removes_the_previous_attempts_answer(worktree: Path) -> None:
    """Without this, a second attempt that crashes before writing anything
    inherits the first attempt's verdict and reports its result."""
    write(worktree, json.dumps({"status": "passed"}))
    vd.clear(worktree)
    assert vd.read(worktree) is None


def test_clearing_nothing_is_not_an_error(worktree: Path) -> None:
    vd.clear(worktree)


def test_clearing_a_directory_leaves_it_for_the_reader_to_refuse(worktree: Path) -> None:
    """A cleanup step raising would be a worse failure than the one it hides."""
    vd.path_in(worktree).mkdir()
    vd.clear(worktree)
    with pytest.raises(vd.VerdictError):
        vd.read(worktree)


def test_clearing_removes_a_symlink_without_following_it(worktree: Path) -> None:
    target = worktree.parent / "elsewhere"
    target.write_text("still here\n")
    vd.path_in(worktree).symlink_to(target)

    vd.clear(worktree)
    assert not vd.path_in(worktree).is_symlink()
    assert target.exists()
