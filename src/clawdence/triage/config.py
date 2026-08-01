"""The composition root, as a file: which repositories exist and what drives them.

Every step before this one took its dependencies as arguments and said, in so
many words, that inventing them would be deciding a question a later step owned.
This is that step. S15 deferred ``clawdence repo check`` here because "fail at
configuration time" needs something that says which repositories exist, where
their mirrors live and which credential reaches them; S6 deferred the agent CLI
here because "wiring one to a real CLI is configuration rather than code"; S12
left the model key in the environment because configuration was not built yet.
One file answers all three.

**Repositories are listed by path, and the file at the path is the probe's
output.** ``clawdence probe --out profiles/api.json`` writes a ``RepoProfile``;
this reads it back. That is the whole registry — no second format, no fields
restated, and no way for the registry and the profile to disagree, because they
are the same document. v1's hand-written ``repo-registry.json`` is what this
replaces, and the difference worth naming is that a profile is *derived and
confirmed* rather than typed from memory.

**Every path is resolved against the config file, never the current directory.**
A configuration that means something different depending on where the operator
happened to be standing is one that works in a shell and fails under systemd. The
one exception is ``~``, which is expanded, because a home directory is the thing
an operator most reasonably wants to name.

**No secret is in here, only names.** ``forge_token_env`` and ``secret_env`` hold
*variable names*, resolved through a ``SecretProvider`` at the moment they are
used — the same rule ``RepoProfile.McpServer.bearer_token_env_var`` states and for
the same reason: this file gets committed, printed by ``clawdence repos show``,
and copied into a bug report.

**A missing section is a refusal, never a default.** With no ``runner`` declared,
``runner`` steps refuse exactly as they did before this step existed, and the
message names the key to write. The alternative — a default agent CLI, a default
image — is the system guessing which program to hand a repository to, which is
the one guess with no safe wrong answer.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from clawdence.domain import IsolationTier, RepoProfile, WorkItemType
from clawdence.domain.ids import RepoId
from clawdence.vcs.store import DEFAULT_TOKEN_NAME

#: Bumped when this file's format changes incompatibly. Same rule as a
#: workflow's: a version this build does not understand is refused rather than
#: half-read, because half-reading a composition root means running with a
#: repository list somebody thought they had edited.
CONFIG_SCHEMA_VERSION: Final = 1

#: Where the configuration lives when nobody says. Beside the state database, in
#: ``$CLAWDENCE_HOME``, because the two are the same deployment's two halves —
#: what the system has done, and what it is allowed to do.
CONFIG_FILENAME: Final = "config.yaml"

#: The workflow every request gets when its type routes to nothing. Named here
#: rather than defaulted per type so that the fallback is one value an operator
#: can find, and so a work-item type added to the domain later has an answer
#: before this file is edited.
DEFAULT_WORKFLOW: Final = "sprint"

#: Shipped in ``examples/``. Not a default path — a default that pointed into
#: the installed package would let an upgrade change which process runs.
WORKFLOW_SUFFIX: Final = ".yaml"


class ConfigError(ValueError):
    """The configuration cannot be used, and the message says which part.

    One error type for the file, its repository profiles and its workflow
    directory alike, because from a caller's point of view they are one artefact:
    every one of them means the control plane has nothing valid to work from, and
    all of them are fixed by editing something on disk.
    """

    def __init__(self, message: str, *, origin: str | None = None) -> None:
        super().__init__(f"{origin}: {message}" if origin else message)
        self.origin = origin


class _Section(BaseModel):
    """Closed and immutable, as the domain model is, and for the same reason.

    ``extra="forbid"`` is doing real work in a config file: a misspelled
    ``work_root`` that was silently ignored would put worktrees somewhere the
    reaper is not sweeping, and the operator would find out when the disk filled.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class Paths(_Section):
    """Where this deployment keeps the things runs need on disk."""

    #: Bare mirrors, one per repository (``vcs.RepoStore``). Deliberately *not*
    #: under ``work_root``: the reaper sweeps one level under that root and would
    #: eventually delete an object store living there, taking every repository's
    #: history with it. S15 found that by reading the reaper; the layout is kept
    #: apart here so a config file cannot reintroduce it.
    repo_store: Path = Path("~/.clawdence/repos")

    #: Per-run worktrees (§3.3's ``WORK_ROOT``). One level of run-id directories,
    #: which is the shape ``clawdence reap`` and ``clawdence reset`` expect.
    work_root: Path = Path("~/.clawdence/work")

    #: Directory workflow files are looked up in by name. A routed workflow
    #: ``quick-fix`` is ``<workflows>/quick-fix.yaml`` — a name, never a path
    #: from the request, so nothing a submitter writes can select a file.
    workflows: Path = Path("workflows")


class Routing(_Section):
    """Which workflow each kind of request runs. The first of triage's two
    decisions, expressed as data because the mapping is a policy an operator
    owns and not a fact about the code."""

    by_type: Mapping[WorkItemType, str] = Field(
        default_factory=lambda: {
            WorkItemType.EPIC: "sprint",
            WorkItemType.STORY: "sprint",
            WorkItemType.TASK: "quick-fix",
            WorkItemType.BUG: "quick-fix",
            WorkItemType.SPIKE: "spike",
        }
    )

    default: str = DEFAULT_WORKFLOW

    #: Whether triage may revise a work item's type from its text. On by
    #: default, and narrow: it only ever reconsiders the type ingestion assigns
    #: when nobody said (``ingest.normalise.DEFAULT_TYPE``), never one a
    #: submitter chose. Off means the submitted type is taken at its word, which
    #: is what a deployment whose sources all set it properly wants.
    classify: bool = True


class RunnerConfig(_Section):
    """The agent CLI the data plane runs, and how much of a machine it gets.

    §3.8's two audiences are why ``image`` has no default: a solo user wants one
    that works and a corporate adopter is required to use their own, and the only
    honest resolution is to make the operator say. The digest requirement is the
    runner's own (``ContainerRunner._check``) and is not restated here — this
    file's job is to carry the string, not to re-implement the check that would
    then be in two places.
    """

    tier: IsolationTier = IsolationTier.CONTAINER

    #: Base image for the container tiers. Required for them, ignored by ``host``.
    image: str | None = None

    #: argv of the CLI, e.g. ``[codex, exec, --full-auto]``. ``RepoProfile
    #: .exec_prefix`` is prepended at dispatch, so a toolchain wrapper is applied
    #: without this having to know about it.
    argv: tuple[str, ...] = Field(min_length=1)

    delivery: str = "stdin"

    #: What this CLI reads repository conventions from — ``AGENTS.md`` for codex,
    #: ``CLAUDE.md`` for claude-code.
    conventions_filename: str = "AGENTS.md"

    #: ``{environment variable: secret name}``. Where §3.1's *scoped* runner key
    #: goes — budgeted, separate from the control plane's, and named rather than
    #: written down.
    secret_env: Mapping[str, str] = Field(default_factory=dict)

    #: Non-secret environment for the child. Checked against ``FORBIDDEN_ENV`` by
    #: the runner, which is the layer that knows what a control-plane name is.
    extra_env: Mapping[str, str] = Field(default_factory=dict)

    #: How this CLI's token reports combine — see ``runners.stream.Accumulation``.
    #: Declared rather than inferred because the two are indistinguishable from a
    #: single run and a wrong guess defeats the budget in one direction or the
    #: other.
    accumulation: str = "cumulative"

    #: USD per million tokens, for the cost ledger. Absent means the runner
    #: reports tokens and no money, which is honest for a CLI whose pricing this
    #: deployment has not been told.
    input_usd_per_mtok: Decimal | None = Field(default=None, ge=0)
    output_usd_per_mtok: Decimal | None = Field(default=None, ge=0)

    #: Off by default: the result's ``message`` is persisted with the step, and
    #: stderr is where a provider's echo of a rejected request — and so a pasted
    #: key — ends up.
    include_stderr_tail: bool = False

    #: Accepts a tag instead of a digest. Off, and it should stay off anywhere
    #: runs are not being watched; see ``ContainerRunner``'s refusal for why.
    allow_unpinned_image: bool = False


class Config(_Section):
    """One deployment: its repositories, its paths, its routes and its runner."""

    schema_version: int = Field(default=CONFIG_SCHEMA_VERSION, ge=1)

    paths: Paths = Paths()
    routing: Routing = Routing()
    runner: RunnerConfig | None = None

    #: Name of the environment variable holding the forge credential. A name —
    #: the default is ``vcs.RepoStore``'s own, imported rather than retyped so
    #: the file and the store cannot come to disagree about it. ``None`` means
    #: none is offered, which is correct for ssh remotes and for public
    #: repositories over https, and is why this is not required.
    forge_token_env: (
        Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")] | None
    ) = DEFAULT_TOKEN_NAME

    #: Paths to profiles written by ``clawdence probe --out``, resolved against
    #: this file. Empty is a usable configuration — routing then has nothing to
    #: choose from and says so, which is better than a system that appears
    #: configured until the first request arrives.
    repos: tuple[Path, ...] = ()


class Deployment:
    """A loaded ``Config`` with its paths resolved and its profiles read.

    Separate from ``Config`` because the two answer different questions.
    ``Config`` is *what the file says* — round-trippable, printable, and the
    thing a schema would describe. This is *what it means here*: absolute paths,
    profiles parsed, and every repository indexed by the id the ``VcsPort``
    passes around. Keeping them apart is what lets the file be validated without
    touching a disk that may not have the profiles on it yet.
    """

    __slots__ = ("_config", "_origin", "_profiles")

    def __init__(
        self, config: Config, *, origin: Path, profiles: Mapping[RepoId, RepoProfile]
    ) -> None:
        self._config = config
        self._origin = origin
        self._profiles = dict(profiles)

    @property
    def config(self) -> Config:
        return self._config

    @property
    def origin(self) -> Path:
        """The file this was read from. In every error message that follows."""
        return self._origin

    @property
    def profiles(self) -> Mapping[RepoId, RepoProfile]:
        """Every repository this deployment may work on, by id.

        The *closed set* repository routing chooses from. That it is closed is
        the security property: ``WorkItem.raw_text`` is a stranger's text and it
        selects among these, so the worst a request can do is name one the
        operator already configured. Nothing a submitter writes can add a row.
        """
        return self._profiles

    @property
    def repo_store(self) -> Path:
        return self._config.paths.repo_store

    @property
    def work_root(self) -> Path:
        return self._config.paths.work_root

    def profile(self, repo_id: str) -> RepoProfile:
        profile = self._profiles.get(repo_id)
        if profile is None:
            raise ConfigError(
                f"no repository with id {repo_id!r} is configured; "
                f"this deployment knows {self._known()}",
                origin=str(self._origin),
            )
        return profile

    def workflow_path(self, name: str) -> Path:
        """Where a routed workflow name is looked up.

        The name is joined as a *single component* and rejected if it is
        anything else. A workflow name can reach this from a request's
        ``workflow_override``, and a request that could write ``../../etc`` — or
        an absolute path — would be choosing which file the control plane
        executes.
        """
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ConfigError(
                f"{name!r} is not a workflow name — a name is one path component, "
                f"and the directory it is looked up in is this deployment's to choose",
                origin=str(self._origin),
            )
        return self._config.paths.workflows / f"{name}{WORKFLOW_SUFFIX}"

    def _known(self) -> str:
        return ", ".join(sorted(self._profiles)) or "none"


def default_config_path(home: str | None = None) -> Path:
    """``$CLAWDENCE_HOME/config.yaml``, or the same under ``~/.clawdence``.

    Reads the same variable ``cli.default_state_path`` does, because a
    deployment is one directory: pointing the state database at a test home and
    leaving the configuration in the real one is a mistake worth making
    impossible rather than documenting.
    """
    root = home if home is not None else os.environ.get("CLAWDENCE_HOME")
    return (Path(root) if root else Path.home() / ".clawdence") / CONFIG_FILENAME


def load(path: Path) -> Deployment:
    """Read a configuration file and everything it points at.

    Eager on purpose. Reading the profiles now means a typo in a path is a
    refusal from ``clawdence repos list`` rather than a failure eight minutes
    into a run, which is the same argument the workflow loader makes about
    conditions and the same one ``RepoProfile``'s validator makes about the
    socket tier: the cheapest moment to refuse is the one before anything was
    paid for.
    """
    origin = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            "no configuration file here — `clawdence repos list` needs one to say "
            "which repositories exist, where their mirrors go and what runs them",
            origin=origin,
        ) from exc
    except OSError as exc:
        raise ConfigError(f"cannot be read: {exc.strerror or exc}", origin=origin) from exc

    config = parse(text, origin=origin)
    root = path.parent.resolve()
    resolved = _resolve_paths(config, root)
    return Deployment(
        resolved,
        origin=path,
        profiles=_read_profiles(resolved.repos, origin=origin),
    )


def parse(text: str, *, origin: str = "<config>") -> Config:
    """Validate a configuration document that is already in hand. No disk.

    ``yaml.safe_load`` only, as in the workflow loader: the full loader
    constructs arbitrary Python objects, and this is a file the system reads at
    startup with the operator's privileges.
    """
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(str(exc), origin=origin) from exc
    if document is None:
        raise ConfigError("is empty", origin=origin)
    if not isinstance(document, dict):
        raise ConfigError(
            f"must be a mapping at the top level, not {type(document).__name__}", origin=origin
        )

    _check_schema_version(document, origin)
    try:
        return Config.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(
            "does not match the configuration schema:\n" + _format(exc), origin=origin
        ) from exc


def _check_schema_version(document: dict[str, Any], origin: str) -> None:
    declared = document.get("schema_version", CONFIG_SCHEMA_VERSION)
    if declared == CONFIG_SCHEMA_VERSION:
        return
    if not isinstance(declared, int):
        raise ConfigError(
            f"schema_version must be an integer, not {type(declared).__name__}", origin=origin
        )
    direction = "newer" if declared > CONFIG_SCHEMA_VERSION else "older"
    raise ConfigError(
        f"declares schema_version {declared}, which is {direction} than this build "
        f"understands ({CONFIG_SCHEMA_VERSION})",
        origin=origin,
    )


def _format(exc: ValidationError) -> str:
    return "\n".join(
        f"  {'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors()
    )


def _resolve_paths(config: Config, root: Path) -> Config:
    """Every path made absolute against the config file's directory.

    ``~`` first, then the config file's directory, and never the process's
    working directory — see the module docstring. Applied here rather than at
    each use so there is one answer to "what does this path mean" instead of one
    per caller.
    """
    return config.model_copy(
        update={
            "paths": config.paths.model_copy(
                update={
                    "repo_store": _absolute(config.paths.repo_store, root),
                    "work_root": _absolute(config.paths.work_root, root),
                    "workflows": _absolute(config.paths.workflows, root),
                }
            ),
            "repos": tuple(_absolute(path, root) for path in config.repos),
        }
    )


def _absolute(path: Path, root: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else (root / expanded).resolve()


def _read_profiles(paths: tuple[Path, ...], *, origin: str) -> dict[RepoId, RepoProfile]:
    """Parse each profile, and refuse two repositories sharing an id.

    The duplicate check is not housekeeping. An id is what ``mirror_name``
    derives a directory from and what every ``VcsPort`` call passes, so two
    profiles claiming one id would share an object store and a branch namespace —
    the exact outcome ``mirror_name`` puts a digest in the directory name to
    prevent, arriving from a different direction.
    """
    profiles: dict[RepoId, RepoProfile] = {}
    seen: dict[RepoId, Path] = {}
    for path in paths:
        profile = _read_profile(path, origin=origin)
        if profile.id in seen:
            raise ConfigError(
                f"{path} and {seen[profile.id]} both declare the repository id "
                f"{profile.id!r}; they would share one object store and one branch "
                f"namespace",
                origin=origin,
            )
        seen[profile.id] = path
        profiles[profile.id] = profile
    return profiles


def _read_profile(path: Path, *, origin: str) -> RepoProfile:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"lists the repository profile {path}, which cannot be read: {exc.strerror or exc}",
            origin=origin,
        ) from exc

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid JSON or YAML: {exc}", origin=origin) from exc

    try:
        return RepoProfile.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(
            f"{path} is not a repository profile:\n{_format(exc)}", origin=origin
        ) from exc
