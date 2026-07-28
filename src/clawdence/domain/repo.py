"""Repo profile — what the system needs to know to work on one repository.

Replaces v1's hand-written ``repo-registry.json``. Most of it is derived by the
probe (S9) and confirmed by a human; the fields exist here so the probe has a
target and the runner has a contract.

Two fields carry security weight and are worth reading twice:

``McpServer.bearer_token_env_var``
    The *name* of an environment variable, never a token. A profile is
    committed to disk and shown in ``clawdence probe`` output; a profile that
    can hold a secret is a profile that will eventually leak one.

``needs_docker`` → ``isolation_tier``
    The valuable half of the probe. The tier is inferred from evidence in the
    repo rather than guessed by a user who has no reason to know that mounting
    a docker socket is equivalent to handing out host root.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from clawdence.domain._base import DomainModel
from clawdence.domain.ids import RepoId
from clawdence.domain.verification import TestReporter


class BuildSystem(StrEnum):
    MAVEN = "maven"
    GRADLE = "gradle"
    NPM = "npm"
    YARN = "yarn"
    PNPM = "pnpm"
    UV = "uv"
    POETRY = "poetry"
    PIP = "pip"
    GO = "go"
    CARGO = "cargo"
    UNKNOWN = "unknown"


class IsolationTier(StrEnum):
    """How much the runner is contained. See the plan's §3.2.

    ``CONTAINER_DOCKER_SOCKET`` is not "weaker isolation" — a process that can
    reach the host daemon can ``docker run --net=host -v /:/host``, which
    escapes the network namespace (voiding the egress allowlist) and mounts the
    host filesystem (voiding the plane split). It is *no* isolation, with extra
    steps, and it is forbidden for anything that arrived via public ingestion.

    ``MICROVM`` is declared and not implemented: the interface is open so the
    tier can land later without reshaping everything above it.
    """

    HOST = "host"
    CONTAINER = "container"
    CONTAINER_DOCKER_SOCKET = "container+docker:socket"
    CONTAINER_DOCKER_DIND_ROOTLESS = "container+docker:dind-rootless"
    MICROVM = "microvm"


class E2EPolicy(StrEnum):
    AVAILABLE = "available"
    CI_ONLY = "ci-only"
    SKIP = "skip"


class McpServer(DomainModel):
    """An MCP server a repo's tooling needs.

    This is the honest exception to "the runner holds no secrets": a repo that
    configures MCP hands the runner a credential. The boundary is therefore
    *no control-plane secrets*, and these tokens are scoped per repo and
    injected per run rather than made ambient.
    """

    name: str
    url: str
    #: Name of the env var holding the token. Never the token.
    bearer_token_env_var: str | None = None


class EgressPolicy(DomainModel):
    """Per-run network allowlist.

    Container isolation stops the runner reaching the host and says nothing
    about it reaching the internet — an agent with a full checkout and open
    egress exfiltrates the codebase in one request. This is also the strongest
    available mitigation for prompt injection, because it holds whether or not
    the model was fooled.
    """

    allow_llm_api: bool = True
    allow_package_registries: bool = True
    allow_mcp_servers: bool = True

    #: Denied by default: the control plane pushes, not the runner.
    allow_git_remote: bool = False

    extra_allowed_hosts: tuple[str, ...] = ()

    #: The documented escape hatch, off by default. Set this and the allowlist
    #: stops being a security control.
    unrestricted: bool = False


class ResourceCaps(DomainModel):
    """Caps per run. A container without these is a DoS surface against the
    host the control plane is running on."""

    cpu_count: float | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, gt=0)
    disk_mb: int | None = Field(default=None, gt=0)
    pid_limit: int | None = Field(default=None, gt=0)
    wall_clock_seconds: float | None = Field(default=None, gt=0)


class RepoProfile(DomainModel):
    """Everything the system knows about one repository."""

    id: RepoId
    name: str
    remote_url: str
    default_branch: str = "main"

    build_system: BuildSystem = BuildSystem.UNKNOWN

    #: Toolchain pins read from ``.tool-versions`` / ``.mise.toml`` / ``.nvmrc``
    #: / ``.java-version``, as ``{"node": "24.5", "java": "21"}``.
    toolchain: dict[str, str] = Field(default_factory=dict)

    #: Wrapper argv prepended to every command, e.g.
    #: ``("mise", "exec", "node@24.5", "--")``.
    exec_prefix: tuple[str, ...] = ()

    #: argv, never shell strings — see ``ScriptStage.command``.
    install_command: tuple[str, ...] = ()
    build_command: tuple[str, ...] = ()
    test_command: tuple[str, ...] = ()

    #: Inferred by the probe from testcontainers deps or a compose file, and it
    #: is what selects the isolation tier.
    needs_docker: bool = False
    isolation_tier: IsolationTier = IsolationTier.CONTAINER

    #: Base image for the container tiers, overriding the runner's own default
    #: (§3.8). Digest-pinned: a tag is a mutable pointer, and resolving one at
    #: dispatch means executing whatever was pushed over it since the last run.
    #: Absent for most repositories — it exists because corporate adopters have a
    #: mandated base image and no way to publish it anywhere this project reaches.
    runner_image: str | None = None

    test_reporter: TestReporter = TestReporter.NONE
    e2e_runner: E2EPolicy = E2EPolicy.SKIP
    require_full_test_suite: bool = False

    #: Repo conventions file installed into the worktree — v1's ``agentsMd``.
    agents_md_path: str | None = None

    egress: EgressPolicy = EgressPolicy()
    caps: ResourceCaps = ResourceCaps()
    mcp_servers: tuple[McpServer, ...] = ()

    #: Repo routing signal (S11). Matched against a work item's *raw* text.
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
