"""The plan's verification for S9, as five snapshots.

*"Probe four fixtures (Maven, Gradle, Node monorepo, Python) and one
testcontainers repo. Each produces a correct profile; the testcontainers one
sets ``needs_docker: true``. Snapshot-tested."*

The snapshot holds the findings as well as the profile, which is the part worth
defending: the reasoning is what a reviewer of a proposal is actually reading,
so a change to it should be as visible in a diff as a change to a command.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawdence.domain import BuildSystem, IsolationTier
from clawdence.domain import TestReporter as Reporter
from clawdence.probe import probe, render_json
from tests.harness.repos import git_available
from tests.probe.fixtures import BUILDERS
from tests.probe.snapshots import assert_matches


@pytest.fixture(autouse=True)
def _needs_git() -> None:
    if not git_available():
        pytest.skip("git is not on PATH, so a real fixture repository cannot be built")


@pytest.mark.parametrize("name", list(BUILDERS))
def test_profile_matches_snapshot(name: str, workspace: Path) -> None:
    repo = BUILDERS[name](workspace)
    assert_matches(name, render_json(probe(repo.path)))


def test_maven_proposes_the_wrapper_and_junit(workspace: Path) -> None:
    """The wrapper is preferred over `mvn` from PATH: it is the version the
    repository pins, and it is committed executable here."""
    result = probe(BUILDERS["maven"](workspace).path)
    assert result.profile.build_system is BuildSystem.MAVEN
    assert result.profile.test_command[0] == "./mvnw"
    assert result.profile.test_reporter is Reporter.JUNIT_XML


def test_node_monorepo_finds_docker_in_a_member_and_not_in_node_modules(
    workspace: Path,
) -> None:
    """The whole reason the layout is read at all.

    The root manifest declares no testcontainers; ``packages/api`` does; and
    ``node_modules`` has one installed. The first must be looked past, the
    second found, the third ignored.
    """
    result = probe(BUILDERS["node-monorepo"](workspace).path)

    assert result.profile.needs_docker is True
    evidence = {path for finding in result.findings for path in finding.evidence}
    assert "packages/api/package.json" in evidence
    assert not any(path.startswith("node_modules/") for path in evidence)


def test_the_testcontainers_repo_needs_docker_and_is_not_granted_it(workspace: Path) -> None:
    """S8's gate, seen from the other side.

    ``needs_docker`` is the probe's inference and it is loud. The tier is not:
    the profile it proposes has no daemon in it, and it says whose decision that
    is. A probe that filled in ``docker_socket_acknowledged`` would be defeating
    the gate it was inferring for.
    """
    result = probe(BUILDERS["testcontainers"](workspace).path)

    assert result.profile.needs_docker is True
    assert result.profile.isolation_tier is IsolationTier.CONTAINER
    assert result.profile.docker_socket_acknowledged is False

    asked = [finding for finding in result.actions if finding.profile_field == "isolation_tier"]
    assert len(asked) == 1
    assert "docker_socket_acknowledged" in asked[0].message


def test_every_proposed_profile_validates(workspace: Path) -> None:
    """A proposal that does not round-trip is one nobody can commit.

    Cheap here and worth having: the profile is assembled field by field from
    detection results, and ``RepoProfile`` has a validator that rejects some
    combinations of them outright.
    """
    for name, build in BUILDERS.items():
        result = probe(build(workspace).path)
        assert result.profile.model_validate(result.profile.model_dump()) == result.profile, name
