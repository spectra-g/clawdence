"""Fixture repositories — real git, deterministic hashes, hermetic config."""

from __future__ import annotations

from pathlib import Path

import pytest

from clawdence.domain import BuildSystem
from tests.conftest import RepoFactory
from tests.harness.repos import GitUnavailableError, build_repo, git_available


def test_a_fixture_is_a_real_repository(repos: RepoFactory) -> None:
    """Real git because evidence binds to a tree hash, and the whole
    merge-safety property is about hashes changing when they should. A fixture
    that invented them would let those tests pass without meeting a real one."""
    repo = repos()
    assert (repo.path / ".git").is_dir()
    assert len(repo.head) == 40
    assert int(repo.head, 16) >= 0


def test_hashes_are_deterministic(repos: RepoFactory) -> None:
    """Identity and both timestamps are pinned, so the same files give the same
    commit. That is what lets a test assert on an exact hash."""
    assert repos().head == repos().head


def test_committing_moves_the_head(repos: RepoFactory) -> None:
    repo = repos()
    repo.write("src/thing.py", "def thing() -> int:\n    return 1\n")
    moved = repo.commit("add a thing")
    assert moved != repo.head
    assert len(moved) == 40


@pytest.mark.parametrize(
    ("build_system", "manifest"),
    [
        (BuildSystem.MAVEN, "pom.xml"),
        (BuildSystem.GRADLE, "build.gradle.kts"),
        (BuildSystem.NPM, "package.json"),
        (BuildSystem.UV, "pyproject.toml"),
        (BuildSystem.GO, "go.mod"),
        (BuildSystem.CARGO, "Cargo.toml"),
    ],
)
def test_each_build_system_gets_its_manifest(
    repos: RepoFactory, build_system: BuildSystem, manifest: str
) -> None:
    """S9's probe reads which files are present. That is the whole shape these
    fixtures need to have, and it is why they can be synthesised."""
    repo = repos(build_system)
    assert (repo.path / manifest).exists()
    assert repo.build_system is build_system


@pytest.mark.parametrize(
    "build_system",
    [BuildSystem.MAVEN, BuildSystem.GRADLE, BuildSystem.NPM, BuildSystem.UV, BuildSystem.CARGO],
)
def test_testcontainers_shows_up_in_the_manifest(
    repos: RepoFactory, build_system: BuildSystem
) -> None:
    """The probe's single most valuable inference: the isolation tier follows
    from evidence in the repo, not from a user who has no reason to know that
    mounting a docker socket is equivalent to host root."""
    plain = repos(build_system)
    docked = repos(build_system, testcontainers=True)

    manifest = next(
        name
        for name in ("pom.xml", "build.gradle.kts", "package.json", "pyproject.toml", "Cargo.toml")
        if (docked.path / name).exists()
    )
    assert "testcontainers" in docked.read(manifest)
    assert "testcontainers" not in plain.read(manifest)
    assert docked.needs_docker is True
    assert plain.needs_docker is False


def test_go_declares_testcontainers_too(repos: RepoFactory) -> None:
    repo = repos(BuildSystem.GO, testcontainers=True)
    assert "testcontainers-go" in repo.read("go.mod")


def test_extra_files_are_the_escape_hatch(repos: RepoFactory) -> None:
    """For the test that needs one specific file without a new build system."""
    repo = repos(extra_files={"AGENTS.md": "# Conventions\n\nUse tabs. (Do not.)\n"})
    assert "Conventions" in repo.read("AGENTS.md")


def test_wrapper_scripts_are_present_but_not_runnable(repos: RepoFactory) -> None:
    """The probe reads that ``mvnw`` exists; nothing here ever runs it, and a
    fixture that tried to be buildable would need a network."""
    repo = repos(BuildSystem.MAVEN)
    assert (repo.path / "mvnw").exists()
    assert repo.read("mvnw").startswith("#!/bin/sh")


def test_toolchain_pins_are_present(repos: RepoFactory) -> None:
    """``RepoProfile.toolchain`` is read from these, so S9 needs them to exist."""
    assert repos(BuildSystem.MAVEN).read(".java-version").strip() == "21"
    assert repos(BuildSystem.NPM).read(".nvmrc").strip() == "24"
    assert repos(BuildSystem.UV).read(".python-version").strip() == "3.12"


def test_building_without_git_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skipping is honest; being quietly replaced by something that does not
    test the same thing is not."""
    monkeypatch.setattr("tests.harness.repos.git_available", lambda: False)
    with pytest.raises(GitUnavailableError):
        build_repo(tmp_path / "repo")


def test_git_availability_is_checkable() -> None:
    assert isinstance(git_available(), bool)
