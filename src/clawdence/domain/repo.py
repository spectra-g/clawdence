"""Repo profile — what the system needs to know to work on one repository.

Replaces v1's hand-written ``repo-registry.json``. Most of it is derived by the
probe (S9) and confirmed by a human; the fields exist here so the probe has a
target and the runner has a contract.

Three fields carry security weight and are worth reading twice:

``McpServer.bearer_token_env_var``
    The *name* of an environment variable, never a token. A profile is
    committed to disk and shown in ``clawdence probe`` output; a profile that
    can hold a secret is a profile that will eventually leak one.

``needs_docker`` → ``isolation_tier``
    The valuable half of the probe. The tier is inferred from evidence in the
    repo rather than guessed by a user who has no reason to know that mounting
    a docker socket is equivalent to handing out host root.

``docker_socket_acknowledged``
    And this is what stops the probe's inference from being obeyed silently.
    ``needs_docker`` says the repository's tests want a daemon; it does not say
    the operator agreed to hand one over. The socket tier is unusable without
    this second field, so the decision is taken by a person writing a profile
    rather than by a detector reading a lockfile.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field, StringConstraints, model_validator

from clawdence.domain._base import DomainModel
from clawdence.domain.ids import RepoId
from clawdence.domain.verification import TestReporter

#: Namespace every branch this system creates goes under, so a repository owner
#: can protect, filter or bulk-delete them with one pattern.
DEFAULT_BRANCH_PREFIX: Final = "clawdence/"


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


class MergeMethod(StrEnum):
    """How a merge is performed.

    ``SQUASH`` is the default everywhere it is offered, because a squashed merge
    produces one commit whose tree is the tree that was verified. A merge commit
    produces a tree that is the *result* of combining two, which no test ran
    against — the same invalidation ``VcsPort.merge``'s ``expect_*`` arguments
    exist to catch, arriving one step later.
    """

    SQUASH = "squash"
    MERGE = "merge"
    REBASE = "rebase"


class CheckoutPolicy(DomainModel):
    """How much of the repository is fetched, and how much of it lands on disk.

    **Partial, not shallow, and the distinction is the whole field.** ``--depth``
    is the obvious way to make a large clone fast and it is the wrong one here:
    a shallow repository has no merge base, so "is this branch behind" cannot be
    answered, a rebase cannot be computed, and S13's evidence-to-tree binding has
    nothing to compare against. ``--filter=blob:none`` keeps every commit and
    every tree and defers only file *contents*, which is the part a first clone
    actually spends its time on, and git fetches what it needs on demand.

    **Sparse checkout is per worktree, not per clone.** The object store is
    shared between concurrent runs (``RepoStore``); the set of paths one run
    wants is not. Declaring it here and applying it at checkout keeps that
    straight.
    """

    #: ``--filter=blob:none``. Off means a full clone, which is right for a small
    #: repository on a fast link and for an air-gapped mirror that cannot serve
    #: the lazy fetches a partial clone depends on.
    partial: bool = True

    #: Cone-mode sparse-checkout patterns. Empty checks out the whole tree.
    sparse_paths: tuple[str, ...] = ()

    #: Off by default: LFS content is large, binary, and almost never what an
    #: agent is editing. A repository whose build needs the real files says so,
    #: and pays for them.
    fetch_lfs: bool = False


class PullRequestPolicy(DomainModel):
    """What a pull request from this system looks like in someone else's repo.

    Cheap to implement and disproportionately important: the difference between
    output that looks like it belongs in a project and output that looks like bot
    spam is reviewers, labels, and a body that follows the repository's template.
    """

    draft: bool = False

    #: Forge usernames and team slugs. Requested, never enforced — a reviewer who
    #: no longer has access makes the request fail, and failing a run over a
    #: stale username would be the tail wagging the dog (``GhVcs`` treats an
    #: unassignable reviewer as a warning on an opened PR, not an error).
    reviewers: tuple[str, ...] = ()
    team_reviewers: tuple[str, ...] = ()

    labels: tuple[str, ...] = ()

    #: Repository-relative path to the PR body template, e.g.
    #: ``.github/pull_request_template.md``. Read from the *base* commit, never
    #: from the worktree: the worktree is output from a model, and a template
    #: read from there is text an agent could have written for us to sign.
    body_template_path: str | None = None

    merge_method: MergeMethod = MergeMethod.SQUASH


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

    #: The opt-in that ``container+docker:socket`` requires, and the reason it is
    #: a *field* rather than a paragraph in the docs: §3.2 asks for socket mode
    #: to be "opt-in per repo, loudly documented, and never the default", and a
    #: warning printed somewhere is one nobody has to read. This makes the
    #: profile inexpressible without the acknowledgement — see the validator
    #: below, whose message is the warning.
    docker_socket_acknowledged: bool = False

    #: Base image for the container tiers, overriding the runner's own default
    #: (§3.8). Digest-pinned: a tag is a mutable pointer, and resolving one at
    #: dispatch means executing whatever was pushed over it since the last run.
    #: Absent for most repositories — it exists because corporate adopters have a
    #: mandated base image and no way to publish it anywhere this project reaches.
    runner_image: str | None = None

    #: How many runs against this repository may be in flight at once (§3.4).
    #: **One by default, and the default is the interesting part**: v1 had a
    #: global single-story lock, and the natural replacement is not "no lock" but
    #: a lock scoped to the thing that is actually shared. Two runs on one
    #: repository share a warm dependency cache and, for most build systems, a
    #: lock file inside it — gradle and maven both take one — so raising this is
    #: a statement that the repository's toolchain tolerates concurrent installs.
    #: Runs on *different* repositories are unaffected, which is the change from
    #: v1: the cap is per repo, not global.
    max_concurrent_runs: int = Field(default=1, gt=0)

    test_reporter: TestReporter = TestReporter.NONE
    e2e_runner: E2EPolicy = E2EPolicy.SKIP
    require_full_test_suite: bool = False

    #: Repo conventions file installed into the worktree — v1's ``agentsMd``.
    agents_md_path: str | None = None

    #: Namespace for every branch this system pushes here. Constrained to end in
    #: ``/`` so it is a namespace rather than a concatenation: without the slash
    #: ``clawdence`` + ``wi-1`` is ``clawdencewi-1``, which reads as a typo and
    #: cannot be matched by a branch-protection pattern. Empty is permitted and
    #: means "no namespace", which is a real if unfriendly choice.
    branch_prefix: Annotated[
        str, StringConstraints(pattern=r"^$|^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(/[a-z0-9._-]+)*/$")
    ] = DEFAULT_BRANCH_PREFIX

    checkout: CheckoutPolicy = CheckoutPolicy()
    pull_request: PullRequestPolicy = PullRequestPolicy()

    egress: EgressPolicy = EgressPolicy()
    caps: ResourceCaps = ResourceCaps()
    mcp_servers: tuple[McpServer, ...] = ()

    #: Repo routing signal (S11). Matched against a work item's *raw* text.
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _socket_mode_is_acknowledged(self) -> RepoProfile:
        """Socket mode cannot be reached by editing one enum value.

        The tier that mounts the host daemon's socket is not weaker isolation,
        it is none with extra steps: the process inside can
        ``docker run --net=host -v /:/host``, which escapes the network
        namespace S7b builds and mounts the filesystem the container tier
        removed. §3.2 asks for it to be opt-in per repository and loudly
        documented, and the loud part is the problem — a warning in a README is
        a warning nobody read.

        So the acknowledgement is a second field, and a profile that names the
        tier without it does not validate. That places the refusal at
        *configuration* time, on the person writing the profile, rather than at
        dispatch on whoever is watching the run — which is the only moment where
        knowing costs nothing.
        """
        if self.isolation_tier is not IsolationTier.CONTAINER_DOCKER_SOCKET:
            return self
        if not self.docker_socket_acknowledged:
            raise ValueError(
                f"{self.name!r} asks for {IsolationTier.CONTAINER_DOCKER_SOCKET.value!r} "
                f"isolation, which mounts the host's Docker socket into the runner. A process "
                f"that can reach the host daemon can start a container with the host's network "
                f"and the host's filesystem in it, so this tier defeats the plane split and the "
                f"egress allowlist at once and is equivalent to giving the agent host root. Set "
                f"docker_socket_acknowledged=true to say that is understood and intended, or use "
                f"{IsolationTier.CONTAINER.value!r} and run the repository's tests without Docker"
            )
        return self
