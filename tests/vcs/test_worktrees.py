"""Checkouts: the layout, the concurrency, and what release is allowed to delete."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from clawdence.domain import CheckoutPolicy, RepoProfile
from clawdence.ports.errors import PermanentError
from clawdence.runners import worktree as wt
from clawdence.vcs import NoSpaceError, RepoStore, Worktree, WorktreeManager
from clawdence.vcs.git import git
from tests.ports.factories import run
from tests.vcs.conftest import ProfileFactory


def commit(worktree: Worktree, name: str = "new.py", text: str = "print('hi')\n") -> str:
    """Write a file and commit it, as a runner producing work would."""
    (worktree.path / name).write_text(text, encoding="utf-8")
    head = run(wt.commit_all(worktree.path, "the agent's work"))
    assert head is not None
    return head


def test_a_worktree_is_a_real_checkout_on_its_own_branch(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42", title="Add a bit"))
    assert (lease.path / "app.py").exists()
    assert lease.branch == "clawdence/wi-42-add-a-bit"
    assert run(wt.head(lease.path)) == lease.base_commit
    assert run(git(lease.path, "rev-parse", "--abbrev-ref", "HEAD")) == lease.branch


def test_the_run_id_is_the_first_component_under_the_work_root(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    """Not decoration: ``runners.reaper`` sweeps one level under the work root
    and protects a child whose name is a live run id. Any other layout makes
    every sweep a decision about a directory name that means nothing to it."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    assert lease.path.parent.name == "run.1"
    assert lease.path.parent.parent == worktrees.work_root


def test_the_object_store_is_not_under_the_work_root(
    worktrees: WorktreeManager, store: RepoStore, profile: RepoProfile
) -> None:
    """A mirror there would be a directory whose name is not a run id, which is
    the reaper's definition of debris — deleted after a week of quiet, taking
    every repository's history with it."""
    run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    assert store.root not in worktrees.work_root.parents
    assert worktrees.work_root not in store.root.parents


def test_acquiring_twice_for_one_run_returns_the_same_checkout(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    """What makes a resumed run continue its own branch rather than open a second
    pull request beside it."""
    first = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    second = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    assert first == second


def test_two_runs_cannot_share_one_work_items_branch(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    """Git refuses a branch already checked out elsewhere, and that refusal is
    worth keeping: two live runs on one branch would each have the other's
    commits in their diff."""
    run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    with pytest.raises(PermanentError) as caught:
        run(worktrees.acquire(profile, run_id="run.2", work_item_id="wi.42"))
    assert caught.value.kind == "worktree-not-created"


def test_three_concurrent_worktrees_on_one_repository_do_not_corrupt_each_other(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    """S7's concurrency, against S15's shared object store. Each run gets its own
    directory and its own branch, and the commit each one makes is visible only
    on its own."""

    async def three() -> list[Worktree]:
        return list(
            await asyncio.gather(
                *(
                    worktrees.acquire(profile, run_id=f"run.{n}", work_item_id=f"wi.{n}")
                    for n in (1, 2, 3)
                )
            )
        )

    leases = run(three())
    assert len({lease.path for lease in leases}) == 3
    assert len({lease.branch for lease in leases}) == 3

    heads = {lease.branch: commit(lease, text=f"# {lease.run_id}\n") for lease in leases}
    for lease in leases:
        assert run(wt.commits_ahead(lease.path, lease.base_commit)) == 1
        assert run(git(lease.mirror, "rev-parse", lease.branch)) == heads[lease.branch]


def test_release_removes_the_checkout_and_an_untouched_branch(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    """The cancelled-run case: nothing was committed, so the ref is litter."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    assert run(worktrees.release(lease)) is True
    assert not lease.path.exists()
    assert not lease.path.parent.exists()
    assert run(git(lease.mirror, "for-each-ref", "--format=%(refname)", "refs/heads/")) == ""


def test_release_keeps_a_branch_that_holds_work(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    """Between the agent committing and the push succeeding, the local branch is
    the only copy. Deleting it on the way out would discard the thing the run was
    for, so a moved branch is left for a human."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    head = commit(lease)
    assert run(worktrees.release(lease)) is False
    assert not lease.path.exists()
    assert run(git(lease.mirror, "rev-parse", f"refs/heads/{lease.branch}")) == head


def test_release_can_be_told_to_keep_the_checkout(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    assert run(worktrees.release(lease, keep=True)) is False
    assert lease.path.exists()


def test_release_does_not_raise_when_the_directory_is_already_gone(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    """It runs in a ``finally`` after something has already decided how the run
    went. A cleanup that can fail the run it is cleaning up after turns a
    recorded failure into an unrecorded one."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    shutil.rmtree(lease.path)
    assert run(worktrees.release(lease)) is True


def test_a_reaped_directory_does_not_block_the_next_run(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    """The reaper deletes directories and knows nothing about git, which leaves
    an administrative record under ``.git/worktrees`` naming a path that is gone.
    Without a prune, git refuses to create a worktree there again — which is
    exactly what a retried run tries to do."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    shutil.rmtree(lease.path.parent)  # what runners.reaper does

    again = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    assert again.path == lease.path
    assert again.path.exists()


def test_prune_is_available_on_its_own(worktrees: WorktreeManager, profile: RepoProfile) -> None:
    """A deployment running ``clawdence reap`` on a schedule wants the git half
    of the same reconciliation on the same schedule."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    shutil.rmtree(lease.path.parent)
    run(worktrees.prune(profile))
    listed = run(git(lease.mirror, "worktree", "list", "--porcelain"))
    assert str(lease.path) not in listed


def test_a_full_disk_refuses_before_the_checkout(
    store: RepoStore, workspace: Path, profile: RepoProfile
) -> None:
    """The state database is on this filesystem too. A full disk there is not a
    failed run, it is a control plane that cannot record that the run failed."""
    manager = WorktreeManager(
        store=store, work_root=workspace / "work", min_free_mb=1024 * 1024 * 1024
    )
    with pytest.raises(NoSpaceError) as caught:
        run(manager.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    assert caught.value.retryable is True


def test_sparse_paths_are_applied_per_worktree(
    store: RepoStore, workspace: Path, profile_for: ProfileFactory
) -> None:
    """The object store is shared between concurrent runs; the set of paths one
    of them wants is not."""
    profile = profile_for(checkout=CheckoutPolicy(sparse_paths=("docs",)))
    manager = WorktreeManager(store=store, work_root=workspace / "work", min_free_mb=0)
    lease = run(manager.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    assert (lease.path / "docs" / "guide.md").exists()
    assert not (lease.path / "lib").exists()
    # Cone mode always keeps the root, which is why the assertion above is about
    # a directory: a sparse checkout that dropped the manifest would be one no
    # build system could run in.
    assert (lease.path / "app.py").exists()


def test_a_checkout_hides_the_systems_own_directory_from_git(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    """``.gitignore`` is tracked content and editing it *is* a change to the
    repository; ``info/exclude`` is local and is what this is for."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    (lease.path / ".clawdence").mkdir()
    (lease.path / ".clawdence" / "plan.md").write_text("a plan", encoding="utf-8")
    assert run(wt.pending_changes(lease.path)) == ()
