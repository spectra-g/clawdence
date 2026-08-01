"""The bounds, and what happens at them.

A repository is not input the probe chose — it is a fork, a branch, or under
S10b a path a stranger named. Every one of these is a refusal that has to stay
*visible*: a probe that silently skipped a file it could not read would report a
missing dependency as an absent one, and the difference between those two is the
difference between a profile that is incomplete and a profile that is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawdence.probe import MAX_FILE_BYTES, MAX_MEMBERS, Level, probe
from tests.probe.test_detection import messages, write


def test_a_symlink_pointing_out_of_the_repo_is_not_read(tmp_path: Path) -> None:
    """The file that would be read is chosen by the repository, so a manifest
    that is a symlink to something private is the obvious attack. Resolving
    first and judging where it landed costs one syscall."""
    secret = tmp_path / "outside" / "secrets.toml"
    secret.parent.mkdir(parents=True)
    secret.write_text('[project]\ndependencies = ["testcontainers"]\n', encoding="utf-8")

    root = write(tmp_path / "repo", {"go.mod": "module x\n"})
    (root / "pyproject.toml").symlink_to(secret)

    result = probe(root)

    assert result.profile.needs_docker is False
    assert "resolves outside the repository" in messages(result.findings)


def test_an_oversized_manifest_is_skipped_and_said_so(tmp_path: Path) -> None:
    """Something claiming to be a manifest and holding a megabyte is not one,
    and reading it is the probe's problem rather than the repository's."""
    root = write(tmp_path, {"go.mod": "module x\n"})
    (root / "pyproject.toml").write_text("# " + "x" * MAX_FILE_BYTES, encoding="utf-8")

    result = probe(root)

    assert "over the" in messages(result.findings)
    assert any("pyproject.toml" in path for f in result.findings for path in f.evidence)


def test_a_malformed_manifest_is_an_action_not_a_silence(tmp_path: Path) -> None:
    """A broken ``package.json`` treated as an absent one produces a profile
    with no test command and no explanation for it."""
    result = probe(write(tmp_path, {"package.json": "{not json", "package-lock.json": "{}"}))

    assert "not valid JSON" in messages(result.actions)


def test_a_malformed_pyproject_is_reported(tmp_path: Path) -> None:
    result = probe(write(tmp_path, {"pyproject.toml": "[project\n"}))
    assert "not valid TOML" in messages(result.actions)


def test_workspace_members_are_capped(tmp_path: Path) -> None:
    """A glob comes from the repository, so it is treated as one that might
    match the world."""
    files = {
        "package.json": json.dumps({"workspaces": ["packages/*"]}),
        "package-lock.json": "{}",
    }
    for index in range(MAX_MEMBERS + 5):
        files[f"packages/p{index:03d}/package.json"] = "{}"

    result = probe(write(tmp_path, files))

    assert f"stopped at {MAX_MEMBERS} workspace members" in messages(result.findings)


def test_members_are_read_in_a_stable_order(tmp_path: Path) -> None:
    """`glob` yields in directory order, which differs between machines and
    would make the snapshot tests flap."""
    files = {
        "package.json": json.dumps({"workspaces": ["packages/*"]}),
        "package-lock.json": "{}",
        "packages/zulu/package.json": '{"devDependencies": {"testcontainers": "^10"}}',
        "packages/alpha/package.json": '{"devDependencies": {"testcontainers": "^10"}}',
    }
    result = probe(write(tmp_path, files))
    docker = next(f for f in result.findings if f.profile_field == "needs_docker")

    assert docker.evidence == (
        "packages/alpha/package.json",
        "packages/zulu/package.json",
    )


@pytest.mark.parametrize("escape", ["../*", "/etc", "!packages/x"])
def test_hostile_workspace_globs_are_dropped(tmp_path: Path, escape: str) -> None:
    files = {
        "package.json": json.dumps({"workspaces": [escape]}),
        "package-lock.json": "{}",
    }
    result = probe(write(tmp_path / "repo", files))

    assert not any(f.level is Level.NOTE and "monorepo" in f.message for f in result.findings)
