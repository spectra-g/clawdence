"""Fixtures for S11: a deployment on disk, with two repositories to choose from.

Two, not one, and that is the whole point of the fixture. With a single
repository configured, routing has one answer and every scoring test would pass
against a function that returned the first item — the walking skeleton's shape
is a special case in ``routing._repo`` precisely because it is not the case worth
testing. The second repository is what makes a wrong alias observable.

Everything under it is real: a bare git repository standing in for the forge, a
real mirror, real worktrees, and profiles written to disk in the format
``clawdence probe --out`` emits. The only fake is ``gh``, for the reason S15
gave — the alternative is a GitHub account and a network the suite is denied.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from clawdence.domain import RepoProfile
from clawdence.ports.secrets import StaticSecrets
from clawdence.triage import Deployment, load
from clawdence.vcs import GhVcs, RepoStore, WorktreeManager
from tests.conftest import RepoFactory
from tests.harness.forge import Forge, build_forge
from tests.harness.repos import FixtureRepo

#: The repository every routing test expects a request to land in.
WIDGET = "repo.widget"

#: The one it must *not* land in unless the request says so. Named after a
#: different product on purpose: two repositories with overlapping vocabularies
#: would make a passing test ambiguous about why it passed.
PORTAL = "repo.portal"

#: Where the shipped workflows live, so a routed name resolves to a real file.
#: The examples rather than a fixture: a routing test that pointed at workflows
#: invented here would not notice `sprint.yaml` losing a stage.
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

ConfigWriter = Callable[..., Path]


@pytest.fixture
def origin(repos: RepoFactory) -> FixtureRepo:
    return repos(extra_files={"app.py": "def add(a, b):\n    return a + b\n"})


@pytest.fixture
def forge(workspace: Path, origin: FixtureRepo) -> Forge:
    return build_forge(workspace / "forge", origin.path)


@pytest.fixture
def widget(forge: Forge) -> RepoProfile:
    """The adder. Aliased and keyworded the way an operator would write it."""
    return RepoProfile.model_validate(
        {
            "id": WIDGET,
            "name": "widget",
            "remote_url": forge.url,
            "default_branch": "main",
            "aliases": ("widget", "widget-api"),
            "keywords": ("adder", "arithmetic", "sum"),
        }
    )


@pytest.fixture
def portal(forge: Forge) -> RepoProfile:
    """The other one. Same remote — nothing here tests two remotes, and giving
    it a second bare repository would be a fixture that costs a clone to prove
    something no test asserts."""
    return RepoProfile.model_validate(
        {
            "id": PORTAL,
            "name": "portal",
            "remote_url": forge.url,
            "default_branch": "main",
            "aliases": ("portal", "customer portal"),
            "keywords": ("login", "signup"),
        }
    )


@pytest.fixture
def write_config(workspace: Path, widget: RepoProfile, portal: RepoProfile) -> ConfigWriter:
    """Writes a config file and its profiles, and returns the path to it.

    Profiles are written with ``model_dump`` rather than by hand, so the fixture
    and ``clawdence probe --out`` cannot drift: if a required field is added to
    ``RepoProfile``, every test here starts exercising it rather than testing an
    older shape.
    """

    def build(
        *profiles: RepoProfile,
        body: str | None = None,
        **overrides: object,
    ) -> Path:
        root = workspace / "deployment"
        (root / "profiles").mkdir(parents=True, exist_ok=True)
        chosen = profiles or (widget, portal)
        paths = []
        for profile in chosen:
            path = root / "profiles" / f"{profile.id}.json"
            path.write_text(json.dumps(profile.model_dump(mode="json"), indent=2), encoding="utf-8")
            paths.append(f"profiles/{profile.id}.json")

        config = root / "config.yaml"
        if body is not None:
            config.write_text(body, encoding="utf-8")
            return config

        lines = [
            "schema_version: 1",
            "paths:",
            "  repo_store: mirrors",
            "  work_root: work",
            f"  workflows: {EXAMPLES}",
            # No credential: a ``file://`` remote authenticates nobody, and a
            # store that went looking for one would make every test here depend
            # on the secret provider rather than on git.
            "forge_token_env: null",
            "repos:",
            *(f"  - {path}" for path in paths),
        ]
        for name, value in overrides.items():
            lines.append(f"{name}: {value}")
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return config

    return build


@pytest.fixture
def config_path(write_config: ConfigWriter) -> Path:
    return write_config()


@pytest.fixture
def deployment(config_path: Path) -> Deployment:
    return load(config_path)


@pytest.fixture
def repo_store(deployment: Deployment) -> RepoStore:
    return RepoStore(root=deployment.repo_store, secrets=StaticSecrets(), token_name=None)


@pytest.fixture
def worktrees(deployment: Deployment, repo_store: RepoStore) -> WorktreeManager:
    # ``min_free_mb=0`` for the reason S15's fixtures give: the suite runs on
    # whatever CI gives it, a checkout here is kilobytes, and the disk budget has
    # its own test that raises the floor deliberately.
    return WorktreeManager(store=repo_store, work_root=deployment.work_root, min_free_mb=0)


@pytest.fixture
def vcs(deployment: Deployment, repo_store: RepoStore, forge: Forge) -> GhVcs:
    return GhVcs(
        store=repo_store,
        profiles=deployment.profiles,
        gh_path=forge.gh,
        token_name=None,
        environ={"PATH": os.environ.get("PATH", ""), "HOME": str(forge.root)},
    )
