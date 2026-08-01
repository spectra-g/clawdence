"""Reset: everything goes, or nothing does, and it says which.

Two questions are being asked over and over here, and they are the two v1 got
wrong in ``reset-pipeline.sh``:

*Does anything survive that points at something that did not?* That is the
``sessions.json`` bug — the file cleared, the session ids kept, and messages
silently dropped afterwards. In v2 the same shape is an ``acknowledged``
request whose run has been deleted.

*Does it stop when stopping is the right answer?* The reaper's protections are
switched off here on purpose, so the refusal is the only thing left between this
command and a run in flight.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from clawdence.devloop import Reset, ResetRefused, reset
from clawdence.domain import EventKind, RunStatus
from clawdence.engine import StubHandler
from clawdence.runners import Cache, Reaper
from clawdence.store import ArrivalState, Inbox, Intake, StateStore
from tests.devloop.factories import RUN_ID, go
from tests.engine.factories import script, workflow
from tests.harness.engine import FakeEngine
from tests.ports.factories import run as await_
from tests.ports.factories import work_item
from tests.store.factories import at, make_run

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def fake_engine(tmp_path: Path) -> Iterator[FakeEngine]:
    """The runner suite's engine fake, which obeys and records."""
    yield FakeEngine(root=tmp_path / "engine")


def clear(store: StateStore, **kwargs: Any) -> Reset:
    return await_(reset(store, at=NOW, **kwargs))


def rows(store: StateStore, table: str) -> int:
    count: int = store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    return count


def submitted(store: StateStore, ref: str = "REQ-1") -> str:
    """One request in the intake, through the real verb."""
    item = work_item(f"wi.{ref.lower()}", external_id=ref)
    Intake(store).submit(item, at=NOW)
    return item.id


class TestTheDatabase:
    def test_every_table_is_emptied(self, state: StateStore) -> None:
        go(state, workflow(script("a"), script("b")), StubHandler())
        submitted(state)
        Inbox(state).send(RUN_ID, body="look at the tests", sender="a-person", at=at(1))

        clear(state)

        for table in ("runs", "steps", "audit", "intake", "intake_turns", "steering"):
            assert rows(state, table) == 0, table

    def test_the_report_counts_what_it_removed(self, state: StateStore) -> None:
        go(state, workflow(script("a")), StubHandler())
        before = {name: rows(state, name) for name in ("runs", "steps", "audit")}

        result = clear(state)

        assert {name: result.rows[name] for name in before} == before

    def test_a_dry_run_removes_nothing_and_reports_the_same(self, state: StateStore) -> None:
        """The property the reaper keeps for the same reason: what you are
        shown and what happens are computed by one code path."""
        go(state, workflow(script("a")), StubHandler())

        planned = clear(state, dry_run=True)
        assert rows(state, "steps") > 0

        done = clear(state)
        assert planned.rows == done.rows
        assert planned.dry_run and not done.dry_run

    def test_an_already_clean_database_is_not_an_error(self, state: StateStore) -> None:
        result = clear(state)

        assert not result
        assert result.records == 0

    def test_the_sequence_counters_restart(self, state: StateStore) -> None:
        """AUTOINCREMENT keeps its high-water mark after the last row is gone.

        Without this, the first event of a freshly reset environment comes back
        as ``seq`` 4001, which is a database that says it is not clean.
        """
        for index in range(3):
            state.audit.record(EventKind.RUN_STARTED, at=at(index), run_id=f"run.{index}")

        clear(state)
        state.audit.record(EventKind.RUN_STARTED, at=at(9), run_id="run.fresh")

        assert state.connection.execute("SELECT seq FROM audit").fetchone()[0] == 1

    def test_a_kept_inbox_keeps_its_counter(self, state: StateStore) -> None:
        """Resetting a counter for a table that still has rows is how a reset
        hands out an id something is already using."""
        submitted(state, "REQ-1")
        first = state.connection.execute("SELECT seq FROM intake").fetchone()[0]

        clear(state, keep_inbox=True)
        submitted(state, "REQ-2")

        seqs = [seq for (seq,) in state.connection.execute("SELECT seq FROM intake ORDER BY seq")]
        assert seqs == [first, first + 1]


class TestTheInbox:
    def test_the_inbox_goes_by_default(self, state: StateStore) -> None:
        submitted(state)

        clear(state)

        assert Intake(state).list() == ()

    def test_a_kept_inbox_survives(self, state: StateStore) -> None:
        submitted(state)

        clear(state, keep_inbox=True)

        assert len(Intake(state).list()) == 1

    def test_a_kept_acknowledged_request_goes_back_in_the_queue(self, state: StateStore) -> None:
        """v1's ``sessions.json`` bug, in the one place v2 can still have it.

        ``acknowledged`` means "the pipeline has this". After a reset there is
        no pipeline and no run, so a row left in that state is collected by
        nothing and re-queued by nothing — it simply never happens again, and
        nothing reports that it did not.
        """
        item_id = submitted(state)
        intake = Intake(state)
        intake.acknowledge(item_id, at=NOW)

        result = clear(state, keep_inbox=True)

        (admission,) = intake.list()
        assert admission.state is ArrivalState.PENDING
        assert admission.acknowledged_revision is None
        assert result.requeued == 1

    def test_a_dry_run_says_how_many_would_go_back(self, state: StateStore) -> None:
        intake = Intake(state)
        intake.acknowledge(submitted(state), at=NOW)

        result = clear(state, keep_inbox=True, dry_run=True)

        assert result.requeued == 1
        assert intake.list()[0].state is ArrivalState.ACKNOWLEDGED

    def test_a_kept_inbox_still_loses_the_runs(self, state: StateStore) -> None:
        go(state, workflow(script("a")), StubHandler())
        submitted(state)

        clear(state, keep_inbox=True)

        assert rows(state, "runs") == 0
        assert rows(state, "intake") == 1


class TestTheRefusal:
    def test_a_live_run_refuses(self, state: StateStore) -> None:
        state.create_run(make_run("run.live", status=RunStatus.RUNNING))

        with pytest.raises(ResetRefused, match=r"run\.live"):
            clear(state)

        assert rows(state, "runs") == 1

    def test_the_refusal_names_the_ways_out(self, state: StateStore) -> None:
        state.create_run(make_run("run.live", status=RunStatus.RUNNING))

        with pytest.raises(ResetRefused) as raised:
            clear(state)

        assert "runs recover" in str(raised.value)
        assert "--force" in str(raised.value)

    def test_force_resets_and_names_what_it_abandoned(self, state: StateStore) -> None:
        state.create_run(make_run("run.live", status=RunStatus.RUNNING))

        result = clear(state, force=True)

        assert result.abandoned == ("run.live",)
        assert rows(state, "runs") == 0

    def test_a_dry_run_never_refuses(self, state: StateStore) -> None:
        """Refusing to *describe* something is not a protection."""
        state.create_run(make_run("run.live", status=RunStatus.RUNNING))

        result = clear(state, dry_run=True)

        assert result.abandoned == ("run.live",)
        assert rows(state, "runs") == 1

    def test_a_finished_run_is_not_live(self, state: StateStore) -> None:
        state.create_run(make_run("run.done", status=RunStatus.DONE))

        assert clear(state).records == 1


class TestTheMachine:
    def test_a_worktree_too_new_for_the_reaper_still_goes(
        self, state: StateStore, workspace: Path
    ) -> None:
        """The whole difference between ``reap`` and ``reset``.

        A directory created a moment ago is inside the reaper's grace period,
        so the reaper leaves it — correctly, because a run may be writing to it.
        Reset is the operator saying there is no such run.
        """
        fresh = workspace / "run.recent"
        fresh.mkdir()
        (fresh / "file.txt").write_text("work", encoding="utf-8")

        assert await_(Reaper(work_root=workspace).sweep(())).worktrees == ()

        result = clear(state, work_root=workspace)

        assert result.debris.worktrees == (fresh,)
        assert not fresh.exists()

    def test_a_live_runs_worktree_goes_too_under_force(
        self, state: StateStore, workspace: Path
    ) -> None:
        """No live set at all, which is what ``--force`` was agreeing to."""
        state.create_run(make_run("run.live", status=RunStatus.RUNNING))
        owned = workspace / "run.live"
        owned.mkdir()

        clear(state, work_root=workspace, force=True)

        assert not owned.exists()

    def test_caches_are_left_alone_unless_asked_for(
        self, state: StateStore, tmp_path: Path
    ) -> None:
        """Kept by default: a cache holds no state and points at no run, so
        clearing it costs a slow install and makes nothing cleaner."""
        cache_root = tmp_path / "caches"
        cached = cache_root / "repo-uv-abc"
        cached.mkdir(parents=True)

        assert clear(state).debris.caches == ()
        assert cached.exists()

        assert clear(state, cache=Cache(root=cache_root)).debris.caches == (cached,)
        assert not cached.exists()

    def test_containers_go_regardless_of_age_or_ownership(
        self, state: StateStore, fake_engine: FakeEngine
    ) -> None:
        state.create_run(make_run("run.live", status=RunStatus.RUNNING))
        _leftover(fake_engine, name="clawdence-code-live", run_id="run.live", when=NOW)

        result = clear(state, engine=fake_engine.engine, force=True)

        assert result.debris.containers == ("clawdence-code-live",)
        assert "clawdence-code-live" in fake_engine.removals()

    def test_no_work_root_sweeps_no_worktrees(self, state: StateStore, workspace: Path) -> None:
        """There is no safe guess at where somebody keeps them."""
        (workspace / "run.old").mkdir()

        assert clear(state).debris.worktrees == ()
        assert (workspace / "run.old").exists()


def _leftover(fake_engine: FakeEngine, *, name: str, run_id: str, when: datetime) -> None:
    """A container the daemon still has. Written into the fake's state directly,
    for the reason ``tests/runners/test_reaper`` gives: a run that completes
    always removes its own."""
    root = fake_engine.root
    root.mkdir(parents=True, exist_ok=True)
    state = {
        "ExitCode": 0,
        "OOMKilled": False,
        "Error": "",
        "Created": (when - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    }
    (root / f"state-{name}.json").write_text(json.dumps(state), encoding="utf-8")
    with (root / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(
            json.dumps(
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
