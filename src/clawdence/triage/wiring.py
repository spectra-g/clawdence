"""Configuration to objects: the adapters a pipeline runs with.

Split out of ``pipeline`` because the two are different kinds of code and fail in
different ways. This is a set of constructors over a ``Deployment`` — no I/O, no
control flow, nothing to go wrong except an unusable configuration, which it says
so about. ``pipeline`` is the sequence, and it takes what it needs already built,
which is what lets its tests hand it fakes without a config file existing.

**The secret provider is an allowlist and the allowlist is derived.** ``EnvSecrets``
without one is ``os.environ`` with extra steps, and any caller that chooses the
name it asks for could then read ``AWS_SECRET_ACCESS_KEY``. So the set of readable
variables is computed from the configuration: the forge token, the runner's
declared ``secret_env`` values, and the model key — every name the operator wrote
down, and no others. A repository profile adding an MCP token adds its own name to
that set, which is the one place the list is not fixed at startup.

**The runner is optional and its absence is a refusal.** A deployment with no
``runner:`` section gets a registry whose ``runner`` steps fail, naming the key to
write, which is the behaviour ``default_registry`` has had since S6 — the message
changes, not the outcome. A composition root that quietly substituted the host
tier would be choosing, on an operator's behalf, to run model-authored code
outside a container.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum

from clawdence.domain import IsolationTier
from clawdence.ports.model import TokenPrice
from clawdence.ports.runner import RunnerPort
from clawdence.ports.secrets import EnvSecrets, SecretProvider
from clawdence.runners import (
    Accumulation,
    AgentCommand,
    ContainerRunner,
    HostRunner,
    PlanDelivery,
)
from clawdence.triage.config import ConfigError, Deployment, RunnerConfig
from clawdence.vcs import GhVcs, RepoStore, WorktreeManager

#: Where the control plane's own model key is read from. The same name S12 wired
#: into the CLI, kept rather than made configurable: it is the variable every
#: provider's documentation uses, and an operator who has one exported should not
#: have to learn ours.
MODEL_KEY_ENV = "ANTHROPIC_API_KEY"


def secret_names(deployment: Deployment) -> frozenset[str]:
    """Every environment variable this deployment is allowed to read.

    Derived rather than declared, so that adding a repository with an MCP server
    or a runner with a scoped key extends the allowlist by editing the thing that
    needs it. A separate ``allowed:`` list in the config file would be a second
    place to keep in step, and the failure mode of forgetting is a credential
    that silently resolves to nothing.
    """
    names = {MODEL_KEY_ENV}
    config = deployment.config
    if config.forge_token_env:
        names.add(config.forge_token_env)
    if config.runner is not None:
        names.update(config.runner.secret_env.values())
    for profile in deployment.profiles.values():
        for server in profile.mcp_servers:
            if server.bearer_token_env_var:
                names.add(server.bearer_token_env_var)
    return frozenset(names)


def secrets_for(deployment: Deployment, environ: Mapping[str, str] | None = None) -> SecretProvider:
    """The control plane's secret provider, scoped to the names above."""
    return EnvSecrets(os.environ if environ is None else environ, allowed=secret_names(deployment))


def repo_store(deployment: Deployment, secrets: SecretProvider) -> RepoStore:
    """The local object stores, with the forge credential resolved by name."""
    return RepoStore(
        root=deployment.repo_store,
        secrets=secrets,
        token_name=deployment.config.forge_token_env,
    )


def worktrees(deployment: Deployment, store: RepoStore) -> WorktreeManager:
    return WorktreeManager(store=store, work_root=deployment.work_root)


def vcs(
    deployment: Deployment,
    store: RepoStore,
    secrets: SecretProvider,
    *,
    gh_path: str = "gh",
) -> GhVcs:
    """The forge adapter, holding the whole configured repository set.

    ``profiles`` is passed whole rather than per call because the adapter turns a
    ``repo_id`` — all the port carries — into a remote URL and a branch
    namespace, and an adapter that discovered repositories for itself would be
    deciding what this system is allowed to touch.
    """
    return GhVcs(
        store=store,
        profiles=deployment.profiles,
        gh_path=gh_path,
        secrets=secrets,
        token_name=deployment.config.forge_token_env,
    )


def runner(
    config: RunnerConfig,
    secrets: SecretProvider,
    *,
    environ: Mapping[str, str] | None = None,
) -> RunnerPort:
    """The data plane, per the ``runner:`` section.

    Tiers are not interchangeable and the refusals say so. ``host`` runs
    model-authored code as the control-plane user, on the control plane's
    filesystem, which the plan permits and does not recommend; the container
    tiers need an image, because there is no default that is both safe and
    somebody else's. The socket tiers are reachable only through a repository
    profile that acknowledges them (``RepoProfile`` validates that), so naming one
    here would be the deployment-wide setting §3.2 says must not exist.
    """
    try:
        delivery = PlanDelivery(config.delivery)
    except ValueError:
        raise _bad_value("runner.delivery", config.delivery, PlanDelivery) from None
    try:
        accumulation = Accumulation(config.accumulation)
    except ValueError:
        raise _bad_value("runner.accumulation", config.accumulation, Accumulation) from None

    command = AgentCommand(
        argv=config.argv,
        delivery=delivery,
        conventions_filename=config.conventions_filename,
        extra_env=dict(config.extra_env),
        secret_env=dict(config.secret_env),
        accumulation=accumulation,
        prices=_prices(config),
        include_stderr_tail=config.include_stderr_tail,
    )

    if config.tier is IsolationTier.HOST:
        return HostRunner(command, secrets=secrets, environ=environ)

    if config.tier is not IsolationTier.CONTAINER:
        raise ConfigError(
            f"runner.tier is {config.tier.value!r}, which is a per-repository decision "
            f"rather than a deployment-wide one: the tiers that reach the host's Docker "
            f"daemon are chosen by a profile that acknowledges what they cost "
            f"(RepoProfile.docker_socket_acknowledged), and setting one here would apply "
            f"it to every repository at once"
        )
    if not config.image:
        raise ConfigError(
            "runner.tier is 'container' and runner.image is not set — there is no "
            "default image, because the one that would work for a solo user is not "
            "the one a corporate adopter is required to use (§3.8). Pin one as "
            "name@sha256:…"
        )
    return ContainerRunner(
        command,
        image=config.image,
        secrets=secrets,
        environ=environ,
        allow_unpinned_image=config.allow_unpinned_image,
    )


def _prices(config: RunnerConfig) -> TokenPrice | None:
    """Both halves or neither. Half a price sheet costs half of what a run cost,
    which is a number the budget would then enforce against."""
    if config.input_usd_per_mtok is None and config.output_usd_per_mtok is None:
        return None
    if config.input_usd_per_mtok is None or config.output_usd_per_mtok is None:
        raise ConfigError(
            "runner.input_usd_per_mtok and runner.output_usd_per_mtok go together — "
            "with one of them missing the ledger would record half of what a run cost, "
            "and the budget would enforce against that number"
        )
    return TokenPrice(
        input_usd=Decimal(config.input_usd_per_mtok),
        output_usd=Decimal(config.output_usd_per_mtok),
    )


def _bad_value(where: str, value: str, choices: type[StrEnum]) -> ConfigError:
    allowed = ", ".join(repr(member.value) for member in choices)
    return ConfigError(f"{where} is {value!r}; expected one of {allowed}")
