"""Running git from a process that holds every credential in the system.

This is the invocation the whole project shares. It was written for the runner
(``runners.worktree``), which runs git against a directory a model has been
editing; it lives here now because the control plane runs git too, and two
copies of a security control is how one copy drifts. ``runners.worktree``
re-exports what it used to define, so nothing there changed except where the
bytes are.

The hardening is unchanged and the reasoning is worth restating, because it is
not obvious that ``git status`` is a code-execution primitive: **git reads
configuration from inside the thing it is inspecting.** A repository can set
``core.fsmonitor`` to a command and git runs it; ``core.hooksPath`` names a
directory of scripts; ``protocol.ext`` lets a URL name a program to speak the
transport. Every invocation here pins those three off and replaces the
environment wholesale, so nothing from the operator's shell — a ``GIT_DIR``, a
credential helper, an ssh agent — reaches a repository we did not write.

What is genuinely new is the half the runner never needed:

**The control plane authenticates and the runner does not.** ``EgressPolicy``
denies the runner the git remote by default — the control plane pushes, the
runner writes to a worktree — so the runner's git has no credential and needs
none. This one does. That is why the environment is produced by a function
rather than being a constant: authenticated is a *mode*, entered deliberately,
for one host, for the length of one operation.

**A token never enters argv, and never enters a remote URL.** ``ps`` is readable
by every user on the box, so ``-c http.extraHeader=Authorization: Basic …``
publishes the credential to anyone logged in. A URL of the form
``https://x-access-token:TOKEN@github.com/…`` is worse: ``git clone`` writes the
remote URL into the clone's own ``.git/config``, so the token outlives the
process that used it and is sitting in a file when the next person looks. So the
header goes into a config *file*, mode 0600, in a directory only this user can
enter, named by ``GIT_CONFIG_GLOBAL`` — which was already ``/dev/null`` here, so
pointing it at a file we wrote ourselves keeps the operator's own config just as
ignored as before.

**The header is scoped to one host.** ``[http "https://github.com/"]`` rather
than a bare ``http.extraHeader``. An unscoped header is attached to *every*
request git makes, and git makes requests to hosts this system did not choose:
a submodule URL is content the repository controls, and an unscoped credential
plus an attacker-authored ``.gitmodules`` is a token sent to a host of their
naming. Submodules are also never fetched here, for the same reason.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from clawdence.ports.secrets import Secret


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """Who a commit is attributed to."""

    name: str
    email: str


#: Deliberately not a person, and deliberately ``.invalid`` (RFC 2606), so a
#: commit made by the runner is unambiguous in ``git log`` and any mail sent to
#: the address bounces rather than reaching somebody unrelated.
DEFAULT_IDENTITY: Final = GitIdentity(name="Clawdence runner", email="runner@clawdence.invalid")

#: Config overrides passed to every invocation. Each one names a mechanism by
#: which repository-local configuration gets git to execute something:
#: ``core.fsmonitor`` and ``core.hooksPath`` run programs outright, and
#: ``protocol.ext`` lets a URL name a command to speak the transport.
HARDENING: Final[tuple[str, ...]] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
)

#: Environment for every invocation. Replaced, not extended — a ``GIT_DIR`` or a
#: credential helper leaking in from the operator's shell is how a runner ends up
#: committing somewhere nobody asked it to.
BASE_ENV: Final[Mapping[str, str]] = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    # A prompt inside a run is a hang, not a failure.
    "GIT_TERMINAL_PROMPT": "0",
    # Read-only inspection should not take the index lock; a runner that leaves a
    # stale lock behind breaks the *next* run, which is the worst kind of leak.
    "GIT_OPTIONAL_LOCKS": "0",
}

#: Username half of the basic-auth pair. GitHub ignores it for a personal access
#: token and requires exactly this for an app installation token, so it is the
#: one value that works for both.
_TOKEN_USER: Final = "x-access-token"  # noqa: S105 - a username, not a password


class GitError(RuntimeError):
    """A git invocation failed. Carries the command and git's own complaint."""

    def __init__(self, argv: tuple[str, ...], stderr: str) -> None:
        self.argv = argv
        self.stderr = stderr
        super().__init__(f"git {' '.join(argv)} failed: {stderr.strip() or '(no output)'}")


async def git(
    cwd: Path,
    *args: str,
    path: str | None = None,
    strip: bool = True,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> str:
    """Run one git command in ``cwd`` and return its stdout.

    ``strip`` is on because almost every caller wants a single hash. It is a
    parameter rather than a rule because ``git status`` puts a *space* in the
    first column, and stripping it silently removes the first character of the
    first filename — a bug that only shows up when the first changed file is
    modified rather than added.

    ``path`` pins the git executable for a caller that would rather not inherit
    a ``PATH`` lookup. It defaults to a lookup because the common case is a
    developer machine, where pinning it is friction with no reader.

    ``env`` replaces the environment rather than extending it, and defaults to
    ``BASE_ENV``. Callers that need a remote pass ``authenticated(...)``'s value,
    which is ``BASE_ENV`` with one variable changed.

    ``stdin`` is for the batch plumbing — ``cat-file --batch-check`` answers a
    hundred questions in one process, and asking them one at a time is a hundred
    forks. It defaults to a closed descriptor, because a git command that reads
    from an inherited terminal is a run that hangs.
    """
    argv = (path or "git", *HARDENING, *args)
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(BASE_ENV if env is None else env),
    )
    raw_out, raw_err = await process.communicate(None if stdin is None else stdin.encode("utf-8"))
    if process.returncode != 0:
        raise GitError(args, raw_err.decode("utf-8", errors="replace"))
    decoded = raw_out.decode("utf-8", errors="replace")
    return decoded.strip() if strip else decoded


async def head(worktree: Path, ref: str = "HEAD") -> str:
    """Resolve a ref to a full commit id."""
    return await git(worktree, "rev-parse", ref)


async def exclude(worktree: Path, *patterns: str) -> None:
    """Hide paths from git without touching a tracked file.

    The runner installs files into the worktree — a conventions file, a plan, a
    verdict — and every one of them would otherwise land in ``git add --all``
    and then in somebody's pull request. ``.gitignore`` is tracked content and
    editing it *is* a change to the repository; ``$GIT_DIR/info/exclude`` is
    local, untracked, and exactly what this is for.

    The path is resolved with ``rev-parse --git-path`` rather than assembled,
    because in a linked worktree ``.git`` is a file and the real directory is
    somewhere under the main repository.
    """
    target = Path(await git(worktree, "rev-parse", "--git-path", "info/exclude"))
    if not target.is_absolute():
        target = worktree / target
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    missing = [pattern for pattern in patterns if pattern not in existing.splitlines()]
    if not missing:
        return
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    target.write_text(existing + prefix + "\n".join(missing) + "\n", encoding="utf-8")


def origin_of(remote_url: str) -> str:
    """The ``scheme://host[:port]/`` prefix a credential is scoped to.

    A trailing slash, because git matches ``[http "<prefix>"]`` by path prefix
    and ``https://github.com`` without one also matches ``https://github.com.evil
    .example`` — the same parse-don't-prefix-match bug the model provider's
    ``require_secure`` had, in a different dialect.
    """
    parts = urlsplit(remote_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"{remote_url!r} is not an absolute URL, so it has no origin to scope to")
    return f"{parts.scheme}://{parts.netloc}/"


@contextlib.contextmanager
def authenticated(token: Secret | None, *, remote_url: str) -> Iterator[Mapping[str, str]]:
    """An environment that can push to ``remote_url``, for as long as the block.

    Yields ``BASE_ENV`` unchanged when there is no token — an SSH remote, a
    local path, a public repository being read — so callers do not branch. The
    token, when there is one, reaches git through a file this writes and removes,
    never through argv and never through a URL.

    The file is deleted on the way out even if the body raised. That matters more
    than it looks: the directory is under the system temp root, and a crash that
    left one behind would leave a readable-by-us copy of a forge token sitting in
    ``/tmp`` until the next reboot.
    """
    if token is None or not token:
        yield BASE_ENV
        return

    origin = origin_of(remote_url)
    basic = base64.b64encode(f"{_TOKEN_USER}:{token.reveal()}".encode()).decode("ascii")
    with tempfile.TemporaryDirectory(prefix="clawdence-git-") as directory:
        config = Path(directory) / "config"
        # Created 0600 *before* anything is written to it. Writing first and
        # chmod-ing after leaves a window in which the file exists with the
        # process umask, and on a shared box that window is the whole exposure.
        config.touch(mode=0o600)
        config.write_text(
            f'[http "{origin}"]\n'
            f"\textraHeader = Authorization: Basic {basic}\n"
            # An empty helper list clears any that a repository-local config
            # tries to add. Nothing should be consulting a keychain on our
            # behalf, and a helper that prompts is a hang.
            "[credential]\n"
            "\thelper =\n",
            encoding="utf-8",
        )
        yield {**BASE_ENV, "GIT_CONFIG_GLOBAL": str(config)}
