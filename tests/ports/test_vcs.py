"""The in-memory VCS against the contract, and the merge-safety edges."""

from __future__ import annotations

import pytest

from clawdence.ports import (
    InMemoryVcs,
    MergeMethod,
    PermanentError,
    PullRequestState,
    StaleMergeError,
    TransientError,
)
from clawdence.ports._common import counting_clock
from tests.ports import factories as make
from tests.ports.contract import VcsContract
from tests.ports.factories import START, run


def _vcs() -> InMemoryVcs:
    return InMemoryVcs(clock=counting_clock(START))


class TestInMemoryVcs(VcsContract):
    @pytest.fixture
    def vcs(self) -> InMemoryVcs:
        return _vcs()

    def seed(self, vcs: InMemoryVcs) -> str:  # type: ignore[override]
        return vcs.seed(self.repo_id)

    def new_commit(self, vcs: InMemoryVcs) -> str:  # type: ignore[override]
        return vcs.commit()

    def advance_main(self, vcs: InMemoryVcs) -> str:  # type: ignore[override]
        return vcs.advance(self.repo_id, "main")


def test_a_merge_moves_the_base_branch_on() -> None:
    """The next story branches from the merge commit, not from where main was
    when this one started — so the fake has to model that or every merge-safety
    test after the first would pass for the wrong reason."""
    vcs = _vcs()
    vcs.seed(make.REPO_ID)
    base = run(vcs.head(make.REPO_ID, "main"))
    run(vcs.create_branch(make.REPO_ID, "feature", from_commit=base))
    head = vcs.commit()
    run(vcs.push(make.REPO_ID, "feature", worktree_path=make.WORKTREE, expect_commit=head))
    pull = run(
        vcs.open_pull_request(
            make.REPO_ID,
            title="A change",
            body="",
            head_branch="feature",
            base_branch="main",
        )
    )
    merged = run(
        vcs.merge(
            make.REPO_ID,
            pull.number,
            expect_head=pull.head_commit,
            expect_base=pull.base_commit,
        )
    )
    assert run(vcs.head(make.REPO_ID, "main")) == merged.merge_commit
    assert merged.merge_commit != base


def test_pushing_updates_an_open_pull_request() -> None:
    """A fake that quietly kept the old head would let the merge-safety tests
    pass with the check doing nothing."""
    vcs = _vcs()
    base = vcs.seed(make.REPO_ID)
    run(vcs.create_branch(make.REPO_ID, "feature", from_commit=base))
    run(vcs.push(make.REPO_ID, "feature", worktree_path=make.WORKTREE, expect_commit=vcs.commit()))
    pull = run(
        vcs.open_pull_request(
            make.REPO_ID, title="A change", body="", head_branch="feature", base_branch="main"
        )
    )

    moved = vcs.commit()
    run(vcs.push(make.REPO_ID, "feature", worktree_path=make.WORKTREE, expect_commit=moved))
    refetched = run(vcs.get_pull_request(make.REPO_ID, pull.number))
    assert refetched is not None
    assert refetched.head_commit == moved


def test_pushing_to_a_branch_that_does_not_exist_is_permanent() -> None:
    vcs = _vcs()
    vcs.seed(make.REPO_ID)
    with pytest.raises(PermanentError):
        run(
            vcs.push(
                make.REPO_ID,
                "never-created",
                worktree_path=make.WORKTREE,
                expect_commit=vcs.commit(),
            )
        )


def test_merging_something_that_does_not_exist_is_permanent() -> None:
    vcs = _vcs()
    vcs.seed(make.REPO_ID)
    with pytest.raises(PermanentError):
        run(
            vcs.merge(
                make.REPO_ID,
                404,
                expect_head=make.commit(1),
                expect_base=make.commit(2),
            )
        )


def test_a_stale_merge_names_which_hash_moved() -> None:
    """The operator reading this has to know whether to re-verify the branch or
    to rebase it, and those are different actions."""
    vcs = _vcs()
    base = vcs.seed(make.REPO_ID)
    run(vcs.create_branch(make.REPO_ID, "feature", from_commit=base))
    head = vcs.commit()
    run(vcs.push(make.REPO_ID, "feature", worktree_path=make.WORKTREE, expect_commit=head))
    pull = run(
        vcs.open_pull_request(
            make.REPO_ID, title="A change", body="", head_branch="feature", base_branch="main"
        )
    )
    advanced = vcs.advance(make.REPO_ID, "main")

    with pytest.raises(StaleMergeError) as caught:
        run(
            vcs.merge(
                make.REPO_ID,
                pull.number,
                expect_head=pull.head_commit,
                expect_base=pull.base_commit,
            )
        )
    assert "main" in caught.value.what
    assert caught.value.actual == advanced
    assert caught.value.kind == "stale-merge"


def test_merge_methods_are_declared() -> None:
    """``SQUASH`` by default: a squashed merge lands the tree that was verified,
    where a merge commit lands the *result* of combining two, which nothing ran
    a test against."""
    assert MergeMethod.SQUASH.value == "squash"
    vcs = _vcs()
    base = vcs.seed(make.REPO_ID)
    run(vcs.create_branch(make.REPO_ID, "feature", from_commit=base))
    head = vcs.commit()
    run(vcs.push(make.REPO_ID, "feature", worktree_path=make.WORKTREE, expect_commit=head))
    pull = run(
        vcs.open_pull_request(
            make.REPO_ID, title="A change", body="", head_branch="feature", base_branch="main"
        )
    )
    merged = run(
        vcs.merge(
            make.REPO_ID,
            pull.number,
            expect_head=pull.head_commit,
            expect_base=pull.base_commit,
            method=MergeMethod.MERGE,
        )
    )
    assert merged.state is PullRequestState.MERGED


def test_merging_a_closed_pull_request_is_refused() -> None:
    """Somebody closed it in the UI while the run was verifying. Permanent:
    reopening is a human's decision, not something a retry can reach."""
    vcs = _vcs()
    base = vcs.seed(make.REPO_ID)
    run(vcs.create_branch(make.REPO_ID, "feature", from_commit=base))
    head = vcs.commit()
    run(vcs.push(make.REPO_ID, "feature", worktree_path=make.WORKTREE, expect_commit=head))
    pull = run(
        vcs.open_pull_request(
            make.REPO_ID, title="A change", body="", head_branch="feature", base_branch="main"
        )
    )
    vcs.close(make.REPO_ID, pull.number)

    with pytest.raises(PermanentError) as caught:
        run(
            vcs.merge(
                make.REPO_ID,
                pull.number,
                expect_head=pull.head_commit,
                expect_base=pull.base_commit,
            )
        )
    assert caught.value.kind == "pull-request-closed"


def test_the_fake_can_be_taken_down() -> None:
    vcs = _vcs()
    vcs.seed(make.REPO_ID)
    vcs.fail_with(TransientError("unavailable", "502"))
    with pytest.raises(TransientError):
        run(vcs.head(make.REPO_ID, "main"))


def test_pull_requests_are_listed() -> None:
    vcs = _vcs()
    base = vcs.seed(make.REPO_ID)
    for name in ("one", "two"):
        run(vcs.create_branch(make.REPO_ID, name, from_commit=base))
        run(vcs.push(make.REPO_ID, name, worktree_path=make.WORKTREE, expect_commit=vcs.commit()))
        run(
            vcs.open_pull_request(
                make.REPO_ID, title=name, body="", head_branch=name, base_branch="main"
            )
        )
    assert [pull.head_branch for pull in vcs.pull_requests] == ["one", "two"]
