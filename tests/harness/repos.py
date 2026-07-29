"""Repositories to test against, built rather than committed.

S8 needs a repo with testcontainers, S9 needs one per build system, S21 needs a
real one. What all of them actually read is the *shape* — which manifest is
present, whether a wrapper script exists, whether a dependency implies Docker —
so this builds that shape into a real git repository at test time.

**Real git, on purpose.** ``VerificationResult`` binds evidence to a tree hash,
and the whole merge-safety property is about hashes changing when they should.
A fixture that invented hashes would let those tests pass without ever meeting a
real one. It also means S9's probe and S15's VCS adapter get something they can
actually run against.

**Deterministic hashes.** Author, committer and both dates are pinned, so a repo
built from the same files twice has the same commit id. That is what lets a test
assert on an exact hash instead of on "some hash", and it costs four environment
variables.

**Hermetic.** The user's global git config is ignored and hooks are disabled.
Otherwise a maintainer with ``commit.gpgsign = true`` gets a suite that hangs
waiting for a passphrase, and a maintainer with a global pre-commit hook gets
somebody else's linter running inside our fixtures.

Skipping is honest: without ``git`` on PATH the fixtures skip and say so, rather
than being quietly replaced by something that does not test the same thing.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from clawdence.domain import BuildSystem

#: Fixed identity and timestamps, so a repo built from the same files always
#: gets the same commit hash.
_GIT_ENV: Final[Mapping[str, str]] = {
    "GIT_AUTHOR_NAME": "Clawdence Fixtures",
    "GIT_AUTHOR_EMAIL": "fixtures@clawdence.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Clawdence Fixtures",
    "GIT_COMMITTER_EMAIL": "fixtures@clawdence.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    # Ignore the developer's own config. A global `commit.gpgsign = true` turns
    # the suite into a prompt for a passphrase that nobody is watching for.
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    # A terminal prompt inside a test run is a hang, not a failure.
    "GIT_TERMINAL_PROMPT": "0",
}

#: Files that identify each build system, and what the probe (S9) will read.
#: Contents are the minimum that makes the file recognisable — nothing here is
#: ever built, and a fixture that tried to be buildable would need a network.
_MANIFESTS: Final[Mapping[BuildSystem, Mapping[str, str]]] = {
    BuildSystem.MAVEN: {
        "pom.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>invalid.clawdence</groupId>\n"
            "  <artifactId>fixture</artifactId>\n"
            "  <version>0.0.1</version>\n"
            "  <dependencies>\n%s  </dependencies>\n"
            "</project>\n"
        ),
        "mvnw": "#!/bin/sh\nexit 1\n",
        ".java-version": "21\n",
    },
    BuildSystem.GRADLE: {
        "build.gradle.kts": "plugins { java }\n\ndependencies {\n%s}\n",
        "gradlew": "#!/bin/sh\nexit 1\n",
        "settings.gradle.kts": 'rootProject.name = "fixture"\n',
    },
    BuildSystem.NPM: {
        "package.json": '{\n  "name": "fixture",\n  "version": "0.0.1",\n'
        '  "devDependencies": {\n%s  }\n}\n',
        "package-lock.json": '{\n  "lockfileVersion": 3\n}\n',
        ".nvmrc": "24\n",
    },
    BuildSystem.UV: {
        "pyproject.toml": '[project]\nname = "fixture"\nversion = "0.0.1"\ndependencies = [\n%s]\n',
        "uv.lock": "version = 1\n",
        ".python-version": "3.12\n",
    },
    BuildSystem.GO: {
        "go.mod": "module clawdence.invalid/fixture\n\ngo 1.23\n\nrequire (\n%s)\n",
        "main.go": "package main\n\nfunc main() {}\n",
    },
    BuildSystem.CARGO: {
        "Cargo.toml": '[package]\nname = "fixture"\nversion = "0.0.1"\n\n[dependencies]\n%s',
        "src/main.rs": "fn main() {}\n",
    },
}

#: The dependency line that makes ``needs_docker`` true, per build system. This
#: is the probe's single most valuable inference (plan §3.5): the isolation tier
#: is derived from evidence in the repo rather than guessed by a user who has no
#: reason to know that mounting a docker socket is equivalent to host root.
_TESTCONTAINERS: Final[Mapping[BuildSystem, str]] = {
    BuildSystem.MAVEN: "    <dependency>\n"
    "      <groupId>org.testcontainers</groupId>\n"
    "      <artifactId>testcontainers</artifactId>\n"
    "    </dependency>\n",
    BuildSystem.GRADLE: '    testImplementation("org.testcontainers:testcontainers:1.20.4")\n',
    BuildSystem.NPM: '    "testcontainers": "^10.0.0"\n',
    BuildSystem.UV: '    "testcontainers==4.9.0",\n',
    BuildSystem.GO: "\tgithub.com/testcontainers/testcontainers-go v0.34.0\n",
    BuildSystem.CARGO: 'testcontainers = "0.23"\n',
}


#: Written with the executable bit set, because git records that bit and the
#: probe (S9) checks it: a repository whose ``mvnw`` is committed non-executable
#: is a repository whose wrapper cannot be invoked, on every machine. A fixture
#: without the bit would silently be testing that case instead of the normal one.
_EXECUTABLE: Final[frozenset[str]] = frozenset({"mvnw", "gradlew"})


class GitUnavailableError(RuntimeError):
    """``git`` is not on PATH, so a real repository cannot be built."""


def git_available() -> bool:
    return shutil.which("git") is not None


@dataclass(frozen=True, slots=True)
class FixtureRepo:
    """A real git repository, with the shape a probe would recognise."""

    path: Path
    head: str
    build_system: BuildSystem
    needs_docker: bool

    def read(self, name: str) -> str:
        return (self.path / name).read_text(encoding="utf-8")

    def write(self, name: str, contents: str) -> None:
        """Change a file without committing it — an unclean worktree."""
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")

    def commit(self, message: str = "change") -> str:
        """Commit whatever is in the worktree; return the new head.

        Timestamps stay pinned, so a second commit of the same content on the
        same parent is the same hash — which is what makes "did the head move"
        assertions mean something.
        """
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "-m", message)
        return _head(self.path)


def build_repo(
    root: Path,
    *,
    build_system: BuildSystem = BuildSystem.UV,
    testcontainers: bool = False,
    extra_files: Mapping[str, str] | None = None,
) -> FixtureRepo:
    """Create a repository at ``root`` and commit it.

    ``extra_files`` is the escape hatch for a test that needs one specific file
    — an ``AGENTS.md``, a ``compose.yaml`` — without a new build system entry.
    """
    if not git_available():
        raise GitUnavailableError("git is not on PATH")

    manifests = _MANIFESTS[build_system]
    dependency = _TESTCONTAINERS[build_system] if testcontainers else ""

    root.mkdir(parents=True, exist_ok=True)
    for name, template in manifests.items():
        # Only the manifest carries a dependency slot; the wrappers and version
        # files are literal, and `%` in them would be a formatting accident.
        contents = template % dependency if "%s" in template else template
        _write(root / name, contents)
    for name, contents in (extra_files or {}).items():
        _write(root / name, contents)

    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "Initial fixture")

    return FixtureRepo(
        path=root,
        head=_head(root),
        build_system=build_system,
        needs_docker=testcontainers,
    )


def _write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    if path.name in _EXECUTABLE:
        path.chmod(0o755)


def _git(cwd: Path, *args: str) -> str:
    """Run git with a hermetic environment. argv, never a shell.

    ``env`` is replaced rather than extended so nothing from the developer's
    session — a ``GIT_DIR``, an ssh agent, a credential helper — reaches a
    fixture. ``core.hooksPath`` is emptied because a global hooks directory
    would run somebody else's scripts inside our temporary repository.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-c", "core.hooksPath=/dev/null", *args],  # noqa: S607 - PATH lookup is checked
        cwd=cwd,
        env=dict(_GIT_ENV),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {completed.stderr.strip() or completed.stdout}"
        )
    return completed.stdout.strip()


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD")
