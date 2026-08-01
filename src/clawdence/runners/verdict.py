"""What the agent says it did, read from a file it wrote itself.

v1 called this ``.tdd-verdict.json`` and put it *inside* the worktree for a
reason that still holds: the worktree is the one path a sandboxed runner can
write to, so it is the only channel that works for every isolation tier. Nothing
else about the mechanism survives unchanged, because everything else about it
was implicit.

**This file is untrusted input from a process that ran model-generated code.**
Not "input from our own runner" — the agent writes it, repo code runs alongside
it, and on the ``host`` tier nothing is contained at all. Three consequences, and
each is a line of code below:

1. **It is read as a path, checked, then opened** — never followed. A symlink at
   the verdict path pointing at ``~/.aws/credentials`` would otherwise have the
   control plane read that file and put it in a run record.
2. **It is size-capped before it is parsed.** A gigabyte of JSON is a denial of
   service against the process that reads it, and the process that reads it is
   the control plane.
3. **It is validated, not trusted.** ``extra="forbid"`` via ``DomainModel``, so a
   verdict from a newer agent protocol is a parse failure rather than a silently
   half-understood record.

**A missing or unreadable verdict is not an error.** It is an absence, and
``read`` returns ``None``. What that absence *means* is the caller's decision —
and for a contract that requires passing tests it means the same thing as
failing ones, since nothing shows the tests passed either way.

**Nothing here decides an outcome.** ``status`` is what the agent claims;
``RunnerOutcome`` is what the runner observed. Keeping the claim and the
observation in separate types is what lets S13 re-derive evidence from the tree
instead of taking the agent's word for it.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final

from pydantic import Field, StringConstraints, ValidationError

from clawdence.domain import TestEvidence, TokenUsage
from clawdence.domain._base import DomainModel

#: Where the runner asks for the verdict, relative to the worktree root. Under a
#: directory rather than at the root so one exclude entry covers everything the
#: runner installs, and dot-prefixed so it does not show up in a casual listing
#: of somebody's repository.
VERDICT_PATH: Final = ".clawdence/verdict.json"

#: Refused past this. A verdict is a summary; anything larger is either a mistake
#: or an attempt to make the reader do work.
MAX_VERDICT_BYTES: Final = 256 * 1024


#: One free-text note. Capped per item as well as per collection: the caps are
#: what stop a repository from deciding how much of the next prompt it occupies.
Note = Annotated[str, StringConstraints(max_length=2000)]


class VerdictStatus(StrEnum):
    """What the agent claims about the work it did.

    ``BLOCKED`` is the value v1 lacked and needed: an agent that cannot proceed —
    a missing dependency, an instruction it will not follow, a test fixture that
    does not exist — reported failure, which looked identical to code that did
    not work and got retried three times for nothing.
    """

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class RunnerVerdict(DomainModel):
    """The agent's own account of the attempt."""

    status: VerdictStatus

    #: One line for a human. Not parsed, not branched on.
    summary: str | None = Field(default=None, max_length=2000)

    #: Structured test output. Absent when the contract asked for none.
    tests: TestEvidence | None = None

    #: Findings about the codebase, bound for the memory layer (S14). Written by
    #: a process that read repository content, so they are an injection vector
    #: into any later prompt that quotes them — data, never instructions. Bounded
    #: in both directions, because "a note" that is forty thousand words is a
    #: context budget being spent by whoever wrote the repository.
    discovery_notes: tuple[Note, ...] = Field(default=(), max_length=100)

    #: Work deliberately left undone, carried into the next story's plan (§3.9).
    unresolved_stubs: tuple[Note, ...] = Field(default=(), max_length=100)

    #: Reported by CLIs that can. Preferred over the numbers scraped from stdout,
    #: because a structured claim beats a regular expression over prose.
    usage: TokenUsage | None = None


class VerdictError(ValueError):
    """The verdict file exists and could not be believed.

    Distinct from absence: "the agent wrote nothing" and "the agent wrote
    something malformed" are different signals, and only the second one says the
    agent's protocol and ours have diverged.
    """


def path_in(worktree: Path) -> Path:
    return worktree / VERDICT_PATH


def read(worktree: Path) -> RunnerVerdict | None:
    """The verdict, ``None`` if there is none, ``VerdictError`` if it is bad.

    The safety checks run in the order that keeps each one meaningful: existence
    before type, type before size, size before parse. Checking size after reading
    would be a comment rather than a control.
    """
    target = path_in(worktree)

    # `is_symlink` before `exists`, because `exists` follows the link and would
    # answer a question about the target rather than about what is in the
    # worktree. A symlink here is a redirect the agent chose, and the control
    # plane does not follow redirects out of a directory it does not trust.
    if target.is_symlink():
        raise VerdictError(f"{VERDICT_PATH} is a symlink, which the runner will not follow")
    if not target.exists():
        return None
    if not target.is_file():
        raise VerdictError(f"{VERDICT_PATH} is not a regular file")

    size = target.stat().st_size
    if size > MAX_VERDICT_BYTES:
        raise VerdictError(
            f"{VERDICT_PATH} is {size} bytes, over the {MAX_VERDICT_BYTES}-byte limit"
        )

    raw = target.read_bytes()
    try:
        return RunnerVerdict.model_validate_json(raw)
    except ValidationError as exc:
        # The exception's own message quotes the input, and the input came from a
        # process we do not trust; what propagates is the count and the field
        # names, which are ours.
        fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        raise VerdictError(
            f"{VERDICT_PATH} did not validate "
            f"({exc.error_count()} problems: {fields or 'document'})"
        ) from None


def clear(worktree: Path) -> None:
    """Remove any verdict left over from an earlier attempt.

    Called before the agent starts. Without it, a second attempt that crashes
    before writing anything inherits the first attempt's verdict and reports its
    result — which is the retry loop reading its own previous answer and
    concluding it succeeded.
    """
    target = path_in(worktree)
    try:
        target.unlink(missing_ok=True)
    except OSError:
        # A directory at the verdict path, or one we cannot remove. Left alone
        # deliberately: `read` will refuse it and say what it found, which is a
        # better failure than this one raising from a cleanup step.
        return
