"""Collecting what a dead control plane left behind.

Every run gives back what it allocated: ``AgentRunner._teardown`` removes the
containers, and it runs in a ``finally``, so it runs on a timeout, on a budget
kill and on a cancel. What it cannot run on is the control plane being killed —
and that is the case this exists for. §3.7 asks for it by name, in one line
("ephemeral containers plus per-language images plus dependency caches will fill
a disk; needs a reaper with a retention policy"), and the line is doing more
work than it looks: three kinds of debris, three different reasons they are
safe to remove, and one rule they all share.

**The shared rule is that a live run's things are never touched.** The reaper is
given the set of run ids the state store says are running, and anything
belonging to one of them is left alone regardless of how old it looks. That
check is not the only one, because there is a window where a run has started and
its container exists before anything durable says so — so age is required as
well. Together they mean the reaper reclaims things that are both unclaimed and
not recent, and a wrong answer to either question is not enough on its own.

**Containers** are the easy case: labelled ``dev.clawdence/run-id`` at creation,
so "ours" is a filter the engine evaluates rather than a name we parse, and a
container is worth nothing once its run is over — its exit state has already
been read by ``_observe``, before the run that created it removed it.

**Worktrees** are the case worth being careful about. A stale directory under
``/clawdence/work`` may hold work an agent committed that nobody collected,
because collecting it is what the crashed process was going to do. So they get a
much longer retention than containers, they are only ever removed when their run
id is not live, and a symlink there is unlinked rather than walked — a symlink
under the work root pointing at somebody's home directory is the one way a
``rmtree`` here becomes a catastrophe rather than a cleanup.

**Caches** are the safe case and get the longest retention of the three, because
deleting one costs a slow install and nothing else. Their mtime is refreshed at
every dispatch (``CachePlan.prepare``), so "untouched for a month" means unused
rather than merely complete.

**Images are deliberately not swept.** §3.7 mentions orphaned image layers, and
this system builds no images: it runs the digest an operator pinned. The layers
on the host therefore belong to that operator's registry pulls, and a tool that
ran ``image prune`` on somebody's machine because it happened to be nearby would
be deleting things it never created. ``docker image prune`` is one command and
it is theirs to type.

**No byte accounting.** ``Reclaimed`` counts things, not space. Totalling a
Maven cache means walking a few hundred thousand files, which costs more than
the reclaim it is reporting on, and the number would be stale by the time it was
printed.
"""

from __future__ import annotations

import shutil
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from clawdence.ports._common import Clock, utc_now
from clawdence.runners.cache import Cache
from clawdence.runners.container import RUN_ID_LABEL
from clawdence.runners.engine import ContainerEngine

#: How recent is too recent to reclaim, whatever the run set says. Covers the
#: window between a container existing and the store recording the run that owns
#: it, and it is the floor under every retention below.
DEFAULT_GRACE: Final = timedelta(hours=1)

#: Worktrees. Long, because a stale one can hold committed work nobody collected
#: — the process that was going to collect it is the process that died.
DEFAULT_WORKTREE_RETENTION: Final = timedelta(days=7)

#: Caches. Longest, because losing one costs a cold install and nothing else.
DEFAULT_CACHE_RETENTION: Final = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Reclaimed:
    """What a sweep removed, or would have removed under ``dry_run``."""

    containers: tuple[str, ...] = ()
    worktrees: tuple[Path, ...] = ()
    caches: tuple[Path, ...] = ()

    #: Paths a sweep decided to reclaim and then could not — a permission error,
    #: a mount in the way. Reported rather than raised: one undeletable
    #: directory must not stop the rest of the sweep, and it must not be silent
    #: either, because a reaper that reports success while reclaiming nothing is
    #: how a disk fills with a cleanup job scheduled against it.
    failed: tuple[Path, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.containers or self.worktrees or self.caches)

    @property
    def total(self) -> int:
        return len(self.containers) + len(self.worktrees) + len(self.caches)


@dataclass(frozen=True, slots=True)
class Reaper:
    """Reclaims what runs did not, on a retention policy.

    Every source is optional and absent by default. A deployment that has no
    work root configured has none to sweep, and a reaper that guessed at
    ``/clawdence/work`` on a machine that keeps its worktrees elsewhere would be
    deleting whatever *was* at that path.
    """

    engine: ContainerEngine = field(default_factory=ContainerEngine)

    #: Where worktrees live (§3.3's ``/clawdence/work``). ``None`` sweeps none.
    work_root: Path | None = None

    #: ``None`` sweeps no caches. ``Cache.default()`` is the usual value.
    cache: Cache | None = None

    grace: timedelta = DEFAULT_GRACE
    worktree_retention: timedelta = DEFAULT_WORKTREE_RETENTION
    cache_retention: timedelta = DEFAULT_CACHE_RETENTION
    clock: Clock = utc_now

    async def sweep(self, live: Collection[str] = (), *, dry_run: bool = False) -> Reclaimed:
        """Reclaim everything unclaimed and old enough. ``live`` is protected.

        ``dry_run`` decides *nothing* differently — it removes nothing, and the
        report is the same report. That is the property worth having: an
        operator who runs the dry sweep and then the real one is looking at the
        same decisions, rather than at a preview computed by a second code path.
        """
        now = self.clock()
        running = set(live)
        failed: list[Path] = []
        return Reclaimed(
            containers=await self._containers(now, running, dry_run=dry_run),
            worktrees=self._directories(
                self.work_root,
                now=now,
                retention=self.worktree_retention,
                live=running,
                dry_run=dry_run,
                failed=failed,
            ),
            caches=self._directories(
                None if self.cache is None else self.cache.root,
                now=now,
                retention=self.cache_retention,
                # Caches are not per run and so cannot be matched against the
                # live set at all. What protects one in use is ``grace``: every
                # dispatch touches the directory, so a cache belonging to a
                # running attempt is minutes old.
                live=set(),
                dry_run=dry_run,
                failed=failed,
            ),
            failed=tuple(failed),
        )

    # ---------------------------------------------------------- the daemon's

    async def _containers(self, now: datetime, live: set[str], *, dry_run: bool) -> tuple[str, ...]:
        reclaimed: list[str] = []
        for name, run_id in await self.engine.labelled(RUN_ID_LABEL):
            if run_id in live:
                continue
            created = await self.engine.created(name)
            # An unreadable creation time is left alone. The container is ours
            # and its run is gone, so it probably should go — but "probably"
            # plus a daemon that is answering oddly is how a reaper removes a
            # container a run is still writing to.
            if created is None or now - created < self.grace:
                continue
            if not dry_run:
                await self.engine.remove(name)
            reclaimed.append(name)
        return tuple(reclaimed)

    # ------------------------------------------------------------ the disk's

    def _directories(
        self,
        root: Path | None,
        *,
        now: datetime,
        retention: timedelta,
        live: set[str],
        dry_run: bool,
        failed: list[Path],
    ) -> tuple[Path, ...]:
        """One level under ``root``, by age, skipping anything live.

        One level and not a walk: what is being reclaimed is a whole worktree or
        a whole cache, and descending would offer the chance to delete half of
        one — which is worse than either deleting it or leaving it.
        """
        reclaimed: list[Path] = []
        cutoff = max(retention, self.grace)
        for child in _children(root):
            if child.name in live:
                continue
            # Symlinks are never followed and never counted. One under the work
            # root is not something this system creates, and the only thing a
            # sweep can safely do with a link to somewhere unknown is remove the
            # link. It is also the one way an ``rmtree`` here becomes a
            # catastrophe rather than a cleanup.
            link = child.is_symlink()
            if not link and (not child.is_dir() or now - _modified(child) < cutoff):
                continue
            if dry_run:
                reclaimed.append(child)
                continue
            try:
                child.unlink() if link else shutil.rmtree(child)
            except OSError:
                failed.append(child)
                continue
            reclaimed.append(child)
        return tuple(reclaimed)


def _children(root: Path | None) -> Iterator[Path]:
    if root is None:
        return
    try:
        yield from sorted(root.iterdir())
    except OSError:
        # A root that does not exist yet is a deployment that has not run
        # anything, which is not a failure to sweep.
        return


def _modified(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:  # pragma: no cover - it was listed a moment ago
        return datetime.now().astimezone()
