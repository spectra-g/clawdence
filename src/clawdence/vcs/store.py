"""The local copy of a repository: one object store per repo, many worktrees.

S7 runs N containers against one repository, and git's index, refs and pack
files are shared state. The plan asks for the locking model to be established
here rather than discovered under load, and the shape that makes that tractable
is one **bare mirror** per repository with worktrees hung off it:

    <root>/<repo>.git          objects, refs, one lock          <- this module
    <work>/<run-id>/<repo>     a checkout per run               <- worktrees.py

Four decisions are worth the words.

**Partial, never shallow.** ``--depth=1`` is the famous way to make a big clone
fast and it is unusable here: a shallow repository cannot compute a merge base,
so nothing can answer "is this branch behind", a rebase has nothing to rebase
onto, and S13's binding of evidence to a tree hash has no history to bind
against. ``--filter=blob:none`` gets the same first-clone win — the bytes are in
the file contents, not in the commits — while leaving every commit and tree
present. Git fetches the blobs it actually needs on demand.

**The remote's refs live under ``refs/remotes/origin/``, ours under
``refs/heads/``.** That is why this builds the repository with ``init`` and a
configured refspec rather than ``clone --mirror``: a mirror's refspec is
``+refs/*:refs/*``, so a pruning fetch **deletes local branches the remote has
not seen** — which is precisely the state a run is in between creating its
branch and pushing it. The failure would be a worktree whose branch vanished
underneath it, at an interval determined by whenever some other run fetched.

**One lock per repository, held by the kernel.** ``flock`` rather than a
lockfile containing a pid, because the case that matters is the control plane
being killed: a pid file outlives the process and the next run has to decide
whether the owner is alive, which is a race with a wrong answer available. A
flock is released by the kernel on exit, on crash, and on ``kill -9``, so there
is no stale state to reason about. The in-process ``asyncio.Lock`` beside it is
not redundant — flocks conflict between file descriptors in the *same* process
too, so without it one coroutine would block a thread waiting for a lock another
coroutine in the same loop is holding.

**Fetching is not free and is not done per operation.** ``ensure`` fetches; the
callers that need a fresh answer about the remote say so. A control plane that
re-fetched before every ``head`` would spend a network round trip to answer a
question it asked itself thirty seconds ago, and the answer that matters —
whether the base moved between verification and merge — is checked at the merge
by hash comparison, not by fetch frequency.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from clawdence.domain import RepoProfile
from clawdence.ports.errors import PermanentError, TransientError
from clawdence.ports.secrets import NullSecrets, Secret, SecretProvider
from clawdence.vcs import refs
from clawdence.vcs.git import GitError, authenticated, git

#: Environment variable naming the forge token, when the caller does not say.
#: A *name*, resolved through the ``SecretProvider``, never a value.
DEFAULT_TOKEN_NAME: Final = "CLAWDENCE_FORGE_TOKEN"  # noqa: S105 - the name of one, which is the point

#: How long to wait for another run to finish its fetch or its worktree add.
#: Generous, because the operation on the other side may be the first clone of a
#: large monorepo; bounded, because a lock waited on forever is a hung run that
#: never reports why.
DEFAULT_LOCK_TIMEOUT: Final = 300.0

#: Polling interval while waiting. ``flock`` has a blocking mode, but using it
#: would mean a thread that cannot be given up if the run is cancelled.
_LOCK_POLL: Final = 0.05


class LockTimeout(TransientError):
    """Another process is still holding this repository's lock.

    Transient: the holder is doing legitimate work — a clone, a fetch, a
    worktree add — and will finish. The caller retries or gives the slot back to
    the scheduler, and neither is a decision a human has to make.
    """

    def __init__(self, path: Path, seconds: float) -> None:
        super().__init__(
            "repo-locked",
            f"waited {seconds:.0f}s for the lock on {path.name}; another run is still "
            f"fetching or creating a worktree there",
        )


def mirror_name(repo_id: str) -> str:
    """Directory name for a repository's object store.

    A slug for a human reading ``ls``, and a digest of the *full* id because the
    slug is lossy: ``repo:api`` and ``repo.api`` are different repositories and
    both slugify to ``repo-api``. Two repositories sharing one object store and
    one branch namespace is the worst outcome available in this module, so the
    twelve characters that make it impossible are cheap.
    """
    digest = hashlib.sha256(repo_id.encode("utf-8")).hexdigest()[:12]
    return f"{refs.slugify(repo_id) or 'repo'}-{digest}.git"


@dataclass(slots=True)
class RepoStore:
    """Mirrors on local disk, one per repository, with the lock that guards them.

    ``token_name`` is a secret *name*: the value is resolved at the moment a
    remote operation starts and is never held on this object. A store that
    carried a revealed token would put it in every traceback that printed a
    frame's locals.
    """

    root: Path
    secrets: SecretProvider = field(default_factory=NullSecrets)
    token_name: str | None = DEFAULT_TOKEN_NAME
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT

    #: Pins the git executable. Defaults to a ``PATH`` lookup.
    git_path: str | None = None

    #: The one caller environment value an SSH transport may inherit is
    #: ``SSH_AUTH_SOCK``. Injected for tests and deployments that do not use the
    #: process environment; never forwarded to a runner.
    environ: Mapping[str, str] | None = None

    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    # ------------------------------------------------------------------ paths

    def mirror(self, profile: RepoProfile) -> Path:
        return self.root / mirror_name(profile.id)

    # ------------------------------------------------------------ the network

    async def ensure(self, profile: RepoProfile, *, fetch: bool = True) -> Path:
        """The mirror for this repository, created and up to date.

        Split in two on purpose. Creating the directory and configuring the
        remote touches no network, so a failure there is a disk or a permission
        problem and says so; fetching is where a wrong URL, a missing credential
        or an unreachable forge shows up, and those are different conversations.
        """
        path = self.mirror(profile)
        async with self.locked(profile):
            if not (path / "HEAD").exists():
                await self._create(profile, path)
            if fetch:
                await self._fetch(profile, path)
        return path

    async def _create(self, profile: RepoProfile, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        try:
            await self.git(path, "init", "--bare", "--initial-branch", profile.default_branch)
            await self.git(path, "remote", "add", "origin", "--", profile.remote_url)
            # Ours under refs/heads, theirs under refs/remotes/origin. See the
            # module docstring: a mirror refspec would let a pruning fetch delete
            # a branch a live run has created and not yet pushed.
            await self.git(
                path,
                "config",
                "remote.origin.fetch",
                "+refs/heads/*:refs/remotes/origin/*",
            )
            if profile.checkout.partial:
                await self.git(path, "config", "remote.origin.promisor", "true")
                await self.git(path, "config", "remote.origin.partialclonefilter", "blob:none")
            if not profile.checkout.fetch_lfs:
                # Not a filter and not a hook: the smudge filter is what turns a
                # pointer file into megabytes, and skipping it leaves the pointer
                # in the tree, which is what a diff and a build that does not
                # touch assets both want.
                await self.git(path, "config", "filter.lfs.smudge", "git-lfs smudge --skip -- %f")
                await self.git(
                    path, "config", "filter.lfs.process", "git-lfs filter-process --skip"
                )
        except (GitError, OSError) as exc:
            raise PermanentError(
                "mirror-not-created",
                f"could not create a local mirror for {profile.id} at {path}: {exc}",
            ) from exc

    async def _fetch(self, profile: RepoProfile, path: Path) -> None:
        argv = ["fetch", "--prune", "--quiet"]
        if profile.checkout.partial:
            argv.append("--filter=blob:none")
        argv.append("origin")
        try:
            await self.remote_git(profile, path, *argv)
        except (GitError, OSError) as exc:
            raise remote_error(profile, "fetch", exc, token_name=self.token_name) from exc

    async def resolve(self, profile: RepoProfile, ref: str) -> str:
        """A ref on the remote, as a full commit id.

        Remote-tracking first and the bare name second, so ``"main"`` means "what
        origin has on main" rather than "whatever a local branch of that name
        happens to point at". A local branch shadowing the remote's is exactly
        the state a half-finished run leaves behind, and resolving to it would
        make the next run branch from unmerged work.
        """
        path = self.mirror(profile)
        for candidate in (f"refs/remotes/origin/{ref}", ref):
            with contextlib.suppress(GitError, OSError):
                return await self.git(path, "rev-parse", "--verify", f"{candidate}^{{commit}}")
        raise PermanentError("unknown-ref", f"{profile.id} has no ref {ref!r}")

    async def push(self, profile: RepoProfile, cwd: Path, refspec: str) -> None:
        """Publish a refspec from ``cwd`` — a worktree, or the mirror itself.

        No ``--force``, ever, and that is a design position rather than a default
        left alone: a force push discards commits the remote has and this system
        has no way to know whether they were somebody else's. A branch that
        cannot fast-forward is a conflict, and a conflict is a decision.
        """
        try:
            await self.remote_git(profile, cwd, "push", "--quiet", "origin", refspec)
        except (GitError, OSError) as exc:
            raise remote_error(
                profile,
                "push",
                exc,
                token_name=self.token_name,
                permanent_markers=(
                    "non-fast-forward",
                    "fetch first",
                    "protected branch",
                    "does not match any",
                ),
            ) from exc

    # ----------------------------------------------------------------- plumbing

    async def git(self, cwd: Path, *args: str) -> str:
        """One local git command against the mirror or a worktree. No network."""
        return await git(cwd, *args, path=self.git_path)

    async def remote_git(self, profile: RepoProfile, cwd: Path, *args: str) -> str:
        """One git command that talks to the forge, with a credential if we have
        one. ``find`` rather than ``resolve``: a public repository over https and
        any repository over ssh need no token, and demanding one would refuse a
        configuration that works."""
        with authenticated(
            self._token(), remote_url=profile.remote_url, environ=self.environ
        ) as env:
            return await git(cwd, *args, path=self.git_path, env=env)

    def _token(self) -> Secret | None:
        return None if self.token_name is None else self.secrets.find(self.token_name)

    # --------------------------------------------------------------- the lock

    @contextlib.asynccontextmanager
    async def locked(self, profile: RepoProfile) -> AsyncIterator[None]:
        """Exclusive access to one repository's mirror, in-process and across.

        Both halves are needed and they guard different things. The
        ``asyncio.Lock`` stops two coroutines in this event loop from each
        blocking a thread on a descriptor the other holds. The ``flock`` stops
        two control planes — or a control plane and an operator's shell — from
        fetching into the same object store at once.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self._locks.setdefault(profile.id, asyncio.Lock())
        path = self.root / f"{mirror_name(profile.id)}.lock"
        async with lock:
            handle = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _flock, handle, path, self.lock_timeout
                )
                yield
            finally:
                os.close(handle)


def _flock(handle: int, path: Path, timeout: float) -> None:
    """Take an exclusive flock, or raise once ``timeout`` has passed.

    Polled rather than blocking so the wait is bounded by a number an operator
    configured instead of by the other side's behaviour. Closing the descriptor
    releases the lock, which is what the ``finally`` above is for and what makes
    a killed process leave nothing behind.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise LockTimeout(path, timeout) from None
            time.sleep(_LOCK_POLL)


_AUTH_DENIED: Final[tuple[str, ...]] = (
    "authentication failed",
    "permission denied (publickey",
    "could not read username",
    "http 401",
    "http 403",
    "returned error: 401",
    "returned error: 403",
)


def remote_error(
    profile: RepoProfile,
    operation: str,
    exc: BaseException,
    *,
    token_name: str | None,
    permanent_markers: tuple[str, ...] = (),
) -> PermanentError | TransientError:
    """Translate transport details at the adapter boundary.

    Authentication and an explicit forge rejection cannot improve by retrying
    unchanged. Disconnects, DNS failures and timeouts can. Keeping that decision
    here means callers receive ``PortError`` consistently and never have to
    interpret Git's prose or accidentally let ``GitError`` escape as a traceback.
    """
    text = str(exc)
    lowered = text.lower()
    if any(marker in lowered for marker in _AUTH_DENIED):
        return PermanentError(
            f"{operation}-denied",
            f"the forge refused the credential for {profile.id}; "
            f"{token_name or 'the configured SSH identity'} is what was offered",
        )
    if any(marker in lowered for marker in permanent_markers):
        return PermanentError(
            f"{operation}-rejected", f"the forge rejected {operation} for {profile.id}: {text}"
        )
    return TransientError(
        f"{operation}-failed", f"the remote operation for {profile.id} could not finish: {text}"
    )
