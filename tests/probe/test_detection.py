"""The detection table, one row at a time.

These build plain directories rather than git repositories: what is under test
is what a *file* implies, and a commit adds nothing to that while costing a
subprocess. The five real repositories in ``test_snapshots`` cover the whole
shape end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawdence.domain import BuildSystem
from clawdence.domain import TestReporter as Reporter
from clawdence.probe import Level, probe
from clawdence.probe.findings import Finding


def write(root: Path, files: dict[str, str]) -> Path:
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    return root


def messages(findings: tuple[Finding, ...], *, field: str | None = None) -> str:
    chosen = [f for f in findings if field is None or f.profile_field == field]
    return "\n".join(f.message for f in chosen)


# -- build system --------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"pom.xml": "<project/>"}, BuildSystem.MAVEN),
        ({"build.gradle.kts": "plugins { java }"}, BuildSystem.GRADLE),
        ({"go.mod": "module x\n"}, BuildSystem.GO),
        ({"Cargo.toml": '[package]\nname = "x"\n'}, BuildSystem.CARGO),
        ({"pyproject.toml": "[project]\n", "uv.lock": "version = 1\n"}, BuildSystem.UV),
        ({"pyproject.toml": "[tool.poetry]\n"}, BuildSystem.POETRY),
        ({"setup.py": "from setuptools import setup\n"}, BuildSystem.PIP),
        ({"package.json": "{}", "package-lock.json": "{}"}, BuildSystem.NPM),
        ({"package.json": "{}", "yarn.lock": ""}, BuildSystem.YARN),
        ({"package.json": "{}", "pnpm-lock.yaml": ""}, BuildSystem.PNPM),
    ],
)
def test_each_manifest_identifies_its_build_system(
    tmp_path: Path, files: dict[str, str], expected: BuildSystem
) -> None:
    assert probe(write(tmp_path, files)).profile.build_system is expected


def test_nothing_recognised_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """A directory with no manifest still produces a profile — with the fields
    empty and a finding saying which files were looked for. An exception here
    would make `clawdence probe` unusable as the first thing anyone runs."""
    result = probe(write(tmp_path, {"README.md": "# hello\n"}))

    assert result.profile.build_system is BuildSystem.UNKNOWN
    assert result.profile.test_command == ()
    assert "no build system recognised" in messages(result.actions)


def test_a_polyglot_repo_picks_one_and_says_so(tmp_path: Path) -> None:
    """Maven wins over an incidental package.json — a lint script in a Java
    service is the common case, a Node service with a stray pom.xml is not —
    but the reviewer is told, because the commands are probably wrong."""
    result = probe(write(tmp_path, {"pom.xml": "<project/>", "package.json": "{}"}))

    assert result.profile.build_system is BuildSystem.MAVEN
    assert "more than one build system" in messages(result.actions, field="build_system")


# -- commands ------------------------------------------------------------


def test_a_wrapper_without_its_executable_bit_is_not_proposed(tmp_path: Path) -> None:
    """git records the bit, so this is a property of the commit: the wrapper
    fails the same way on every machine, several minutes into a container, with
    an error that names nothing anybody could search for."""
    root = write(tmp_path, {"pom.xml": "<project/>", "mvnw": "#!/bin/sh\n"})
    result = probe(root)

    assert result.profile.test_command[0] == "mvn"
    assert "executable bit" in messages(result.actions)


def test_the_npm_placeholder_script_is_not_a_test_command(tmp_path: Path) -> None:
    """`npm init` writes a test script that exits 1 by design. Proposing it
    would give the profile a command whose failure looks like this
    repository's tests failing."""
    manifest = json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})
    result = probe(write(tmp_path, {"package.json": manifest, "package-lock.json": "{}"}))

    assert result.profile.test_command == ()
    assert "placeholder" in messages(result.actions, field="test_command")


def test_a_node_repo_without_a_lockfile_gets_no_install_command(tmp_path: Path) -> None:
    """`npm install` resolves fresh versions, so two runs of the same commit
    would install two different dependency trees."""
    result = probe(write(tmp_path, {"package.json": json.dumps({"scripts": {"test": "jest"}})}))

    assert result.profile.install_command == ()
    assert result.profile.test_command == ("npm", "test")
    assert "no lockfile" in messages(result.actions, field="install_command")


def test_corepack_declaration_beats_the_lockfile(tmp_path: Path) -> None:
    """`packageManager` is what corepack will enforce inside the runner, so a
    disagreeing lockfile is stale rather than authoritative."""
    manifest = json.dumps({"packageManager": "pnpm@9.12.0", "scripts": {"test": "vitest"}})
    result = probe(write(tmp_path, {"package.json": manifest, "package-lock.json": "{}"}))

    assert result.profile.build_system is BuildSystem.PNPM
    assert result.profile.install_command == ()
    assert "stale" in messages(result.findings)


def test_yarn_berry_gets_the_flag_it_accepts(tmp_path: Path) -> None:
    """Yarn 2+ rejects `--frozen-lockfile` outright."""
    result = probe(
        write(
            tmp_path,
            {"package.json": "{}", "yarn.lock": "", ".yarnrc.yml": "nodeLinker: node-modules\n"},
        )
    )
    assert result.profile.install_command == ("yarn", "install", "--immutable")


def test_python_without_pytest_evidence_gets_no_test_command(tmp_path: Path) -> None:
    """pytest is the right guess most of the time, which is exactly why it is
    not guessed: on a unittest repository the proposed command collects nothing
    and exits 0, which reads downstream as a suite that passed."""
    result = probe(write(tmp_path, {"pyproject.toml": '[project]\nname = "x"\n'}))

    assert result.profile.test_command == ()
    assert "nothing in this repository says it runs pytest" in messages(result.actions)


@pytest.mark.parametrize(
    "files",
    [
        {"pyproject.toml": "[project]\n", "pytest.ini": "[pytest]\n"},
        {"pyproject.toml": "[tool.pytest.ini_options]\n"},
        {"pyproject.toml": '[project]\ndependencies = ["pytest>=8"]\n'},
        {"pyproject.toml": "[project]\n", "tests/conftest.py": ""},
    ],
)
def test_any_pytest_evidence_is_enough(tmp_path: Path, files: dict[str, str]) -> None:
    assert probe(write(tmp_path, files)).profile.test_command == ("pytest",)


def test_the_reporter_is_claimed_only_where_the_format_is_written_anyway(
    tmp_path: Path,
) -> None:
    """Surefire writes JUnit XML with no flag; `go test` needs `-json`, and
    pytest needs a plugin. A reporter is a claim about output that exists, not
    about a library being installable."""
    maven = probe(write(tmp_path / "jvm", {"pom.xml": "<project/>"}))
    go = probe(write(tmp_path / "go", {"go.mod": "module x\n"}))

    assert maven.profile.test_reporter is Reporter.JUNIT_XML
    assert go.profile.test_reporter is Reporter.NONE


# -- needs_docker --------------------------------------------------------


@pytest.mark.parametrize(
    "files",
    [
        {"pom.xml": "<project><dependency>org.testcontainers</dependency></project>"},
        {"build.gradle.kts": 'testImplementation("org.testcontainers:junit-jupiter:1.20.4")'},
        {"go.mod": "module x\n\nrequire github.com/testcontainers/testcontainers-go v0.34.0\n"},
        {"Cargo.toml": '[dev-dependencies]\ntestcontainers = "0.23"\n'},
        {"pyproject.toml": '[project]\ndependencies = ["testcontainers==4.9.0"]\n'},
        {"package.json": '{"devDependencies": {"testcontainers": "^10.0.0"}}'},
        {"pom.xml": "<project/>", "compose.yaml": "services: {}\n"},
    ],
)
def test_declared_testcontainers_or_a_compose_file_needs_docker(
    tmp_path: Path, files: dict[str, str]
) -> None:
    assert probe(write(tmp_path, files)).profile.needs_docker is True


def test_an_indirect_go_dependency_does_not_need_docker(tmp_path: Path) -> None:
    """go.mod lists the indirect closure inline, so a substring match would
    make every Go repository that transitively touches testcontainers ask for
    a daemon."""
    module = (
        "module x\n\nrequire (\n"
        "\tgithub.com/testcontainers/testcontainers-go v0.34.0 // indirect\n)\n"
    )
    assert probe(write(tmp_path, {"go.mod": module})).profile.needs_docker is False


def test_a_dockerfile_is_not_a_docker_requirement(tmp_path: Path) -> None:
    """An image the project publishes is built by CI, not by the tests the
    runner runs. On that signal nearly every modern repository would ask for a
    daemon — and the report says so rather than staying silent."""
    result = probe(write(tmp_path, {"go.mod": "module x\n", "Dockerfile": "FROM scratch\n"}))

    assert result.profile.needs_docker is False
    assert "Dockerfile" in messages(result.findings, field="needs_docker")


def test_an_installed_dependency_is_not_a_declared_one(tmp_path: Path) -> None:
    """The trap that would otherwise flip the tier of every repository probed
    after an install."""
    files = {
        "package.json": '{"dependencies": {}}',
        "package-lock.json": "{}",
        "node_modules/testcontainers/package.json": '{"name": "testcontainers"}',
        "vendor/testcontainers/go.mod": "module testcontainers\n",
    }
    assert probe(write(tmp_path, files)).profile.needs_docker is False


# -- toolchain, layout, identity ----------------------------------------


def test_toolchain_pins_are_read_but_exec_prefix_is_not_invented(tmp_path: Path) -> None:
    """Whether a pin can be honoured is a fact about the runner image. A
    guessed `mise exec --` turns every command in the profile into one that
    fails with "mise: not found"."""
    files = {
        "go.mod": "module x\n",
        ".tool-versions": "# comment\nnodejs 22.11.0\ngolang 1.23.4\n",
        ".java-version": "21\n",
    }
    result = probe(write(tmp_path, files))

    assert result.profile.toolchain == {"node": "22.11.0", "go": "1.23.4", "java": "21"}
    assert result.profile.exec_prefix == ()
    assert "exec_prefix was left empty" in messages(result.findings, field="exec_prefix")


def test_mise_wins_over_the_single_language_files(tmp_path: Path) -> None:
    files = {
        "go.mod": "module x\n",
        ".mise.toml": '[tools]\nnode = "24.5.0"\njava = { version = "21.0.5" }\n',
        ".nvmrc": "18\n",
    }
    assert probe(write(tmp_path, files)).profile.toolchain["node"] == "24.5.0"


def test_the_conventions_file_is_found(tmp_path: Path) -> None:
    result = probe(write(tmp_path, {"go.mod": "module x\n", "AGENTS.md": "# rules\n"}))
    assert result.profile.agents_md_path == "AGENTS.md"


def test_the_name_and_id_come_from_the_directory_without_a_remote(tmp_path: Path) -> None:
    root = write(tmp_path / "Acme Service!", {"go.mod": "module x\n"})
    result = probe(root)

    assert result.profile.name == "Acme Service!"
    assert result.profile.id == "Acme-Service-"
    assert "not a git repository" in messages(result.actions, field="remote_url")


def test_overrides_are_taken_verbatim(tmp_path: Path) -> None:
    result = probe(write(tmp_path, {"go.mod": "module x\n"}), name="ledger", repo_id="acme.ledger")
    assert (result.profile.name, result.profile.id) == ("ledger", "acme.ledger")


def test_a_missing_directory_is_an_error_not_a_profile(tmp_path: Path) -> None:
    from clawdence.probe import ProbeError

    with pytest.raises(ProbeError):
        probe(tmp_path / "nope")


def test_findings_carry_the_file_that_justified_them(tmp_path: Path) -> None:
    """Evidence is the whole point of a proposal: a reviewer's only question is
    how the probe knows, and every path is repo-relative so the report can be
    pasted into an issue without leaking the prober's directory layout."""
    result = probe(write(tmp_path, {"pom.xml": "<project/>"}))
    decided = [f for f in result.findings if f.level is Level.DECIDED and f.evidence]

    assert decided
    assert all(not path.startswith("/") for f in result.findings for path in f.evidence)
