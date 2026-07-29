"""The detection table from §3.5, and the commands each build system implies.

Two rules run through all of it, and they are the reason this is longer than a
dictionary of filenames.

**The probe leaves a field empty rather than guessing.** An empty
``test_command`` asks the reviewer a question; a wrong one answers it. So a Node
repository whose ``package.json`` has no ``scripts.test`` gets no test command
and a finding saying why — not ``npm test``, which would produce a profile that
looks complete and fails on first use, several minutes into a container.

**A command the probe proposes is one the runner can execute.** ``install`` is
the warm-cache phase (S7), so it is the package manager's fetch step and not its
build. Every command is argv, never a shell string — ``ScriptStage.command``'s
rule — which is also why the Node commands delegate to the package manager
rather than inlining what ``scripts.test`` contains: that value *is* shell, and
the package manager is the thing that knows how to run it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from clawdence.domain import BuildSystem, TestReporter
from clawdence.probe.findings import FindingLog
from clawdence.probe.scan import Tree

#: What ``npm init`` writes when nobody supplied a test command. Reporting it as
#: this repository's test command would give the profile a command that exits 1
#: by design, which reads downstream as a repository whose tests are broken.
_NPM_PLACEHOLDER: Final = "no test specified"

#: Dependency-declaring manifests, per build system. Lockfiles are absent on
#: purpose — see ``scan``: a lockfile is the transitive closure, not what this
#: repository asked for.
MANIFESTS: Final[Mapping[BuildSystem, tuple[str, ...]]] = {
    BuildSystem.MAVEN: ("pom.xml",),
    BuildSystem.GRADLE: ("build.gradle", "build.gradle.kts", "gradle/libs.versions.toml"),
    BuildSystem.NPM: ("package.json",),
    BuildSystem.YARN: ("package.json",),
    BuildSystem.PNPM: ("package.json",),
    BuildSystem.UV: ("pyproject.toml",),
    BuildSystem.POETRY: ("pyproject.toml",),
    BuildSystem.PIP: ("pyproject.toml", "setup.py", "requirements.txt", "requirements-dev.txt"),
    BuildSystem.GO: ("go.mod",),
    BuildSystem.CARGO: ("Cargo.toml",),
    BuildSystem.UNKNOWN: (),
}


@dataclass(frozen=True, slots=True)
class Stack:
    """One build system, and everything derived from it."""

    build_system: BuildSystem
    #: The file that identified it. This is the evidence for ``build_system``.
    manifest: str
    install: tuple[str, ...] = ()
    build: tuple[str, ...] = ()
    test: tuple[str, ...] = ()
    reporter: TestReporter = TestReporter.NONE
    #: Workspace globs this manifest declares, for the member scan.
    workspace_globs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A build system the probe might select, and how to tell."""

    label: str
    markers: tuple[str, ...]
    detect: Callable[[Tree, FindingLog], Stack]


def detect(tree: Tree, log: FindingLog) -> Stack:
    """Identify the build system, then derive everything from it.

    Selection is two-phase — cheap markers first, then one full detection —
    because detection *reports*. Running every detector to pick one would fill
    the report with findings about build systems this repository does not have.
    """
    present = [candidate for candidate in _CANDIDATES if _marks(tree, candidate)]
    if not present:
        log.action(
            "no build system recognised: none of pom.xml, build.gradle[.kts], package.json, "
            "pyproject.toml, setup.py, go.mod or Cargo.toml is at the repository root. "
            "Set build_system and the three commands by hand",
            field="build_system",
        )
        return Stack(build_system=BuildSystem.UNKNOWN, manifest="")

    chosen = present[0]
    if len(present) > 1:
        others = ", ".join(candidate.label for candidate in present[1:])
        log.action(
            f"more than one build system is present ({chosen.label}, {others}). The probe took "
            f"{chosen.label} because a root manifest for it is the least likely to be "
            f"incidental, but a polyglot repository usually needs its commands written by "
            f"hand — check them before committing this",
            field="build_system",
        )
    return chosen.detect(tree, log)


def _marks(tree: Tree, candidate: _Candidate) -> bool:
    return any(tree.has(marker) for marker in candidate.markers)


# -- JVM -----------------------------------------------------------------


def _maven(tree: Tree, log: FindingLog) -> Stack:
    launcher = _wrapper(tree, log, wrapper="mvnw", fallback="mvn")
    log.decided("Maven, from the project object model", "pom.xml", field="build_system")
    # `-ntp` (no transfer progress) because the runner captures this output and
    # feeds a slice of it to a model: a download progress bar is thousands of
    # lines of context spent on nothing.
    flags = ("-B", "-ntp")
    log.decided(
        "Surefire writes JUnit XML to target/surefire-reports on every run, so the reporter "
        "needs no flag and no plugin",
        "pom.xml",
        field="test_reporter",
    )
    return Stack(
        build_system=BuildSystem.MAVEN,
        manifest="pom.xml",
        install=(launcher, *flags, "dependency:go-offline"),
        build=(launcher, *flags, "-DskipTests", "package"),
        test=(launcher, *flags, "test"),
        reporter=TestReporter.JUNIT_XML,
        workspace_globs=_maven_modules(tree),
    )


def _maven_modules(tree: Tree) -> tuple[str, ...]:
    """``<module>`` entries, by substring rather than by parsing (see ``scan``)."""
    text = tree.text("pom.xml") or ""
    modules: list[str] = []
    for chunk in text.split("<module>")[1:]:
        name, _, rest = chunk.partition("</module>")
        if rest and name.strip() and "/" not in name.strip()[:1]:
            modules.append(name.strip())
    return tuple(modules)


def _gradle(tree: Tree, log: FindingLog) -> Stack:
    launcher = _wrapper(tree, log, wrapper="gradlew", fallback="gradle")
    manifest = tree.first("build.gradle.kts", "build.gradle") or "settings.gradle.kts"
    log.decided("Gradle, from the build script", manifest, field="build_system")
    log.decided(
        "the test task writes JUnit XML under build/test-results by default",
        manifest,
        field="test_reporter",
    )
    # `--no-daemon`: the daemon is a background JVM that outlives the command
    # that started it. In a container that is a process the run does not know it
    # owns, and the runner's teardown would kill it mid-write anyway.
    flags = ("--no-daemon",)
    includes = _gradle_includes(tree)
    if includes:
        log.note(
            "the install command runs the root project's `dependencies` task, which resolves "
            "the root's configurations and not every subproject's. It warms most of the cache "
            "for a multi-project build and not all of it",
            manifest,
            field="install_command",
        )
    return Stack(
        build_system=BuildSystem.GRADLE,
        manifest=manifest,
        install=(launcher, *flags, "dependencies"),
        build=(launcher, *flags, "assemble"),
        test=(launcher, *flags, "test"),
        reporter=TestReporter.JUNIT_XML,
        workspace_globs=includes,
    )


def _gradle_includes(tree: Tree) -> tuple[str, ...]:
    """``include("a:b")`` from settings, as directory paths.

    Substring again, and the reason is stronger here than for Maven: a settings
    file is Kotlin or Groovy, so the authoritative reading of it is executing
    it, which is not something a probe does to a repository.
    """
    settings = tree.first("settings.gradle.kts", "settings.gradle")
    text = tree.text(settings) if settings else None
    if not text:
        return ()
    projects: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("include"):
            continue
        for quoted in _quoted(stripped):
            path = quoted.lstrip(":").replace(":", "/")
            if path and path not in projects:
                projects.append(path)
    return tuple(projects)


def _quoted(line: str) -> tuple[str, ...]:
    parts: list[str] = []
    for quote in ("'", '"'):
        chunks = line.split(quote)
        parts.extend(chunk for index, chunk in enumerate(chunks) if index % 2 == 1)
    return tuple(parts)


def _wrapper(tree: Tree, log: FindingLog, *, wrapper: str, fallback: str) -> str:
    """The repo's wrapper script if it can actually be run, else the bare tool.

    The executable bit is checked rather than assumed because git records it:
    a wrapper committed without it is a property of the repository, it breaks
    the same way on every machine, and the failure — "permission denied" from a
    container, several minutes in — names nothing that would lead anyone here.
    """
    if not tree.has(wrapper):
        log.note(
            f"no {wrapper} in the repository, so commands call {fallback} from PATH and depend "
            f"on the runner image's version rather than the one this repository pins"
        )
        return fallback
    if not tree.is_executable(wrapper):
        log.action(
            f"{wrapper} is committed without its executable bit, so the runner cannot invoke "
            f"it; commands fall back to {fallback}. `git update-index --chmod=+x {wrapper}` "
            f"fixes it in the repository",
            wrapper,
        )
        return fallback
    return f"./{wrapper}"


# -- Node ----------------------------------------------------------------

#: Lockfile → package manager. Order is precedence for the pathological repo
#: that has committed more than one.
_NODE_LOCKS: Final[tuple[tuple[str, BuildSystem], ...]] = (
    ("pnpm-lock.yaml", BuildSystem.PNPM),
    ("yarn.lock", BuildSystem.YARN),
    ("package-lock.json", BuildSystem.NPM),
    ("npm-shrinkwrap.json", BuildSystem.NPM),
)

_NODE_INSTALL: Final[Mapping[BuildSystem, tuple[str, ...]]] = {
    BuildSystem.NPM: ("npm", "ci"),
    BuildSystem.PNPM: ("pnpm", "install", "--frozen-lockfile"),
    BuildSystem.YARN: ("yarn", "install", "--frozen-lockfile"),
}


def _node(tree: Tree, log: FindingLog) -> Stack:
    manifest = tree.mapping("package.json") or {}
    system, lockfile = _package_manager(tree, log, manifest)

    install: tuple[str, ...] = ()
    if lockfile is not None:
        install = _NODE_INSTALL[system]
        if system is BuildSystem.YARN and tree.has(".yarnrc.yml"):
            # Yarn 2+ renamed the flag and rejects the old one outright.
            install = ("yarn", "install", "--immutable")
            log.decided("Yarn 2+ (.yarnrc.yml), so the install is --immutable", ".yarnrc.yml")
        log.decided(f"{system.value}, from the lockfile", lockfile, field="build_system")
    else:
        log.action(
            f"no lockfile for {system.value}, so no install command was proposed. An install "
            f"that resolves versions fresh gives two runs of the same commit two different "
            f"dependency trees, which is a class of failure nobody attributes to the runner",
            "package.json",
            field="install_command",
        )

    runner = system.value
    scripts = _scripts(manifest)
    return Stack(
        build_system=system,
        manifest="package.json",
        install=install,
        build=(runner, "run", "build") if "build" in scripts else (),
        test=_node_test(scripts, runner, log),
        workspace_globs=_node_workspaces(tree, manifest),
    )


def _package_manager(
    tree: Tree, log: FindingLog, manifest: Mapping[str, Any]
) -> tuple[BuildSystem, str | None]:
    """Corepack's declaration first, then the lockfile.

    ``packageManager`` is the repository saying which tool it is *for*; a
    lockfile is evidence of which tool was last run. When they disagree the
    declaration wins, because that is what corepack itself will enforce inside
    the runner.
    """
    lockfile = next((name for name, _ in _NODE_LOCKS if tree.has(name)), None)
    from_lock = dict(_NODE_LOCKS).get(lockfile or "", BuildSystem.NPM)

    declared = manifest.get("packageManager")
    if isinstance(declared, str) and declared:
        name = declared.split("@", 1)[0].strip().lower()
        for system in (BuildSystem.PNPM, BuildSystem.YARN, BuildSystem.NPM):
            if name == system.value:
                if lockfile is not None and from_lock is not system:
                    log.note(
                        f"packageManager says {name} and the committed lockfile is for "
                        f"{from_lock.value}; the declaration wins, and the lockfile is "
                        f"probably stale",
                        "package.json",
                        lockfile,
                    )
                    lockfile = None
                return system, lockfile
        log.action(
            f"packageManager names {name!r}, which is not one this system supports "
            f"(npm, yarn, pnpm). Set build_system and the commands by hand",
            "package.json",
            field="build_system",
        )
    return from_lock, lockfile


def _scripts(manifest: Mapping[str, Any]) -> Mapping[str, str]:
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {name: value for name, value in scripts.items() if isinstance(value, str)}


def _node_test(scripts: Mapping[str, str], runner: str, log: FindingLog) -> tuple[str, ...]:
    declared = scripts.get("test")
    if declared is None:
        log.action(
            "package.json declares no scripts.test, so no test command was proposed — a "
            "repository whose tests the system cannot run can still be built and reviewed, "
            "but nothing will verify it",
            "package.json",
            field="test_command",
        )
        return ()
    if _NPM_PLACEHOLDER in declared:
        log.action(
            "scripts.test is the placeholder `npm init` writes, which exits 1 by design. No "
            "test command was proposed; a profile carrying that one would look like a "
            "repository whose tests fail",
            "package.json",
            field="test_command",
        )
        return ()
    log.decided(f"scripts.test is {declared!r}, run through {runner}", "package.json")
    return (runner, "test")


def _node_workspaces(tree: Tree, manifest: Mapping[str, Any]) -> tuple[str, ...]:
    declared = manifest.get("workspaces")
    globs: list[str] = []
    if isinstance(declared, list):
        globs.extend(entry for entry in declared if isinstance(entry, str))
    elif isinstance(declared, dict):
        packages = declared.get("packages")
        if isinstance(packages, list):
            globs.extend(entry for entry in packages if isinstance(entry, str))

    workspace = tree.yaml("pnpm-workspace.yaml")
    if workspace is not None:
        packages = workspace.get("packages")
        if isinstance(packages, list):
            globs.extend(entry for entry in packages if isinstance(entry, str))
    return tuple(dict.fromkeys(globs))


# -- Python --------------------------------------------------------------


def _python(tree: Tree, log: FindingLog) -> Stack:
    pyproject = tree.toml("pyproject.toml") or {}
    system, evidence = _python_manager(tree, pyproject)
    log.decided(f"Python with {system.value}", evidence, field="build_system")

    install = {
        BuildSystem.UV: ("uv", "sync", "--frozen"),
        BuildSystem.POETRY: ("poetry", "install", "--no-interaction", "--no-ansi"),
        BuildSystem.PIP: _pip_install(tree),
    }[system]
    prefix = {
        BuildSystem.UV: ("uv", "run"),
        BuildSystem.POETRY: ("poetry", "run"),
        BuildSystem.PIP: (),
    }[system]
    log.note(
        "no build command: a Python project has no build step between installing and "
        "testing, so an empty build_command here is the shape of the language rather than "
        "something the probe failed to find",
        field="build_command",
    )

    return Stack(
        build_system=system,
        manifest=tree.first("pyproject.toml", "setup.py") or "requirements.txt",
        install=install,
        test=(*prefix, "pytest") if _has_pytest(tree, pyproject, log) else (),
        workspace_globs=_python_workspaces(pyproject),
    )


def _python_manager(tree: Tree, pyproject: Mapping[str, Any]) -> tuple[BuildSystem, str]:
    if tree.has("uv.lock"):
        return BuildSystem.UV, "uv.lock"
    if tree.has("poetry.lock"):
        return BuildSystem.POETRY, "poetry.lock"
    tool = pyproject.get("tool")
    if isinstance(tool, dict) and "poetry" in tool:
        return BuildSystem.POETRY, "pyproject.toml"
    return BuildSystem.PIP, tree.first("pyproject.toml", "setup.py") or "requirements.txt"


def _pip_install(tree: Tree) -> tuple[str, ...]:
    requirements = tree.first("requirements.txt", "requirements-dev.txt")
    if requirements is not None:
        return ("pip", "install", "-r", requirements)
    if tree.has("pyproject.toml") or tree.has("setup.py"):
        return ("pip", "install", "-e", ".")
    return ()


def _has_pytest(tree: Tree, pyproject: Mapping[str, Any], log: FindingLog) -> bool:
    """Evidence that this repository runs pytest, rather than the assumption.

    ``pytest`` is the default a human would reach for and it is right most of
    the time, which is exactly why it is worth being strict: the repositories it
    is wrong for are the ones running unittest or nose, where the proposed
    command collects nothing and reports success.
    """
    configs = ("pytest.ini", "tox.ini", "setup.cfg", "conftest.py", "tests/conftest.py")
    config = tree.first(*configs)
    if config is not None and (config != "tox.ini" or "pytest" in (tree.text(config) or "")):
        log.decided("pytest, from its configuration", config, field="test_command")
        return True

    tool = pyproject.get("tool")
    if isinstance(tool, dict) and "pytest" in tool:
        log.decided(
            "pytest, from [tool.pytest] in pyproject.toml", "pyproject.toml", field="test_command"
        )
        return True

    if any("pytest" in requirement for requirement in python_requirements(pyproject)):
        log.decided(
            "pytest, from the declared dependencies", "pyproject.toml", field="test_command"
        )
        return True

    log.action(
        "nothing in this repository says it runs pytest — no configuration section, no "
        "conftest.py, no dependency — so no test command was proposed rather than one that "
        "would collect nothing and exit 0",
        field="test_command",
    )
    return False


def _python_workspaces(pyproject: Mapping[str, Any]) -> tuple[str, ...]:
    tool = pyproject.get("tool")
    if not isinstance(tool, dict):
        return ()
    uv = tool.get("uv")
    if not isinstance(uv, dict):
        return ()
    workspace = uv.get("workspace")
    if not isinstance(workspace, dict):
        return ()
    members = workspace.get("members")
    if not isinstance(members, list):
        return ()
    return tuple(member for member in members if isinstance(member, str))


def python_requirements(pyproject: Mapping[str, Any]) -> tuple[str, ...]:
    """Every declared dependency string, from all four places they live."""
    found: list[str] = []
    project = pyproject.get("project")
    if isinstance(project, dict):
        found.extend(_strings(project.get("dependencies")))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                found.extend(_strings(group))
    groups = pyproject.get("dependency-groups")
    if isinstance(groups, dict):
        for group in groups.values():
            found.extend(_strings(group))
    tool = pyproject.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            for table in ("dependencies", "dev-dependencies"):
                entries = poetry.get(table)
                if isinstance(entries, dict):
                    found.extend(str(name) for name in entries)
            groups_table = poetry.get("group")
            if isinstance(groups_table, dict):
                for group in groups_table.values():
                    entries = group.get("dependencies") if isinstance(group, dict) else None
                    if isinstance(entries, dict):
                        found.extend(str(name) for name in entries)
    return tuple(found)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(entry for entry in value if isinstance(entry, str))


# -- Go, Rust ------------------------------------------------------------


def _go(tree: Tree, log: FindingLog) -> Stack:
    log.decided("Go modules", "go.mod", field="build_system")
    log.note(
        "`go test` writes machine-readable output only with -json, which is left off: the "
        "flag replaces the human-readable stream the runner captures, and nothing consumes "
        "the structured form yet (S13)",
        field="test_reporter",
    )
    return Stack(
        build_system=BuildSystem.GO,
        manifest="go.mod",
        install=("go", "mod", "download"),
        build=("go", "build", "./..."),
        test=("go", "test", "./..."),
    )


def _cargo(tree: Tree, log: FindingLog) -> Stack:
    log.decided("Cargo", "Cargo.toml", field="build_system")
    manifest = tree.toml("Cargo.toml") or {}
    workspace = manifest.get("workspace")
    members: tuple[str, ...] = ()
    if isinstance(workspace, dict):
        members = _strings(workspace.get("members"))
    return Stack(
        build_system=BuildSystem.CARGO,
        manifest="Cargo.toml",
        install=("cargo", "fetch", "--locked") if tree.has("Cargo.lock") else ("cargo", "fetch"),
        build=("cargo", "build"),
        test=("cargo", "test"),
        workspace_globs=members,
    )


#: Precedence, and it is a judgement rather than an ordering of importance. A
#: root ``package.json`` is the manifest most likely to be *incidental* — a docs
#: site, a commit-hook config, a single lint script in a repository whose real
#: build is Maven — so the languages whose root manifest is rarely accidental go
#: first, and Node goes last.
_CANDIDATES: Final[tuple[_Candidate, ...]] = (
    _Candidate("Maven", ("pom.xml",), _maven),
    _Candidate(
        "Gradle",
        ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
        _gradle,
    ),
    _Candidate("Go", ("go.mod",), _go),
    _Candidate("Cargo", ("Cargo.toml",), _cargo),
    _Candidate("Python", ("pyproject.toml", "setup.py", "requirements.txt"), _python),
    _Candidate("Node", ("package.json",), _node),
)
