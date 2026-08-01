"""The signals that are not about which build system this is.

Toolchain pins, workspace members, the conventions file — and ``needs_docker``,
which §3.5 calls the valuable half of the probe and which is the only inference
here with a security consequence attached.

**The Docker inference decides a question, not an answer.** ``needs_docker``
says the repository's tests want a daemon. It does not say the operator agreed
to hand one over, and S8 made that a separate field precisely so a lockfile
cannot select an isolation tier. So a false positive here costs a human one
question, and a false negative costs a run that fails saying no daemon was
found. Both are recoverable, which is what lets the inference be evidence-based
rather than timid — and it is why the probe is allowed to read this from the
repository at all.

**What is deliberately not a signal.** A ``Dockerfile`` says the project is
*packaged* as an image, which is CI's job and not the test suite's; on that
signal nearly every modern repository would ask for a daemon. Nor is a
``docker`` mention in a CI workflow, for the same reason. The signals kept are
the two that mean *the tests start containers*: a declared dependency on
testcontainers, and a compose file the suite would bring up.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from clawdence.probe.findings import FindingLog
from clawdence.probe.scan import Tree
from clawdence.probe.stacks import MANIFESTS, Stack, python_requirements

#: Every dependency-declaring manifest, whatever the build system. A monorepo
#: member is often a different stack from its root, so the member scan cannot be
#: narrowed to the root's own manifests.
_ALL_MANIFESTS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(name for names in MANIFESTS.values() for name in names)
)

#: A compose file at a scanned root means the suite has something to bring up.
_COMPOSE_FILES: Final[tuple[str, ...]] = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

#: Seen and not used. Named in the report so a reader can tell the difference
#: between a probe that missed a file and one that read it and declined.
_NOT_SIGNALS: Final[tuple[str, ...]] = ("Dockerfile", "Containerfile", ".dockerignore")

_TESTCONTAINERS: Final = "testcontainers"

#: Single-language version files. asdf and mise both read the first two; the
#: rest are the ecosystem's own conventions.
_VERSION_FILES: Final[Mapping[str, str]] = {
    ".nvmrc": "node",
    ".node-version": "node",
    ".java-version": "java",
    ".python-version": "python",
    ".ruby-version": "ruby",
    ".go-version": "go",
}

#: asdf's plugin names for tools mise and everything else spell differently.
_TOOL_ALIASES: Final[Mapping[str, str]] = {"nodejs": "node", "golang": "go"}

_MISE_FILES: Final[tuple[str, ...]] = (
    ".mise.toml",
    "mise.toml",
    ".config/mise/config.toml",
    ".mise/config.toml",
)


@dataclass(frozen=True, slots=True)
class Layout:
    """Where this repository's buildable units are."""

    members: tuple[str, ...]
    #: The tool that says so, when a marker file names one.
    tool: str | None


def layout(tree: Tree, log: FindingLog, stack: Stack) -> Layout:
    """Workspace members, from the globs the manifests declare.

    Nothing in ``RepoProfile`` records this, and that is deliberate: no step has
    yet said what it would do with a list of members, and a field invented ahead
    of its reader is a field that gets filled in wrong. What the layout is for
    *here* is the Docker scan — a monorepo where only ``packages/api`` depends
    on testcontainers has an isolation requirement that a root-only scan misses
    entirely, which is the common shape rather than the exotic one.
    """
    markers = {
        "nx": "nx.json",
        "turborepo": "turbo.json",
        "lerna": "lerna.json",
        "pnpm": "pnpm-workspace.yaml",
    }
    tool = next((name for name, marker in markers.items() if tree.has(marker)), None)
    members = tree.members(stack.workspace_globs)

    if members:
        named = ", ".join(members[:4]) + (" …" if len(members) > 4 else "")
        log.note(
            f"monorepo: {len(members)} workspace member(s) declared by "
            f"{stack.manifest or 'the workspace file'}"
            + (f" and {markers[tool]}" if tool else "")
            + f" ({named}). Their manifests are read for the Docker inference; the commands "
            f"above are the root's",
            stack.manifest,
        )
    elif tool is not None:
        log.note(
            f"{markers[tool]} is present but no workspace globs were found, so no member "
            f"manifests were read",
            markers[tool],
        )
    return Layout(members=members, tool=tool)


def needs_docker(tree: Tree, log: FindingLog, members: tuple[str, ...]) -> bool:
    """Whether this repository's tests need a Docker daemon (§3.5)."""
    evidence: list[str] = []
    for prefix in ("", *(f"{member}/" for member in members)):
        for name in _ALL_MANIFESTS:
            rel = f"{prefix}{name}"
            if tree.has(rel) and _declares_testcontainers(tree, rel):
                evidence.append(rel)
        for name in _COMPOSE_FILES:
            rel = f"{prefix}{name}"
            if tree.has(rel):
                evidence.append(rel)

    ignored = [name for name in _NOT_SIGNALS if tree.has(name)]
    if ignored:
        log.note(
            f"{', '.join(ignored)} present and not treated as a Docker requirement: an image "
            f"the project publishes is built by CI, not by the tests the runner runs",
            *ignored,
            field="needs_docker",
        )

    if not evidence:
        log.decided(
            "no declared testcontainers dependency and no compose file, so the tests are "
            "assumed not to need a daemon",
            field="needs_docker",
        )
        return False

    log.decided(
        f"the tests need a Docker daemon: {', '.join(evidence)}",
        *evidence,
        field="needs_docker",
    )
    return True


def _declares_testcontainers(tree: Tree, rel: str) -> bool:
    """Is testcontainers a *declared* dependency of this manifest?

    Parsed where the file is already parsed and precision is free, substring
    where it is not. The one place substring would be actively wrong is
    ``go.mod``, which lists the indirect closure inline — so those lines are
    dropped, or every Go repository that transitively touches testcontainers
    would ask for a daemon.
    """
    name = rel.rsplit("/", 1)[-1]
    if name == "package.json":
        manifest = tree.mapping(rel) or {}
        return any(
            _TESTCONTAINERS in dependency.lower()
            for table in (
                "dependencies",
                "devDependencies",
                "peerDependencies",
                "optionalDependencies",
            )
            for dependency in _keys(manifest.get(table))
        )
    if name == "pyproject.toml":
        pyproject = tree.toml(rel) or {}
        return any(
            _TESTCONTAINERS in requirement.lower() for requirement in python_requirements(pyproject)
        )
    if name == "go.mod":
        text = tree.text(rel) or ""
        return any(
            _TESTCONTAINERS in line.lower() and "// indirect" not in line
            for line in text.splitlines()
        )
    return _TESTCONTAINERS in (tree.text(rel) or "").lower()


def _keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(str(key) for key in value)


def toolchain(tree: Tree, log: FindingLog) -> dict[str, str]:
    """Version pins the repository declares, as ``{"node": "24.5"}``.

    What this does **not** do is set ``exec_prefix``. The prefix (v1's
    ``mise exec node@24.5 --``) is a claim about the *runner image* — that mise
    is installed in it and on PATH — and the probe is reading a repository. A
    prefix guessed from a pinned version turns every command in the profile into
    one that fails with "mise: not found", which is a worse outcome than the
    unpinned toolchain it was trying to fix.
    """
    pins: dict[str, str] = {}
    sources: list[str] = []

    for rel in _MISE_FILES:
        parsed = tree.toml(rel) if tree.has(rel) else None
        tools = parsed.get("tools") if parsed else None
        if isinstance(tools, dict):
            for tool, spec in tools.items():
                version = _version_of(spec)
                if version:
                    pins.setdefault(_TOOL_ALIASES.get(tool.lower(), tool.lower()), version)
            sources.append(rel)

    if tree.has(".tool-versions"):
        for line in (tree.text(".tool-versions") or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            tool, _, rest = stripped.partition(" ")
            version = rest.strip().split(" ")[0]
            if tool and version:
                pins.setdefault(_TOOL_ALIASES.get(tool.lower(), tool.lower()), version)
        sources.append(".tool-versions")

    for rel, tool in _VERSION_FILES.items():
        contents = (tree.text(rel) or "").strip() if tree.has(rel) else ""
        if contents:
            pins.setdefault(tool, contents.splitlines()[0].strip().lstrip("v"))
            sources.append(rel)

    if not pins:
        log.note(
            "no toolchain pins found, so the runner image's own versions are what this "
            "repository will be built with",
            field="toolchain",
        )
        return pins

    listed = ", ".join(f"{tool}={version}" for tool, version in sorted(pins.items()))
    log.decided(f"toolchain pins: {listed}", *sources, field="toolchain")
    log.note(
        "exec_prefix was left empty. Whether these pins can be honoured is a fact about the "
        "runner image, not about this repository — set it to something like "
        "`mise exec --` only if the image you run has that tool in it",
        field="exec_prefix",
    )
    return pins


def _version_of(spec: object) -> str | None:
    """mise's ``[tools]`` values: a string, a list, or a table."""
    if isinstance(spec, str):
        return spec
    if isinstance(spec, list):
        return next((entry for entry in spec if isinstance(entry, str)), None)
    if isinstance(spec, Mapping):
        version = spec.get("version")
        return version if isinstance(version, str) else None
    return None


def conventions(tree: Tree, log: FindingLog) -> str | None:
    """v1's ``agentsMd``: the file the runner installs into the worktree."""
    found = tree.first("AGENTS.md", "CLAUDE.md", ".github/AGENTS.md", "docs/AGENTS.md")
    if found is None:
        return None
    log.decided(
        "this repository has a conventions file, and the runner installs it into the worktree "
        "so the agent is told the local rules rather than inferring them",
        found,
        field="agents_md_path",
    )
    return found
