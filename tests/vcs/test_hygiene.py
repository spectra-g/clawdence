"""The diff audit, against real trees git actually produced."""

from __future__ import annotations

from pathlib import Path

import pytest

from clawdence.runners import worktree as wt
from clawdence.vcs import Problem, audit
from clawdence.vcs.git import git
from tests.harness.repos import FixtureRepo
from tests.ports.factories import run


def change(repo: FixtureRepo, files: dict[str, str]) -> tuple[str, str]:
    """Commit ``files`` on top of the fixture; return (base, head)."""
    base = run(wt.head(repo.path))
    for name, contents in files.items():
        target = repo.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    head = run(wt.commit_all(repo.path, "work"))
    assert head is not None
    return base, head


def problems(findings: tuple[object, ...]) -> set[Problem]:
    return {finding.problem for finding in findings}  # type: ignore[attr-defined]


def test_an_ordinary_change_is_clean(origin: FixtureRepo) -> None:
    base, head = change(origin, {"app.py": "def add(a, b):\n    return a + b + 0\n"})
    assert audit_of(origin, base, head) == ()


def audit_of(repo: FixtureRepo, base: str, head: str) -> tuple[object, ...]:
    return run(audit(repo.path, base, head))


def test_a_committed_symlink_is_refused(origin: FixtureRepo) -> None:
    """Its content is a path, which resolves on whichever machine checks it out
    next — a CI runner, a reviewer's laptop."""
    base = run(wt.head(origin.path))
    (origin.path / "shortcut").symlink_to("/etc/passwd")
    head = run(wt.commit_all(origin.path, "add a link"))
    assert head is not None

    found = audit_of(origin, base, head)
    assert problems(found) == {Problem.SYMLINK}
    assert found[0].path == "shortcut"  # type: ignore[attr-defined]


def test_a_submodule_pointer_is_refused(origin: FixtureRepo, workspace: Path) -> None:
    """A gitlink is an instruction to fetch a repository from a URL in
    ``.gitmodules``, and both halves are content the agent just wrote."""
    other = workspace / "other"
    other.mkdir()
    run(git(other, "init", "--quiet", "-b", "main"))
    (other / "x").write_text("x", encoding="utf-8")
    run(git(other, "add", "-A"))
    run(git(other, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-qm", "x"))

    base = run(wt.head(origin.path))
    run(
        git(
            origin.path,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "--quiet",
            str(other),
            "vendor-lib",
        )
    )
    head = run(wt.commit_all(origin.path, "add a submodule"))
    assert head is not None
    assert Problem.SUBMODULE in problems(audit_of(origin, base, head))


@pytest.mark.parametrize(
    "path",
    ["node_modules/left-pad/index.js", "packages/api/node_modules/x.js", ".venv/lib/thing.py"],
)
def test_package_manager_output_is_refused(origin: FixtureRepo, path: str) -> None:
    base, head = change(origin, {path: "generated\n"})
    found = audit_of(origin, base, head)
    assert problems(found) == {Problem.VENDORED}


def test_a_name_that_merely_contains_a_vendored_word_is_fine(origin: FixtureRepo) -> None:
    """Matched as a path component. A general "looks generated" heuristic would
    block a legitimate change to a file whose name happened to rhyme."""
    base, head = change(origin, {"docs/my_node_modules_notes.md": "notes\n"})
    assert audit_of(origin, base, head) == ()


def test_a_large_file_is_refused_and_the_limit_is_stated(origin: FixtureRepo) -> None:
    """A hundred small files is a change; one 90 MB file is an artefact."""
    base, head = change(origin, {"dump.bin": "x" * 4096})
    found = run(audit(origin.path, base, head, max_file_bytes=1024))
    assert problems(found) == {Problem.OVERSIZED}
    assert "limit" in found[0].detail


def test_deleting_the_problem_is_not_a_finding(origin: FixtureRepo) -> None:
    """A commit that removes a symlink is the fix, and reporting it as the
    violation would make the only available remedy look like one."""
    (origin.path / "shortcut").symlink_to("/etc/passwd")
    run(wt.commit_all(origin.path, "add a link"))

    base = run(wt.head(origin.path))
    (origin.path / "shortcut").unlink()
    head = run(wt.commit_all(origin.path, "remove the link"))
    assert head is not None
    assert audit_of(origin, base, head) == ()


def test_a_renamed_file_is_read_at_its_destination(origin: FixtureRepo) -> None:
    """``diff --raw`` emits two paths for a rename, and reading the first would
    report the name the file no longer has."""
    base = run(wt.head(origin.path))
    (origin.path / "node_modules").mkdir(exist_ok=True)
    (origin.path / "app.py").rename(origin.path / "node_modules" / "app.py")
    head = run(wt.commit_all(origin.path, "move it"))
    assert head is not None
    found = audit_of(origin, base, head)
    assert problems(found) == {Problem.VENDORED}
    assert found[0].path == "node_modules/app.py"  # type: ignore[attr-defined]


def test_a_filename_containing_a_newline_does_not_split_a_record(origin: FixtureRepo) -> None:
    """An agent can create a file called anything, so the input to this parser is
    attacker-influenced by construction. Without ``-z`` git quotes the path and
    one record becomes two."""
    base, head = change(origin, {"node_modules/we\nird.js": "x\n"})
    found = audit_of(origin, base, head)
    assert problems(found) == {Problem.VENDORED}
    assert found[0].path == "node_modules/we\nird.js"  # type: ignore[attr-defined]


def test_one_file_can_have_two_problems(origin: FixtureRepo) -> None:
    base = run(wt.head(origin.path))
    (origin.path / "node_modules").mkdir(exist_ok=True)
    (origin.path / "node_modules" / "link").symlink_to("/etc/passwd")
    head = run(wt.commit_all(origin.path, "both at once"))
    assert head is not None
    assert problems(audit_of(origin, base, head)) == {Problem.SYMLINK, Problem.VENDORED}


def test_a_finding_describes_itself_for_a_human(origin: FixtureRepo) -> None:
    base, head = change(origin, {"node_modules/x.js": "x\n"})
    described = audit_of(origin, base, head)[0].describe()  # type: ignore[attr-defined]
    assert described.startswith("node_modules/x.js: ")
