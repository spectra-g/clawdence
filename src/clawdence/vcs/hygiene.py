"""What a branch is not allowed to contain, checked once, where it matters.

v1 defended against the ``node_modules`` cascade with four layers — a symlink
farm, an ignore file, a path filter and a cleanup pass — and the plan's
instruction for this step is explicit that they should not all be ported
reflexively, because containerising the runner removed most of what they were
compensating for. What it did not remove is the failure they were aimed at: a
pull request nobody can review, because the diff has eleven thousand files in
it, and the interesting three are somewhere in the middle.

So there is one layer, and it is at the boundary. Not prevention — an agent can
write whatever it likes into a worktree, and a check that tried to stop it would
be racing a process designed to create files. This runs on the **diff between
the base and what is about to be pushed**, which is the last moment where the
answer is still cheap and the first moment where it is complete.

Four things are refused, and each is refused for a reason that is not
housekeeping:

**Symlinks.** Mode ``120000``. The content of a symlink is a path, so a link
committed into a repository is a path that gets resolved on *somebody else's*
machine — a CI runner, a reviewer's checkout — against whatever is there. v1
created symlinks deliberately, which is how they ended up committed by accident.

**Submodule pointers.** Mode ``160000``. A gitlink is an instruction to fetch a
repository from a URL in ``.gitmodules``, and both halves are content the agent
just wrote. Adding one turns the next clone into a network request to a host
nobody chose.

**Vendored directories.** Package manager output belongs to the machine that
produced it. The list is short and specific rather than a general "looks
generated" heuristic, because a wrong guess here blocks a legitimate change to a
directory whose name happened to match, and the failure is a refusal a human has
to override.

**Files past a size limit.** Not a repository-size policy — that is the forge's
job and it has one. It catches the specific accident that produces an
unreviewable branch: a build artefact, a core dump, a captured log. The limit is
per file, because a hundred small files is a change and one 90 MB file is a
mistake.

Findings are returned, never raised. The caller decides — the same shape as
``probe.findings`` — because "refuse to push" and "push and say so in the body"
are both defensible and it is not this function's decision which one applies.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from clawdence.vcs.git import git

#: Per file. Generous enough that a legitimately large committed asset — a test
#: fixture, an image — is not caught, and small enough that the accidents are.
DEFAULT_MAX_FILE_BYTES: Final = 10 * 1024 * 1024

#: Directories whose contents belong to the machine that built them. Matched as
#: a *path component*, so ``node_modules/x`` and ``packages/a/node_modules/x``
#: both match and ``my_node_modules_notes.md`` does not.
DEFAULT_VENDORED: Final[frozenset[str]] = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".gradle",
        ".terraform",
    }
)

#: Git's file modes, as they appear in ``diff --raw``.
_SYMLINK: Final = "120000"
_GITLINK: Final = "160000"
_ABSENT: Final = "000000"


class Problem(StrEnum):
    SYMLINK = "symlink"
    SUBMODULE = "submodule"
    VENDORED = "vendored"
    OVERSIZED = "oversized"


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason this diff should not be published as it stands."""

    problem: Problem
    path: str
    detail: str

    def describe(self) -> str:
        return f"{self.path}: {self.detail}"


@dataclass(frozen=True, slots=True)
class _Change:
    """One row of ``diff --raw``, reduced to what matters here."""

    mode: str
    blob: str
    path: str


async def audit(
    worktree: Path,
    base: str,
    target: str = "HEAD",
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    vendored: Iterable[str] = DEFAULT_VENDORED,
    git_path: str | None = None,
) -> tuple[Finding, ...]:
    """Everything wrong with the tree ``target`` adds to ``base``.

    Only *additions and modifications* are examined. A commit that deletes a
    symlink or empties ``node_modules`` is the fix, and reporting it as the
    problem would make the only available remedy look like a violation.
    """
    changes = _parse(
        await git(worktree, "diff", "--raw", "-z", base, target, strip=False, path=git_path)
    )
    directories = frozenset(vendored)
    findings = [finding for change in changes for finding in _inspect(change, directories)]
    findings.extend(
        await _oversized(worktree, changes, max_file_bytes=max_file_bytes, git_path=git_path)
    )
    return tuple(findings)


def _inspect(change: _Change, vendored: frozenset[str]) -> Iterator[Finding]:
    if change.mode == _SYMLINK:
        yield Finding(
            Problem.SYMLINK,
            change.path,
            "is a symbolic link; its content is a path that resolves on whichever "
            "machine checks it out next",
        )
    if change.mode == _GITLINK:
        yield Finding(
            Problem.SUBMODULE,
            change.path,
            "is a submodule pointer, which makes the next clone fetch a repository "
            "named by a file in this same change",
        )
    directory = next((part for part in Path(change.path).parts if part in vendored), None)
    if directory is not None:
        yield Finding(
            Problem.VENDORED,
            change.path,
            f"is inside {directory!r}, which holds output from a package manager "
            f"rather than source",
        )


async def _oversized(
    worktree: Path,
    changes: Sequence[_Change],
    *,
    max_file_bytes: int,
    git_path: str | None,
) -> list[Finding]:
    """Sizes for every added or modified blob, in one git process.

    ``cat-file --batch-check`` rather than a call per file: a diff with two
    thousand entries is not unusual for a dependency bump, and two thousand
    forks is a measurable part of a run.
    """
    blobs = [change for change in changes if change.mode not in (_SYMLINK, _GITLINK)]
    if not blobs:
        return []
    raw = await git(
        worktree,
        "cat-file",
        "--batch-check=%(objectsize)",
        "--buffer",
        strip=False,
        stdin="".join(f"{change.blob}\n" for change in blobs),
        path=git_path,
    )
    findings: list[Finding] = []
    for change, line in zip(blobs, raw.splitlines(), strict=False):
        # A line that is not a number is git saying "missing" — a promisor
        # repository that has not fetched the blob. Unknown size is not a
        # finding: refusing a push because a lazy fetch has not happened would
        # make partial clone and this check mutually exclusive.
        if not line.strip().isdigit():
            continue
        size = int(line)
        if size > max_file_bytes:
            findings.append(
                Finding(
                    Problem.OVERSIZED,
                    change.path,
                    f"is {size // (1024 * 1024)} MB, over the {max_file_bytes // (1024 * 1024)} MB "
                    f"limit; one file this size is an artefact, not a change",
                )
            )
    return findings


def _parse(raw: str) -> tuple[_Change, ...]:
    """Read ``diff --raw -z``.

    Each record is ``:<src-mode> <dst-mode> <src-sha> <dst-sha> <status>`` and
    then the path, both NUL-terminated; a rename or copy adds a second path. ``-z``
    is not optional here — without it git *quotes* paths with unusual characters,
    and an agent can create a file called anything, so the input to this parser is
    attacker-influenced by construction.
    """
    fields = [field for field in raw.split("\0") if field]
    changes: list[_Change] = []
    index = 0
    while index < len(fields):
        meta = fields[index]
        if not meta.startswith(":"):  # pragma: no cover - git does not emit these
            index += 1
            continue
        parts = meta[1:].split()
        if len(parts) < 5:  # pragma: no cover - nor these
            index += 1
            continue
        _, dst_mode, _, dst_blob, status = parts[:5]
        # A rename or a copy is followed by two paths; the destination is the
        # second, and it is the one that exists after the change.
        renamed = status[:1] in ("R", "C")
        path_index = index + 2 if renamed else index + 1
        if path_index >= len(fields):  # pragma: no cover - truncated output
            break
        if dst_mode != _ABSENT:
            changes.append(_Change(mode=dst_mode, blob=dst_blob, path=fields[path_index]))
        index = path_index + 1
    return tuple(changes)
