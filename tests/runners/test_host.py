"""The host runner, driving real subprocesses against a real git repository.

Everything here spawns a process. That is the point: the interesting questions —
does a timeout actually kill the child, does a budget cap fire before the money
is spent, does a control-plane credential reach the environment — are all
questions about a process, and a mocked ``create_subprocess_exec`` answers none
of them.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from clawdence.domain import (
    Budget,
    ContractKind,
    RunnerOutcome,
    RunnerResult,
    VerificationContract,
)
from clawdence.ports import PermanentError, StaticSecrets
from clawdence.runners import (
    VERDICT_PATH,
    HostRunner,
    LogLine,
    PlanDelivery,
    TokenPrice,
)
from clawdence.runners import process as process_module
from clawdence.runners import worktree as wt
from clawdence.runners.installed import WORK_DIR
from clawdence.runners.process import kill, kill_and_reap
from tests.harness.agent import FakeAgent, missing_command
from tests.harness.repos import FixtureRepo
from tests.ports.contract import RunnerContract
from tests.ports.factories import run
from tests.runners.conftest import RequestFactory, host_profile

CHANGED = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"

#: The default contract here is ``TEST_AFTER``, which means evidence. An agent
#: that claims success without any is held to ``TESTS_FAILED`` on purpose, so
#: the baseline agent has to produce counts — and every test that leaves them
#: out is saying something about the absence.
TESTS_PASSED = {"reporter": "pytest-json-report", "total": 4, "passed": 4}


def working(**verdict: object) -> FakeAgent:
    """An agent that edits a file, commits it, runs its tests, reports success.

    The ``commit`` is not decoration and was added in S6b. An agent that edits
    and never commits is §3.7a's dropped commit — a distinct outcome now — and a
    baseline fixture that skipped it would make every test here assert against
    the failure rather than against the happy path.
    """
    verdict.setdefault("tests", TESTS_PASSED)
    return (
        FakeAgent().say("working").write("app.py", CHANGED).commit().verdict(**verdict)  # type: ignore[arg-type]
    )


async def changed_paths(worktree: Path, base: str) -> frozenset[str]:
    """What the run's commits actually touched, asked of git.

    Used wherever the question is "did our own installed file reach the branch",
    which ``DiffStat``'s counts cannot answer — it reports how many files
    changed, not which.
    """
    raw = await wt.git(worktree, "diff", "--name-only", "-z", base, "HEAD", strip=False)
    return frozenset(record for record in raw.split("\0") if record)


# --------------------------------------------------------------------------- #
# The contract every adapter is held to
# --------------------------------------------------------------------------- #


class TestHostRunner(RunnerContract):
    """S5's promise, cashed: the first real adapter subclasses the obligations
    rather than getting a suite of its own."""

    @pytest.fixture
    def runner(self) -> HostRunner:
        return HostRunner(working().command())

    @pytest.fixture
    def make_request(self, request_for: RequestFactory) -> RequestFactory:
        return request_for


# --------------------------------------------------------------------------- #
# The happy path, and what comes back
# --------------------------------------------------------------------------- #


def test_a_diff_and_a_result_come_back(request_for: RequestFactory, repo: FixtureRepo) -> None:
    runner = HostRunner(working(summary="added mul").command())
    result = run(runner.dispatch(request_for()))

    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert result.diff is not None
    assert (result.diff.files_changed, result.diff.insertions) == (1, 4)
    assert result.tree_hash is not None and result.tree_hash != repo.head
    assert result.exit_code == 0
    assert "added mul" in (result.message or "")


def test_the_agents_work_lands_on_a_tree_the_result_can_name(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """The result binds evidence to a tree, so there has to be one to bind to —
    whether the agent committed its own work or the runner's safety commit did
    it for them."""
    runner = HostRunner(working().command())
    result = run(runner.dispatch(request_for()))
    assert repo.read("app.py") == CHANGED
    assert result.tree_hash is not None


def test_the_diff_is_re_derived_and_not_believed(request_for: RequestFactory) -> None:
    """The agent claims success and changes nothing. The number that decides
    whether a pull request opens does not come from the process being judged."""
    runner = HostRunner(FakeAgent().verdict(status="passed", tests=TESTS_PASSED).command())
    result = run(runner.dispatch(request_for()))
    assert result.outcome is RunnerOutcome.EMPTY_DIFF
    assert result.tree_hash is None


def test_test_evidence_and_notes_are_carried_out(request_for: RequestFactory) -> None:
    agent = working(
        tests={"reporter": "pytest-json-report", "total": 9, "passed": 9},
        discovery_notes=["the parser is generated"],
        unresolved_stubs=["error path for empty input"],
    )
    result = run(HostRunner(agent.command()).dispatch(request_for()))

    assert result.test_evidence is not None
    assert result.test_evidence.total == 9
    assert result.discovery_notes == ("the parser is generated",)
    assert result.unresolved_stubs == ("error path for empty input",)


def test_tokens_are_scraped_from_the_stream(request_for: RequestFactory) -> None:
    agent = FakeAgent().say("input tokens: 900 output tokens: 120")
    agent.write("app.py", CHANGED).verdict()
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert (result.usage.input_tokens, result.usage.output_tokens) == (900, 120)


def test_a_verdicts_own_usage_beats_the_scraper(request_for: RequestFactory) -> None:
    """A structured claim beats a regular expression run over prose."""
    agent = FakeAgent().say("tokens used: 5")
    agent.write("app.py", CHANGED).verdict(usage={"input_tokens": 1000, "output_tokens": 200})
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert result.usage.input_tokens == 1000


def test_cost_is_recorded_when_prices_are_configured(request_for: RequestFactory) -> None:
    agent = FakeAgent().say("input tokens: 1000000 output tokens: 1000000")
    agent.write("app.py", CHANGED).verdict()
    prices = TokenPrice(input_usd=Decimal("3"), output_usd=Decimal("15"))
    result = run(HostRunner(agent.command(prices=prices)).dispatch(request_for()))

    assert result.cost is not None
    assert result.cost.usd == Decimal("18")


def test_no_prices_means_no_cost_entry_rather_than_a_zero(request_for: RequestFactory) -> None:
    """A zero would read as "this run was free", which is a different claim
    from "nobody told us what it costs"."""
    result = run(HostRunner(working().command()).dispatch(request_for()))
    assert result.cost is None


# --------------------------------------------------------------------------- #
# Streamed output — v1's blackout
# --------------------------------------------------------------------------- #


def test_output_reaches_the_sink_line_by_line(request_for: RequestFactory) -> None:
    """v1 ran the agent with ``capture_output=True``, so forty minutes of
    silence looked the same whether or not anything was happening."""
    lines: list[LogLine] = []
    agent = FakeAgent().say("step one").say("step two").warn("a warning")
    agent.write("app.py", CHANGED).verdict()

    run(HostRunner(agent.command(), sink=lines.append).dispatch(request_for()))

    assert [line.text for line in lines if line.stream == "stdout"] == ["step one", "step two"]
    assert [line.text for line in lines if line.stream == "stderr"] == ["a warning"]


def test_stderr_is_not_persisted_by_default(request_for: RequestFactory) -> None:
    """The message is stored with the step result, and stderr is where a
    provider's echo of a rejected request ends up."""
    agent = FakeAgent().warn("Bearer sk-not-a-real-key").write("app.py", CHANGED).verdict()
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert "sk-not-a-real-key" not in (result.message or "")

    opted_in = run(
        HostRunner(agent.command(include_stderr_tail=True)).dispatch(request_for("docs"))
    )
    assert "sk-not-a-real-key" in (opted_in.message or "")


# --------------------------------------------------------------------------- #
# The failure taxonomy, end to end
# --------------------------------------------------------------------------- #


def test_a_non_zero_exit(request_for: RequestFactory) -> None:
    agent = FakeAgent().write("app.py", CHANGED).warn("boom").exit_with(3)
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.NON_ZERO_EXIT
    assert result.exit_code == 3
    assert result.tree_hash is None


def test_an_empty_diff(request_for: RequestFactory) -> None:
    agent = FakeAgent().say("nothing to do").verdict(tests=TESTS_PASSED)
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.EMPTY_DIFF


def test_tests_failed_keeps_the_tree_it_failed_on(request_for: RequestFactory) -> None:
    """That tree is exactly what the next attempt starts from."""
    agent = working(status="failed", tests={"reporter": "junit-xml", "total": 4, "failed": 1})
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.TESTS_FAILED
    assert result.tree_hash is not None


def test_a_blocked_agent_is_not_a_failing_test(request_for: RequestFactory) -> None:
    agent = working(status="blocked", summary="postgres is not running")
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.BLOCKED


def test_a_timeout_from_the_resource_cap(request_for: RequestFactory) -> None:
    agent = FakeAgent().write("app.py", CHANGED).sleep(30)
    result = run(HostRunner(agent.command()).dispatch(request_for(wall_clock_seconds=0.4)))
    assert result.outcome is RunnerOutcome.TIMED_OUT


def test_a_wall_clock_budget_is_a_budget_not_a_timeout(request_for: RequestFactory) -> None:
    """Handled differently: one is worth retrying with a larger budget, the
    other is worth asking why a twenty-minute job took an hour."""
    agent = FakeAgent().write("app.py", CHANGED).sleep(30)
    request = request_for(budget=Budget(max_wall_clock_seconds=0.4))
    result = run(HostRunner(agent.command()).dispatch(request))
    assert result.outcome is RunnerOutcome.BUDGET_EXCEEDED


def test_the_smaller_of_the_two_limits_wins(request_for: RequestFactory) -> None:
    agent = FakeAgent().write("app.py", CHANGED).sleep(30)
    request = request_for(budget=Budget(max_wall_clock_seconds=30), wall_clock_seconds=0.4)
    result = run(HostRunner(agent.command()).dispatch(request))
    assert result.outcome is RunnerOutcome.TIMED_OUT


def test_a_token_budget_stops_the_run_while_it_is_running(request_for: RequestFactory) -> None:
    """A cap checked after the process exits is an epitaph. The agent reports
    its spend and then settles in for thirty seconds; it does not get them."""
    agent = FakeAgent().tokens(50_000).sleep(30).write("app.py", CHANGED)
    request = request_for(budget=Budget(max_tokens=1000))
    result = run(HostRunner(agent.command()).dispatch(request))

    assert result.outcome is RunnerOutcome.BUDGET_EXCEEDED
    assert (result.finished_at - result.started_at).total_seconds() < 20


def test_a_dollar_budget_stops_the_run(request_for: RequestFactory) -> None:
    agent = FakeAgent().say("input tokens: 2000000").sleep(30).write("app.py", CHANGED)
    prices = TokenPrice(input_usd=Decimal("3"), output_usd=Decimal("15"))
    request = request_for(budget=Budget(max_usd=Decimal("1")))
    result = run(HostRunner(agent.command(prices=prices)).dispatch(request))
    assert result.outcome is RunnerOutcome.BUDGET_EXCEEDED


def test_a_dollar_budget_with_no_prices_is_refused_rather_than_ignored(
    request_for: RequestFactory,
) -> None:
    """A runner that accepted a cap it had no way to evaluate would report a
    budget as enforced while enforcing nothing."""
    runner = HostRunner(working().command())
    with pytest.raises(PermanentError) as caught:
        run(runner.dispatch(request_for(budget=Budget(max_usd=Decimal("5")))))
    assert caught.value.kind == "no-token-prices"


def test_a_sigkill_reads_as_an_oom_kill(request_for: RequestFactory) -> None:
    """What the kernel's OOM killer does, without needing to exhaust the
    machine's memory to find out."""
    agent = FakeAgent().write("app.py", CHANGED).sigkill()
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.OOM_KILLED


def test_a_full_disk(request_for: RequestFactory) -> None:
    agent = FakeAgent().warn("fatal: write error: No space left on device").exit_with(1)
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.DISK_FULL


def test_a_missing_binary_is_a_startup_failure(request_for: RequestFactory) -> None:
    """Nothing ran, so nothing about the repository is implied — which is why
    this is not ``non-zero-exit``."""
    result = run(HostRunner(missing_command()).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.STARTUP_FAILED
    assert result.exit_code is None


def test_a_worktree_that_is_not_a_repository_is_a_startup_failure(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    plain = repo.path.parent / "plain"
    plain.mkdir()
    result = run(HostRunner(working().command()).dispatch(request_for(worktree=plain)))
    assert result.outcome is RunnerOutcome.STARTUP_FAILED
    assert "not a git repository" in (result.message or "")


def test_an_unknown_base_commit_is_found_before_the_agent_runs(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """Finding this out after twenty minutes of agent time is finding it out
    too late."""
    request = request_for().model_copy(update={"base_commit": "0" * 40})
    result = run(HostRunner(working().command()).dispatch(request))
    assert result.outcome is RunnerOutcome.STARTUP_FAILED


# --------------------------------------------------------------------------- #
# §3.7a — the failures the exit status cannot see, end to end (S6b)
# --------------------------------------------------------------------------- #


def test_a_provider_error_is_reported_instead_of_a_false_success(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """**The test that matters most in S6b.**

    Everything about this run looks like a success from outside: exit 0, a
    committed diff, a verdict claiming passing tests. What actually happened is
    on the event stream — the last turn carried a provider failure — and until
    S6b nothing read it. A pull request would have opened and the workflow would
    have advanced, which is a false success, and a false success is a different
    severity of bug from a misclassified failure.
    """
    agent = working().turn("starting").provider_error("your credit balance is too low").exit_with(0)
    result = run(HostRunner(agent.command()).dispatch(request_for()))

    assert result.outcome is RunnerOutcome.PROVIDER_ERROR
    assert result.exit_code == 0
    assert "credit balance" in (result.message or "")
    # No tree hash, because nothing downstream should be able to merge this.
    assert result.tree_hash is None


def test_an_error_the_agent_recovered_from_does_not_fail_the_run(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """The other half of the same rule, and the one that stops this being a
    false-failure machine: a rate limit the CLI waited out and carried on past
    is not a terminal turn."""
    agent = (
        FakeAgent()
        .provider_error("rate limited, retrying")
        .turn("carrying on")
        .say("working")
        .write("app.py", CHANGED)
        .commit()
        .verdict(tests=TESTS_PASSED)
    )
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.SUCCEEDED


def test_a_rejected_credential_is_not_a_startup_failure(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """Events flowed — an init frame, a banner — and not one model turn did.
    ``startup-failed`` is also what a missing image produces, and the two want
    opposite repairs: one is a key, the other is a registry."""
    agent = (
        FakeAgent()
        .event(type="system", subtype="init", model="some-model")
        .event(type="error", error={"message": "invalid x-api-key"})
        .exit_with(1)
    )
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.NO_MODEL_RESPONSE


def test_an_agent_that_edits_and_never_commits_is_a_dropped_commit(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """§3.7a's second failure, and the one this codebase had to work to see: the
    runner's own safety commit means there *is* a diff, so the exit status, the
    tree and the diff stat all look like a run that worked.

    The work is still preserved — that is what the safety commit is for, and the
    tree hash comes back so a person can go and look at what was nearly done.
    What says the agent never finished is ``commits_ahead == 0``.
    """
    agent = FakeAgent().say("editing").write("app.py", CHANGED).verdict(tests=TESTS_PASSED)
    result = run(HostRunner(agent.command()).dispatch(request_for()))

    assert result.outcome is RunnerOutcome.DROPPED_COMMIT
    assert result.commits_ahead == 0
    assert result.dirty is True
    assert "app.py" in result.dirty_paths
    assert result.tree_hash is not None
    assert repo.read("app.py") == CHANGED


def test_an_agent_that_changes_nothing_is_a_no_op_and_not_a_dropped_commit(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """The clean half of the split. The agent read the plan and concluded there
    was nothing to do, which is a conclusion rather than a failure to finish."""
    agent = FakeAgent().say("nothing to do here").verdict(tests=TESTS_PASSED)
    result = run(HostRunner(agent.command()).dispatch(request_for()))

    assert result.outcome is RunnerOutcome.EMPTY_DIFF
    assert result.dirty is False
    assert result.dirty_paths == ()
    assert result.commits_ahead == 0


def test_dirt_that_is_only_the_runners_own_installed_files_is_still_a_no_op(
    request_for: RequestFactory, repos: object, tmp_path: Path
) -> None:
    """The third case, and the reason the split cannot be a one-line
    ``is_dirty`` check — **including when the repository tracks a file at the
    path we install to**, which is where ``$GIT_DIR/info/exclude`` stops
    working. The agent did nothing; the only thing making that tree dirty is our
    own conventions file, and calling that a dropped commit would fail every
    run against every repository that keeps one.
    """
    source = tmp_path / "AGENTS.md"
    source.write_text("ours\n")
    owned = repos(extra_files={"AGENTS.md": "theirs\n", "app.py": "x = 1\n"})  # type: ignore[operator]

    profile = host_profile(agents_md_path=str(source))
    request = request_for(profile=profile, worktree=owned.path).model_copy(
        update={"base_commit": owned.head}
    )
    agent = FakeAgent().say("nothing to do here").verdict(tests=TESTS_PASSED)
    result = run(HostRunner(agent.command()).dispatch(request))

    assert result.outcome is RunnerOutcome.EMPTY_DIFF
    assert result.dirty is False
    assert result.dirty_paths == ()
    assert owned.read("AGENTS.md") == "theirs\n"


def test_the_result_carries_the_artifacts_rather_than_a_path_to_go_and_look_at(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """§3.10. Everything needed to decide an outcome is on the payload, taken at
    the moment the work was collected — not derived afterwards from a directory
    the control plane may not be able to reach."""
    agent = (
        FakeAgent()
        .write("app.py", CHANGED)
        .commit("first")
        .write("extra.py", "y = 2\n")
        .commit("second")
        .write("scratch.txt", "left behind\n")
        .verdict(tests=TESTS_PASSED)
    )
    result = run(HostRunner(agent.command()).dispatch(request_for()))

    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert result.commits_ahead == 2
    assert result.dirty is True
    assert result.dirty_paths == ("scratch.txt",)


# --------------------------------------------------------------------------- #
# Refusals — requests that cannot honestly be run
# --------------------------------------------------------------------------- #


def test_a_container_profile_is_refused_rather_than_downgraded(
    request_for: RequestFactory,
) -> None:
    """The failure being guarded against is a repository configured for
    ``container`` quietly executing on the host after somebody wired the wrong
    adapter at startup."""
    runner = HostRunner(working().command())
    with pytest.raises(PermanentError) as caught:
        run(runner.dispatch(request_for(profile=host_profile(isolation_tier="container"))))
    assert caught.value.kind == "isolation-tier-mismatch"
    assert caught.value.retryable is False


def test_a_missing_worktree_is_refused(request_for: RequestFactory, repo: FixtureRepo) -> None:
    runner = HostRunner(working().command())
    with pytest.raises(PermanentError) as caught:
        run(runner.dispatch(request_for(worktree=repo.path / "nowhere")))
    assert caught.value.kind == "worktree-missing"


# --------------------------------------------------------------------------- #
# The trust boundary (§3.1, threat model T3)
# --------------------------------------------------------------------------- #


def test_no_control_plane_credential_reaches_the_runner(
    request_for: RequestFactory, repo: FixtureRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The automated assertion the threat model says T3 needs, for this tier.

    The control plane's own environment holds every credential in the system.
    The child gets an allowlist, so this holds by construction — and this is the
    test that says so out loud, rather than a comment claiming it.
    """
    for name, value in {
        "SLACK_BOT_TOKEN": "xoxb-not-real",
        "JIRA_API_TOKEN": "jira-not-real",
        "GITHUB_TOKEN": "ghp-not-real",
        "AWS_SECRET_ACCESS_KEY": "aws-not-real",
        "OPENAI_API_KEY": "sk-not-real",
    }.items():
        monkeypatch.setenv(name, value)

    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    run(HostRunner(agent.command()).dispatch(request_for()))

    seen = repo.read("env.txt")
    for leaked in ("xoxb-not-real", "jira-not-real", "ghp-not-real", "aws-not-real", "sk-not-real"):
        assert leaked not in seen
    assert "PATH=" in seen


def test_passing_a_control_plane_credential_in_is_refused(request_for: RequestFactory) -> None:
    """The allowlist stops inheritance; this stops a caller doing it on purpose,
    which is the mistake that reads as reasonable in a diff."""
    agent = working().command(extra_env={"GITHUB_TOKEN": "ghp-not-real"})
    with pytest.raises(PermanentError) as caught:
        run(HostRunner(agent).dispatch(request_for()))
    assert caught.value.kind == "control-plane-secret-in-runner-env"
    assert "GITHUB_TOKEN" in caught.value.message


def test_the_scoped_llm_key_is_resolved_by_name_and_passed(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """§3.1 gives the runner a budgeted key of its own. It is resolved as late
    as possible, by the one component allowed to."""
    secrets = StaticSecrets({"runner-llm-key": "sk-scoped"})
    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    command = agent.command(secret_env={"MODEL_API_KEY": "runner-llm-key"})

    run(HostRunner(command, secrets=secrets).dispatch(request_for()))
    assert "MODEL_API_KEY=sk-scoped" in repo.read("env.txt")


def test_an_mcp_token_is_injected_per_run(request_for: RequestFactory, repo: FixtureRepo) -> None:
    """The honest exception to "the runner holds no secrets": a repo that
    configures MCP hands the runner a credential."""
    secrets = StaticSecrets({"DOCS_MCP_TOKEN": "mcp-scoped"})
    profile = host_profile(
        mcp_servers=[
            {"name": "docs", "url": "https://mcp.invalid", "bearer_token_env_var": "DOCS_MCP_TOKEN"}
        ]
    )
    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()

    run(HostRunner(agent.command(), secrets=secrets).dispatch(request_for(profile=profile)))
    assert "DOCS_MCP_TOKEN=mcp-scoped" in repo.read("env.txt")


def test_the_runner_is_told_who_to_commit_as(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """An agent that commits its own work with no identity configured fails
    outright on a machine with no global git config, which is every container."""
    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    run(HostRunner(agent.command()).dispatch(request_for()))
    assert "GIT_AUTHOR_EMAIL=runner@clawdence.invalid" in repo.read("env.txt")


# --------------------------------------------------------------------------- #
# What the runner installs, and takes back
# --------------------------------------------------------------------------- #


def test_the_plan_reaches_the_agent_on_stdin(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    agent = FakeAgent().read_stdin("plan.txt").write("app.py", CHANGED).verdict()
    run(HostRunner(agent.command()).dispatch(request_for()))
    assert "make add() handle strings" in repo.read("plan.txt")


def test_the_plan_can_be_delivered_as_a_file(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    agent = working()
    run(HostRunner(agent.command(delivery=PlanDelivery.FILE)).dispatch(request_for()))
    # Written, used, and taken back — the worktree is left as it was found.
    assert not (repo.path / WORK_DIR / "plan.md").exists()


def test_nothing_the_runner_installs_reaches_the_diff(
    request_for: RequestFactory, repo: FixtureRepo, tmp_path: Path
) -> None:
    """A conventions file and a verdict landing in somebody's pull request is
    changes nobody asked for, appearing under our name."""
    conventions = tmp_path / "AGENTS.md"
    conventions.write_text("# House style\nUse tabs, obviously.\n")
    profile = host_profile(agents_md_path=str(conventions))

    agent = working()
    result = run(HostRunner(agent.command()).dispatch(request_for(profile=profile)))

    assert result.diff is not None
    assert result.diff.files_changed == 1
    assert not (repo.path / "AGENTS.md").exists()
    assert (repo.path / VERDICT_PATH).exists()


def test_a_conventions_file_the_repository_tracks_is_installed_then_put_back(
    request_for: RequestFactory, repos: object, tmp_path: Path
) -> None:
    """§3.9's repair, and the case that breaks a naive probe.

    ``$GIT_DIR/info/exclude`` hides everything the runner installs, but it has
    no effect on a path the repository already **tracks** — and a repository
    that keeps its own ``AGENTS.md`` is the common case here. S6 avoided that by
    not installing at all, which quietly ignored the conventions file an
    operator had configured. S6b installs, records the bytes, and puts the path
    back afterwards wherever it still holds them.
    """
    source = tmp_path / "AGENTS.md"
    source.write_text("ours\n")
    owned = repos(extra_files={"AGENTS.md": "theirs\n", "app.py": "x = 1\n"})  # type: ignore[operator]

    profile = host_profile(agents_md_path=str(source))
    request = request_for(profile=profile, worktree=owned.path).model_copy(
        update={"base_commit": owned.head}
    )
    # The agent reads the conventions file it was given and copies it out, which
    # is how the test sees that our version was the one in the tree while the
    # agent ran — the whole point of installing it.
    agent = (
        FakeAgent()
        .say("working")
        .copy("AGENTS.md", "seen.txt")
        .write("app.py", CHANGED)
        .commit()
        .verdict(tests=TESTS_PASSED)
    )
    result = run(HostRunner(agent.command()).dispatch(request))

    assert owned.read("seen.txt") == "ours\n"
    assert owned.read("AGENTS.md") == "theirs\n"
    # And it never reached the branch: the file is unchanged against the base,
    # so the diff is `app.py` and `seen.txt` and nothing else.
    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert "AGENTS.md" not in run(changed_paths(owned.path, request.base_commit))


def test_an_agents_own_edit_to_the_conventions_file_survives(
    request_for: RequestFactory, repos: object, tmp_path: Path
) -> None:
    """The byte comparison is what separates our copy from the agent's work.

    Reverting the path unconditionally would be the same bug in the other
    direction: an agent told to update the conventions file would watch its
    change be deleted by the cleanup that runs after it.
    """
    source = tmp_path / "AGENTS.md"
    source.write_text("ours\n")
    owned = repos(extra_files={"AGENTS.md": "theirs\n", "app.py": "x = 1\n"})  # type: ignore[operator]

    profile = host_profile(agents_md_path=str(source))
    request = request_for(profile=profile, worktree=owned.path).model_copy(
        update={"base_commit": owned.head}
    )
    agent = (
        FakeAgent()
        .write("AGENTS.md", "the agent rewrote this\n")
        .commit()
        .verdict(tests=TESTS_PASSED)
    )
    result = run(HostRunner(agent.command()).dispatch(request))

    assert owned.read("AGENTS.md") == "the agent rewrote this\n"
    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert "AGENTS.md" in run(changed_paths(owned.path, request.base_commit))


def test_a_conventions_path_that_does_not_exist_is_skipped(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """A profile pointing at a file somebody deleted is a configuration problem,
    not a reason to fail a run that has nothing to do with it."""
    profile = host_profile(agents_md_path="/nowhere/AGENTS.md")
    result = run(HostRunner(working().command()).dispatch(request_for(profile=profile)))
    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert not (repo.path / "AGENTS.md").exists()


def test_a_conventions_file_that_cannot_be_installed_does_not_fail_the_run(
    request_for: RequestFactory, repo: FixtureRepo, tmp_path: Path
) -> None:
    """Same reasoning one step later: the path resolves and the write still
    fails, because something is already at the destination that is not a file.
    The agent works without its conventions file, slightly worse — which is a
    much better outcome than refusing to run at all."""
    source = tmp_path / "AGENTS.md"
    source.write_text("ours\n")
    (repo.path / "AGENTS.md").mkdir()

    profile = host_profile(agents_md_path=str(source))
    result = run(HostRunner(working().command()).dispatch(request_for(profile=profile)))
    assert result.outcome is RunnerOutcome.SUCCEEDED


def test_a_worktree_git_cannot_read_afterwards_is_a_startup_failure(
    request_for: RequestFactory,
) -> None:
    """Reported rather than invented as an empty diff, which would look exactly
    like an agent that did nothing."""
    agent = FakeAgent().write("app.py", CHANGED).write(".git/index.lock", "held")
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.STARTUP_FAILED
    assert "could not be read" in (result.message or "")


def test_the_plan_can_be_delivered_as_an_argument(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """Three delivery modes because the CLIs anybody will wire this to each want
    a different one, and guessing wrong runs the agent with an empty prompt."""
    agent = (
        FakeAgent()
        .dump_env("env.txt")
        .write("app.py", CHANGED)
        .commit()
        .verdict(tests=TESTS_PASSED)
    )
    result = run(HostRunner(agent.command(delivery=PlanDelivery.ARGUMENT)).dispatch(request_for()))
    assert result.outcome is RunnerOutcome.SUCCEEDED


def test_an_mcp_server_without_a_token_needs_no_secret(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """Not every MCP server is authenticated, and a runner that insisted on a
    credential for one that needs none would refuse to start."""
    profile = host_profile(
        mcp_servers=[
            {"name": "open", "url": "https://mcp.invalid"},
            {"name": "docs", "url": "https://docs.invalid", "bearer_token_env_var": "ABSENT_TOKEN"},
        ]
    )
    agent = (
        FakeAgent()
        .dump_env("env.txt")
        .write("app.py", CHANGED)
        .commit()
        .verdict(tests=TESTS_PASSED)
    )
    result = run(HostRunner(agent.command()).dispatch(request_for(profile=profile)))

    # An unconfigured token is left out rather than passed as an empty string:
    # an empty credential fails at the far end with an authentication error
    # nobody can trace back to a missing secret.
    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert "ABSENT_TOKEN" not in repo.read("env.txt")


def test_a_previous_attempts_verdict_does_not_answer_this_one(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """Otherwise a second attempt that crashes before writing anything inherits
    the first attempt's verdict and reports its result."""
    stale = repo.path / VERDICT_PATH
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(json.dumps({"status": "passed", "summary": "from last time"}))

    agent = FakeAgent().write("app.py", CHANGED).exit_with(1)
    result = run(HostRunner(agent.command()).dispatch(request_for()))
    assert "from last time" not in (result.message or "")


def test_a_malformed_verdict_does_not_fail_the_dispatch(request_for: RequestFactory) -> None:
    """It is an absent verdict plus a complaint. What an absence means is the
    contract's business."""
    agent = FakeAgent().write("app.py", CHANGED).commit().verdict(raw="{not json")
    contract = VerificationContract(kind=ContractKind.BUILD_ONLY)
    result = run(HostRunner(agent.command()).dispatch(request_for(contract=contract)))
    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert result.test_evidence is None


# --------------------------------------------------------------------------- #
# Idempotency and cancellation
# --------------------------------------------------------------------------- #


def test_a_redelivered_dispatch_does_not_run_the_agent_twice(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """Two agents editing one worktree is not a race that produces a bad diff;
    it is a race that produces a tree neither of them intended."""
    agent = FakeAgent().append("runs.txt", "x").write("app.py", CHANGED).verdict(tests=TESTS_PASSED)
    runner = HostRunner(agent.command())
    request = request_for()

    first = run(runner.dispatch(request))
    second = run(runner.dispatch(request))

    assert second == first
    assert (repo.path / "runs.txt").read_text() == "x"


def test_a_second_attempt_is_new_work(request_for: RequestFactory, repo: FixtureRepo) -> None:
    """``attempt`` is in the key, so a retry is genuinely a second run of the
    agent rather than the first one's answer served again."""
    agent = FakeAgent().append("runs.txt", "x").write("app.py", CHANGED).verdict(tests=TESTS_PASSED)
    runner = HostRunner(agent.command())

    run(runner.dispatch(request_for(attempt=1)))
    run(runner.dispatch(request_for(attempt=2)))
    assert (repo.path / "runs.txt").read_text() == "xx"


def test_a_concurrent_redelivery_joins_the_run_in_flight(request_for: RequestFactory) -> None:
    """The watchdog recovering a step whose process is still alive is how a
    redelivery happens in practice, and it happens *during* the run."""
    agent = FakeAgent().sleep(0.3).write("app.py", CHANGED).verdict()
    runner = HostRunner(agent.command())
    request = request_for()

    async def both() -> tuple[RunnerResult, RunnerResult]:
        first = asyncio.ensure_future(runner.dispatch(request))
        second = asyncio.ensure_future(runner.dispatch(request))
        return await first, await second

    first, second = run(both())
    assert first == second


def test_a_redelivery_giving_up_does_not_stop_the_original(
    request_for: RequestFactory, repo: FixtureRepo
) -> None:
    """The dispatch is shielded, so the waiter that walks away is the only thing
    that stops. Otherwise a watchdog's redelivery timing out would kill the run
    it was checking on."""
    agent = FakeAgent().sleep(0.5).write("app.py", CHANGED).commit().verdict(tests=TESTS_PASSED)
    runner = HostRunner(agent.command())
    request = request_for()

    async def abandon_the_second() -> RunnerResult:
        original = asyncio.ensure_future(runner.dispatch(request))
        await asyncio.sleep(0.1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(runner.dispatch(request), timeout=0.05)
        return await original

    result = run(abandon_the_second())
    assert result.outcome is RunnerOutcome.SUCCEEDED


def test_cancelling_a_run_gives_a_cancelled_result(request_for: RequestFactory) -> None:
    """An operator stopping a run should leave a record of what happened, not an
    exception some layer above renders as "failed"."""
    agent = FakeAgent().sleep(30)
    runner = HostRunner(agent.command())
    request = request_for()

    async def start_then_stop() -> RunnerResult:
        dispatch = asyncio.ensure_future(runner.dispatch(request))
        await asyncio.sleep(0.2)
        assert await runner.cancel(request) is True
        return await dispatch

    result = run(start_then_stop())
    assert result.outcome is RunnerOutcome.CANCELLED
    assert result.tree_hash is None


def test_cancelling_something_that_never_started_is_false(request_for: RequestFactory) -> None:
    assert run(HostRunner(working().command()).cancel(request_for())) is False


def test_an_engine_timeout_still_propagates(request_for: RequestFactory) -> None:
    """The executor's contract is one path out for "did not finish". A step
    timeout is not an operator pressing stop, and must not be turned into a
    result that says it was."""
    agent = FakeAgent().sleep(30)
    runner = HostRunner(agent.command())

    async def time_out() -> None:
        await asyncio.wait_for(runner.dispatch(request_for()), timeout=0.3)

    with pytest.raises(TimeoutError):
        run(time_out())


def test_a_cancelled_run_leaves_no_child_behind(request_for: RequestFactory) -> None:
    """Without the kill the process outlives the run — v1's stale-spawn bug.
    Without the reap it is a zombie whose pipes stay open, and the suite reports
    a warning from a thread nobody is looking at."""
    marker = "sentinel-for-the-reaper"
    agent = FakeAgent().say(marker).sleep(30)
    runner = HostRunner(agent.command())

    async def time_out() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(runner.dispatch(request_for()), timeout=0.4)

    run(time_out())
    assert marker not in _process_table()


def test_a_process_the_agent_left_behind_does_not_hang_the_run(
    request_for: RequestFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that starts a dev server and is then killed for a timeout.

    The grandchild holds the same stdout, so the pipe does not close and waiting
    for the transport waits for *it* — the run produces no result, no failure and
    no timeout, and there is nothing for the watchdog to recover because the
    dispatch is still politely waiting. Bounding the reap is what turns that into
    a ``TIMED_OUT`` result at the moment the timeout was supposed to fire.
    """
    monkeypatch.setattr(process_module, "REAP_TIMEOUT_SECONDS", 0.5)
    agent = FakeAgent().spawn(6).sleep(30)

    started = time.monotonic()
    result = run(HostRunner(agent.command()).dispatch(request_for(wall_clock_seconds=0.5)))
    elapsed = time.monotonic() - started

    assert result.outcome is RunnerOutcome.TIMED_OUT
    assert elapsed < 5, "the run waited for a process it had already given up on"


def test_reaping_a_process_that_already_exited_does_nothing() -> None:
    """The race the watchdog creates: a step is declared overdue at the moment
    it finishes. Killing what is already gone must not raise."""

    async def already_done() -> int | None:
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
        await process.wait()
        kill(process)
        await kill_and_reap(process)
        return process.returncode

    assert run(already_done()) == 0


def _process_table() -> str:
    import subprocess

    return subprocess.run(
        ["/bin/ps", "-Ao", "args"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
