"""Telling the runner's own files apart from the agent's work.

One question — *is this path ours?* — and everything §3.7a needs hangs off it.
Answer "yes" too readily and a deliberate agent edit gets deleted by our cleanup;
answer "no" too readily and every run reports a dropped commit, because our plan
and conventions file are in that tree every single time.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clawdence.runners.installed import MAX_COMPARE_BYTES, WORK_DIR, Installed, Record


@pytest.fixture
def installed(tmp_path: Path) -> Installed:
    return Installed(worktree=tmp_path)


def test_a_file_we_wrote_and_nobody_touched_is_ours(installed: Installed) -> None:
    installed.write("AGENTS.md", "conventions\n")
    assert installed.owns("AGENTS.md")


def test_a_file_the_agent_changed_is_not_ours_any_more(installed: Installed) -> None:
    """The byte comparison is the whole control. An agent asked to update the
    conventions file must not have its change reverted by our cleanup."""
    installed.write("AGENTS.md", "conventions\n")
    (installed.worktree / "AGENTS.md").write_text("the agent rewrote this\n")
    assert not installed.owns("AGENTS.md")


def test_a_change_of_the_same_length_is_still_a_change(installed: Installed) -> None:
    """Size is checked first because it is cheap, not because it is the answer."""
    installed.write("AGENTS.md", "aaaa\n")
    (installed.worktree / "AGENTS.md").write_text("bbbb\n")
    assert not installed.owns("AGENTS.md")


def test_a_path_we_never_wrote_is_never_ours(installed: Installed) -> None:
    (installed.worktree / "app.py").write_text("x = 1\n")
    assert not installed.owns("app.py")


def test_a_file_the_agent_deleted_is_not_ours(installed: Installed) -> None:
    """Deleting it is a change like any other, and one the agent may have meant."""
    installed.write("AGENTS.md", "conventions\n")
    (installed.worktree / "AGENTS.md").unlink()
    assert not installed.owns("AGENTS.md")


def test_a_copy_records_what_landed_rather_than_what_was_asked_for(tmp_path: Path) -> None:
    source = tmp_path / "source" / "AGENTS.md"
    source.parent.mkdir()
    source.write_text("conventions\n")
    installed = Installed(worktree=tmp_path / "tree")
    (tmp_path / "tree").mkdir()

    installed.copy(source, "AGENTS.md")
    assert (installed.worktree / "AGENTS.md").read_text() == "conventions\n"
    assert installed.owns("AGENTS.md")


# --------------------------------------------------------------------------- #
# The directory rule, which is not the byte rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [WORK_DIR, f"{WORK_DIR}/plan.md", f"{WORK_DIR}/verdict.json", f"{WORK_DIR}/home/config.toml"],
)
def test_anything_under_the_runners_own_directory_is_ours(installed: Installed, path: str) -> None:
    """Not compared, because there is nothing to compare it against: the agent's
    verdict and its CLI's config file were written by them, into a directory
    this run created for exactly that. Contents are irrelevant — the prefix is
    the whole claim."""
    assert installed.owns(path)


def test_a_path_that_merely_starts_with_the_same_letters_is_not_ours(
    installed: Installed,
) -> None:
    """``.clawdence-notes.md`` is somebody's file, not our directory."""
    assert not installed.owns(f"{WORK_DIR}-notes.md")


def test_a_leading_slash_does_not_change_the_answer(installed: Installed) -> None:
    """git reports paths relative and unprefixed, but the exclude entries this
    is paired with are written ``/.clawdence/``, so both spellings turn up in
    the same code and one of them must not silently mean something else."""
    installed.write("AGENTS.md", "conventions\n")
    assert installed.owns("/AGENTS.md")
    assert installed.owns(f"/{WORK_DIR}/plan.md")


# --------------------------------------------------------------------------- #
# The tree is output from a process that ran model-generated code
# --------------------------------------------------------------------------- #


def test_a_symlink_at_an_installed_path_is_not_ours_and_is_not_followed(
    installed: Installed, tmp_path: Path
) -> None:
    """Following it would have the control plane read whatever it points at in
    order to decide whether the file was its own — and then, on a match, revert
    a path the agent had aimed somewhere outside the worktree."""
    secret = tmp_path / "outside.txt"
    secret.write_text("conventions\n")

    installed.write("AGENTS.md", "conventions\n")
    target = installed.worktree / "AGENTS.md"
    target.unlink()
    target.symlink_to(secret)

    assert not installed.owns("AGENTS.md")


def test_a_directory_where_a_file_was_is_not_ours(installed: Installed) -> None:
    installed.write("AGENTS.md", "conventions\n")
    target = installed.worktree / "AGENTS.md"
    target.unlink()
    target.mkdir()
    assert not installed.owns("AGENTS.md")


def test_a_file_too_large_to_compare_is_not_ours(installed: Installed) -> None:
    """The cap is a guard, not a speed-up: without it, deciding whether a path
    is ours means reading whatever an agent decided to write there into the
    control plane's memory. Refused on size alone, so the digest is never even
    reached — hence a record whose digest is deliberately wrong."""
    size = MAX_COMPARE_BYTES + 1
    installed.records["big.bin"] = Record(size=size, digest="never compared")
    (installed.worktree / "big.bin").write_bytes(b"\0" * size)
    assert not installed.owns("big.bin")


def test_paths_come_back_in_a_stable_order(installed: Installed) -> None:
    installed.write("z.md", "z")
    installed.write("a.md", "a")
    assert installed.paths() == ("a.md", "z.md")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a file with no permission bits")
def test_a_file_that_cannot_be_read_is_not_ours(installed: Installed) -> None:
    """Reached from cleanup and from collection, both of which run after a
    process that was allowed to do whatever it liked to this tree. Raising there
    would replace a real outcome with a plumbing one."""
    installed.write("AGENTS.md", "conventions\n")
    (installed.worktree / "AGENTS.md").chmod(0o000)
    try:
        assert not installed.owns("AGENTS.md")
    finally:
        (installed.worktree / "AGENTS.md").chmod(0o600)
