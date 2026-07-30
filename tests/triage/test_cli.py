"""The three verbs S11 adds, through ``main`` rather than around it.

``cli.main`` is called with argv, because the entry-point rule says it is the
only supported path in and a test that imported ``_triage_command`` would be
testing a function nobody can reach. What is asserted is the exit status and
what a person sees, since between them those are the whole contract of a command
somebody puts in a shell script.

Exit statuses are the interesting part and they mean the same thing in all
three: 0 is "the answer is yes", 1 is "the command worked and the answer is no",
2 is "the command could not do its job". A queue-draining script branches on
that distinction, and collapsing 1 into 2 would make "nothing routed" look like
"the database is unreadable".
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from clawdence.cli import main
from clawdence.ingest import cli as ingest_cli
from clawdence.store import Intake, StateStore

AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(workspace: Path) -> Path:
    """A state database of this test's own. Never the user's home."""
    return workspace / "state.db"


def put(db: Path, text: str, *, ref: str) -> str:
    with StateStore.open(db) as store:
        return ingest_cli.submit(Intake(store), text=text, at=AT, ref=ref).item.id


# --------------------------------------------------------------------- repos


def test_repos_list_shows_what_the_deployment_is_wired_to(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["repos", "list", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "repo.widget" in out
    assert "repo.portal" in out
    # The absent runner is stated rather than left to be inferred: a deployment
    # without one stops every workflow at its first runner step, and that looks
    # like a bug until you have seen this line.
    assert "none configured" in out


def test_repos_show_prints_the_signals_routing_reads(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["repos", "show", "repo.widget", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "widget-api" in out
    assert "arithmetic" in out


def test_a_missing_configuration_is_exit_two_and_says_what_it_was_for(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["repos", "list", "--config", str(tmp_path / "nope.yaml")]) == 2
    assert "which repositories exist" in capsys.readouterr().err


def test_repos_check_answers_for_every_repository(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The verb S15 wrote ``check_policy`` for and could not expose.

    The fake forge reports a repository with no protection rules, so both come
    back clean. What is being tested is that the command reaches the adapter at
    all — the rules themselves are ``tests/vcs/test_policy``'s.
    """
    assert main(["repos", "check", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert out.count("ok") == 2


# -------------------------------------------------------------------- triage


def test_triage_explains_every_pending_request_and_changes_nothing(
    config_path: Path, db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(db, "The widget adder mishandles floats", ref="a")
    put(db, "The portal login is broken", ref="b")

    assert main(["triage", "--config", str(config_path), "--state", str(db)]) == 0
    out = capsys.readouterr().out
    assert "repo.widget" in out
    assert "repo.portal" in out

    # Nothing was taken off the queue: `triage` is the command you run *before*
    # deciding, and a command that acknowledged what it reported on would make
    # asking the question destructive.
    with StateStore.open(db) as store:
        assert len(Intake(store).collect()) == 2


def test_triage_exits_one_when_something_did_not_route(
    config_path: Path, db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The finding, not the failure. A queue with an unroutable request in it
    needs a person, and a script draining it has to be able to see that."""
    put(db, "Please make the thing faster.", ref="vague")
    assert main(["triage", "--config", str(config_path), "--state", str(db)]) == 1
    assert "unrouted" in capsys.readouterr().out


def test_triage_can_be_asked_about_one_reference(
    config_path: Path, db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = put(db, "The widget adder mishandles floats", ref="a")
    second = put(db, "The portal login is broken", ref="b")
    assert main(["triage", "a", "--config", str(config_path), "--state", str(db)]) == 0

    # By work item id, not by repository name: the runner-up appears in the
    # winner's *reason* ("scores 4 against 0 for repo.portal"), which is the
    # report doing its job rather than a second request being shown.
    out = capsys.readouterr().out
    assert first in out
    assert second not in out


def test_triage_json_is_the_routing_payload(
    config_path: Path, db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    put(db, "The widget adder mishandles floats", ref="a")
    assert main(["triage", "a", "--config", str(config_path), "--state", str(db), "--json"]) == 0
    decoded = json.loads(capsys.readouterr().out)
    assert decoded[0]["repo"] == "repo.widget"
    assert decoded[0]["workflow"] == "quick-fix"


def test_an_unknown_reference_is_exit_two(
    config_path: Path, db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(db, "anything", ref="a")
    assert main(["triage", "zzz", "--config", str(config_path), "--state", str(db)]) == 2
    assert "nothing submitted" in capsys.readouterr().out


def test_an_empty_queue_is_not_a_failure(
    config_path: Path, db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asking an empty queue what is pending is a legitimate thing to do often,
    which is why it is 0 — a cron job that failed nightly on 'nothing to do'
    would be a cron job somebody turned off."""
    with StateStore.open(db):
        pass
    assert main(["triage", "--config", str(config_path), "--state", str(db)]) == 0
    assert "nothing pending" in capsys.readouterr().out


# ---------------------------------------------------------------------- work


def test_work_dry_run_is_triage(
    config_path: Path, db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(db, "The widget adder mishandles floats", ref="a")
    assert main(["work", "--config", str(config_path), "--state", str(db), "--dry-run"]) == 0
    assert "repo.widget" in capsys.readouterr().out
    with StateStore.open(db) as store:
        assert len(Intake(store).collect()) == 1


def test_work_takes_one_request_by_default(
    config_path: Path, db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each request spends money, so a queue drained by accident is an expensive
    surprise. ``--limit`` is how somebody asks for more, deliberately.

    The runs themselves fail here — no runner is configured, so the ``quick-fix``
    workflow's only stage refuses — which is what makes the *count* observable:
    exactly one request left the queue.
    """
    put(db, "The widget adder mishandles floats", ref="a")
    put(db, "The widget adder mishandles ints too", ref="b")

    assert main(["work", "--config", str(config_path), "--state", str(db)]) == 1
    with StateStore.open(db) as store:
        assert len(Intake(store).collect()) == 1


def test_a_deployment_with_no_runner_says_so_rather_than_substituting_one(
    config_path: Path, db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal ``default_registry`` has had since S6, reached through the
    pipeline. A composition root that quietly substituted the host tier would be
    choosing, for an operator, to run model-authored code outside a container.
    """
    put(db, "The widget adder mishandles floats", ref="a")
    assert main(["work", "--config", str(config_path), "--state", str(db)]) == 1
    assert "failed" in capsys.readouterr().out


def test_work_refuses_an_unroutable_request_and_leaves_it_queued(
    config_path: Path, db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(db, "Please make the thing faster.", ref="vague")
    assert main(["work", "--config", str(config_path), "--state", str(db)]) == 2
    assert "not routed" in capsys.readouterr().out
    with StateStore.open(db) as store:
        assert len(Intake(store).collect()) == 1
