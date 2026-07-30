"""Scaffold-level checks: the package imports and the entry point runs.

These exist so CI has something real to fail on from commit 1.

Every test that executes a workflow passes ``--no-state`` or an explicit
``--state``. Neither is optional: ``clawdence run`` records to
``~/.clawdence/state.db`` by default, and a test suite that writes into the
home directory of whoever runs it is a test suite that has a side effect.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clawdence import __version__, cli
from clawdence.agent import AgentHandler, AnthropicModels
from clawdence.cli import main
from clawdence.domain import Run, RunStatus, StepResult, StepStatus, StepType
from clawdence.engine import UnimplementedHandler
from clawdence.ports.ingest import SELF_ID
from clawdence.store import Intake, StateStore


def test_version_is_populated() -> None:
    assert __version__
    assert __version__ != "0.0.0+unknown"


def test_main_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "clawdence" in capsys.readouterr().out


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_schema_export_then_check_agree(tmp_path: Path) -> None:
    assert main(["schema", "export", "--out", str(tmp_path)]) == 0
    assert main(["schema", "check", "--out", str(tmp_path)]) == 0


def test_schema_check_fails_on_a_stale_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-zero exit is the whole point: this runs in CI.

    A drift check that reports the problem and exits zero is a drift check
    nobody finds out about.
    """
    assert main(["schema", "check", "--out", str(tmp_path)]) == 1
    assert "out of date" in capsys.readouterr().out


def test_run_executes_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    """Exit 1, because ``toy.yaml`` induces a failure on purpose.

    The distinction the exit codes carry: 1 means the workflow ran and did not
    succeed, 2 means it never ran at all. A script wrapping ``clawdence run``
    needs to tell those apart.
    """
    assert main(["run", "examples/toy.yaml", "--no-state"]) == 1
    out = capsys.readouterr().out
    assert "classify" in out
    assert "status: halted" in out


def test_run_emits_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "examples/toy.yaml", "--json", "--no-state"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow"]["name"] == "toy"


def test_run_reports_a_bad_workflow_on_stderr_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("name: demo\nversion: 1.0.0\nstages: []\n", encoding="utf-8")
    assert main(["run", str(path), "--no-state"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not match the workflow schema" in captured.err
    assert "Traceback" not in captured.err


def test_run_accepts_a_work_item_id(capsys: pytest.CaptureFixture[str]) -> None:
    main(["run", "examples/toy.yaml", "--json", "--no-state", "--work-item", "wi.chosen"])
    assert json.loads(capsys.readouterr().out)["run"]["work_item_id"] == "wi.chosen"


def test_resume_without_a_state_database_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "examples/toy.yaml", "--no-state", "--resume", "run.x"]) == 2
    assert "--resume needs the state database" in capsys.readouterr().err


def test_resuming_a_run_that_was_never_recorded_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state.db"
    assert main(["run", "examples/toy.yaml", "--state", str(state), "--resume", "run.x"]) == 2
    assert "no run with id" in capsys.readouterr().err


class TestRunsCommands:
    """The operator surface. HQ (S19) is the read surface; this is the one for
    when HQ is the thing that is not working."""

    def state(self, tmp_path: Path) -> Path:
        path = tmp_path / "state.db"
        main(["run", "examples/toy.yaml", "--state", str(path)])
        return path

    def test_list_shows_the_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["runs", "list", "--state", str(self.state(tmp_path))]) == 0
        assert "toy@1.0.0" in capsys.readouterr().out

    def test_list_of_an_empty_store_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["runs", "list", "--state", str(tmp_path / "empty.db")]) == 0
        assert "no runs recorded" in capsys.readouterr().out

    def test_list_filters_by_status(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()
        assert main(["runs", "list", "--state", str(state), "--status", "done"]) == 0
        assert "no runs recorded" in capsys.readouterr().out

    def test_show_lists_every_step(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()
        run_id = _only_run_id(state)

        assert main(["runs", "show", run_id, "--state", str(state)]) == 0
        out = capsys.readouterr().out
        assert "classify" in out
        assert "audit record(s)" in out

    def test_show_of_an_unknown_run_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["runs", "show", "run.x", "--state", str(tmp_path / "state.db")]) == 2
        assert "no run with id" in capsys.readouterr().err

    def test_recover_reports_a_quiet_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["runs", "recover", "--state", str(tmp_path / "state.db")]) == 0
        assert "nothing stalled" in capsys.readouterr().out

    def test_recover_dry_run_changes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state.db"
        _abandon_a_step(state)
        capsys.readouterr()

        assert main(["runs", "recover", "--state", str(state), "--dry-run"]) == 0
        assert "would recover" in capsys.readouterr().out

        with StateStore.open(state) as store:
            assert store.running_steps() != ()

    def test_recover_times_the_step_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state.db"
        _abandon_a_step(state)
        capsys.readouterr()

        assert main(["runs", "recover", "--state", str(state)]) == 0
        assert "recovered: " in capsys.readouterr().out

        with StateStore.open(state) as store:
            assert store.running_steps() == ()

    def test_recover_drains_the_dead_letter_queue(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state.db"
        with StateStore.open(state) as store:
            store.audit.submit({"id": "ev.bad", "kind": "no.such.kind"}, at=_now())

        assert main(["runs", "recover", "--state", str(state)]) == 0
        assert "dead letters: 1 parked, 0 replayed" in capsys.readouterr().out


def _now() -> datetime:
    return datetime.now(UTC)


def _only_run_id(state: Path) -> str:
    with StateStore.open(state) as store:
        (run,) = store.list_runs()
        return run.id


def _abandon_a_step(state: Path) -> None:
    """A step row left running long enough to be overdue, as a crash leaves it."""
    with StateStore.open(state) as store:
        run = store.create_run(
            Run(
                id="run.abandoned",
                work_item_id="wi.test",
                workflow="toy",
                workflow_version="1.0.0",
                status=RunStatus.RUNNING,
                created_at=_now() - timedelta(hours=4),
                updated_at=_now() - timedelta(hours=4),
            )
        )
        store.start_step(
            StepResult(
                id="sr.stuck",
                run_id=run.id,
                stage_id="build",
                type=StepType.SCRIPT,
                status=StepStatus.RUNNING,
                attempt=1,
                idempotency_key="run.abandoned:build:1",
                timeout_seconds=60,
                started_at=_now() - timedelta(hours=4),
            )
        )


class TestReapCommand:
    """The reaper, from the operator's side.

    Every test here points ``--work-root`` at a temporary directory and passes
    ``--no-caches`` unless the cache is the subject. Both are deliberate: this
    command deletes things, and a suite that let it fall back to the machine's
    real cache home would be a suite that reclaims the developer's caches when
    it runs.
    """

    def test_a_quiet_machine_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            [
                "reap",
                "--state",
                str(tmp_path / "state.db"),
                "--work-root",
                str(tmp_path / "work"),
                "--no-caches",
            ]
        )

        assert code == 0
        assert "nothing to reclaim" in capsys.readouterr().out

    def test_a_stale_worktree_is_reclaimed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stale = _stale_worktree(tmp_path / "work", "run.gone")

        code = main(
            [
                "reap",
                "--state",
                str(tmp_path / "state.db"),
                "--work-root",
                str(tmp_path / "work"),
                "--no-caches",
                "--older-than",
                "1",
            ]
        )

        assert code == 0
        assert "reclaimed" in capsys.readouterr().out
        assert not stale.exists()

    def test_a_worktree_belonging_to_a_running_run_is_protected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The safety property, through the command an operator actually types.

        The live set comes from the store, so a run recorded as ``running``
        keeps its worktree however old the directory looks — which is what
        stops a scheduled reap eating the work of a long run.
        """
        state = tmp_path / "state.db"
        with StateStore.open(state) as store:
            store.create_run(
                Run(
                    id="run.busy",
                    work_item_id="wi.busy",
                    workflow="toy",
                    workflow_version="1.0.0",
                    status=RunStatus.RUNNING,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        live = _stale_worktree(tmp_path / "work", "run.busy")

        code = main(
            [
                "reap",
                "--state",
                str(state),
                "--work-root",
                str(tmp_path / "work"),
                "--no-caches",
                "--older-than",
                "0",
            ]
        )

        assert code == 0
        assert "1 run(s) still live" in capsys.readouterr().out
        assert live.is_dir()

    def test_a_dry_run_reports_without_removing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stale = _stale_worktree(tmp_path / "work", "run.gone")

        code = main(
            [
                "reap",
                "--state",
                str(tmp_path / "state.db"),
                "--work-root",
                str(tmp_path / "work"),
                "--no-caches",
                "--older-than",
                "1",
                "--dry-run",
            ]
        )

        assert code == 0
        assert "would reclaim" in capsys.readouterr().out
        assert stale.is_dir()

    def test_no_work_root_means_no_worktree_is_touched(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """There is no safe default for this: guessing at ``/clawdence/work``
        on a machine that keeps its worktrees elsewhere deletes whatever *was*
        at that path."""
        stale = _stale_worktree(tmp_path / "work", "run.gone")

        assert main(["reap", "--state", str(tmp_path / "state.db"), "--no-caches"]) == 0
        assert "nothing to reclaim" in capsys.readouterr().out
        assert stale.is_dir()


class TestProbe:
    """``clawdence probe`` — the proposal, and what it refuses to do with it."""

    def test_a_report_names_the_commands_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit 1 because this checkout has no remote to record, which is the
        one thing outstanding — see ``test_unfinished_business_is_the_exit_status``."""
        repo = _python_repo(tmp_path)
        before = sorted(path.name for path in repo.iterdir())

        assert main(["probe", str(repo)]) == 1

        out = capsys.readouterr().out
        assert "uv run pytest" in out
        assert "proposal" in out
        assert sorted(path.name for path in repo.iterdir()) == before

    def test_json_is_the_profile_and_the_reasoning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["probe", str(_python_repo(tmp_path)), "--json"]) == 1

        payload = json.loads(capsys.readouterr().out)
        assert payload["profile"]["build_system"] == "uv"
        assert payload["findings"]

    def test_out_writes_the_profile_alone_and_will_not_clobber(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The profile is what gets committed; the findings are the review.
        Refusing to overwrite matters because the file a second probe would
        replace is the one somebody has already edited by hand."""
        target = tmp_path / "profiles" / "etl.json"

        assert main(["probe", str(_python_repo(tmp_path)), "--out", str(target)]) == 1
        capsys.readouterr()
        assert json.loads(target.read_text(encoding="utf-8"))["id"] == "repo"

        assert main(["probe", str(_python_repo(tmp_path)), "--out", str(target)]) == 2
        assert "pass --force" in capsys.readouterr().err

    def test_unfinished_business_is_the_exit_status(self, tmp_path: Path) -> None:
        """1 means "a person still has to look at this" — the answer a script
        probing twenty repositories wants. The repository here is fine; the
        profile is not finished, because nothing has granted the daemon its
        tests need."""
        repo = tmp_path / "needs-docker"
        (repo / "src").mkdir(parents=True)
        (repo / "go.mod").write_text(
            "module x\n\nrequire github.com/testcontainers/testcontainers-go v0.34.0\n",
            encoding="utf-8",
        )

        assert main(["probe", str(repo)]) == 1

    def test_a_finished_proposal_exits_zero(self, tmp_path: Path) -> None:
        """The other half of the exit status: a checkout with a remote, a
        lockfile and a test command leaves a person nothing to do."""
        if shutil.which("git") is None:
            pytest.skip("git is not on PATH")
        repo = _python_repo(tmp_path)
        for args in (
            ("init", "-q", "-b", "main"),
            ("remote", "add", "origin", "https://github.invalid/acme/etl.git"),
        ):
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "-C", str(repo), *args],  # noqa: S607 - PATH lookup is checked above
                check=True,
                capture_output=True,
            )

        assert main(["probe", str(repo)]) == 0

    def test_a_bad_id_is_refused_rather_than_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["probe", str(_python_repo(tmp_path)), "--id", "not a valid id"]) == 2
        assert "id" in capsys.readouterr().err

    def test_a_path_that_is_not_a_directory_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["probe", str(tmp_path / "absent")]) == 2
        assert "not a directory" in capsys.readouterr().err


class TestSubmit:
    """``clawdence submit`` — the CLI as an ``IngestPort`` source.

    Every test names an explicit ``--state`` for the reason in the module
    docstring, and here it is load-bearing twice over: the whole point of this
    adapter is that its dedupe guard is a file, so a test that shared the real
    one would be sharing state with the developer's own inbox.
    """

    def db(self, root: Path) -> str:
        return str(root / "state.db")

    def test_a_request_is_created(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["submit", "--state", self.db(tmp_path), "--text", "Fix the total"]) == 0
        assert "created" in capsys.readouterr().out

    def test_the_same_ref_twice_creates_one_request(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The verification S10 asks for, at the surface a person uses: a script
        that re-submits on retry must not fan out into two pieces of work."""
        argv = ["submit", "--state", self.db(tmp_path), "--ref", "REQ-1", "--text", "Fix the total"]
        assert main(argv) == 0
        capsys.readouterr()
        assert main(argv) == 0
        assert "already submitted" in capsys.readouterr().out

        assert main(["inbox", "list", "--state", self.db(tmp_path)]) == 0
        assert capsys.readouterr().out.count("cli:REQ-1") == 1

    def test_dedupe_survives_the_process(self, tmp_path: Path) -> None:
        """Two ``main`` calls are one process here, so the real check is that
        the guard is in the database rather than in a live object — nothing is
        carried between the calls but the path."""
        argv = ["submit", "--state", self.db(tmp_path), "--ref", "REQ-1", "--text", "Fix the total"]
        main(argv)
        with StateStore.open(self.db(tmp_path)) as store:
            rows = store.connection.execute("SELECT COUNT(*) FROM intake").fetchone()[0]
        main(argv)
        with StateStore.open(self.db(tmp_path)) as store:
            assert store.connection.execute("SELECT COUNT(*) FROM intake").fetchone()[0] == rows

    def test_editing_updates_rather_than_duplicates(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["submit", "--state", self.db(tmp_path), "--ref", "REQ-1", "--text", "Fix the total"])
        capsys.readouterr()
        assert (
            main(
                [
                    "submit",
                    "--state",
                    self.db(tmp_path),
                    "--ref",
                    "REQ-1",
                    "--text",
                    "Fix the tax line",
                    "--amend",
                ]
            )
            == 0
        )
        assert "amended" in capsys.readouterr().out

        assert main(["inbox", "show", "--state", self.db(tmp_path), "REQ-1"]) == 0
        out = capsys.readouterr().out
        assert "Fix the tax line" in out
        assert "Fix the total" not in out

    def test_an_edit_after_pickup_is_a_distinct_exit_status(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """3, not 0. Something is working from the older text, and a script
        submitting into a running pipeline has to be able to branch on that."""
        main(["submit", "--state", self.db(tmp_path), "--ref", "REQ-1", "--text", "Fix the total"])
        capsys.readouterr()
        with StateStore.open(self.db(tmp_path)) as store:
            item = Intake(store).collect()[0]
            Intake(store).acknowledge(item.id)

        argv = ["submit", "--state", self.db(tmp_path), "--ref", "REQ-1", "--text", "Fix the tax"]
        assert main(argv) == 3
        assert "older version" in capsys.readouterr().out

    def test_withdrawing_takes_it_out_of_the_queue(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["submit", "--state", self.db(tmp_path), "--ref", "REQ-1", "--text", "Fix the total"])
        capsys.readouterr()
        assert main(["submit", "--state", self.db(tmp_path), "--withdraw", "REQ-1"]) == 0
        assert "withdrawn" in capsys.readouterr().out

        assert main(["inbox", "list", "--state", self.db(tmp_path), "--status", "pending"]) == 0
        assert "nothing submitted" in capsys.readouterr().out

    def test_a_reply_lands_on_the_originating_request(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Reply routing symmetry, inbound. The answer belongs to the request
        that asked, and it must not become a second one."""
        main(
            [
                "submit",
                "--state",
                self.db(tmp_path),
                "--ref",
                "REQ-1",
                "--conversation",
                "thread-9",
                "--text",
                "Flaky checkout test",
            ]
        )
        capsys.readouterr()
        assert (
            main(["submit", "--state", self.db(tmp_path), "--reply", "thread-9", "--text", "On CI"])
            == 0
        )
        assert "reply recorded" in capsys.readouterr().out

        main(["inbox", "show", "--state", self.db(tmp_path), "REQ-1"])
        assert "On CI" in capsys.readouterr().out
        main(["inbox", "list", "--state", self.db(tmp_path)])
        assert capsys.readouterr().out.count("cli:") == 1

    def test_a_reply_to_no_conversation_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["submit", "--state", self.db(tmp_path), "--reply", "thread-9", "--text", "hello?"]
        assert main(argv) == 2
        assert "nothing for this reply to continue" in capsys.readouterr().err

    def test_the_system_will_not_submit_to_itself(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Bot-loop prevention, at the surface. ``--as clawdence`` is the CLI's
        version of the system's own summary arriving back through a channel."""
        argv = [
            "submit",
            "--state",
            self.db(tmp_path),
            "--as",
            SELF_ID,
            "--text",
            "Run finished: 3 PRs opened",
        ]
        assert main(argv) == 2
        assert "loop" in capsys.readouterr().err

    def test_the_request_is_read_from_a_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "request.md"
        path.write_text("Fix the checkout total\n\nIt rounds down.\n", encoding="utf-8")
        assert main(["submit", "--state", self.db(tmp_path), "--file", str(path)]) == 0
        assert "Fix the checkout total" in capsys.readouterr().out

    def test_two_sources_for_the_body_are_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["submit", "--state", self.db(tmp_path), "--text", "a", "--file", "b"]
        assert main(argv) == 2
        assert "pass one" in capsys.readouterr().err

    def test_two_verbs_are_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["submit", "--state", self.db(tmp_path), "--withdraw", "REQ-1", "--amend"]
        assert main(argv) == 2
        assert "ask for different things" in capsys.readouterr().err

    def test_json_carries_the_work_item(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            main(["submit", "--state", self.db(tmp_path), "--text", "Fix the total", "--json"]) == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["disposition"] == "created"
        assert payload["work_item"]["raw_text"] == "Fix the total"

    def test_the_verbatim_body_is_what_show_prints(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nothing between the submitting surface and the reviewing one rewrites
        it — the ``slackMessageRaw`` lesson, checked end to end."""
        body = "Fix **Checkout-Pro**'s totals.\n\nThey round down."
        main(["submit", "--state", self.db(tmp_path), "--ref", "REQ-1", "--text", body])
        capsys.readouterr()
        main(["inbox", "show", "--state", self.db(tmp_path), "REQ-1"])
        out = capsys.readouterr().out
        assert "Fix **Checkout-Pro**'s totals." in out
        assert "They round down." in out

    def test_showing_something_never_submitted_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["inbox", "show", "--state", self.db(tmp_path), "REQ-9"]) == 2
        assert "REQ-9" in capsys.readouterr().err

    def test_an_empty_inbox_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["inbox", "list", "--state", self.db(tmp_path)]) == 0
        assert "nothing submitted" in capsys.readouterr().out


class TestDevLoop:
    """Reset, replay and the audit view, from the command line.

    Every test points ``--state`` at a temporary database and never passes
    ``--caches``: ``reset`` deletes things, and a suite that let it fall back to
    the machine's real cache home would reclaim the developer's caches when it
    runs. ``--yes`` throughout, because stdin under pytest is not a terminal and
    the confirmation is one of the things being tested.
    """

    def state(self, tmp_path: Path) -> Path:
        path = tmp_path / "state.db"
        main(["run", "examples/toy.yaml", "--state", str(path)])
        return path

    # -------------------------------------------------------------- reset

    def test_reset_returns_a_dirty_database_to_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """S20's acceptance criterion: one command, dirty to clean."""
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["reset", "--state", str(state), "--yes"]) == 0
        assert "removed" in capsys.readouterr().out

        assert main(["runs", "list", "--state", str(state)]) == 0
        assert "no runs recorded" in capsys.readouterr().out

    def test_reset_without_a_terminal_refuses_rather_than_prompting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unattended is exactly where a destructive command should not guess.

        pytest's stdin is not a tty, which makes this the default path here —
        and the reason ``--yes`` is on every other test in the class.
        """
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["reset", "--state", str(state)]) == 2
        captured = capsys.readouterr()
        assert "--yes" in captured.err
        # And it still showed the list, so the refusal is informative rather
        # than merely obstructive.
        assert "would remove" in captured.out
        assert main(["runs", "list", "--state", str(state)]) == 0
        assert "toy@1.0.0" in capsys.readouterr().out

    def test_reset_confirms_at_a_terminal(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = self.state(tmp_path)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "n")
        capsys.readouterr()

        assert main(["reset", "--state", str(state)]) == 2
        assert "nothing was removed" in capsys.readouterr().out

        monkeypatch.setattr("builtins.input", lambda _: "y")
        assert main(["reset", "--state", str(state)]) == 0
        assert "removed" in capsys.readouterr().out

    def test_a_dry_reset_changes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["reset", "--state", str(state), "--dry-run"]) == 0
        assert "would remove" in capsys.readouterr().out

        assert main(["runs", "list", "--state", str(state)]) == 0
        assert "toy@1.0.0" in capsys.readouterr().out

    def test_reset_refuses_while_a_run_is_running(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state.db"
        _abandon_a_step(state)
        capsys.readouterr()

        assert main(["reset", "--state", str(state), "--yes"]) == 2
        assert "--force" in capsys.readouterr().err

        assert main(["reset", "--state", str(state), "--yes", "--force"]) == 0
        assert "abandoned" in capsys.readouterr().out

    def test_reset_keeps_the_inbox_when_asked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = self.state(tmp_path)
        main(["submit", "--state", str(state), "--ref", "REQ-1", "--text", "do a thing"])
        capsys.readouterr()

        assert main(["reset", "--state", str(state), "--yes", "--keep-inbox"]) == 0
        assert "kept the inbox" in capsys.readouterr().out

        assert main(["inbox", "list", "--state", str(state)]) == 0
        assert "REQ-1" in capsys.readouterr().out

    def test_reset_removes_a_worktree_the_reaper_would_spare(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The difference between the two commands, at the surface.

        A directory made a moment ago is inside the reaper's grace period, so
        ``reap`` leaves it. ``reset`` is the operator saying no run owns it.
        """
        work = tmp_path / "work"
        fresh = work / "run.recent"
        fresh.mkdir(parents=True)

        assert (
            main(
                ["reap", "--state", str(tmp_path / "s.db"), "--work-root", str(work), "--no-caches"]
            )
            == 0
        )
        assert fresh.exists()

        assert (
            main(["reset", "--state", str(tmp_path / "s.db"), "--work-root", str(work), "--yes"])
            == 0
        )
        assert not fresh.exists()

    # ------------------------------------------------------------- replay

    def test_replay_agrees_with_the_run_it_describes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = self.state(tmp_path)
        run_id = _only_run_id(state)
        capsys.readouterr()

        assert main(["replay", run_id, "--state", str(state)]) == 0
        out = capsys.readouterr().out
        assert "the log and the stored state agree" in out
        assert "not carried by the log" in out

    def test_replay_exits_non_zero_on_a_divergence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A script wrapping this needs to branch on the answer, and the answer
        is that something wrote state without recording it."""
        state = self.state(tmp_path)
        run_id = _only_run_id(state)
        with StateStore.open(state) as store:
            (step, *_) = store.steps_for(run_id)
            store.finish_step(step.model_copy(update={"status": StepStatus.FAILED}))
        capsys.readouterr()

        assert main(["replay", run_id, "--state", str(state)]) == 1
        assert "divergence" in capsys.readouterr().out

    def test_a_truncated_replay_says_it_did_not_compare(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["replay", _only_run_id(state), "--state", str(state), "--through", "3"]) == 0
        assert "not compared" in capsys.readouterr().out

    def test_replay_emits_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["replay", _only_run_id(state), "--state", str(state), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["agrees"] is True

    def test_replaying_a_run_that_never_existed_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["replay", "run.x", "--state", str(tmp_path / "state.db")]) == 2
        assert "run.x" in capsys.readouterr().err

    # -------------------------------------------------------------- audit

    def test_audit_shows_the_timeline(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["audit", "--state", str(state), "--run", _only_run_id(state)]) == 0
        out = capsys.readouterr().out
        assert "run.started" in out
        assert "step.finished" in out

    def test_audit_filters_by_kind_and_takes_the_newest(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["audit", "--state", str(state), "--kind", "run.finished", "--limit", "1"]) == 0
        out = capsys.readouterr().out
        assert "run.finished" in out
        assert "most recent" in out

    def test_audit_refuses_a_naive_instant(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Naive means "my timezone" to a person and "UTC" to the database, and
        picking one silently makes the filter wrong by however many hours the
        reader is from Greenwich."""
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["audit", "--state", str(state), "--since", "2026-01-01T00:00:00"]) == 2
        assert "no timezone" in capsys.readouterr().err

        assert main(["audit", "--state", str(state), "--since", "not-a-time"]) == 2
        assert "ISO-8601" in capsys.readouterr().err

    def test_audit_shows_parked_records(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state = tmp_path / "state.db"
        with StateStore.open(state) as store:
            store.audit.submit({"not": "an event"}, at=datetime.now(UTC))
        capsys.readouterr()

        assert main(["audit", "--state", str(state), "--dead-letters"]) == 0
        assert "from submit" in capsys.readouterr().out

    def test_audit_emits_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["audit", "--state", str(state), "--json", "--limit", "2"]) == 0
        assert len(json.loads(capsys.readouterr().out)) == 2

    # ----------------------------------------------------------- runs show

    def test_runs_show_carries_durations_and_the_error_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The message is here and not in the log, which is the point of both."""
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["runs", "show", _only_run_id(state), "--state", str(state)]) == 0
        out = capsys.readouterr().out
        assert "script-exit:" in out
        assert "audit record(s)" in out

    def test_runs_show_emits_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        state = self.state(tmp_path)
        capsys.readouterr()

        assert main(["runs", "show", _only_run_id(state), "--state", str(state), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["run"]["workflow"] == "toy"
        assert payload["events"] > 0


def _python_repo(root: Path) -> Path:
    """A uv project, with no git and no remote: probing does not need either."""
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / "tests" / "conftest.py").write_text("", encoding="utf-8")
    return repo


def _stale_worktree(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "app.py").write_text("x = 1\n", encoding="utf-8")
    old = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    os.utime(directory, (old, old))
    return directory


# --------------------------------------------------------------------------- #
# Which handlers a CLI run gets (S12)
# --------------------------------------------------------------------------- #


def test_with_no_api_key_the_agent_step_refuses_and_names_what_to_wire() -> None:
    """A workflow with no agent steps must run perfectly well on a machine with no
    credentials, so this cannot be a startup error — it has to be a refusal at the
    step, and the refusal has to say what is missing."""
    registry = cli._registry({})
    handler = registry.for_type(StepType.AGENT)
    assert isinstance(handler, UnimplementedHandler)


def test_with_an_api_key_the_agent_step_is_wired_to_the_provider() -> None:
    registry = cli._registry({cli.API_KEY_ENV: "not-a-real-key"})
    handler = registry.for_type(StepType.AGENT)
    assert isinstance(handler, AgentHandler)
    assert isinstance(handler.model, AnthropicModels)
    assert handler.model.describe("claude-sonnet-5").model == "claude-sonnet-5"


def test_an_empty_api_key_counts_as_absent() -> None:
    """``export ANTHROPIC_API_KEY=`` is how a credential goes missing in a shell
    script, and wiring a provider that would 401 is worse than refusing."""
    assert isinstance(
        cli._registry({cli.API_KEY_ENV: ""}).for_type(StepType.AGENT), UnimplementedHandler
    )


def test_the_credential_allowlist_is_one_name() -> None:
    """So a workflow naming a secret cannot make this resolve a different variable
    — ``EnvSecrets`` without an allowlist is ``os.environ``."""
    registry = cli._registry({cli.API_KEY_ENV: "not-a-real-key", "AWS_SECRET_ACCESS_KEY": "no"})
    handler = registry.for_type(StepType.AGENT)
    assert isinstance(handler, AgentHandler)
    assert isinstance(handler.model, AnthropicModels)
    assert handler.model.headers()["x-api-key"] == "not-a-real-key"


def test_running_a_workflow_with_an_agent_step_and_no_key_halts_at_that_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shipped example, end to end, on a machine with no credentials. It has to
    fail *and say why* rather than reporting a success for work nobody did.

    The first stage is the agent one now: S11 removed the ``intake`` script step
    that used to echo a hardcoded request, because ``${request.json.text}`` is
    where a workflow gets one. So the refusal arrives one stage earlier, which is
    the same behaviour with one fewer stage in front of it.
    """
    assert main(["run", "examples/spike.yaml", "--no-state"]) == 1
    out = capsys.readouterr().out
    assert "understand" in out
    assert "step-type-not-implemented" in out
    assert "status: halted" in out
