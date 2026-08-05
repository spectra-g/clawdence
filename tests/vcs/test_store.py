"""Mirrors: how they are built, what they fetch, and the lock around them."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from clawdence.domain import CheckoutPolicy, RepoProfile
from clawdence.ports.errors import PermanentError, TransientError
from clawdence.ports.secrets import StaticSecrets
from clawdence.vcs import LockTimeout, RepoStore, mirror_name
from clawdence.vcs.git import GitError, git
from tests.harness.forge import Forge
from tests.ports.factories import run
from tests.vcs.conftest import ProfileFactory

#: The variable a deployment would name in ``forge_token_env``. A name, which is
#: the whole point of the mechanism these tests are about.
TOKEN_NAME = "GITHUB_TOKEN"  # noqa: S105 - the name of one, not one


def config(mirror: Path, key: str) -> str | None:
    """``git config --get`` exits non-zero for an unset key; that answer is None."""
    try:
        return run(git(mirror, "config", "--get", key))
    except GitError:
        return None


def test_a_mirror_is_created_and_fetched(store: RepoStore, profile: RepoProfile) -> None:
    mirror = run(store.ensure(profile))
    assert (mirror / "HEAD").exists()
    assert run(store.resolve(profile, "main"))


def test_the_directory_name_survives_a_lossy_slug() -> None:
    """``repo:api`` and ``repo.api`` are different repositories and slugify the
    same way. Two of them sharing one object store and one branch namespace is
    the worst thing that can happen in this module."""
    assert mirror_name("repo:api") != mirror_name("repo.api")
    assert mirror_name("repo:api").startswith("repo-api-")


def test_the_refspec_keeps_the_remote_out_of_refs_heads(
    store: RepoStore, profile: RepoProfile
) -> None:
    """A mirror refspec is ``+refs/*:refs/*``, so a pruning fetch would delete a
    branch a live run created and has not pushed yet. Ours go under
    ``refs/heads``; theirs under ``refs/remotes/origin``."""
    mirror = run(store.ensure(profile))
    assert config(mirror, "remote.origin.fetch") == "+refs/heads/*:refs/remotes/origin/*"
    assert run(git(mirror, "for-each-ref", "--format=%(refname)", "refs/heads/")) == ""
    assert "refs/remotes/origin/main" in run(git(mirror, "for-each-ref", "--format=%(refname)"))


def test_a_partial_clone_is_configured_and_a_full_one_is_not(
    store: RepoStore, profile_for: ProfileFactory
) -> None:
    """``--filter=blob:none`` rather than ``--depth``: a shallow repository has
    no merge base, so nothing can rebase, compare or bind evidence to a tree."""
    partial = run(store.ensure(profile_for(id="repo.partial")))
    assert config(partial, "remote.origin.partialclonefilter") == "blob:none"

    full = run(store.ensure(profile_for(id="repo.full", checkout=CheckoutPolicy(partial=False))))
    assert config(full, "remote.origin.partialclonefilter") is None


def test_lfs_smudging_is_skipped_unless_asked_for(
    store: RepoStore, profile_for: ProfileFactory
) -> None:
    skipped = run(store.ensure(profile_for(id="repo.nolfs")))
    assert "--skip" in (config(skipped, "filter.lfs.smudge") or "")

    wanted = run(store.ensure(profile_for(id="repo.lfs", checkout=CheckoutPolicy(fetch_lfs=True))))
    assert config(wanted, "filter.lfs.smudge") is None


def test_resolve_prefers_the_remotes_view_of_a_branch(
    store: RepoStore, profile: RepoProfile, forge: Forge
) -> None:
    """A local branch shadowing the remote's is what a half-finished run leaves
    behind, and resolving to it would branch the next run from unmerged work."""
    mirror = run(store.ensure(profile))
    remote_main = run(store.resolve(profile, "main"))
    run(git(mirror, "branch", "main", remote_main))
    forge.advance("main")
    run(store.ensure(profile))

    assert run(store.resolve(profile, "main")) == forge.head("main")
    assert run(store.resolve(profile, "main")) != remote_main


def test_an_unknown_ref_is_permanent(store: RepoStore, profile: RepoProfile) -> None:
    run(store.ensure(profile))
    with pytest.raises(PermanentError) as caught:
        run(store.resolve(profile, "no-such-branch"))
    assert caught.value.kind == "unknown-ref"


def test_a_second_ensure_is_a_fetch_not_a_reclone(
    store: RepoStore, profile: RepoProfile, forge: Forge
) -> None:
    mirror = run(store.ensure(profile))
    marker = mirror / "clawdence-was-here"
    marker.touch()
    forge.advance("main")

    assert run(store.ensure(profile)) == mirror
    assert marker.exists()
    assert run(store.resolve(profile, "main")) == forge.head("main")


def test_an_unreachable_remote_is_transient_and_names_the_repository(
    store: RepoStore, profile_for: ProfileFactory
) -> None:
    """Unreachable forges, expired proxies and rate limits are all worth another
    attempt, and none of them is a decision a human has to make."""
    profile = profile_for(id="repo.gone", remote_url="file:///nowhere/at/all.git")
    with pytest.raises(TransientError) as caught:
        run(store.ensure(profile))
    assert caught.value.retryable is True
    assert "repo.gone" in caught.value.message


def test_the_lock_serialises_two_processes(
    store: RepoStore, profile: RepoProfile, workspace: Path
) -> None:
    """A second store is a second process for this purpose: the lock is a
    ``flock`` on a file, so two ``RepoStore`` objects contend exactly as two
    control planes would."""
    run(store.ensure(profile))
    other = RepoStore(root=store.root, token_name=None, lock_timeout=0.2)

    async def hold_then_try() -> None:
        async with store.locked(profile):
            with pytest.raises(LockTimeout) as caught:
                async with other.locked(profile):  # pragma: no cover - it must not enter
                    pass
            assert caught.value.retryable is True
            assert mirror_name(profile.id) in caught.value.message

    run(hold_then_try())


def test_the_lock_is_released_when_the_block_ends(store: RepoStore, profile: RepoProfile) -> None:
    other = RepoStore(root=store.root, token_name=None, lock_timeout=0.2)

    async def sequentially() -> None:
        async with store.locked(profile):
            pass
        async with other.locked(profile):
            pass

    run(sequentially())


def test_two_coroutines_in_one_loop_do_not_deadlock(store: RepoStore, profile: RepoProfile) -> None:
    """flocks conflict between descriptors in the *same* process too. Without the
    in-process ``asyncio.Lock`` the second coroutine would block a thread waiting
    for a lock the first is holding, and neither would ever proceed."""
    order: list[str] = []

    async def hold(name: str) -> None:
        async with store.locked(profile):
            order.append(f"enter {name}")
            await asyncio.sleep(0)
            order.append(f"leave {name}")

    async def both() -> None:
        await asyncio.wait_for(asyncio.gather(hold("a"), hold("b")), timeout=10)

    run(both())
    assert order in (
        ["enter a", "leave a", "enter b", "leave b"],
        ["enter b", "leave b", "enter a", "leave a"],
    )


def test_push_publishes_a_refspec(store: RepoStore, profile: RepoProfile, forge: Forge) -> None:
    mirror = run(store.ensure(profile))
    base = run(store.resolve(profile, "main"))
    run(store.push(profile, mirror, f"{base}:refs/heads/clawdence/wi-1"))
    assert forge.head("clawdence/wi-1") == base


def test_a_rejected_push_is_permanent(store: RepoStore, profile: RepoProfile) -> None:
    """No ``--force``, ever: a branch that cannot fast-forward is a conflict, and
    a conflict is a decision rather than a flag."""
    mirror = run(store.ensure(profile))
    with pytest.raises(PermanentError) as caught:
        run(store.push(profile, mirror, "refs/heads/nothing:refs/heads/x"))
    assert caught.value.kind == "push-rejected"


# ------------------------------------------------ what the refusal names


def test_an_ssh_remote_is_never_blamed_on_the_token_variable(
    store: RepoStore, profile_for: ProfileFactory
) -> None:
    """``authenticated`` branches on the URL *before* it looks at a token, so an
    ssh remote never reads ``forge_token_env``. A refusal that named the variable
    anyway sent the reader off to set a token nothing would have read — and left
    the actual cause, a passphrase-protected key that is not in the agent,
    unmentioned, which is the one thing ``BatchMode`` guarantees cannot be
    prompted for."""
    profile = profile_for(remote_url="git@github.com:acme/widget.git")
    named = RepoStore(root=store.root, secrets=StaticSecrets({TOKEN_NAME: "ghp-x"}))
    note = named.credential_note(profile)

    assert "SSH identity" in note
    assert "ssh-add -l" in note
    assert TOKEN_NAME not in note


def test_an_https_remote_names_the_variable_that_was_read(
    store: RepoStore, profile_for: ProfileFactory
) -> None:
    profile = profile_for(remote_url="https://github.com/acme/widget.git")
    resolved = RepoStore(
        root=store.root,
        secrets=StaticSecrets({TOKEN_NAME: "ghp-x"}),
        token_name=TOKEN_NAME,
    )
    assert resolved.credential_note(profile) == f"{TOKEN_NAME} is what was offered"


def test_a_configured_but_unresolvable_token_says_so_rather_than_claiming_it(
    store: RepoStore, profile_for: ProfileFactory
) -> None:
    """A secret *name* is not a secret. Reporting the name as "what was offered"
    when the provider had no value for it describes a request that never carried
    a credential at all, which is a different fix."""
    profile = profile_for(remote_url="https://github.com/acme/widget.git")
    missing = RepoStore(root=store.root, secrets=StaticSecrets(), token_name=TOKEN_NAME)
    note = missing.credential_note(profile)

    assert note.startswith("nothing was offered")
    assert TOKEN_NAME in note


def test_no_configured_token_is_its_own_answer(
    store: RepoStore, profile_for: ProfileFactory
) -> None:
    profile = profile_for(remote_url="https://github.com/acme/widget.git")
    assert store.credential_note(profile) == (
        "nothing was offered: this deployment configures no forge_token_env"
    )
