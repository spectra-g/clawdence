"""Git, run against a directory that model-generated code has been writing to.

Everything here treats the worktree as **output**, not as a workspace we own.
That framing is not decoration; it changes three things about how git is called:

**Git reads configuration from inside the thing it is inspecting.** A repository
can set ``core.fsmonitor`` to a command, and git runs it — so a plain
``git status`` in a worktree an agent just edited is arbitrary code execution in
whatever process runs it. On the ``container`` tier (S7) that process is inside
the container; on ``host`` it is the control plane itself. That hardening now
lives in ``clawdence.vcs.git``, because at S15 the control plane runs git too and
two copies of a security control is how one copy drifts. ``git``, ``head``,
``exclude``, ``GitError`` and the identity types are re-exported here, so this
module's callers did not have to move with it.

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

**Nothing here authenticates.** ``EgressPolicy.allow_git_remote`` is false by
default: the control plane pushes, the runner does not, so no function in this
module has any business holding a credential. ``clawdence.vcs.git.authenticated``
is the other side of that line.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from clawdence.domain import DiffStat
from clawdence.vcs.git import (
    DEFAULT_IDENTITY,
    GitError,
    GitIdentity,
    exclude,
    git,
    head,
)

__all__ = [
    "DEFAULT_IDENTITY",
    "GitError",
    "GitIdentity",
    "commit_all",
    "commits_ahead",
    "diff_stat",
    "exclude",
    "exists_at",
    "git",
    "has_commit",
    "head",
    "is_repository",
    "pending_changes",
    "revert_to",
]


async def is_repository(worktree: Path) -> bool:
    """Whether ``worktree`` is inside a git working tree."""
    try:
        return await git(worktree, "rev-parse", "--is-inside-work-tree") == "true"
    except (GitError, OSError):
        return False


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


async def commits_ahead(worktree: Path, base: str, target: str = "HEAD") -> int:
    """How many commits ``target`` has that ``base`` does not.

    Called **before** the runner's own safety commit, which is the only time it
    answers the question anybody wants: how many commits *the agent* made. After
    ``commit_all`` the number always includes ours, and "the agent committed
    nothing" — §3.7a's dropped commit, the characteristic weak-model failure —
    stops being expressible.

    ``--count`` rather than counting lines, because a repository can produce a
    lot of them and this only ever needs the number.
    """
    raw = await git(worktree, "rev-list", "--count", f"{base}..{target}")
    return int(raw or 0)


async def exists_at(worktree: Path, commit: str, path: str) -> bool:
    """Whether ``commit`` has anything at ``path``."""
    try:
        await git(worktree, "cat-file", "-e", f"{commit}:{path}")
    except (GitError, OSError):
        return False
    return True


async def revert_to(worktree: Path, base: str, path: str) -> None:
    """Put ``path`` back to whatever ``base`` had there — including nothing.

    This is §3.9's repair, and it is deliberately anchored on the **base commit**
    rather than on ``HEAD``. The obvious version — ``git checkout -- path``,
    undo the modification — works only if the file is still merely modified, and
    by the time anybody looks it usually is not: the agent ran ``git add --all``
    like every coding CLI does, ``$GIT_DIR/info/exclude`` has no effect on a
    tracked path, and so the runner's own conventions file is already *inside* the
    agent's commit. Reverting against ``HEAD`` at that point restores our copy.

    Against the base it is right in all four cases: the repository tracked the
    path and gets its own content back, or it did not and the file goes away,
    and either is true whether or not the agent committed over it. What is left
    behind is a change against ``HEAD``, which the runner's own commit then
    records — so the branch ends up with a tree that never contained our file,
    which is the only property that matters to whoever reviews it.
    """
    if await exists_at(worktree, base, path):
        await git(worktree, "checkout", base, "--", path)
        return

    # Nothing at the base, so the file leaves entirely. `--ignore-unmatch`
    # because the common case is a path git never knew about — the plan, under
    # the runner's own directory — and `rm` refusing that would make this a
    # branch instead of a call.
    await git(worktree, "rm", "--force", "--quiet", "--ignore-unmatch", "--", path)
    with contextlib.suppress(OSError):
        (worktree / path).unlink(missing_ok=True)


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
