"""Reclaiming what a dead control plane left behind, and refusing to reclaim
anything else.

Every test here is really the same test asked from a different angle: *does the
reaper delete something it should not*. That asymmetry is deliberate. A reaper
that misses a stale container costs a gigabyte until the next sweep; one that
removes a live run's worktree costs the work in it, and there is no next sweep
that puts it back.

The container half runs against the fake engine — the real one — so "the reaper
finds it by the label the runner set" is a claim about the label the runner
actually sets, rather than about a constant this file and the runner both
happen to spell the same way.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from clawdence.domain import BuildSystem
from clawdence.runners import Cache, ContainerEngine, Reaper
from tests.harness.engine import FakeEngine
from tests.ports.factories import run
from tests.runners.conftest import PINNED_IMAGE, RequestFactory, container_profile
from tests.runners.test_container import runner_for, working

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def at(**offset: float) -> datetime:
    return NOW - timedelta(**offset)


def frozen() -> datetime:
    return NOW


def age(path: Path, **offset: float) -> Path:
    """Backdate a directory. Faster than waiting seven days."""
    when = at(**offset).timestamp()
    os.utime(path, (when, when))
    return path


def worktree(root: Path, name: str, **offset: float) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "file.txt").write_text("work", encoding="utf-8")
    return age(directory, **offset)


# --------------------------------------------------------------------------- #
# Containers
# --------------------------------------------------------------------------- #


def test_a_container_whose_run_is_gone_is_removed(fake_engine: FakeEngine) -> None:
    """The case the reaper exists for: the control plane was killed between the
    container starting and the ``finally`` that would have removed it."""
    _leftover(fake_engine, name="clawdence-code-dead-agent", run_id="run.dead", when=at(hours=6))

    reclaimed = run(Reaper(engine=fake_engine.engine, clock=frozen).sweep(live=("run.alive",)))

    assert reclaimed.containers == ("clawdence-code-dead-agent",)
    assert "clawdence-code-dead-agent" in fake_engine.removals()


def test_a_run_that_completes_leaves_the_reaper_nothing_to_do(
    fake_engine: FakeEngine, request_for: RequestFactory, tmp_path: Path
) -> None:
    """The reaper's whole subject is what a *crash* leaves behind.

    A run that reaches its ``finally`` removes both of its containers itself, so
    a sweep straight afterwards — with the run no longer live — must still find
    nothing. This is also the test that would catch the reaper's label going out
    of step with the runner's, in the direction that matters: the runner sets
    it, the fake records it, and the engine filters on it.
    """
    runner = runner_for(fake_engine, working(), cache=Cache(root=tmp_path, enabled=False))
    request = request_for("code", profile=container_profile(), run_id="run.finished")

    result = run(runner.dispatch(request))
    reclaimed = run(Reaper(engine=fake_engine.engine, clock=frozen).sweep(live=()))

    assert result.outcome.value in {"succeeded", "tests-failed"}
    assert reclaimed.containers == ()


def test_a_container_belonging_to_a_live_run_is_left_alone(fake_engine: FakeEngine) -> None:
    """However old it looks. A long run is still a run, and its container is the
    process doing the work."""
    _leftover(fake_engine, name="clawdence-code-live-agent", run_id="run.alive", when=at(days=3))

    reclaimed = run(Reaper(engine=fake_engine.engine, clock=frozen).sweep(live=("run.alive",)))

    assert reclaimed.containers == ()
    assert fake_engine.removals() == ()


def test_a_container_younger_than_the_grace_period_is_left_alone(
    fake_engine: FakeEngine,
) -> None:
    """The window the live set cannot cover.

    A run's container exists before anything durable says the run is running, so
    for a moment a perfectly healthy run looks orphaned. Age is the second
    condition that closes it.
    """
    _leftover(fake_engine, name="clawdence-code-new-agent", run_id="run.new", when=at(minutes=2))

    reclaimed = run(Reaper(engine=fake_engine.engine, clock=frozen).sweep(live=()))

    assert reclaimed.containers == ()


def test_a_container_with_no_readable_creation_time_is_left_alone(
    fake_engine: FakeEngine,
) -> None:
    """ "Probably stale" plus a daemon answering oddly is how a reaper removes a
    container a run is still writing to."""
    _leftover(fake_engine, name="clawdence-code-odd-agent", run_id="run.odd", when=None)

    reclaimed = run(Reaper(engine=fake_engine.engine, clock=frozen).sweep(live=()))

    assert reclaimed.containers == ()


def test_a_dry_run_removes_nothing_and_reports_the_same_decisions(
    fake_engine: FakeEngine,
) -> None:
    """One code path, two behaviours — so the preview an operator reads is the
    decision the real sweep will make, not a second implementation of it."""
    _leftover(fake_engine, name="clawdence-code-old-agent", run_id="run.old", when=at(days=1))
    reaper = Reaper(engine=fake_engine.engine, clock=frozen)

    preview = run(reaper.sweep(live=(), dry_run=True))
    assert preview.containers == ("clawdence-code-old-agent",)
    assert fake_engine.removals() == ()

    assert run(reaper.sweep(live=())).containers == preview.containers
    assert "clawdence-code-old-agent" in fake_engine.removals()


def test_nothing_of_ours_means_nothing_removed(fake_engine: FakeEngine) -> None:
    """The engine filters on our label, so somebody else's containers are not
    merely skipped — they are never listed."""
    assert not run(Reaper(engine=fake_engine.engine, clock=frozen).sweep(live=()))


# --------------------------------------------------------------------------- #
# Worktrees
# --------------------------------------------------------------------------- #


def test_a_stale_worktree_is_reclaimed_and_a_live_one_is_not(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    stale = worktree(work_root, "run.dead", days=30)
    live = worktree(work_root, "run.alive", days=30)

    reclaimed = run(Reaper(work_root=work_root, clock=frozen).sweep(live=("run.alive",)))

    assert reclaimed.worktrees == (stale,)
    assert not stale.exists()
    assert live.exists()


def test_a_recent_worktree_survives_even_with_no_run_claiming_it(tmp_path: Path) -> None:
    """A stale worktree can hold committed work nobody collected, because the
    process that was going to collect it is the process that died. So the
    retention is long, and "no live run" alone is not enough."""
    work_root = tmp_path / "work"
    recent = worktree(work_root, "run.yesterday", hours=20)

    reclaimed = run(Reaper(work_root=work_root, clock=frozen).sweep(live=()))

    assert reclaimed.worktrees == ()
    assert recent.exists()


def test_a_symlink_under_the_work_root_is_unlinked_and_never_followed(
    tmp_path: Path,
) -> None:
    """The one way an ``rmtree`` here becomes a catastrophe.

    Nothing this system creates puts a symlink under the work root, so one that
    is there points somewhere unknown — and the only safe thing to do with a
    link to somewhere unknown is remove the link.
    """
    work_root = tmp_path / "work"
    work_root.mkdir()
    precious = tmp_path / "home"
    precious.mkdir()
    (precious / "taxes.txt").write_text("do not delete", encoding="utf-8")
    link = work_root / "run.symlink"
    link.symlink_to(precious)

    reclaimed = run(Reaper(work_root=work_root, clock=frozen).sweep(live=()))

    assert reclaimed.worktrees == (link,)
    assert not link.exists()
    assert (precious / "taxes.txt").is_file()


def test_no_work_root_means_no_worktree_is_ever_touched(tmp_path: Path) -> None:
    """Absent by default, because a reaper that guessed at ``/clawdence/work``
    on a machine keeping its worktrees elsewhere would delete whatever *was* at
    that path."""
    worktree(tmp_path / "work", "run.dead", days=30)

    assert run(Reaper(clock=frozen).sweep(live=())).worktrees == ()


def test_a_work_root_that_does_not_exist_is_not_a_failure(tmp_path: Path) -> None:
    """A deployment that has not run anything yet has nothing to sweep."""
    assert not run(Reaper(work_root=tmp_path / "never", clock=frozen).sweep(live=()))


def test_a_worktree_that_cannot_be_removed_is_reported_rather_than_raised(
    tmp_path: Path,
) -> None:
    """One undeletable directory must not stop the sweep, and it must not be
    silent either — a reaper that reports success while reclaiming nothing is
    how a disk fills with a cleanup job scheduled against it."""
    work_root = tmp_path / "work"
    stuck = worktree(work_root, "run.stuck", days=30)
    other = worktree(work_root, "run.other", days=30)
    work_root.chmod(0o500)
    try:
        reclaimed = run(Reaper(work_root=work_root, clock=frozen).sweep(live=()))
    finally:
        work_root.chmod(0o700)

    assert reclaimed.failed == (other, stuck) or reclaimed.failed == (stuck, other)
    assert stuck.exists()


# --------------------------------------------------------------------------- #
# Caches
# --------------------------------------------------------------------------- #


def test_a_cache_nobody_has_used_for_a_month_is_reclaimed(tmp_path: Path) -> None:
    """The safe case, and it gets the longest retention: deleting one costs a
    cold install and nothing else."""
    cache = Cache(root=tmp_path / "deps")
    plan = cache.plan(container_profile(build_system=BuildSystem.NPM))
    assert plan is not None
    plan.prepare()
    age(plan.directory, days=45)

    reclaimed = run(Reaper(cache=cache, clock=frozen).sweep(live=()))

    assert reclaimed.caches == (plan.directory,)
    assert not plan.directory.exists()


def test_a_cache_a_running_attempt_just_touched_is_left_alone(tmp_path: Path) -> None:
    """Caches are not per run, so the live set cannot protect one. What does is
    the grace period plus ``CachePlan.prepare`` touching the directory at every
    dispatch — a cache belonging to a running attempt is minutes old."""
    cache = Cache(root=tmp_path / "deps")
    plan = cache.plan(container_profile(build_system=BuildSystem.UV))
    assert plan is not None
    plan.prepare()

    reclaimed = run(Reaper(cache=cache, clock=frozen).sweep(live=()))

    assert reclaimed.caches == ()
    assert plan.directory.is_dir()


def test_the_grace_period_is_a_floor_under_every_retention(tmp_path: Path) -> None:
    """``--older-than 0`` must not quietly mean "one hour", and it must not mean
    "delete the cache of a run that started ten seconds ago" either. The floor
    is what makes both true."""
    cache = Cache(root=tmp_path / "deps")
    plan = cache.plan(container_profile(build_system=BuildSystem.UV))
    assert plan is not None
    plan.prepare()
    age(plan.directory, minutes=30)

    reaper = Reaper(
        cache=cache,
        cache_retention=timedelta(0),
        grace=timedelta(hours=1),
        clock=frozen,
    )

    assert run(reaper.sweep(live=())).caches == ()


def test_a_file_sitting_in_the_cache_root_is_not_mistaken_for_a_cache(
    tmp_path: Path,
) -> None:
    """Only directories are reclaimed. A stray file is somebody else's, and one
    level of listing is the whole of what this looks at."""
    root = tmp_path / "deps"
    root.mkdir()
    stray = root / "README"
    stray.write_text("notes", encoding="utf-8")
    age(stray, days=90)

    reclaimed = run(Reaper(cache=Cache(root=root), clock=frozen).sweep(live=()))

    assert reclaimed.caches == ()
    assert stray.is_file()


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def test_an_empty_sweep_is_falsey_and_counts_nothing(tmp_path: Path) -> None:
    reclaimed = run(Reaper(work_root=tmp_path / "work", clock=frozen).sweep(live=()))

    assert not reclaimed
    assert reclaimed.total == 0


def test_the_report_counts_every_source(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    worktree(work_root, "run.a", days=30)
    worktree(work_root, "run.b", days=30)
    cache = Cache(root=tmp_path / "deps")
    plan = cache.plan(container_profile(build_system=BuildSystem.GO))
    assert plan is not None
    plan.prepare()
    age(plan.directory, days=90)

    reclaimed = run(Reaper(work_root=work_root, cache=cache, clock=frozen).sweep(live=()))

    assert reclaimed.total == 3
    assert bool(reclaimed) is True


def test_the_default_engine_is_the_real_client_name() -> None:
    """Guarding the one field that would make a sweep silently do nothing: a
    reaper pointed at a binary that is not there lists no containers and reports
    a clean machine."""
    assert Reaper().engine == ContainerEngine()
    assert PINNED_IMAGE.startswith("registry.invalid/")


def _leftover(fake_engine: FakeEngine, *, name: str, run_id: str, when: datetime | None) -> None:
    """A container the daemon still has and no run removed.

    Written directly into the fake's state rather than produced by a run,
    because a run that completes always removes its own containers — the
    leftover this reclaims only exists when the process that would have removed
    it was killed, which a test cannot arrange from inside that process.
    """
    root = fake_engine.root
    root.mkdir(parents=True, exist_ok=True)
    state = {
        "ExitCode": 0,
        "OOMKilled": False,
        "Error": "",
        # ``None`` here stands for a daemon whose answer this cannot read —
        # a format that drifted, a truncated response. Not a missing field: a
        # real daemon always has one, and the interesting case is the one where
        # it is there and means nothing.
        "Created": "unreadable" if when is None else when.isoformat().replace("+00:00", "Z"),
    }
    (root / f"state-{name}.json").write_text(_json(state), encoding="utf-8")
    with (root / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(
            _json(
                [
                    "run",
                    "--name",
                    name,
                    "--label",
                    f"dev.clawdence/run-id={run_id}",
                    "image",
                    "true",
                ]
            )
            + "\n"
        )


def _json(value: object) -> str:
    import json

    return json.dumps(value)
