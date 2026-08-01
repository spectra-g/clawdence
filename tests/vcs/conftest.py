"""Fixtures for S15: a remote, a store pointed at it, and a place to check out.

Everything here is real. The remote is a bare git repository, the mirrors are
real clones of it, and the worktrees are real checkouts — the only fake in the
stack is ``gh``, because the alternative is a GitHub account and a network the
suite is denied. That is the same line S6 drew for the runner: fixture repos with
genuine hashes, because every property worth testing here is a statement about a
hash changing when it should.

Roots are kept apart on purpose. ``mirrors`` and ``work`` are siblings rather
than nested, and ``tests/vcs/test_worktrees`` has a test that says why: the
reaper sweeps one level under the work root and would eventually delete an object
store that lived there.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from clawdence.domain import RepoProfile
from clawdence.ports.secrets import StaticSecrets
from clawdence.vcs import GhVcs, RepoStore, WorktreeManager
from tests.conftest import RepoFactory
from tests.harness.forge import Forge, build_forge
from tests.harness.repos import FixtureRepo

#: The id every fixture repository is registered under.
REPO_ID = "repo.widget"

ProfileFactory = Callable[..., RepoProfile]


@pytest.fixture
def origin(repos: RepoFactory) -> FixtureRepo:
    """The repository the forge is built from, with a file worth changing.

    Two subdirectories as well as a root file, because a sparse checkout in cone
    mode always keeps the root — a fixture that was flat could not demonstrate
    one at all.
    """
    return repos(
        extra_files={
            "app.py": "def add(a, b):\n    return a + b\n",
            "docs/guide.md": "# Guide\n",
            "lib/impl.py": "VALUE = 1\n",
        }
    )


@pytest.fixture
def forge(workspace: Path, origin: FixtureRepo) -> Forge:
    return build_forge(workspace / "forge", origin.path)


@pytest.fixture
def profile_for(forge: Forge) -> ProfileFactory:
    """Builds profiles pointed at the forge. Everything else is a default."""

    def build(**overrides: object) -> RepoProfile:
        fields: dict[str, object] = {
            "id": REPO_ID,
            "name": "widget",
            "remote_url": forge.url,
            "default_branch": "main",
        }
        fields.update(overrides)
        return RepoProfile.model_validate(fields)

    return build


@pytest.fixture
def profile(profile_for: ProfileFactory) -> RepoProfile:
    return profile_for()


@pytest.fixture
def store(workspace: Path) -> RepoStore:
    """Mirrors under their own root.

    ``token_name=None`` because a ``file://`` remote authenticates nobody, and a
    store that went looking for a credential it does not need would make every
    test here depend on the secret provider's behaviour rather than on git's.
    """
    return RepoStore(root=workspace / "mirrors", secrets=StaticSecrets(), token_name=None)


@pytest.fixture
def worktrees(store: RepoStore, workspace: Path) -> WorktreeManager:
    return WorktreeManager(
        store=store,
        work_root=workspace / "work",
        # The suite runs on whatever CI gives it. A checkout here is a handful of
        # kilobytes, so the floor is set to nothing and the budget's own test
        # raises it deliberately.
        min_free_mb=0,
    )


@pytest.fixture
def vcs(store: RepoStore, forge: Forge, profile: RepoProfile) -> GhVcs:
    return GhVcs(
        store=store,
        profiles={profile.id: profile},
        gh_path=forge.gh,
        token_name=None,
        environ={"PATH": os.environ.get("PATH", ""), "HOME": str(forge.root)},
    )
