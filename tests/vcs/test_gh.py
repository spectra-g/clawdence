"""``GhVcs`` against a real bare repository and a fake ``gh``.

The contract subclass at the bottom is the load-bearing part: the obligations
every adapter meets are the same ones the in-memory fake meets, and an adapter
that needed them weakened would be one the fake is the only thing satisfying.
What it costs is a producer worktree and a chain of real commits, because this
adapter pushes objects rather than recording a string.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawdence.domain import MergeMethod, PullRequestPolicy, RepoProfile
from clawdence.ports.errors import PermanentError, TransientError
from clawdence.ports.secrets import StaticSecrets
from clawdence.ports.vcs import PullRequestState, StaleMergeError, VcsPort
from clawdence.vcs import GhUnavailableError, GhVcs, RepoStore, read_template, render_body
from clawdence.vcs.gh import repo_slug
from clawdence.vcs.git import GitError, git
from tests.harness.forge import Forge, build_forge
from tests.harness.repos import FixtureRepo
from tests.ports.contract import VcsContract
from tests.ports.factories import run
from tests.vcs.conftest import REPO_ID, ProfileFactory


@pytest.fixture
def producer(workspace: Path, forge: Forge) -> Path:
    """A clone standing in for the worktree a runner produced work in.

    A real clone rather than a path, because ``push`` sends objects from the
    directory it is given: an adapter that took the commit id on trust would push
    nothing and report success.
    """
    run(git(workspace, "clone", "--quiet", forge.url, "producer"))
    return workspace / "producer"


def dangling(producer: Path, parent: str, message: str = "the agent's work") -> str:
    """A commit reachable from nothing — what a runner has just made.

    Chained onto ``parent`` rather than always onto main, because two siblings
    are not fast-forwardable and this adapter never force-pushes.
    """
    tree = run(git(producer, "rev-parse", f"{parent}^{{tree}}"))
    return run(git(producer, "commit-tree", tree, "-p", parent, "-m", message))


# --------------------------------------------------------------------- slugs


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/acme/widget", "acme/widget"),
        ("https://github.com/acme/widget.git", "acme/widget"),
        ("https://github.com/acme/widget/", "acme/widget"),
        ("git@github.com:acme/widget.git", "acme/widget"),
        ("ssh://git@github.com/acme/widget.git", "acme/widget"),
    ],
)
def test_a_remote_url_becomes_an_owner_and_a_name(url: str, slug: str) -> None:
    """Both forms, because both are what people have in their configuration —
    and the scp-like ssh form is not a URL at all, which ``urlsplit`` parses into
    something confidently wrong."""
    assert repo_slug(url) == slug


@pytest.mark.parametrize("url", ["https://github.com/acme", "not-a-url", "https://github.com/"])
def test_a_remote_that_names_no_repository_is_refused(url: str) -> None:
    with pytest.raises(PermanentError) as caught:
        repo_slug(url)
    assert caught.value.kind == "unrecognised-remote"


def test_a_disconnect_during_ls_remote_is_a_typed_transient_failure(
    vcs: GhVcs, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def disconnected(*args: object, **kwargs: object) -> str:
        raise GitError(("ls-remote",), "Received disconnect: Bye Bye")

    monkeypatch.setattr(RepoStore, "remote_git", disconnected)

    with pytest.raises(TransientError) as caught:
        run(vcs.head(REPO_ID, "main"))
    assert caught.value.kind == "remote-read-failed"


def test_an_ssh_identity_rejection_is_a_typed_permanent_failure(
    vcs: GhVcs, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def denied(*args: object, **kwargs: object) -> str:
        raise GitError(("ls-remote",), "Permission denied (publickey).")

    monkeypatch.setattr(RepoStore, "remote_git", denied)

    with pytest.raises(PermanentError) as caught:
        run(vcs.head(REPO_ID, "main"))
    assert caught.value.kind == "remote-read-denied"
    assert "configured SSH identity" in caught.value.message


# ------------------------------------------------------------ pull requests


def open_one(vcs: GhVcs, producer: Path, forge: Forge, **kwargs: object) -> object:
    base = run(vcs.head(REPO_ID, "main"))
    run(vcs.create_branch(REPO_ID, "clawdence/wi-1", from_commit=base))
    run(
        vcs.push(
            REPO_ID,
            "clawdence/wi-1",
            worktree_path=str(producer),
            expect_commit=dangling(producer, base),
        )
    )
    return run(
        vcs.open_pull_request(
            REPO_ID,
            title="Add a thing",
            body="what it does",
            head_branch="clawdence/wi-1",
            base_branch="main",
            **kwargs,  # type: ignore[arg-type]
        )
    )


def test_a_pull_request_carries_the_repositorys_metadata(
    vcs: GhVcs, producer: Path, forge: Forge
) -> None:
    """Reviewers, labels and draft status are what make output look like it
    belongs in a project rather than like bot spam."""
    policy = PullRequestPolicy(draft=True, reviewers=("clawbot",), labels=("automated",))
    pull = open_one(vcs, producer, forge, policy=policy)

    assert pull.draft is True  # type: ignore[attr-defined]
    recorded = forge.state()["pulls"][0]
    assert recorded["reviewers"] == ["clawbot"]
    assert recorded["labels"] == ["automated"]


def test_a_second_call_returns_the_same_pull_request_untouched(
    vcs: GhVcs, producer: Path, forge: Forge
) -> None:
    """A retry that reasserted the policy would undo a human's edits, and a human
    editing a bot's pull request is the system working."""
    first = open_one(vcs, producer, forge, policy=PullRequestPolicy(labels=("automated",)))
    forge.state()  # nothing else happens between the two calls

    again = run(
        vcs.open_pull_request(
            REPO_ID,
            title="A different title now",
            body="and a different body",
            head_branch="clawdence/wi-1",
            base_branch="main",
            policy=PullRequestPolicy(labels=("bug",)),
        )
    )
    assert again.number == first.number  # type: ignore[attr-defined]
    assert len(forge.state()["pulls"]) == 1
    assert forge.state()["pulls"][0]["labels"] == ["automated"]


def test_an_unassignable_reviewer_does_not_cost_the_run_its_pull_request(
    vcs: GhVcs, producer: Path, forge: Forge
) -> None:
    """A reviewer who left the organisation fails the whole ``pr create``, and
    that is not a reason to throw away working code."""
    policy = PullRequestPolicy(reviewers=("someone-who-left",), labels=("automated",))
    pull = open_one(vcs, producer, forge, policy=policy)

    assert pull.state is PullRequestState.OPEN  # type: ignore[attr-defined]
    assert forge.state()["pulls"][0]["reviewers"] == []


def test_the_base_commit_is_read_live_rather_than_from_the_pull_request(
    vcs: GhVcs, producer: Path, forge: Forge
) -> None:
    """GitHub's ``baseRefOid`` is where the base was when the PR last synced,
    which is precisely not the question a merge decision asks."""
    pull = open_one(vcs, producer, forge)
    advanced = forge.advance("main")

    refetched = run(vcs.get_pull_request(REPO_ID, pull.number))  # type: ignore[attr-defined]
    assert refetched is not None
    assert refetched.base_commit == advanced


def test_the_forge_enforces_the_head_match_as_well(
    vcs: GhVcs, producer: Path, forge: Forge
) -> None:
    """Our own comparison is a read followed by a write and a push can land in
    between. ``--match-head-commit`` hands the same check to the server, where it
    is atomic with the merge — so a merge that skipped the flag would be caught
    by the fake rather than passing quietly."""
    pull = open_one(vcs, producer, forge)
    stale = pull.head_commit  # type: ignore[attr-defined]
    run(
        vcs.push(
            REPO_ID,
            "clawdence/wi-1",
            worktree_path=str(producer),
            expect_commit=dangling(producer, stale, "a follow-up"),
        )
    )

    with pytest.raises(StaleMergeError):
        run(
            vcs.merge(
                REPO_ID,
                pull.number,  # type: ignore[attr-defined]
                expect_head=stale,
                expect_base=forge.head("main"),
            )
        )


def test_a_draft_is_not_merged(vcs: GhVcs, producer: Path, forge: Forge) -> None:
    """A draft is *open* and unmergeable, which no other combination of the
    fields can express."""
    pull = open_one(vcs, producer, forge, policy=PullRequestPolicy(draft=True))
    with pytest.raises(PermanentError) as caught:
        run(
            vcs.merge(
                REPO_ID,
                pull.number,  # type: ignore[attr-defined]
                expect_head=pull.head_commit,  # type: ignore[attr-defined]
                expect_base=forge.head("main"),
            )
        )
    assert caught.value.kind == "pull-request-draft"


def test_a_closed_pull_request_is_not_merged(vcs: GhVcs, producer: Path, forge: Forge) -> None:
    pull = open_one(vcs, producer, forge)
    forge.close(pull.number)  # type: ignore[attr-defined]
    with pytest.raises(PermanentError) as caught:
        run(
            vcs.merge(
                REPO_ID,
                pull.number,  # type: ignore[attr-defined]
                expect_head=pull.head_commit,  # type: ignore[attr-defined]
                expect_base=forge.head("main"),
            )
        )
    assert caught.value.kind == "pull-request-closed"


def test_the_merge_method_comes_from_the_profile(
    store: RepoStore, forge: Forge, producer: Path, profile_for: ProfileFactory
) -> None:
    profile = profile_for(pull_request=PullRequestPolicy(merge_method=MergeMethod.MERGE))
    vcs = GhVcs(store=store, profiles={profile.id: profile}, gh_path=forge.gh, token_name=None)
    pull = open_one(vcs, producer, forge)
    merged = run(
        vcs.merge(
            REPO_ID,
            pull.number,  # type: ignore[attr-defined]
            expect_head=pull.head_commit,  # type: ignore[attr-defined]
            expect_base=forge.head("main"),
        )
    )
    assert merged.state is PullRequestState.MERGED
    assert "(merge)" in run(git(forge.remote, "log", "-1", "--format=%s", "refs/heads/main"))
    assert (
        run(git(forge.remote, "log", "-1", "--format=%an <%ae>", "refs/heads/main"))
        == "Clawdence Forge <forge@clawdence.invalid>"
    )


# ------------------------------------------------------------------- wiring


def test_a_repository_nobody_configured_is_refused(vcs: GhVcs) -> None:
    """The adapter is given the repositories it may touch rather than
    discovering them, because discovery would be it deciding what the system is
    allowed to write to."""
    with pytest.raises(PermanentError) as caught:
        run(vcs.head("repo.unheard-of", "main"))
    assert caught.value.kind == "unknown-repository"


def test_a_missing_gh_says_so_rather_than_failing_obscurely(
    store: RepoStore, profile: RepoProfile
) -> None:
    vcs = GhVcs(
        store=store,
        profiles={profile.id: profile},
        gh_path="/nonexistent/gh",
        token_name=None,
    )
    with pytest.raises(GhUnavailableError) as caught:
        run(vcs.get_pull_request(REPO_ID, 1))
    assert caught.value.kind == "gh-unavailable"


def test_gh_is_not_handed_the_control_planes_environment(
    store: RepoStore, profile: RepoProfile
) -> None:
    """This process holds every provider key in the system; handing ``os.environ``
    to a subprocess puts all of them one ``env`` away from anything that reads a
    child's output."""
    vcs = GhVcs(
        store=store,
        profiles={profile.id: profile},
        secrets=StaticSecrets({"CLAWDENCE_FORGE_TOKEN": "t0ken"}),
        environ={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-should-not-travel"},
    )
    env = vcs.environment()
    assert "ANTHROPIC_API_KEY" not in env
    assert env["GH_TOKEN"] == "t0ken"  # noqa: S105 - asserting the value arrived
    assert env["GH_PROMPT_DISABLED"] == "1"


def test_no_token_configured_means_no_token_passed(store: RepoStore, profile: RepoProfile) -> None:
    """gh finds the operator's own login through ``HOME``; forcing an empty
    ``GH_TOKEN`` would override it with nothing."""
    vcs = GhVcs(store=store, profiles={profile.id: profile}, environ={"PATH": "/bin"})
    assert "GH_TOKEN" not in vcs.environment()


# ------------------------------------------------------------------- bodies


def test_a_repository_template_is_included_unfilled(
    store: RepoStore, forge: Forge, profile_for: ProfileFactory
) -> None:
    """A template is a list of questions the repository asks its authors. A
    system that ticked its own boxes would be answering them for somebody who has
    not looked yet."""
    profile = profile_for(
        pull_request=PullRequestPolicy(body_template_path=".github/pull_request_template.md")
    )
    run(store.ensure(profile))
    template = run(read_template(store, profile, run(store.resolve(profile, "main"))))
    assert template is None  # the fixture has none

    body = render_body("This adds a thing.", template="- [ ] tests\n- [ ] changelog\n")
    assert body.startswith("This adds a thing.")
    assert "- [ ] tests" in body
    assert "- [x]" not in body


def test_a_body_with_no_template_is_left_alone() -> None:
    assert render_body("summary") == "summary"
    assert render_body("summary", template="   \n") == "summary"


def test_a_template_is_read_from_the_base_commit(
    store: RepoStore, workspace: Path, repos: object, profile_for: ProfileFactory
) -> None:
    """Never from the worktree: that is output from a model, so a template read
    from there is text an agent could have written for the system to sign."""
    source = build_forge.__module__  # keeps the import honest for readers
    assert source
    with_template = _repo_with_template(workspace)
    other = build_forge(workspace / "forge2", with_template, owner="acme", name="templated")
    profile = profile_for(
        id="repo.templated",
        remote_url=other.url,
        pull_request=PullRequestPolicy(body_template_path=".github/pull_request_template.md"),
    )
    run(store.ensure(profile))
    template = run(read_template(store, profile, run(store.resolve(profile, "main"))))
    assert template is not None
    assert "Checklist" in template


def _repo_with_template(workspace: Path) -> Path:
    from tests.harness.repos import build_repo

    built = build_repo(
        workspace / "templated",
        extra_files={".github/pull_request_template.md": "## Checklist\n\n- [ ] tests\n"},
    )
    return built.path


# ------------------------------------------------------------------ policy


def test_an_unprotected_repository_produces_no_violations(vcs: GhVcs, profile: RepoProfile) -> None:
    assert run(vcs.check_policy(profile)) == ()


def test_branch_protection_is_read_and_evaluated(
    vcs: GhVcs, forge: Forge, profile: RepoProfile
) -> None:
    forge.update(
        protection={
            "required_signatures": {"enabled": True},
            "required_pull_request_reviews": {"required_approving_review_count": 2},
            "required_status_checks": {"contexts": ["ci/build"]},
        }
    )
    found = run(vcs.check_policy(profile))
    assert next(violation.rule.value for violation in found) == "signed-commits"
    assert any(not violation.blocking for violation in found)


def test_a_disabled_merge_method_is_found(
    store: RepoStore, forge: Forge, profile_for: ProfileFactory
) -> None:
    profile = profile_for(pull_request=PullRequestPolicy(merge_method=MergeMethod.REBASE))
    vcs = GhVcs(store=store, profiles={profile.id: profile}, gh_path=forge.gh, token_name=None)
    found = run(vcs.check_policy(profile))
    assert [violation.rule.value for violation in found] == ["merge-method"]


def test_a_forge_that_cannot_be_read_checks_nothing(store: RepoStore, profile: RepoProfile) -> None:
    """The common case is a token with ``repo`` scope and not admin. Refusing to
    work on a repository because its settings could not be read would refuse
    most repositories."""
    vcs = GhVcs(
        store=store, profiles={profile.id: profile}, gh_path="/nonexistent/gh", token_name=None
    )
    assert run(vcs.check_policy(profile)) == ()


# ---------------------------------------------------------------- contract


class TestGhVcs(VcsContract):
    """The port's obligations, met by the real adapter.

    ``_push`` is overridden because the contract's default passes a placeholder
    worktree path: the fake records a hash and this pushes objects, so it needs a
    directory that actually holds them.
    """

    repo_id = REPO_ID

    @pytest.fixture(autouse=True)
    def _wire(self, forge: Forge, producer: Path) -> None:
        self.forge = forge
        self.producer = producer
        self.tip: str | None = None

    @pytest.fixture
    def vcs(self, store: RepoStore, forge: Forge, profile: RepoProfile) -> GhVcs:
        return GhVcs(store=store, profiles={profile.id: profile}, gh_path=forge.gh, token_name=None)

    def seed(self, vcs: VcsPort) -> str:
        return self.forge.head("main")

    def new_commit(self, vcs: VcsPort) -> str:
        self.tip = dangling(self.producer, self.tip or self.forge.head("main"))
        return self.tip

    def advance_main(self, vcs: VcsPort) -> str:
        return self.forge.advance("main")

    def _push(self, vcs: VcsPort, branch: str, commit: str) -> None:
        run(
            vcs.push(
                self.repo_id,
                branch,
                worktree_path=str(self.producer),
                expect_commit=commit,
            )
        )


def test_the_fixture_repo_reaches_the_forge(origin: FixtureRepo, forge: Forge) -> None:
    """The remote is a clone of the fixture, so what lands there is what the
    fixture had — hashes included."""
    assert forge.head("main") == origin.head
