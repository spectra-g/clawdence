"""What the runner put in the worktree, so it can tell its own mess from the agent's.

S6 excluded everything the runner installs — the conventions file, the plan, the
verdict — through ``$GIT_DIR/info/exclude``, which is the right mechanism and
better than editing a tracked ``.gitignore``. It has one hole, and §3.9 names it:
**``info/exclude`` has no effect on a path the repository already tracks.** A
repository that keeps its own ``AGENTS.md`` — which is exactly the kind of
repository this system is pointed at — gets that file written over by the
install, sees a modification to a tracked file, and has it swept into the branch
by ``git add --all``.

The repair is not "do not install over it". It is to **record the bytes**, and at
collection time put a path back only where what is there is still exactly what we
wrote. An agent that deliberately edited the conventions file has its edit
survive; an agent that never touched it has our copy reverted and never sees it
in a pull request. Nothing here needs to know which of those happened, which is
the point — the file's own contents answer it.

That same question is what makes §3.7a's three-way empty-diff split decidable. A
dirty tree means the agent worked and never committed **only** if the dirt is the
agent's; our own plan and verdict are sitting in that tree on every single run,
and a naive ``is_dirty`` probe therefore reports every run as a dropped commit.

Two rules, and they are different on purpose:

**A directory the runner owns is ours whatever is in it.** ``.clawdence/`` was
created by this run, for this run. The agent writes its verdict there and its CLI
writes a config file there, and neither is repository content — so the contents
are not compared, the prefix is enough.

**A file installed outside that directory is ours only while it is unchanged.**
There the byte comparison is the whole control, because the path belongs to the
repository and we are the guest in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Final

#: Directory the runner owns inside the worktree. One entry in git's exclude file
#: covers everything installed into it, so nothing the runner writes can reach a
#: pull request — and one prefix test covers everything in it here.
WORK_DIR: Final = ".clawdence"

#: Where the plan is written when the CLI reads it from a file.
PLAN_PATH: Final = f"{WORK_DIR}/plan.md"

#: A writable ``HOME`` for the agent, inside the directory already hidden from
#: git. The container tier needs one because the image's ``HOME`` may not be
#: writable by the uid we run as, and a CLI whose first act is writing a config
#: file fails on a permission error that reads like a bug in this system.
HOME_DIR: Final = f"{WORK_DIR}/home"

#: Past this, a file at an installed path is not the one we installed and the
#: digest is not computed. A guard rather than an optimisation: the alternative
#: is reading whatever an agent decided to write there into the control plane's
#: memory in order to find out it is not ours.
MAX_COMPARE_BYTES: Final = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Record:
    """One file this run wrote, and what it wrote."""

    size: int
    digest: str


@dataclass(slots=True)
class Installed:
    """The runner's own footprint in one worktree.

    Mutable while ``_prepare`` fills it and read-only afterwards by convention
    rather than by type: it is built and consumed inside a single dispatch, and
    a frozen copy step would be ceremony around a dictionary that never leaves.
    """

    worktree: Path
    records: dict[str, Record] = field(default_factory=dict)

    def write(self, relative: str, contents: str) -> None:
        """Write a file into the worktree, remembering the bytes."""
        self._put(relative, contents.encode("utf-8"))

    def copy(self, source: Path, relative: str) -> None:
        """Copy a control-plane file in, remembering what landed.

        Read and re-written rather than ``copyfile``d because the recorded
        digest has to be of what is *at the destination*: a copy that is
        interrupted, or a source that changes between the copy and the hash,
        would leave a record that never matches and a file that never gets
        reverted.
        """
        self._put(relative, source.read_bytes())

    def paths(self) -> tuple[str, ...]:
        """Installed paths, in a stable order."""
        return tuple(sorted(self.records))

    def owns(self, path: str) -> bool:
        """Whether ``path`` is the runner's rather than the repository's.

        Two rules, per the module docstring: anything under the directory this
        run created is ours outright, and anything else is ours only while it
        still holds exactly the bytes we put there.
        """
        normalised = path.strip("/")
        if normalised == WORK_DIR or normalised.startswith(f"{WORK_DIR}/"):
            return True

        record = self.records.get(normalised)
        if record is None:
            return False

        target = self.worktree / normalised
        try:
            # `is_symlink` first, and not followed: an agent that replaced our
            # conventions file with a link to somewhere outside the worktree
            # would otherwise have the control plane read that file to decide
            # whether it was its own.
            if target.is_symlink() or not target.is_file():
                return False
            if target.stat().st_size != record.size or record.size > MAX_COMPARE_BYTES:
                return False
            return _digest(target.read_bytes()) == record.digest
        except OSError:
            # Unreadable is not ours. The agent's tree is allowed to be strange,
            # and a cleanup step is the wrong place to raise from.
            return False

    def _put(self, relative: str, data: bytes) -> None:
        target = self.worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.records[relative.strip("/")] = Record(size=len(data), digest=_digest(data))


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()
