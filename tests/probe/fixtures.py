"""The five repositories S9 is verified against.

Four build systems plus a testcontainers repo, which is the plan's list. They
are built as real git repositories through the shared harness rather than
committed, for the reason ``tests/harness/repos.py`` gives — and named, because
the probe derives the repository's name from the directory when there is no
remote, and a snapshot of a profile called ``repo1`` is a snapshot nobody can
read.

The monorepo carries two things the other four do not, and both are the point
of having it:

``packages/api`` declares testcontainers
    Only the member does. A probe that reads the root manifest and stops
    reports ``needs_docker: false`` for a repository whose integration tests
    start containers — and that is the *common* shape, not an exotic one.

``node_modules/testcontainers`` exists
    The trap. It is somebody else's manifest, sitting where an installed
    dependency sits, and counting it would flip the isolation tier of every
    repository that happened to be probed after an ``npm install``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from clawdence.domain import BuildSystem
from tests.harness.repos import FixtureRepo, build_repo


def maven_repo(root: Path) -> FixtureRepo:
    """A single-module Maven build with a working wrapper."""
    return build_repo(root / "acme-billing", build_system=BuildSystem.MAVEN)


def gradle_repo(root: Path) -> FixtureRepo:
    """Gradle with two included projects and a version catalog."""
    return build_repo(
        root / "acme-ledger",
        build_system=BuildSystem.GRADLE,
        extra_files={
            "settings.gradle.kts": (
                'rootProject.name = "acme-ledger"\ninclude("core")\ninclude("adapters:http")\n'
            ),
            "core/build.gradle.kts": "plugins { java }\n",
            "adapters/http/build.gradle.kts": "plugins { java }\n",
            ".tool-versions": "java temurin-21.0.5+11\ngradle 8.11\n",
        },
    )


def python_repo(root: Path) -> FixtureRepo:
    """uv, with pytest declared and a conventions file the runner installs."""
    return build_repo(
        root / "acme-etl",
        build_system=BuildSystem.UV,
        extra_files={
            "pyproject.toml": (
                "[project]\n"
                'name = "acme-etl"\n'
                'version = "0.1.0"\n'
                "dependencies = []\n"
                "\n"
                "[dependency-groups]\n"
                'dev = ["pytest==9.1.1"]\n'
                "\n"
                "[tool.pytest.ini_options]\n"
                'testpaths = ["tests"]\n'
            ),
            "AGENTS.md": "# Conventions\n\nUse type hints.\n",
        },
    )


def node_monorepo(root: Path) -> FixtureRepo:
    """npm workspaces + turborepo, with Docker needed by one member only."""
    return build_repo(
        root / "acme-web",
        build_system=BuildSystem.NPM,
        extra_files={
            "package.json": _json(
                {
                    "name": "acme-web",
                    "private": True,
                    "workspaces": ["packages/*"],
                    "scripts": {"build": "turbo run build", "test": "turbo run test"},
                    "devDependencies": {"turbo": "^2.3.0"},
                }
            ),
            "turbo.json": _json({"tasks": {"test": {}, "build": {}}}),
            "packages/web/package.json": _json(
                {"name": "@acme/web", "version": "0.1.0", "scripts": {"test": "vitest run"}}
            ),
            "packages/api/package.json": _json(
                {
                    "name": "@acme/api",
                    "version": "0.1.0",
                    "scripts": {"test": "jest"},
                    "devDependencies": {"jest": "^29.7.0", "testcontainers": "^10.13.2"},
                }
            ),
            # Installed, not declared. See the module docstring.
            "node_modules/testcontainers/package.json": _json(
                {"name": "testcontainers", "version": "10.13.2"}
            ),
            ".nvmrc": "22.11.0\n",
        },
    )


def testcontainers_repo(root: Path) -> FixtureRepo:
    """The one the plan asks for: tests that need a daemon, and no grant."""
    return build_repo(
        root / "acme-orders",
        build_system=BuildSystem.MAVEN,
        testcontainers=True,
        extra_files={"docker-compose.yml": "services:\n  db:\n    image: postgres:17\n"},
    )


#: Name → builder, in the order the plan lists them.
BUILDERS: Final = {
    "maven": maven_repo,
    "gradle": gradle_repo,
    "node-monorepo": node_monorepo,
    "python": python_repo,
    "testcontainers": testcontainers_repo,
}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2) + "\n"
