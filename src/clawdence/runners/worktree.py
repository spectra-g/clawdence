"""Git, run against a directory that model-generated code has been writing to.

Everything here treats the worktree as **output**, not as a workspace we own.
That framing is not decoration; it changes three things about how git is called:

**Git reads configuration from inside the thing it is inspecting.** A repository
can set ``core.fsmonitor`` to a command, and git runs it — so a plain
``git status`` in a worktree an agent just edited is arbitrary code execution in
whatever process runs it. On the ``container`` tier (S7) that process is inside
the container; on ``host`` it is the control plane itself. Every invocation here
therefore pins the knobs that execute things (``_HARDENING``) and ignores the
operator's own global and system config (``_ENV``), for the same reason the
fixture builder does: a maintainer with ``commit.gpgsign = true`` should not get
a runner that blocks forever on a passphrase nobody is watching for.

What that does **not** close: ``.gitattributes`` clean/smudge filters and
textconv drivers still run, because disabling them would silently misreport
content in repositories that legitimately use them (git-lfs, for one). That
residue is contained by the tier, not by this module, and it is one of the
reasons ``host`` is documented as "local dev only, never default".

**Identity is supplied, never discovered.** A commit needs an author, and the
runner must not borrow the operator's — a commit attributed to a human who did
not write it is a lie in the one place a repository keeps permanently.

**Nothing here trusts the diff to be small.** ``diff_stat`` asks for numbers, not
content: the diff can be megabytes and the control plane needs to know whether
there is one and roughly how big, which is exactly what ``DiffStat`` holds.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from clawdence.domain import DiffStat


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """Who the runner's commits are attributed to."""

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
_HARDENING: Final[tuple[str, ...]] = (
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
_ENV: Final[dict[str, str]] = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    # A prompt inside a run is a hang, not a failure.
    "GIT_TERMINAL_PROMPT": "0",
    # Read-only inspection should not take the index lock; a runner that leaves a
    # stale lock behind breaks the *next* run, which is the worst kind of leak.
    "GIT_OPTIONAL_LOCKS": "0",
}


class GitError(RuntimeError):
    """A git invocation failed. Carries the command and git's own complaint."""

    def __init__(self, argv: tuple[str, ...], stderr: str) -> None:
        self.argv = argv
        self.stderr = stderr
        super().__init__(f"git {' '.join(argv)} failed: {stderr.strip() or '(no output)'}")


async def git(worktree: Path, *args: str, path: str | None = None, strip: bool = True) -> str:
    """Run one git command in ``worktree`` and return its stdout.

    ``strip`` is on because almost every caller wants a single hash. It is a
    parameter rather than a rule because ``git status`` puts a *space* in the
    first column, and stripping it silently removes the first character of the
    first filename — a bug that only shows up when the first changed file is
    modified rather than added.

    ``path`` pins the git executable for a caller that would rather not inherit
    a ``PATH`` lookup. It defaults to a lookup because the common case is a
    developer machine, where pinning it is friction with no reader.
    """
    argv = (path or "git", *_HARDENING, *args)
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=worktree,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(_ENV),
    )
    raw_out, raw_err = await process.communicate()
    if process.returncode != 0:
        raise GitError(args, raw_err.decode("utf-8", errors="replace"))
    decoded = raw_out.decode("utf-8", errors="replace")
    return decoded.strip() if strip else decoded


async def is_repository(worktree: Path) -> bool:
    """Whether ``worktree`` is inside a git working tree."""
    try:
        return await git(worktree, "rev-parse", "--is-inside-work-tree") == "true"
    except (GitError, OSError):
        return False


async def head(worktree: Path) -> str:
    return await git(worktree, "rev-parse", "HEAD")


async def has_commit(worktree: Path, commit: str) -> bool:
    """Whether ``commit`` names an object this repository actually holds.

    Checked before a run rather than after: a request naming a base commit that
    does not exist here cannot produce a meaningful diff, and finding that out
    after the agent has spent twenty minutes is finding it out too late.
    """
    try:
        await git(worktree, "cat-file", "-e", f"{commit}^{{commit}}")
    except (GitError, OSError):
        return False
    return True


async def pending_changes(worktree: Path) -> tuple[str, ...]:
    """Paths git would commit — modified, added or untracked, ignored excluded.

    ``--porcelain`` because the human-readable format is explicitly not stable,
    and parsing it is how a tool breaks on a git upgrade.

    ``-z`` for a reason specific to this caller: without it git *quotes* paths
    containing unusual characters, and a filename containing a newline is split
    across two records. An agent can create a file called whatever it likes, so
    "unusual" here is not hypothetical — it is untrusted input to a parser.
    NUL-separated output has no quoting and no ambiguity.
    """
    raw = await git(
        worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        strip=False,
    )
    records = [record for record in raw.split("\0") if record]
    paths: list[str] = []
    skip_next = False
    for record in records:
        if skip_next:
            # A rename or copy emits the destination and then the source as a
            # second record with no status prefix of its own.
            skip_next = False
            continue
        paths.append(record[3:])
        skip_next = record[:1] in ("R", "C")
    return tuple(paths)


async def commit_all(
    worktree: Path,
    message: str,
    *,
    identity: GitIdentity = DEFAULT_IDENTITY,
) -> str | None:
    """Commit whatever the agent left behind. Returns the new head, or ``None``.

    ``None`` means there was nothing to commit — which is the common case when
    the agent committed its own work, and is *not* a failure. The distinction
    between "nothing to commit" and "nothing changed" is not made here: this
    reports what it did, and ``diff_stat`` against the base is what decides
    whether the run produced anything.

    Identity travels as ``-c`` overrides rather than ``git config`` writes,
    because writing config mutates the repository to record who we are, and the
    worktree is somebody else's repository.
    """
    if not await pending_changes(worktree):
        return None
    await git(worktree, "add", "--all")
    await git(
        worktree,
        "-c",
        f"user.name={identity.name}",
        "-c",
        f"user.email={identity.email}",
        "commit",
        "--no-verify",
        "--no-gpg-sign",
        "--message",
        message,
    )
    return await head(worktree)


async def diff_stat(worktree: Path, base: str, target: str = "HEAD") -> DiffStat:
    """How much changed between two commits.

    ``--numstat`` rather than ``--shortstat`` because the summary line's wording
    varies with the change ("1 file changed, 2 insertions(+)") and parsing prose
    is how a counter silently starts returning zero. ``--no-ext-diff`` because an
    external diff driver configured in the repository is another program the
    worktree gets to choose.

    Binary files report ``-`` for both counts. They count as a changed file with
    no line counts, which is the honest answer: "one file changed, and lines are
    not a meaningful unit for it".
    """
    raw = await git(
        worktree,
        "diff",
        "--numstat",
        "--no-ext-diff",
        "--no-color",
        base,
        target,
    )
    files = insertions = deletions = 0
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:  # pragma: no cover - git does not emit these
            continue
        files += 1
        if parts[0] != "-":
            insertions += int(parts[0])
        if parts[1] != "-":
            deletions += int(parts[1])
    return DiffStat(files_changed=files, insertions=insertions, deletions=deletions)


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
