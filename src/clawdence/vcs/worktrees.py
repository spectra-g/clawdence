"""Checkouts: one per run, given out and taken back.

``RepoStore`` owns the objects; this owns the directories work happens in. The
layout is fixed by something that already exists rather than chosen here — the
reaper (``runners.reaper``) sweeps *one level* under the work root and protects a
child whose name is a live run id, so a worktree lives at::

    <work-root>/<run-id>/<repo-slug>

and the mirrors deliberately live somewhere else. A shared object store under
the work root would be a directory whose name is not a run id, which is the
reaper's definition of debris; it would be deleted after a week of quiet, taking
every repository's history with it.

**Every acquire is matched by a release, and release is the interesting half.**
``git worktree remove`` is not ``rm -rf``: the repository holds an administrative
record under ``.git/worktrees/<name>`` naming the directory, and deleting the
directory without telling git leaves that record behind. Git then refuses to
create a worktree at the same path again — which is exactly what a retried run
tries to do — with a message about the path being already registered. The reaper
knows nothing about git and never will, so ``prune`` exists to reconcile the two,
and it runs at acquire time as well as at release: the state it repairs is
created by processes that died, and a dead process does not call anything.

**A branch is removed only if it never moved.** "A cancelled run leaves no
orphaned worktree or branch" is the verify, and the naive reading — delete the
branch on the way out — destroys work. Between the agent committing and the push
succeeding, the local branch is the only copy of the run's output. So release
compares the branch against the base commit it was cut from: unchanged means
nothing was committed and the ref is litter, and anything else is left for a
human, because the alternative is silently discarding the thing the run was for.

**Disk is checked before a checkout, not after.** The control plane's SQLite
state store is on the same filesystem the worktrees are on. A full disk there is
not a failed run, it is a control plane that cannot record that the run failed —
so the refusal happens while there is still room to write it down.
"""

from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from clawdence.domain import RepoProfile
from clawdence.ports.errors import PermanentError, TransientError
from clawdence.vcs import refs
from clawdence.vcs.git import GitError, exclude
from clawdence.vcs.store import RepoStore

#: Free space a checkout requires before it is attempted. Not a repository size
#: estimate — that would need a network round trip to answer badly. It is a floor
#: under the filesystem the state store is also on.
DEFAULT_MIN_FREE_MB: Final = 2048

#: Patterns added to every worktree's ``info/exclude``. The runner adds its own
#: (a plan, a verdict, a conventions file); these are the ones that are true of
#: every checkout regardless of what runs in it.
_ALWAYS_EXCLUDED: Final[tuple[str, ...]] = (".clawdence/",)


class NoSpaceError(TransientError):
    """The filesystem holding worktrees is too full to check one out.

    Transient because the fix is somebody else's run finishing or the reaper
    sweeping, both of which happen without a human. It is still loud: a run that
    waits is visible, and a run that half-checked-out a repository is not.
    """

    def __init__(self, root: Path, free_mb: int, required_mb: int) -> None:
        super().__init__(
            "no-space",
            f"{root} has {free_mb} MB free and a checkout needs {required_mb} MB; "
            f"the state database is on this filesystem too, so this refuses before "
            f"there is no room left to record why",
        )


@dataclass(frozen=True, slots=True)
class Worktree:
    """A checkout, its branch, and what that branch was cut from.

    These three are exactly what ``runners.Dispatch`` needs and could not be
    given before this step — S6's handler took them as data precisely so that
    nothing in the runner had to invent them.
    """

    run_id: str
    repo_id: str
    path: Path
    branch: str
    base_commit: str

    #: The mirror this is linked to. Held so ``release`` does not have to
    #: reconstruct it from a profile the caller may no longer have.
    mirror: Path


@dataclass(slots=True)
class WorktreeManager:
    """Hands out checkouts and takes them back."""

    store: RepoStore
    work_root: Path
    min_free_mb: int = DEFAULT_MIN_FREE_MB

    #: Set to keep worktrees after a failed run, for a human to look at. Off by
    #: default because the thing being kept is a full checkout per failure and
    #: the disk is finite; the reaper's retention is the safety net either way.
    keep_on_failure: bool = False

    _leased: dict[str, Worktree] = field(default_factory=dict, init=False, repr=False)

    async def acquire(
        self,
        profile: RepoProfile,
        *,
        run_id: str,
        work_item_id: str,
        title: str | None = None,
        base_ref: str | None = None,
        fetch: bool = True,
    ) -> Worktree:
        """A checkout of ``profile`` on this work item's branch.

        Idempotent for a run that is retrying: the branch name is a function of
        the work item, the directory is a function of the run, and asking twice
        for the same pair returns the same worktree rather than failing on a
        directory that exists. That is what makes a resumed run continue its own
        branch instead of opening a second pull request beside it.
        """
        mirror = await self.store.ensure(profile, fetch=fetch)
        base = await self.store.resolve(profile, base_ref or profile.default_branch)
        branch = refs.branch_for(work_item_id, title, prefix=profile.branch_prefix)
        path = self.path_for(run_id, profile)

        existing = self._leased.get(_key(run_id, profile.id))
        if existing is not None and existing.path.exists():
            return existing

        self._check_space()
        async with self.store.locked(profile):
            # Before, not only after: the records this repairs were left by
            # processes that are gone, so nothing else is going to.
            await self._prune(mirror)
            await self._add(profile, mirror, path, branch=branch, base=base)

        lease = Worktree(
            run_id=run_id,
            repo_id=profile.id,
            path=path,
            branch=branch,
            base_commit=base,
            mirror=mirror,
        )
        self._leased[_key(run_id, profile.id)] = lease
        return lease

    async def release(self, worktree: Worktree, *, keep: bool | None = None) -> bool:
        """Give a checkout back. Returns whether the branch was removed too.

        Never raises. This is called from the ``finally`` of something that has
        already decided how the run went, and a cleanup that can fail the run it
        is cleaning up after turns a recorded failure into an unrecorded one.
        What it cannot do quietly it leaves for the reaper, which is what the
        reaper is for.
        """
        self._leased.pop(_key(worktree.run_id, worktree.repo_id), None)
        if keep if keep is not None else self.keep_on_failure:
            return False

        moved = await self._branch_moved(worktree)
        with contextlib.suppress(GitError, OSError):
            await self.store.git(
                worktree.mirror, "worktree", "remove", "--force", str(worktree.path)
            )
        # The directory may survive a failed remove — a mount, a permission, a
        # file an editor still has open. Take it directly, then reconcile git's
        # records with what is actually on disk.
        if worktree.path.exists():
            shutil.rmtree(worktree.path, ignore_errors=True)
        await self._prune(worktree.mirror)

        # The run directory is per run and holds nothing else at M1; removing it
        # empty keeps the work root a list of live runs rather than a graveyard
        # of directory skeletons the reaper will visit for a week.
        with contextlib.suppress(OSError):
            worktree.path.parent.rmdir()

        if moved:
            return False
        with contextlib.suppress(GitError, OSError):
            await self.store.git(worktree.mirror, "branch", "--delete", "--force", worktree.branch)
        return True

    def path_for(self, run_id: str, profile: RepoProfile) -> Path:
        """Where this run's checkout of this repository goes.

        The run id is the *first* component because that is the level the reaper
        sweeps and the level it matches against live runs. Putting the repository
        first would make every sweep a decision about a directory name that means
        nothing to it.
        """
        return self.work_root / run_id / (refs.slugify(profile.name) or "repo")

    async def prune(self, profile: RepoProfile) -> None:
        """Discard git's records of worktrees whose directories are gone.

        Public because the reaper deletes directories and this is the other half
        of that; a deployment running ``clawdence reap`` on a schedule wants this
        on the same schedule.
        """
        async with self.store.locked(profile):
            await self._prune(self.store.mirror(profile))

    # ---------------------------------------------------------------- plumbing

    async def _add(
        self, profile: RepoProfile, mirror: Path, path: Path, *, branch: str, base: str
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = await self._branch_exists(mirror, branch)
        try:
            # ``--force`` is absent deliberately. Without it git refuses when the
            # branch is checked out in *another* worktree, and that refusal is
            # the one worth keeping: two live runs sharing a branch would have
            # each other's commits in their diffs.
            if existing:
                await self.store.remote_git(profile, mirror, "worktree", "add", str(path), branch)
            else:
                await self.store.remote_git(
                    profile, mirror, "worktree", "add", "-b", branch, str(path), base
                )
        except (GitError, OSError) as exc:
            raise PermanentError(
                "worktree-not-created",
                f"could not check {profile.id} out at {path} on {branch!r}: {exc}",
            ) from exc

        if profile.checkout.sparse_paths:
            await self.store.remote_git(
                profile,
                path,
                "sparse-checkout",
                "set",
                "--cone",
                "--",
                *profile.checkout.sparse_paths,
            )
        with contextlib.suppress(GitError, OSError):
            await exclude(path, *_ALWAYS_EXCLUDED)

    async def _branch_exists(self, mirror: Path, branch: str) -> bool:
        try:
            await self.store.git(mirror, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        except (GitError, OSError):
            return False
        return True

    async def _branch_moved(self, worktree: Worktree) -> bool:
        """Whether anything was committed on this branch. Unknown counts as yes.

        The conservative direction is the only defensible one: a wrong "it moved"
        leaves a ref behind for the reaper, and a wrong "it did not" deletes the
        only copy of a run's work.
        """
        try:
            head = await self.store.git(
                worktree.mirror, "rev-parse", "--verify", f"refs/heads/{worktree.branch}"
            )
        except (GitError, OSError):
            return True
        return head != worktree.base_commit

    async def _prune(self, mirror: Path) -> None:
        with contextlib.suppress(GitError, OSError):
            await self.store.git(mirror, "worktree", "prune")

    def _check_space(self) -> None:
        self.work_root.mkdir(parents=True, exist_ok=True)
        free_mb = shutil.disk_usage(self.work_root).free // (1024 * 1024)
        if free_mb < self.min_free_mb:
            raise NoSpaceError(self.work_root, free_mb, self.min_free_mb)


def _key(run_id: str, repo_id: str) -> str:
    return f"{run_id}\0{repo_id}"
