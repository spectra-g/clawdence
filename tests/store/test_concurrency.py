"""Two writers, one run.

S4's acceptance criterion says this explicitly: *two concurrent runs writing to
the same aggregate produce a correct final state under a forced interleaving
test* — not "it worked once". So the interleaving is forced rather than raced.
``conflict_window`` runs another writer's whole update inside the gap between
this writer's read and its conditional write, which is the window optimistic
concurrency exists to survive and is otherwise unreachable from a test.

Two connections to one file, because that is what two processes would have.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawdence.domain import Run, RunStatus, StepStatus
from clawdence.store import ConcurrentUpdateError, DuplicateAttemptError, StateStore
from tests.conftest import StoreFactory
from tests.store.factories import RUN_ID, at, make_run, make_step, running_step


def test_a_write_inside_the_conflict_window_is_not_lost(
    stores: StoreFactory, tmp_path: Path
) -> None:
    """The lost-update test: both facts survive, whoever wrote them.

    Without the version check the second writer would overwrite the first's
    ``repo_id`` with the value it read before the first wrote — and the run
    would end up with a status but no repo, which is the shape of corruption
    nobody notices until much later.
    """
    path = tmp_path / "state.db"
    other: StateStore = stores(path)
    fired = False

    def interleave() -> None:
        # Runs once, inside the gap between the read and the write below.
        nonlocal fired
        if fired:
            return
        fired = True
        other.update_run(RUN_ID, lambda run: run.model_copy(update={"repo_id": "repo.written"}))

    writer: StateStore = stores(path, conflict_window=interleave)
    writer.create_run(make_run())

    writer.update_run(RUN_ID, lambda run: run.model_copy(update={"status": RunStatus.DONE}))

    final = writer.require_run(RUN_ID)
    assert final.status is RunStatus.DONE
    assert final.repo_id == "repo.written"


def test_the_loser_retries_against_what_it_now_sees(stores: StoreFactory, tmp_path: Path) -> None:
    """``mutate`` is re-applied to current state, not to the stale read."""
    path = tmp_path / "state.db"
    other: StateStore = stores(path)
    seen: list[str | None] = []
    fired = False

    def interleave() -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        other.update_run(RUN_ID, lambda run: run.model_copy(update={"repo_id": "repo.first"}))

    writer: StateStore = stores(path, conflict_window=interleave)
    writer.create_run(make_run())

    def mutate(run: Run) -> Run:
        seen.append(run.repo_id)
        return run.model_copy(update={"status": RunStatus.HALTED})

    writer.update_run(RUN_ID, mutate)

    assert seen == [None, "repo.first"], "the retry must see the other writer's value"


def test_unrelenting_contention_is_reported_rather_than_absorbed(
    stores: StoreFactory, tmp_path: Path
) -> None:
    path = tmp_path / "state.db"
    other: StateStore = stores(path)
    bumps = 0

    def interleave() -> None:
        nonlocal bumps
        bumps += 1
        other.update_run(RUN_ID, lambda run: run.model_copy(update={"repo_id": f"repo.{bumps}"}))

    writer: StateStore = stores(path, conflict_window=interleave)
    writer.create_run(make_run())

    with pytest.raises(ConcurrentUpdateError, match="modified by another writer"):
        writer.update_run(
            RUN_ID, lambda run: run.model_copy(update={"status": RunStatus.DONE}), retries=3
        )
    assert bumps == 3


def test_two_writers_cannot_both_start_the_same_attempt(
    stores: StoreFactory, tmp_path: Path
) -> None:
    """Idempotency is the database's job, not each caller's."""
    path = tmp_path / "state.db"
    first: StateStore = stores(path)
    second: StateStore = stores(path)
    first.create_run(make_run())

    first.start_step(running_step("build"))
    with pytest.raises(DuplicateAttemptError):
        second.start_step(running_step("build"))

    assert len(first.steps_for(RUN_ID)) == 1


def test_a_second_connection_sees_committed_work(stores: StoreFactory, tmp_path: Path) -> None:
    """WAL: the watchdog reads a live run without blocking the process running it."""
    path = tmp_path / "state.db"
    writer: StateStore = stores(path)
    reader: StateStore = stores(path)

    writer.create_run(make_run())
    writer.start_step(running_step("build", started=0))
    assert [step.stage_id for step in reader.running_steps()] == ["build"]

    writer.finish_step(make_step("build", status=StepStatus.SUCCEEDED))
    assert reader.running_steps() == ()
    assert reader.steps_for(RUN_ID)[0].finished_at == at(1)
