"""S15's verify, end to end: a fixture repo in, a real pull request out.

Every piece here is the product's — the store, the worktree manager, the diff
audit, the adapter — and the only thing standing in for a human is the function
that writes a file into the worktree, which is what a runner would have done.

The sequence is the one a pipeline will follow when S11 exists to start it:
check out, produce work, audit the diff, publish the branch, open the pull
request, merge it if the hashes still match. Reading it top to bottom is the
clearest available answer to "what did S15 actually build".
"""

from __future__ import annotations

from pathlib import Path

from clawdence.domain import ContractKind, PullRequestPolicy, RepoProfile, VerificationContract
from clawdence.ports.vcs import PullRequestState
from clawdence.runners import Dispatch
from clawdence.runners import worktree as wt
from clawdence.vcs import GhVcs, Problem, WorktreeManager, audit, render_body
from clawdence.vcs.git import git
from tests.harness.forge import Forge
from tests.ports.factories import run
from tests.vcs.conftest import REPO_ID, ProfileFactory


def as_the_runner_would(path: Path, files: dict[str, str]) -> str:
    """Write files into the worktree and commit them, as a runner does."""
    for name, contents in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    head = run(wt.commit_all(path, "Add a subtract function"))
    assert head is not None
    return head


def test_a_run_produces_a_real_pull_request_and_merges_it(
    worktrees: WorktreeManager, vcs: GhVcs, forge: Forge, profile: RepoProfile
) -> None:
    lease = run(
        worktrees.acquire(
            profile, run_id="run.1", work_item_id="wi.42", title="Add subtract to app"
        )
    )
    assert lease.branch == "clawdence/wi-42-add-subtract-to-app"

    head = as_the_runner_would(
        lease.path,
        {"app.py": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"},
    )

    assert run(audit(lease.path, lease.base_commit, head)) == ()

    run(vcs.create_branch(REPO_ID, lease.branch, from_commit=lease.base_commit))
    run(vcs.push(REPO_ID, lease.branch, worktree_path=str(lease.path), expect_commit=head))
    pull = run(
        vcs.open_pull_request(
            REPO_ID,
            title="Add subtract to app",
            body=render_body("Adds `sub`, mirroring `add`."),
            head_branch=lease.branch,
            base_branch="main",
            work_item_id="wi.42",
        )
    )

    assert pull.head_branch == lease.branch
    assert pull.base_branch == "main"
    assert pull.head_commit == head
    assert pull.work_item_id == "wi.42"
    assert pull.state is PullRequestState.OPEN

    merged = run(vcs.merge(REPO_ID, pull.number, expect_head=head, expect_base=pull.base_commit))
    assert merged.state is PullRequestState.MERGED
    assert forge.head("main") == merged.merge_commit
    assert "def sub" in run(git(forge.remote, "show", "refs/heads/main:app.py"))


def test_the_diff_that_lands_holds_only_what_changed(
    worktrees: WorktreeManager, vcs: GhVcs, forge: Forge, profile: RepoProfile
) -> None:
    """ "A clean diff" is the verify's phrase, and this is what it means: the
    files the change touched, and nothing the toolchain left lying about."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.7"))
    head = as_the_runner_would(lease.path, {"lib/impl.py": "VALUE = 2\n"})

    run(vcs.create_branch(REPO_ID, lease.branch, from_commit=lease.base_commit))
    run(vcs.push(REPO_ID, lease.branch, worktree_path=str(lease.path), expect_commit=head))

    landed = run(
        git(forge.remote, "diff", "--name-only", f"{lease.base_commit}..refs/heads/{lease.branch}")
    )
    assert landed.splitlines() == ["lib/impl.py"]


def test_an_unreviewable_branch_is_caught_before_it_is_published(
    worktrees: WorktreeManager, vcs: GhVcs, forge: Forge, profile: RepoProfile
) -> None:
    """The audit is the only defence layer S15 ports from v1's four, and it sits
    at the last moment where the answer is cheap and the first where it is
    complete. Nothing here refuses on its behalf — the caller does, which is
    exactly the shape this test demonstrates."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.9"))
    (lease.path / "node_modules" / "left-pad").mkdir(parents=True)
    (lease.path / "node_modules" / "left-pad" / "index.js").write_text("x", encoding="utf-8")
    (lease.path / "shortcut").symlink_to("/etc/passwd")
    head = as_the_runner_would(lease.path, {"app.py": "def add(a, b):\n    return a + b\n#\n"})

    findings = run(audit(lease.path, lease.base_commit, head))
    assert {finding.problem for finding in findings} == {Problem.VENDORED, Problem.SYMLINK}

    # The caller's decision, made here: nothing was published, so the forge has
    # never heard of this branch.
    assert findings
    published = run(git(forge.remote, "for-each-ref", "--format=%(refname)", "refs/heads/"))
    assert published.splitlines() == ["refs/heads/main"]


def test_a_cancelled_run_leaves_no_worktree_and_no_branch(
    worktrees: WorktreeManager, forge: Forge, profile: RepoProfile
) -> None:
    """The third of S15's verify criteria, as a pipeline would meet it: acquire
    in a ``try``, release in the ``finally``, and the cancellation arrives before
    anything was committed."""
    lease = run(worktrees.acquire(profile, run_id="run.cancelled", work_item_id="wi.99"))
    try:
        raise KeyboardInterrupt("the operator cancelled the run")
    except KeyboardInterrupt:
        assert run(worktrees.release(lease)) is True

    assert not lease.path.exists()
    assert not (worktrees.work_root / "run.cancelled").exists()
    assert run(git(lease.mirror, "for-each-ref", "--format=%(refname)", "refs/heads/")) == ""
    assert "clawdence" not in run(git(forge.remote, "for-each-ref", "--format=%(refname)"))


def test_the_checkout_is_what_a_runner_dispatch_needs(
    worktrees: WorktreeManager, profile: RepoProfile
) -> None:
    """S6 took the worktree, the branch and the base commit as data because
    inventing them would have been the runner deciding a later step's question.
    This is that step's answer, and the line where the two meet."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    dispatch = Dispatch.for_worktree(
        lease,
        profile,
        work_item_id="wi.42",
        contract=VerificationContract(kind=ContractKind.TEST_AFTER),
    )
    assert dispatch.worktree_path == str(lease.path)
    assert dispatch.branch == lease.branch
    assert dispatch.base_commit == lease.base_commit
    assert dispatch.profile is profile
    assert dispatch.trusted_provenance is False


def test_a_retried_run_reuses_its_branch_rather_than_opening_a_second_request(
    worktrees: WorktreeManager, vcs: GhVcs, forge: Forge, profile: RepoProfile
) -> None:
    """The branch is a function of the work item and the pull request is
    idempotent on the branch, so a run that lost its answer and tried again ends
    up with one pull request rather than two."""
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    head = as_the_runner_would(lease.path, {"lib/impl.py": "VALUE = 3\n"})
    run(vcs.create_branch(REPO_ID, lease.branch, from_commit=lease.base_commit))
    run(vcs.push(REPO_ID, lease.branch, worktree_path=str(lease.path), expect_commit=head))

    def publish() -> int:
        pull = run(
            vcs.open_pull_request(
                REPO_ID,
                title="Bump the value",
                body="body",
                head_branch=lease.branch,
                base_branch="main",
            )
        )
        return pull.number

    assert publish() == publish()
    assert len(forge.state()["pulls"]) == 1


def test_a_repository_that_requires_signatures_is_refused_before_any_work(
    worktrees: WorktreeManager, vcs: GhVcs, forge: Forge, profile: RepoProfile
) -> None:
    """ "Fail at config time, not at merge time" — and the point of doing it here
    is that no agent step, no container and no test suite has been paid for yet."""
    forge.update(protection={"required_signatures": {"enabled": True}})
    violations = run(vcs.check_policy(profile))
    assert any(violation.blocking for violation in violations)
    assert not (worktrees.work_root / "run.1").exists()


def test_a_pull_request_can_be_opened_as_a_draft_for_review(
    worktrees: WorktreeManager, vcs: GhVcs, forge: Forge, profile_for: ProfileFactory
) -> None:
    """The escape hatch a cautious adopter wants: work lands, review is asked
    for, and nothing merges until a human marks it ready."""
    profile = profile_for(pull_request=PullRequestPolicy(draft=True, labels=("automated",)))
    lease = run(worktrees.acquire(profile, run_id="run.1", work_item_id="wi.42"))
    head = as_the_runner_would(lease.path, {"lib/impl.py": "VALUE = 4\n"})
    run(vcs.create_branch(REPO_ID, lease.branch, from_commit=lease.base_commit))
    run(vcs.push(REPO_ID, lease.branch, worktree_path=str(lease.path), expect_commit=head))

    pull = run(
        vcs.open_pull_request(
            REPO_ID,
            title="Bump the value",
            body="body",
            head_branch=lease.branch,
            base_branch="main",
            policy=profile.pull_request,
        )
    )
    assert pull.draft is True
    assert forge.state()["pulls"][0]["labels"] == ["automated"]
