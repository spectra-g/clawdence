"""Branches, pull requests, merges.

The whole reason this is a port and not a `git` wrapper is the last operation.
Everything up to ``open_pull_request`` is mechanical; ``merge`` is where the
system's central correctness property lives, so the interface is shaped to make
that property hard to get wrong rather than to make the call convenient.

**Merging states what you verified.** ``merge`` requires ``expect_head`` and
``expect_base``, and refuses with ``StaleMergeError`` if either has moved.
Verification evidence is bound to a tree hash (``domain.verification``), and the
failure being prevented is specific: story tests pass at commit X, a conflict
forces a rebase, the PR's head becomes Y, and an auto-merge lands a tree that
nothing ever ran a test against. v1 merged on "checks are green" and had no way
to notice that the checks were green for a different tree.

Making both parameters required rather than optional is the whole trick. An
optional safety check is one a caller under deadline pressure omits, and it
reads as a reasonable diff. A required one means the caller has to produce the
two hashes, which means it has to have looked at its evidence.

**Opening a pull request is idempotent on the branch.** A retried step must not
open a second PR for the same work — v1's ``_EmptyPRError`` cleanup existed
partly because duplicates were routine. The branch is the identity; asking twice
returns the same PR.

``head`` is a read against the remote's current truth and is deliberately not
cached. "What is on main right now" is the question every merge decision starts
from, and a stale answer is the same bug as a stale evidence binding.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime

from clawdence.domain import DomainModel
from clawdence.domain.ids import RepoId, TreeHash, WorkItemId
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.errors import PermanentError


class PullRequestState(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class MergeMethod(StrEnum):
    """How a merge is performed.

    ``SQUASH`` is the default everywhere it is offered, because a squashed merge
    produces one commit whose tree is the tree that was verified. A merge commit
    produces a tree that is the *result* of combining two, which no test ran
    against — the same invalidation the ``expect_*`` arguments exist to catch,
    arriving one step later.
    """

    SQUASH = "squash"
    MERGE = "merge"
    REBASE = "rebase"


class StaleMergeError(PermanentError):
    """The branch moved between verification and merge.

    Permanent, not transient: retrying merges the same wrong tree. The caller
    re-verifies against the new head and tries again with fresh hashes, which
    is a decision, not a retry.
    """

    def __init__(self, *, what: str, expected: str, actual: str) -> None:
        super().__init__(
            "stale-merge",
            f"{what} was {expected} when this was verified and is now {actual}; "
            f"the evidence does not cover the tree that would be merged",
        )
        self.what = what
        self.expected = expected
        self.actual = actual


class Branch(DomainModel):
    """A named ref and what it points at."""

    repo_id: RepoId
    name: str
    head: TreeHash


class PullRequest(DomainModel):
    """A proposed change, as the control plane sees it."""

    repo_id: RepoId

    #: The forge's own identifier. A number on GitHub; opaque here.
    number: int

    title: str
    state: PullRequestState = PullRequestState.OPEN

    head_branch: str
    base_branch: str

    #: What the PR would merge, and what it would merge into. Both are compared
    #: against the caller's evidence before a merge is allowed.
    head_commit: TreeHash
    base_commit: TreeHash

    url: str | None = None
    work_item_id: WorkItemId | None = None

    #: Set once merged. The commit that landed — which for a squash is a tree
    #: nobody has seen before, and is therefore what any post-merge check runs
    #: against.
    merge_commit: TreeHash | None = None

    created_at: AwareDatetime
    updated_at: AwareDatetime


class VcsPort(Protocol):
    """Version control, as much of it as the pipeline needs."""

    async def head(self, repo_id: RepoId, ref: str) -> TreeHash:
        """Resolve a ref to a commit. Raises ``PermanentError`` if unknown."""
        ...

    async def create_branch(self, repo_id: RepoId, name: str, *, from_commit: TreeHash) -> Branch:
        """Create a branch. Creating one that exists at the same commit is a
        no-op; at a different commit it is a ``PermanentError`` — silently
        moving someone else's branch is not a thing this should be able to do."""
        ...

    async def push(
        self,
        repo_id: RepoId,
        branch: str,
        *,
        worktree_path: str,
        expect_commit: TreeHash,
    ) -> Branch:
        """Publish work from a worktree.

        ``expect_commit`` is the hash the runner reported producing. The adapter
        pushes and then confirms the remote agrees — the runner's report is
        output from the data plane, and the control plane does not act on it
        without checking (``domain.runner``).
        """
        ...

    async def open_pull_request(
        self,
        repo_id: RepoId,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        work_item_id: WorkItemId | None = None,
    ) -> PullRequest:
        """Open a PR, or return the one already open for this branch."""
        ...

    async def get_pull_request(self, repo_id: RepoId, number: int) -> PullRequest | None:
        """Current state of a PR, fetched fresh."""
        ...

    async def merge(
        self,
        repo_id: RepoId,
        number: int,
        *,
        expect_head: TreeHash,
        expect_base: TreeHash,
        method: MergeMethod = MergeMethod.SQUASH,
    ) -> PullRequest:
        """Merge, if and only if nothing moved since it was verified.

        Raises ``StaleMergeError`` when either hash no longer matches. Merging
        an already-merged PR returns it unchanged, so a retry after a lost
        response does not fail a run that actually succeeded — its *head* is
        still checked, because a changed head means different work was merged,
        but its base is not, because merging is what moved the base.
        """
        ...


class InMemoryVcs:
    """A repository model with branches and pull requests. The fake.

    Commits are synthetic 40-hex ids from a counter rather than real hashes:
    what the pipeline cares about is that a hash changes when the branch moves,
    and a counter demonstrates that as well as SHA-1 does while keeping the fake
    free of a git dependency. The *real* repo fixtures, with real hashes, are
    the test harness's job — this is for testing the pipeline, not git.
    """

    __slots__ = ("_branches", "_clock", "_commits", "_fail_with", "_pulls")

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._branches: dict[tuple[str, str], str] = {}
        self._pulls: dict[tuple[str, int], PullRequest] = {}
        self._commits = 0
        self._fail_with: BaseException | None = None

    # -- test-side controls, not part of the port ------------------------------

    def seed(self, repo_id: RepoId, branch: str = "main") -> TreeHash:
        """Create a repo with one branch, and return its head."""
        commit = self.commit()
        self._branches[(repo_id, branch)] = commit
        return commit

    def commit(self) -> TreeHash:
        """Mint a new commit id, as a runner producing work would."""
        self._commits += 1
        return f"{self._commits:040x}"

    def advance(self, repo_id: RepoId, branch: str) -> TreeHash:
        """Move a branch on, as someone else merging to main would."""
        commit = self.commit()
        self._branches[(repo_id, branch)] = commit
        return commit

    def close(self, repo_id: RepoId, number: int) -> None:
        """Close a pull request, as a human clicking the button would.

        A test-side control rather than a port method for the same reason
        ``advance`` is one: this state arrives from *outside* the system, and
        what the port has to do is cope with finding it. Closing a PR from
        inside is cancellation, which is S17b's, and it can add a real method
        when it needs one.
        """
        pull = self._pulls[(repo_id, number)]
        self._pulls[(repo_id, number)] = pull.model_copy(
            update={"state": PullRequestState.CLOSED, "updated_at": self._clock()}
        )

    def fail_with(self, error: BaseException | None) -> None:
        self._fail_with = error

    # -- the port -------------------------------------------------------------

    def _check(self) -> None:
        if self._fail_with is not None:
            raise self._fail_with

    async def head(self, repo_id: RepoId, ref: str) -> TreeHash:
        self._check()
        commit = self._branches.get((repo_id, ref))
        if commit is None:
            raise PermanentError("unknown-ref", f"{repo_id} has no ref {ref!r}")
        return commit

    async def create_branch(self, repo_id: RepoId, name: str, *, from_commit: TreeHash) -> Branch:
        self._check()
        existing = self._branches.get((repo_id, name))
        if existing is not None and existing != from_commit:
            raise PermanentError(
                "branch-exists",
                f"{repo_id} already has a branch {name!r}, at {existing} rather than {from_commit}",
            )
        self._branches[(repo_id, name)] = from_commit
        return Branch(repo_id=repo_id, name=name, head=from_commit)

    async def push(
        self,
        repo_id: RepoId,
        branch: str,
        *,
        worktree_path: str,
        expect_commit: TreeHash,
    ) -> Branch:
        self._check()
        if (repo_id, branch) not in self._branches:
            raise PermanentError("unknown-ref", f"{repo_id} has no branch {branch!r} to push to")
        self._branches[(repo_id, branch)] = expect_commit

        # A PR already open for this branch now proposes a different tree. The
        # fake tracks that because it is exactly the state that makes a later
        # merge stale, and a fake that quietly kept the old hash would let the
        # merge-safety tests pass without the check doing anything.
        for key, pull in tuple(self._pulls.items()):
            if pull.repo_id == repo_id and pull.head_branch == branch:
                self._pulls[key] = pull.model_copy(
                    update={"head_commit": expect_commit, "updated_at": self._clock()}
                )
        return Branch(repo_id=repo_id, name=branch, head=expect_commit)

    async def open_pull_request(
        self,
        repo_id: RepoId,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        work_item_id: WorkItemId | None = None,
    ) -> PullRequest:
        self._check()
        for pull in self._pulls.values():
            if (
                pull.repo_id == repo_id
                and pull.head_branch == head_branch
                and pull.state is PullRequestState.OPEN
            ):
                return pull

        head_commit = await self.head(repo_id, head_branch)
        base_commit = await self.head(repo_id, base_branch)
        now = self._clock()
        number = len(self._pulls) + 1
        pull = PullRequest(
            repo_id=repo_id,
            number=number,
            title=title,
            head_branch=head_branch,
            base_branch=base_branch,
            head_commit=head_commit,
            base_commit=base_commit,
            url=f"https://forge.invalid/{repo_id}/pull/{number}",
            work_item_id=work_item_id,
            created_at=now,
            updated_at=now,
        )
        self._pulls[(repo_id, number)] = pull
        return pull

    async def get_pull_request(self, repo_id: RepoId, number: int) -> PullRequest | None:
        self._check()
        pull = self._pulls.get((repo_id, number))
        if pull is None:
            return None
        # The base branch moves without anything touching the PR — someone else
        # merging is the ordinary case — so the base is re-read on every fetch.
        base = self._branches.get((repo_id, pull.base_branch), pull.base_commit)
        return pull if base == pull.base_commit else pull.model_copy(update={"base_commit": base})

    async def merge(
        self,
        repo_id: RepoId,
        number: int,
        *,
        expect_head: TreeHash,
        expect_base: TreeHash,
        method: MergeMethod = MergeMethod.SQUASH,
    ) -> PullRequest:
        self._check()
        pull = await self.get_pull_request(repo_id, number)
        if pull is None:
            raise PermanentError("unknown-pull-request", f"{repo_id} has no pull request {number}")

        # Head first, and it applies to a merged pull request too: a different
        # head means somebody merged different work, whatever its state is.
        if pull.head_commit != expect_head:
            raise StaleMergeError(
                what="the pull request head", expected=expect_head, actual=pull.head_commit
            )

        # Checked before the base, because merging moves the base — so a caller
        # retrying after a lost response necessarily holds the hash from before
        # its own merge. Asking "would this merge cleanly" about something
        # already merged is the wrong question, and answering it with
        # ``StaleMergeError`` would fail a run that in fact succeeded.
        if pull.state is PullRequestState.MERGED:
            return pull

        if pull.base_commit != expect_base:
            raise StaleMergeError(
                what=f"the base branch {pull.base_branch!r}",
                expected=expect_base,
                actual=pull.base_commit,
            )
        if pull.state is PullRequestState.CLOSED:
            raise PermanentError("pull-request-closed", f"pull request {number} was closed")

        merge_commit = self.commit()
        self._branches[(repo_id, pull.base_branch)] = merge_commit
        merged = pull.model_copy(
            update={
                "state": PullRequestState.MERGED,
                "merge_commit": merge_commit,
                "updated_at": self._clock(),
            }
        )
        self._pulls[(repo_id, number)] = merged
        return merged

    @property
    def pull_requests(self) -> Sequence[PullRequest]:
        return tuple(self._pulls.values())
