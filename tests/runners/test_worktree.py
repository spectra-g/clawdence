"""Git against a real repository — hashes, diffs, and what git is told to ignore."""

from __future__ import annotations

import pytest

from clawdence.runners import worktree as wt
from tests.harness.repos import FixtureRepo
from tests.ports.factories import run


def test_a_fixture_repo_is_a_repository(repo: FixtureRepo) -> None:
    assert run(wt.is_repository(repo.path)) is True
    assert run(wt.head(repo.path)) == repo.head


def test_a_plain_directory_is_not(tmp_path: object, repo: FixtureRepo) -> None:
    outside = repo.path.parent / "not-a-repo"
    outside.mkdir()
    assert run(wt.is_repository(outside)) is False


def test_a_commit_is_present_or_it_is_not(repo: FixtureRepo) -> None:
    assert run(wt.has_commit(repo.path, repo.head)) is True
    assert run(wt.has_commit(repo.path, "0" * 40)) is False


def test_a_tree_object_is_not_a_commit(repo: FixtureRepo) -> None:
    """``base_commit`` names a commit. A tree with the right shape of hash is
    not one, and diffing against it later would fail in a less obvious place."""
    tree = run(wt.git(repo.path, "rev-parse", "HEAD^{tree}"))
    assert run(wt.has_commit(repo.path, tree)) is False


def test_pending_changes_lists_what_would_be_committed(repo: FixtureRepo) -> None:
    assert run(wt.pending_changes(repo.path)) == ()
    repo.write("app.py", "changed\n")
    repo.write("new/thing.py", "added\n")
    assert set(run(wt.pending_changes(repo.path))) == {"app.py", "new/thing.py"}


def test_commit_all_moves_the_head(repo: FixtureRepo) -> None:
    repo.write("app.py", "changed\n")
    committed = run(wt.commit_all(repo.path, "work"))
    assert committed is not None
    assert committed != repo.head
    assert run(wt.head(repo.path)) == committed


def test_committing_a_clean_tree_does_nothing(repo: FixtureRepo) -> None:
    """The common case once the agent commits its own work: there is nothing
    left, and that is not a failure."""
    assert run(wt.commit_all(repo.path, "work")) is None
    assert run(wt.head(repo.path)) == repo.head


def test_the_committer_is_the_runner_not_the_operator(repo: FixtureRepo) -> None:
    """A commit attributed to a human who did not write it is a lie in the one
    place a repository keeps permanently."""
    repo.write("app.py", "changed\n")
    run(wt.commit_all(repo.path, "work", identity=wt.GitIdentity("Someone", "s@example.invalid")))
    assert (
        run(wt.git(repo.path, "log", "-1", "--format=%an <%ae>")) == "Someone <s@example.invalid>"
    )


def test_diff_stat_counts_lines(repo: FixtureRepo) -> None:
    repo.write("app.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    run(wt.commit_all(repo.path, "work"))
    stat = run(wt.diff_stat(repo.path, repo.head))
    assert (stat.files_changed, stat.insertions, stat.deletions) == (1, 4, 0)


def test_diff_stat_of_an_unchanged_tree_is_empty(repo: FixtureRepo) -> None:
    """Which is what makes ``EMPTY_DIFF`` a real outcome rather than a guess."""
    stat = run(wt.diff_stat(repo.path, repo.head))
    assert stat.files_changed == 0


def test_a_binary_file_counts_as_a_changed_file(repo: FixtureRepo) -> None:
    """``--numstat`` reports ``-`` for both counts. One file changed, and lines
    are not a meaningful unit for it."""
    (repo.path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")
    run(wt.commit_all(repo.path, "work"))
    stat = run(wt.diff_stat(repo.path, repo.head))
    assert (stat.files_changed, stat.insertions, stat.deletions) == (1, 0, 0)


def test_exclude_hides_a_path_from_a_commit(repo: FixtureRepo) -> None:
    """The property the runner depends on: everything it installs is invisible
    to ``git add --all``, so nothing it wrote reaches a pull request."""
    run(wt.exclude(repo.path, "/.clawdence/"))
    repo.write(".clawdence/verdict.json", '{"status": "passed"}')
    repo.write("app.py", "changed\n")

    assert run(wt.pending_changes(repo.path)) == ("app.py",)
    run(wt.commit_all(repo.path, "work"))
    stat = run(wt.diff_stat(repo.path, repo.head))
    assert stat.files_changed == 1


def test_exclude_does_not_touch_the_repositorys_own_gitignore(repo: FixtureRepo) -> None:
    """Editing ``.gitignore`` *is* a change to the repository. The exclude file
    is local and untracked, which is the entire reason it is used."""
    run(wt.exclude(repo.path, "/.clawdence/"))
    assert run(wt.pending_changes(repo.path)) == ()


def test_excluding_twice_does_not_repeat_the_entry(repo: FixtureRepo) -> None:
    """A runner is dispatched many times against one worktree; an exclude file
    that grew a line per attempt would be a slow leak nobody looks at."""
    run(wt.exclude(repo.path, "/.clawdence/", "/AGENTS.md"))
    run(wt.exclude(repo.path, "/.clawdence/", "/AGENTS.md"))
    path = repo.path / ".git" / "info" / "exclude"
    assert path.read_text(encoding="utf-8").count("/.clawdence/") == 1


def test_exclude_works_in_a_linked_worktree(repo: FixtureRepo) -> None:
    """Which is the shape the runner actually runs in: N worktrees against one
    repository. In a linked worktree ``.git`` is a *file*, and the real exclude
    file lives under the main repository — so the path is asked for rather than
    assembled."""
    linked = repo.path.parent / "linked"
    run(wt.git(repo.path, "worktree", "add", "-b", "feature", str(linked)))

    run(wt.exclude(linked, "/.clawdence/"))
    (linked / ".clawdence").mkdir()
    (linked / ".clawdence" / "verdict.json").write_text("{}", encoding="utf-8")

    assert run(wt.pending_changes(linked)) == ()


def test_a_rename_is_one_pending_path_not_two(repo: FixtureRepo) -> None:
    """A rename emits the destination and then the source, and counting both
    would report twice as much pending work as there is."""
    run(wt.git(repo.path, "mv", "app.py", "core.py"))
    assert run(wt.pending_changes(repo.path)) == ("core.py",)


def test_a_failing_command_says_what_git_said(repo: FixtureRepo) -> None:
    with pytest.raises(wt.GitError) as caught:
        run(wt.git(repo.path, "rev-parse", "does-not-exist"))
    assert "rev-parse" in str(caught.value)


def test_the_operators_git_config_is_ignored(
    repo: FixtureRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment is replaced, not extended.

    A maintainer's global config reaching a runner is how a suite ends up
    blocked on a gpg passphrase nobody is watching for, or how a commit gets
    attributed to whoever happened to be logged in.
    """
    ambient = repo.path.parent / "gitconfig"
    ambient.write_text("[user]\n\tname = Ambient\n\temail = ambient@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(ambient))

    repo.write("app.py", "changed\n")
    run(wt.commit_all(repo.path, "work"))
    assert "Ambient" not in run(wt.git(repo.path, "log", "-1", "--format=%an <%ae>"))


def test_a_filename_with_a_newline_is_one_path(repo: FixtureRepo) -> None:
    """An agent can create a file called whatever it likes. Line-splitting
    git's output turns one such file into two phantom paths."""
    repo.write("odd\nname.txt", "surprise\n")
    assert run(wt.pending_changes(repo.path)) == ("odd\nname.txt",)
