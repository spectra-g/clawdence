"""Re-running the evidence: the ``pre_verify`` hook, and the post-rebase path.

The ordering is the hook's whole meaning — a fixture seeded after the tests ran
is not a fixture — so most of this file is about *which command ran when*, which
is testable precisely because nothing here spawns a process.

The async tests drive one ``asyncio.run`` each rather than reaching for
``pytest-asyncio``, matching the engine, port and runner suites: a decorator is
not worth a pinned dependency and the standing obligation that comes with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawdence.domain import (
    ContractKind,
    DiffStat,
    RepoProfile,
    RunnerOutcome,
    VerificationContract,
)
from clawdence.domain import TestReporter as Reporter
from clawdence.verify import (
    Attempt,
    Command,
    CommandResult,
    Recheck,
    Rechecked,
    evaluate,
    into_attempt,
    parse,
    sequential,
)
from clawdence.verify import run as recheck_run
from tests.ports.factories import run
from tests.verify import reports

TREE = "a" * 40
REBASED = "b" * 40


def profile(**overrides: object) -> RepoProfile:
    fields: dict[str, object] = {
        "id": "acme-billing",
        "name": "billing",
        "remote_url": "https://example.invalid/acme/billing.git",
        "test_command": ("pytest", "--json-report"),
        "test_reporter": Reporter.PYTEST_JSON_REPORT,
        "exec_prefix": ("mise", "exec", "python@3.12", "--"),
    }
    fields.update(overrides)
    return RepoProfile(**fields)


def contract(**overrides: object) -> VerificationContract:
    fields: dict[str, object] = {"kind": ContractKind.TEST_AFTER}
    fields.update(overrides)
    return VerificationContract(**fields)


class TestThePlan:
    def test_the_hook_runs_before_the_tests(self, tmp_path: Path) -> None:
        """A fixture seeded after the tests ran is not a fixture."""
        plan = Recheck.plan(profile(), contract(pre_verify=("make", "fixtures")), tmp_path)

        assert [command.label for command in plan.commands] == ["pre-verify", "test"]

    def test_the_toolchain_prefix_is_applied(self, tmp_path: Path) -> None:
        """It selects the versions the project pins, and a caller that forgets
        it runs the tests under whatever is on the path."""
        plan = Recheck.plan(profile(), contract(pre_verify=("make", "seed")), tmp_path)

        assert plan.commands[0].argv == ("mise", "exec", "python@3.12", "--", "make", "seed")
        assert plan.commands[1].argv[-2:] == ("pytest", "--json-report")

    def test_a_contract_with_no_hook_plans_only_the_tests(self, tmp_path: Path) -> None:
        plan = Recheck.plan(profile(), contract(), tmp_path)

        assert [command.label for command in plan.commands] == ["test"]

    def test_a_repository_with_no_test_command_plans_nothing_to_run(self, tmp_path: Path) -> None:
        plan = Recheck.plan(profile(test_command=()), contract(kind=ContractKind.NONE), tmp_path)

        assert plan.commands == ()


class TestRunning:
    def test_a_passing_recheck_produces_evidence(self, tmp_path: Path) -> None:
        (tmp_path / ".report.json").write_bytes(reports.PYTEST_PASSING)
        plan = Recheck.plan(profile(), contract(), tmp_path)

        rechecked = run(recheck_run(plan, profile(), sequential([CommandResult(exit_code=0)])))

        assert rechecked.error is None
        assert rechecked.evidence is not None
        assert rechecked.evidence.failed == 0

    def test_a_failing_test_command_is_not_an_error(self, tmp_path: Path) -> None:
        """It is the normal way a re-check says the tests do not pass, and the
        evidence for it is in the report the run just wrote."""
        (tmp_path / ".report.json").write_bytes(reports.PYTEST_FAILING)
        plan = Recheck.plan(profile(), contract(), tmp_path)

        rechecked = run(recheck_run(plan, profile(), sequential([CommandResult(exit_code=1)])))

        assert rechecked.error is None
        assert rechecked.evidence is not None
        assert rechecked.evidence.failed == 1

    def test_a_failing_hook_stops_everything(self, tmp_path: Path) -> None:
        """Whatever ran after an unprepared workspace proves nothing either way."""
        (tmp_path / ".report.json").write_bytes(reports.PYTEST_PASSING)
        seen: list[Command] = []

        async def execute(worktree: Path, command: Command) -> CommandResult:
            seen.append(command)
            return CommandResult(exit_code=2, detail="no such target: fixtures")

        plan = Recheck.plan(profile(), contract(pre_verify=("make", "fixtures")), tmp_path)

        rechecked = run(recheck_run(plan, profile(), execute))

        assert [command.label for command in seen] == ["pre-verify"]
        assert rechecked.pre_verify_ok is False
        assert rechecked.failed_at == "pre-verify"
        assert rechecked.error == "no such target: fixtures"
        # The report left by an earlier run is not read as this one's.
        assert rechecked.evidence is None

    def test_an_unparseable_report_is_an_error_not_a_failure(self, tmp_path: Path) -> None:
        (tmp_path / ".report.json").write_bytes(b"{not json")
        plan = Recheck.plan(profile(), contract(), tmp_path)

        rechecked = run(recheck_run(plan, profile(), sequential([CommandResult(exit_code=0)])))

        assert rechecked.failed_at == "report"
        assert rechecked.error is not None
        assert "JSON" in rechecked.error

    def test_every_planned_command_is_run_in_order(self, tmp_path: Path) -> None:
        (tmp_path / ".report.json").write_bytes(reports.PYTEST_PASSING)
        seen: list[Command] = []

        async def execute(worktree: Path, command: Command) -> CommandResult:
            seen.append(command)
            return CommandResult(exit_code=0)

        plan = Recheck.plan(profile(), contract(pre_verify=("make", "seed")), tmp_path)

        run(recheck_run(plan, profile(), execute))

        assert [command.label for command in seen] == ["pre-verify", "test"]

    def test_the_executor_is_given_the_worktree(self, tmp_path: Path) -> None:
        """A re-check may happen long after the run that produced the branch, so
        the worktree cannot be something an executor closed over."""
        (tmp_path / ".report.json").write_bytes(reports.PYTEST_PASSING)
        seen: list[Path] = []

        async def execute(worktree: Path, command: Command) -> CommandResult:
            seen.append(worktree)
            return CommandResult(exit_code=0)

        run(recheck_run(Recheck.plan(profile(), contract(), tmp_path), profile(), execute))

        assert seen == [tmp_path]


class TestFoldingBackIn:
    """The post-rebase path: re-derive the green run and nothing else."""

    def test_the_red_phase_survives_a_re_check(self) -> None:
        """The TDD work was done correctly and the base moved.

        Discarding the red phase because the tests were re-run would report
        ``NO_RED_PHASE`` against a story that did outside-in TDD properly, which
        is the re-verification breaking the thing it exists to protect.
        """
        original = Attempt(
            tree_hash=TREE,
            outcome=RunnerOutcome.SUCCEEDED,
            diff=DiffStat(files_changed=2),
            evidence=parse(Reporter.PYTEST_JSON_REPORT, reports.PYTEST_PASSING),
            red_evidence=parse(Reporter.PYTEST_JSON_REPORT, reports.PYTEST_FAILING),
            full_suite=True,
        )
        rechecked = Rechecked(
            evidence=parse(Reporter.PYTEST_JSON_REPORT, reports.PYTEST_PASSING),
            pre_verify_ok=None,
        )

        folded = into_attempt(rechecked, original, tree_hash=REBASED)

        assert folded.tree_hash == REBASED
        assert folded.red_evidence is not None
        assert folded.diff == original.diff
        assert evaluate(contract(kind=ContractKind.OUTSIDE_IN_TDD), folded).passed

    def test_the_re_verified_result_binds_to_the_new_tree(self) -> None:
        """The point of the whole exercise: evidence for the tree that lands."""
        evidence = parse(Reporter.PYTEST_JSON_REPORT, reports.PYTEST_PASSING)
        original = Attempt(tree_hash=TREE, diff=DiffStat(files_changed=2), evidence=evidence)

        folded = into_attempt(
            Rechecked(evidence=evidence, pre_verify_ok=None), original, tree_hash=REBASED
        )

        assert evaluate(contract(), folded).tree_hash == REBASED

    def test_a_verification_error_is_carried_onto_the_attempt(self) -> None:
        original = Attempt(tree_hash=TREE, diff=DiffStat(files_changed=1))

        folded = into_attempt(
            Rechecked(evidence=None, pre_verify_ok=None, error="report is not well-formed XML"),
            original,
            tree_hash=REBASED,
        )

        assert folded.verification_error == "report is not well-formed XML"


def test_the_scripted_executor_refuses_to_invent_a_result() -> None:
    """A stand-in that ran out of answers must fail loudly, not repeat one."""
    execute = sequential([CommandResult(exit_code=0)])

    async def drive() -> None:
        await execute(Path("/repo"), Command("test", ("pytest",)))
        await execute(Path("/repo"), Command("test", ("pytest",)))

    with pytest.raises(AssertionError, match="no scripted result"):
        run(drive())
