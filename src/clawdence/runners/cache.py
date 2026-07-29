"""The warm dependency cache — where the time in S7 actually goes.

The container is not the expensive part of the container tier. A cold
``yarn install`` on a large monorepo dominates a run, and it is paid *again* on
the next run, because the worktree is ephemeral by design (§3.3) and
``node_modules`` goes with it. v1 worked around exactly this with
``worktree_symlinks``, which is a hack whose whole purpose was to stop the
install happening twice; the plan replaces it with a cache that outlives the
worktree.

**What persists is the package manager's cache, not its output.** ``.venv``,
``node_modules`` and ``target/`` belong to the tree they were built for, and
sharing them across worktrees is how one run's half-installed state becomes
another run's mystery. What is safe to share is the *downloaded artefact* store
— ``~/.npm``, ``~/.m2/repository``, ``~/.cache/uv``, ``$GOMODCACHE`` — which is
content-addressed, immutable once written, and the thing the network is spent
on. So the second run still installs; it installs from disk.

**A host directory, not a named volume.** The obvious spelling is
``docker volume create``, and it is wrong here for two reasons. A fresh named
volume is created owned by ``root``, and this tier runs the container as the
invoking user precisely so that files in a bind mount come back owned by
somebody who can delete them — so the first thing a named volume needs is a
privileged container to ``chown`` it, which is a root container introduced to
support a cache. And a directory works on the ``host`` tier too: there is
nothing to mount, the same environment variables point at the same path, and the
one interesting claim — that the second run is materially faster — becomes
testable without a daemon.

**Path identity, again.** The directory is mounted at the same absolute path it
has on the host, for the same reason the worktree is: the environment variables
below name absolute paths, and a cache that appeared at a different path inside
the container would need two spellings of every one of them.

**Pointed at by environment variable, never by mounting over ``$HOME``.** The
container's ``HOME`` is inside the worktree (see ``ContainerRunner._inherited``),
so the default cache locations are inside the tree that gets thrown away.
Naming them explicitly is also what makes the cache legible: a run's environment
says where its cache is, rather than it being implied by a mount.

What this deliberately does **not** do is build images. §3.8's other half —
who builds a base image, on what cadence, scanned by what — is a supply-chain
question with a release process attached, and ``RepoProfile.runner_image`` plus
the runner's per-build-system image map is the part of it this step owes: a
toolchain baked into a layer is configuration here, not a pipeline.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import blake2s
from pathlib import Path
from typing import Final

from clawdence.domain import BuildSystem, RepoProfile

#: Where caches live when nothing says otherwise. Under the user's cache home
#: rather than beside the state database: this is regenerable data, and a
#: backup or a sync tool that treats ``~/.clawdence`` as precious should not be
#: copying a hundred gigabytes of Maven artefacts with it.
CACHE_HOME_ENV: Final = "CLAWDENCE_CACHE_HOME"

#: Subdirectory of the cache home per repository. Named rather than inlined
#: because the reaper sweeps it and the two must agree.
DEPS_DIR: Final = "deps"

#: Characters allowed in the directory name derived from a repo id. ``RepoId``
#: admits ``:``, which is legal on POSIX, illegal on Windows, and structure to
#: half the tools that will ever look at this directory.
_UNSAFE_IN_NAME: Final = re.compile(r"[^A-Za-z0-9._-]")


def cache_home(environ: Mapping[str, str] | None = None) -> Path:
    """The root of every cache on this machine."""
    env = os.environ if environ is None else environ
    override = env.get(CACHE_HOME_ENV)
    if override:
        return Path(override)
    xdg = env.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path(env.get("HOME", "~")).expanduser() / ".cache"
    return base / "clawdence" / DEPS_DIR


@dataclass(frozen=True, slots=True)
class CachePlan:
    """One repository's warm cache: where it is, and what points at it."""

    #: Host path, and — because of path identity — the container path too. This
    #: is the one directory that gets mounted; everything below is inside it.
    directory: Path

    #: The directories the package manager will write into. Kept separately from
    #: ``env`` because not every variable's value *is* a path — Maven's is a
    #: command-line flag with one inside it — and a ``mkdir`` over the values
    #: would create a directory named after the flag.
    subdirectories: tuple[Path, ...]

    #: Environment naming those directories. Absolute paths, because a package
    #: manager resolves a relative cache path against its own working directory,
    #: which is the worktree, which is the thing being escaped.
    env: Mapping[str, str]

    def prepare(self) -> None:
        """Create the directories, as the invoking user, before the run.

        Before, and not on demand, for the reason the module docstring gives
        about ownership: a bind-mount source the daemon has to create is created
        by the daemon, as root, and a container running as somebody else then
        cannot write to its own cache. Creating them here means the mount finds
        directories that already exist and already belong to the right user.
        """
        for path in self.subdirectories:
            path.mkdir(parents=True, exist_ok=True)
        # Last *used*, not last written. A repository whose dependencies are
        # already complete reads its cache and changes nothing in it, so an
        # mtime left to the package manager would show a cache in daily use as
        # untouched for a month — and the reaper would agree and delete it.
        os.utime(self.directory)


#: Where each build system keeps the artefacts it downloaded, and the variable
#: that moves it. One entry per build system that has a cache worth keeping;
#: ``UNKNOWN`` has none, and a repository whose build system was never
#: identified gets no cache rather than a guessed one.
#:
#: Two of these are compromises worth knowing about:
#:
#: ``MAVEN`` uses ``MAVEN_ARGS`` rather than ``MAVEN_OPTS``. ``MAVEN_OPTS`` is
#: the JVM's arguments and repositories legitimately set it; appending to
#: something the repository owns is how a cache setting silently drops somebody's
#: heap size. ``MAVEN_ARGS`` (Maven 3.9+) is Maven's own, and a repository that
#: pins an older Maven simply does not get a cache here.
#:
#: ``CARGO_HOME`` holds cargo's binaries as well as its registry, and the
#: standard images put them in the same place. Moving it is safe *because* those
#: images also put ``$CARGO_HOME/bin`` on ``PATH`` explicitly, so ``cargo``
#: still resolves; what is lost is a ``config.toml`` baked into the image. There
#: is no narrower variable — the registry path is not separately configurable —
#: so this is the honest maximum.
_LAYOUTS: Final[Mapping[BuildSystem, Mapping[str, str]]] = {
    BuildSystem.MAVEN: {"MAVEN_ARGS": "m2"},
    BuildSystem.GRADLE: {"GRADLE_USER_HOME": "gradle"},
    BuildSystem.NPM: {"npm_config_cache": "npm"},
    BuildSystem.YARN: {"YARN_CACHE_FOLDER": "yarn"},
    BuildSystem.PNPM: {"PNPM_STORE_DIR": "pnpm"},
    BuildSystem.UV: {"UV_CACHE_DIR": "uv"},
    BuildSystem.POETRY: {"POETRY_CACHE_DIR": "poetry"},
    BuildSystem.PIP: {"PIP_CACHE_DIR": "pip"},
    BuildSystem.GO: {"GOMODCACHE": "gomod", "GOCACHE": "gobuild"},
    BuildSystem.CARGO: {"CARGO_HOME": "cargo"},
}

#: Variables whose value is a flag rather than a path. Maven is the only one,
#: and it is why this exists at all: ``MAVEN_ARGS=/some/path`` would be Maven
#: being handed a goal named after a directory.
_AS_FLAG: Final[Mapping[str, str]] = {"MAVEN_ARGS": "-Dmaven.repo.local={path}"}


@dataclass(frozen=True, slots=True)
class Cache:
    """The dependency caches on this machine, one directory per repository."""

    root: Path

    #: Off is a supported configuration, not a missing feature. A repository
    #: whose install is already fast pays nothing for the cache; one whose
    #: build system writes into its cache non-atomically pays correctness for
    #: it, and there has to be a way to say so without editing the profile.
    enabled: bool = True

    @classmethod
    def default(cls, environ: Mapping[str, str] | None = None) -> Cache:
        return cls(root=cache_home(environ))

    def plan(self, profile: RepoProfile) -> CachePlan | None:
        """This repository's cache, or ``None`` if it has nothing to cache."""
        layout = _LAYOUTS.get(profile.build_system)
        if not self.enabled or layout is None:
            return None
        directory = self.directory(profile)
        return CachePlan(
            directory=directory,
            subdirectories=tuple(directory / name for name in sorted(set(layout.values()))),
            env={
                name: _AS_FLAG.get(name, "{path}").format(path=directory / subdirectory)
                for name, subdirectory in layout.items()
            },
        )

    def directory(self, profile: RepoProfile) -> Path:
        """Where this repository's cache lives.

        Keyed on the repo id *and* the build system, so a repository that
        changes build system starts a new cache rather than inheriting one laid
        out for the previous one. The digest is what makes the name unique — the
        readable half is truncated and lossy, and two repo ids differing only in
        a character this strips would otherwise share a cache.
        """
        readable = _UNSAFE_IN_NAME.sub("-", profile.id)[:48]
        digest = blake2s(
            f"{profile.id}\x1f{profile.build_system.value}".encode(), digest_size=8
        ).hexdigest()
        return self.root / f"{readable}-{profile.build_system.value}-{digest}"
