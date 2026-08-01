"""The container tier, against an engine that records what it was asked for.

Two kinds of claim live here and it is worth being clear which is which.

**Construction.** What the runner asks the engine for — one mount, dropped
capabilities, a pid ceiling, a credential passed by name. These are the security
posture of the tier, and they are fully testable offline because they are argv.

**Pipeline.** That the S6 contract still holds when the agent is on the far side
of an engine client: the plan arrives, tokens are scraped, the budget fires, the
tree is re-derived. The fake engine really executes the command it is given, in
the workdir it is given, with exactly the environment the ``--env`` flags
describe — so these are not re-tests of S6 with a longer argv, they are the same
questions asked through a second process boundary.

What is **not** here: that a dropped capability is actually dropped, or that a
memory cap actually kills. Nothing in this file could tell the difference between
a flag that works and a flag with a typo in it. That is
``test_container_live.py``, which needs a daemon and says so.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from clawdence.domain import Budget, ResourceCaps, RunnerOutcome, RunnerRequest
from clawdence.ports import PermanentError, StaticSecrets, TokenPrice
from clawdence.runners import (
    LABEL_NAMESPACE,
    Completion,
    ContainerEngine,
    ContainerRunner,
    ContainerSpec,
    EngineError,
    Installed,
    Mount,
    PlanDelivery,
    container_name,
)
from clawdence.runners import process as process_module
from clawdence.vcs.store import mirror_name
from tests.harness.agent import FakeAgent
from tests.harness.engine import FakeEngine, container_environment, passthrough_names
from tests.harness.repos import FixtureRepo
from tests.ports.contract import RunnerContract
from tests.ports.factories import run
from tests.runners.conftest import (
    PINNED_IMAGE,
    RequestFactory,
    container_profile,
    host_profile,
)

CHANGED = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TESTS_PASSED = {"reporter": "pytest-json-report", "total": 4, "passed": 4}


def working(**verdict: object) -> FakeAgent:
    verdict.setdefault("tests", TESTS_PASSED)
    return (
        FakeAgent().say("working").write("app.py", CHANGED).commit().verdict(**verdict)  # type: ignore[arg-type]
    )


def runner_for(
    fake_engine: FakeEngine,
    agent: FakeAgent | None = None,
    **kwargs: object,
) -> ContainerRunner:
    command = (agent or working()).command(
        **{name: value for name, value in kwargs.items() if name in _COMMAND_FIELDS}  # type: ignore[arg-type]
    )
    options = {name: value for name, value in kwargs.items() if name not in _COMMAND_FIELDS}
    return ContainerRunner(
        command,
        image=str(options.pop("image", PINNED_IMAGE)),
        engine=fake_engine.engine,
        **options,  # type: ignore[arg-type]
    )


_COMMAND_FIELDS = frozenset(
    {"delivery", "conventions_filename", "extra_env", "secret_env", "prices", "accumulation"}
)


async def _until(ready: Callable[[], bool], *, limit: float = 20.0) -> None:
    """Wait for something the agent does, rather than for a duration.

    Two processes have to start before the agent runs anything — the engine
    client and then the agent itself — and how long that takes depends on what
    else the suite is doing. A fixed sleep that is long enough on an idle
    machine is the flake that only appears in a full run.
    """
    deadline = time.monotonic() + limit
    while not ready():
        assert time.monotonic() < deadline, "the agent never started"
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- #
# The contract every adapter is held to
# --------------------------------------------------------------------------- #


class TestContainerRunner(RunnerContract):
    """The second real adapter, subclassing the obligations rather than
    restating them. S6 said an adapter that needed them weakened would be one
    the fakes are the only things meeting; this is that claim being cashed for a
    tier S5 had not seen."""

    @pytest.fixture
    def runner(self, fake_engine: FakeEngine) -> ContainerRunner:
        return runner_for(fake_engine)

    @pytest.fixture
    def make_request(self, request_for: RequestFactory) -> RequestFactory:
        def build(*args: object, **kwargs: object) -> object:
            kwargs.setdefault("profile", container_profile())
            return request_for(*args, **kwargs)

        return build  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# The pipeline still works with an engine in the middle
# --------------------------------------------------------------------------- #


def test_a_diff_and_a_result_come_back(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    runner = runner_for(fake_engine, working(summary="added mul"))
    result = run(runner.dispatch(request_for(profile=container_profile())))

    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert result.diff is not None
    assert (result.diff.files_changed, result.diff.insertions) == (1, 4)
    assert result.tree_hash is not None and result.tree_hash != repo.head
    assert "added mul" in (result.message or "")


def test_the_plan_reaches_the_agent_through_the_engine(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    """Two boundaries now: our stdin to the client's, the client's to the
    container's. ``--interactive`` is what makes the second one exist."""
    agent = FakeAgent().read_stdin("plan.txt").write("app.py", CHANGED).verdict(tests=TESTS_PASSED)
    run(runner_for(fake_engine, agent).dispatch(request_for(profile=container_profile())))

    assert "make add() handle strings" in repo.read("plan.txt")
    assert fake_engine.only_run().has("--interactive")


def test_stdin_is_not_attached_when_the_plan_goes_elsewhere(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """An attached stdin nothing will write to is a CLI waiting forever."""
    runner = runner_for(fake_engine, delivery=PlanDelivery.FILE)
    run(runner.dispatch(request_for(profile=container_profile())))
    assert not fake_engine.only_run().has("--interactive")


def test_tokens_are_scraped_from_the_stream(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """The client's stdout is the agent's stdout — foreground, not detached.
    v1's blackout under ``capture_output=True`` was this going wrong."""
    agent = FakeAgent().say("input tokens: 900 output tokens: 120")
    agent.write("app.py", CHANGED).verdict(tests=TESTS_PASSED)
    result = run(runner_for(fake_engine, agent).dispatch(request_for(profile=container_profile())))
    assert (result.usage.input_tokens, result.usage.output_tokens) == (900, 120)


def test_a_token_budget_stops_the_run_while_it_is_running(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """A cap checked after the process exits is an epitaph — and through an
    engine there is one more process between us and the thing to stop."""
    agent = FakeAgent().tokens(9_000).sleep(30).write("app.py", CHANGED).verdict()
    result = run(
        runner_for(fake_engine, agent).dispatch(
            request_for(profile=container_profile(), budget=Budget(max_tokens=100))
        )
    )
    assert result.outcome is RunnerOutcome.BUDGET_EXCEEDED


def test_a_dollar_budget_with_no_prices_is_refused_rather_than_ignored(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    with pytest.raises(PermanentError) as caught:
        run(
            runner_for(fake_engine).dispatch(
                request_for(profile=container_profile(), budget=Budget(max_usd=Decimal("1")))
            )
        )
    assert caught.value.kind == "no-token-prices"


def test_a_timeout_kills_the_client_and_removes_the_container(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """Killing the client is not enough on its own — a detached container would
    outlive it — so teardown is what actually ends the work."""
    agent = FakeAgent().sleep(30)
    result = run(
        runner_for(fake_engine, agent).dispatch(
            request_for(profile=container_profile(), wall_clock_seconds=0.3)
        )
    )
    assert result.outcome is RunnerOutcome.TIMED_OUT
    assert fake_engine.removals()


def test_an_empty_diff_is_still_an_empty_diff(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    agent = FakeAgent().verdict(status="passed", tests=TESTS_PASSED)
    result = run(runner_for(fake_engine, agent).dispatch(request_for(profile=container_profile())))
    assert result.outcome is RunnerOutcome.EMPTY_DIFF


# --------------------------------------------------------------------------- #
# What the container actually gets: §3.1, one mount
# --------------------------------------------------------------------------- #


def test_the_worktree_is_the_only_mount(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    """§3.1's "all repos ✅ / one worktree ❌", as the line that implements it.
    The other repositories are not permission-checked; they are absent."""
    run(runner_for(fake_engine).dispatch(request_for(profile=container_profile())))
    mounts = fake_engine.only_run().values("--mount")
    assert mounts == (f"type=bind,source={repo.path},target={repo.path}",)


def test_the_worktree_keeps_its_path_inside(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    """Path identity (§3.3). Testcontainers hands *host* paths to the daemon
    when it mounts volumes for siblings, so a differing path breaks S8 silently
    — and silently is the problem, not breaks."""
    run(runner_for(fake_engine).dispatch(request_for(profile=container_profile())))
    call = fake_engine.only_run()
    assert call.value("--workdir") == str(repo.path)
    assert f"target={repo.path}" in call.values("--mount")[0]


def test_the_mirror_is_mounted_when_this_deployment_says_where_it_lives(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine, tmp_path: Path
) -> None:
    """Without this mount, ``git commit`` has nowhere to write the ``HEAD``,
    index and ref a linked worktree keeps in its mirror's own ``.git`` — it
    fails inside the container exactly as it would inside any sandbox that
    takes "stay inside the worktree" literally."""
    profile = container_profile()
    mirror = tmp_path / mirror_name(profile.id)
    mirror.mkdir()
    run(runner_for(fake_engine, repo_store=tmp_path).dispatch(request_for(profile=profile)))
    mounts = fake_engine.only_run().values("--mount")
    assert f"type=bind,source={mirror},target={mirror}" in mounts


def test_no_docker_socket_reaches_the_container(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """Socket mode is not weaker isolation, it is none with extra steps: a
    process that can reach the host daemon can ``-v /:/host``. It has its own
    tier value so it cannot be arrived at by editing a flag."""
    run(runner_for(fake_engine).dispatch(request_for(profile=container_profile())))
    assert not any("docker.sock" in token for token in fake_engine.only_run().argv)


def test_the_restrictive_flags_are_all_there(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    run(runner_for(fake_engine).dispatch(request_for(profile=container_profile())))
    call = fake_engine.only_run()
    assert call.value("--cap-drop") == "ALL"
    assert call.value("--security-opt") == "no-new-privileges"
    assert call.has("--read-only")
    assert call.has("--init")
    assert call.value("--user") is not None
    assert "/tmp:" in (call.value("--tmpfs") or "")  # noqa: S108 - inside the container


def test_the_container_is_not_removed_on_exit(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """``--rm`` deletes the ``State`` that says whether the kernel OOM-killed
    it, which is the one signal this tier exists to be able to report."""
    run(runner_for(fake_engine).dispatch(request_for(profile=container_profile())))
    assert not fake_engine.only_run().has("--rm")


def test_the_container_is_labelled_for_the_reaper(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """The reaper in the rest of S7 has to find containers that outlived their
    run without parsing a
    name for meaning."""
    run(runner_for(fake_engine).dispatch(request_for(profile=container_profile())))
    labels = fake_engine.only_run().values("--label")
    assert f"{LABEL_NAMESPACE}/run-id=run.test" in labels
    assert f"{LABEL_NAMESPACE}/stage-id=code" in labels


# --------------------------------------------------------------------------- #
# Resource caps (§3.7)
# --------------------------------------------------------------------------- #


def test_the_caps_reach_the_engine(request_for: RequestFactory, fake_engine: FakeEngine) -> None:
    caps = ResourceCaps(cpu_count=1.5, memory_mb=2048, pid_limit=256)
    profile = container_profile(caps=caps)
    run(runner_for(fake_engine).dispatch(request_for(profile=profile)))

    call = fake_engine.only_run()
    assert call.value("--cpus") == "1.5"
    assert call.value("--pids-limit") == "256"
    assert call.value("--memory") == "2048m"


def test_swap_is_capped_with_memory(request_for: RequestFactory, fake_engine: FakeEngine) -> None:
    """Memory alone lets the container swap past its cap, which turns an OOM
    kill the taxonomy can report into a host that thrashes."""
    profile = container_profile(caps=ResourceCaps(memory_mb=512))
    run(runner_for(fake_engine).dispatch(request_for(profile=profile)))
    assert fake_engine.only_run().value("--memory-swap") == "512m"


def test_a_whole_number_of_cpus_has_no_trailing_zero(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    profile = container_profile(caps=ResourceCaps(cpu_count=2))
    run(runner_for(fake_engine).dispatch(request_for(profile=profile)))
    assert fake_engine.only_run().value("--cpus") == "2"


def test_a_disk_cap_is_dropped_where_the_driver_cannot_honour_it() -> None:
    """Passing it anyway makes the engine reject the run outright, and a disk
    cap that refuses to start the run is worse than one documented as absent."""
    caps = ResourceCaps(disk_mb=4096)
    assert "--storage-opt" not in ContainerEngine()._cap_argv(caps)
    assert ContainerEngine(supports_storage_quota=True)._cap_argv(caps) == [
        "--storage-opt",
        "size=4096m",
    ]


def test_the_wall_clock_cap_is_not_the_engines_job() -> None:
    """A timeout that depends on the daemon still answering is a timeout that
    fails exactly when it is needed."""
    assert ContainerEngine()._cap_argv(ResourceCaps(wall_clock_seconds=30)) == []


# --------------------------------------------------------------------------- #
# The trust boundary (§3.1, threat model T3)
# --------------------------------------------------------------------------- #


def test_no_control_plane_credential_reaches_the_container(
    request_for: RequestFactory,
    repo: FixtureRepo,
    fake_engine: FakeEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted against the process's real environment, not against our argv.

    The fake engine builds the container's environment from the ``--env`` flags
    and nothing else, exactly as a daemon does, so an implementation that leaked
    the control plane's environment through would fail here rather than pass on
    a technicality.
    """
    for name, value in {
        "SLACK_BOT_TOKEN": "xoxb-not-real",
        "JIRA_API_TOKEN": "jira-not-real",
        "GITHUB_TOKEN": "ghp-not-real",
        "AWS_SECRET_ACCESS_KEY": "aws-not-real",
    }.items():
        monkeypatch.setenv(name, value)

    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    run(runner_for(fake_engine, agent).dispatch(request_for(profile=container_profile())))

    seen = repo.read("env.txt")
    for leaked in ("xoxb-not-real", "jira-not-real", "ghp-not-real", "aws-not-real"):
        assert leaked not in seen


def test_the_control_planes_own_path_and_home_stay_outside(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    """The host tier forwards ``PATH`` and ``HOME``; this one must not. They
    describe a filesystem the container does not have, and ``--read-only`` means
    the image's home is not writable anyway — so the agent gets one inside the
    worktree, under the directory already hidden from git."""
    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    run(runner_for(fake_engine, agent).dispatch(request_for(profile=container_profile())))

    names = {line.split("=", 1)[0] for line in repo.read("env.txt").splitlines() if "=" in line}
    assert f"HOME={repo.path}/.clawdence/home" in repo.read("env.txt")
    assert "PATH" not in names
    assert not names & {"XDG_CACHE_HOME", "TMPDIR", "SHELL", "USER"}


def test_a_scoped_key_is_passed_by_name_and_never_through_argv(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    """``-e NAME=value`` puts the value in the client's command line, and a
    command line is world-readable. ``-e NAME`` does not, and the engine reads
    it from the client's own environment instead."""
    secrets = StaticSecrets({"runner-llm-key": "sk-scoped"})
    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    runner = ContainerRunner(
        agent.command(secret_env={"MODEL_API_KEY": "runner-llm-key"}),
        image=PINNED_IMAGE,
        engine=fake_engine.engine,
        secrets=secrets,
    )
    run(runner.dispatch(request_for(profile=container_profile())))

    call = fake_engine.only_run()
    assert "sk-scoped" not in " ".join(call.argv)
    assert "MODEL_API_KEY" in passthrough_names(call)
    assert "MODEL_API_KEY" not in container_environment(call)
    # …and it still arrives, which is the half a leak-proof design usually loses.
    assert "MODEL_API_KEY=sk-scoped" in repo.read("env.txt")


def test_an_mcp_token_is_passed_the_same_careful_way(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    """The honest exception to "the runner holds no secrets" is still a secret,
    and gets the same treatment rather than a convenient one."""
    secrets = StaticSecrets({"DOCS_MCP_TOKEN": "mcp-scoped"})
    profile = container_profile(
        mcp_servers=[
            {"name": "docs", "url": "https://mcp.invalid", "bearer_token_env_var": "DOCS_MCP_TOKEN"}
        ]
    )
    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    runner = ContainerRunner(
        agent.command(), image=PINNED_IMAGE, engine=fake_engine.engine, secrets=secrets
    )
    run(runner.dispatch(request_for(profile=profile)))

    assert "mcp-scoped" not in " ".join(fake_engine.only_run().argv)
    assert "DOCS_MCP_TOKEN=mcp-scoped" in repo.read("env.txt")


def test_passing_a_control_plane_credential_in_is_still_refused(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    with pytest.raises(PermanentError) as caught:
        run(
            runner_for(fake_engine, extra_env={"GITHUB_TOKEN": "ghp-not-real"}).dispatch(
                request_for(profile=container_profile())
            )
        )
    assert caught.value.kind == "control-plane-secret-in-runner-env"


# --------------------------------------------------------------------------- #
# What the daemon knows and a bare process does not
# --------------------------------------------------------------------------- #


def test_an_oom_kill_is_a_fact_here_rather_than_a_guess(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """The host tier infers this from "a SIGKILL nobody admits to sending" and
    reports an operator's ``kill -9`` as an OOM kill. Here the daemon says so,
    and the agent's own non-zero exit does not overrule it."""
    fake_engine.oom()
    agent = FakeAgent().warn("Killed").exit_with(1)
    result = run(runner_for(fake_engine, agent).dispatch(request_for(profile=container_profile())))
    assert result.outcome is RunnerOutcome.OOM_KILLED


def test_an_engine_that_never_started_a_container_is_a_startup_failure(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """ "The agent failed" and "there was never an agent" are the same non-zero
    exit and opposite handling. v1 conflated exactly this class of thing."""
    fake_engine.refuse_to_start(125)
    result = run(runner_for(fake_engine).dispatch(request_for(profile=container_profile())))
    assert result.outcome is RunnerOutcome.STARTUP_FAILED
    assert "without starting a container" in (result.message or "")


def test_a_missing_engine_binary_is_a_startup_failure(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    runner = ContainerRunner(
        working().command(),
        image=PINNED_IMAGE,
        engine=ContainerEngine(path="clawdence-engine-that-does-not-exist"),
    )
    result = run(runner.dispatch(request_for(profile=container_profile())))
    assert result.outcome is RunnerOutcome.STARTUP_FAILED


def test_a_container_that_vanished_leaves_the_exit_status_alone(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """An operator removing a container mid-run, or a daemon restart, is not a
    startup failure: the process did run, and its exit status is still the best
    account of it. Only the *client's own* statuses mean nothing ever started."""
    fake_engine.refuse_to_start(3)
    result = run(runner_for(fake_engine).dispatch(request_for(profile=container_profile())))
    assert result.outcome is RunnerOutcome.NON_ZERO_EXIT
    assert result.exit_code == 3


# --------------------------------------------------------------------------- #
# Naming, teardown, and surviving our own crash
# --------------------------------------------------------------------------- #


def test_the_container_is_removed_afterwards(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    request = request_for(profile=container_profile())
    run(runner_for(fake_engine).dispatch(request))
    assert container_name(request) in fake_engine.removals()


def test_the_artifacts_are_collected_before_the_container_is_removed(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    """§3.10's ordering requirement, asserted where the ordering actually lives.

    On this tier the worktree is a host bind mount, so the artifacts happen to
    survive teardown — which is exactly why the ordering needs a test rather
    than a comment. Collecting after removal works today and stops working the
    moment a runner is anywhere but this machine, and that is the assumption
    §3.10 exists to remove. A hidden dependency on a coincidence of the one tier
    where it holds is worse than no ordering at all.
    """
    order: list[str] = []

    class Recording(ContainerRunner):
        __slots__ = ()

        async def _collect(
            self,
            request: RunnerRequest,
            worktree: Path,
            completion: Completion,
            installed: Installed,
        ) -> Completion:
            order.append("collect")
            return await super()._collect(request, worktree, completion, installed)

        async def _teardown(self, request: RunnerRequest) -> None:
            order.append("teardown")
            await super()._teardown(request)

    runner = Recording(working().command(), image=PINNED_IMAGE, engine=fake_engine.engine)
    run(runner.dispatch(request_for(profile=container_profile())))

    # The first teardown is `_prepare` clearing a previous attempt's leftovers.
    # What matters is the pair at the end.
    assert order[-2:] == ["collect", "teardown"]


def test_the_artifacts_come_back_from_this_tier_too(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    """§3.10 is a property of the boundary, not of one implementation of it.
    Both tiers fill these in, through the same collection path."""
    agent = (
        FakeAgent()
        .write("app.py", CHANGED)
        .commit()
        .write("scratch.txt", "left behind\n")
        .verdict(tests=TESTS_PASSED)
    )
    result = run(runner_for(fake_engine, agent).dispatch(request_for(profile=container_profile())))

    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert result.commits_ahead == 1
    assert result.dirty is True
    assert result.dirty_paths == ("scratch.txt",)


def test_a_provider_error_is_not_a_success_on_this_tier_either(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    """The stream is read through the engine client, which is a second process
    between the agent and the reader. A false success that only the host tier
    catches is a false success on the tier that is actually the default."""
    agent = working().turn("starting").provider_error("your credit balance is too low")
    result = run(runner_for(fake_engine, agent).dispatch(request_for(profile=container_profile())))

    assert result.outcome is RunnerOutcome.PROVIDER_ERROR
    assert "credit balance" in (result.message or "")


def test_an_agent_that_edits_and_never_commits_is_a_dropped_commit_here_too(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    agent = FakeAgent().write("app.py", CHANGED).verdict(tests=TESTS_PASSED)
    result = run(runner_for(fake_engine, agent).dispatch(request_for(profile=container_profile())))

    assert result.outcome is RunnerOutcome.DROPPED_COMMIT
    assert result.commits_ahead == 0
    assert "app.py" in result.dirty_paths


def test_a_stale_container_from_a_crashed_attempt_is_cleared_first(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """The name is derived from the idempotency key, so a resumed run collides
    with its own previous attempt. Without this the collision is a permanent
    ``STARTUP_FAILED`` on every retry — which is a run that can never recover."""
    request = request_for(profile=container_profile())
    run(runner_for(fake_engine).dispatch(request))

    commands = [call.command for call in fake_engine.calls()]
    assert commands.index("rm") < commands.index("run")


def test_the_name_is_stable_for_an_attempt_and_new_for_the_next(
    request_for: RequestFactory,
) -> None:
    first = request_for(profile=container_profile(), attempt=1)
    again = request_for(profile=container_profile(), attempt=1)
    second = request_for(profile=container_profile(), attempt=2)

    assert container_name(first) == container_name(again)
    assert container_name(first) != container_name(second)


def test_the_name_is_legal_without_escaping_anything(request_for: RequestFactory) -> None:
    """Safe rather than lucky: ``StageId`` is a ``Slug``, and a slug's alphabet
    is already a subset of what a container name accepts. The idempotency key's
    is not, which is why it arrives as a digest rather than as text."""
    name = container_name(request_for(profile=container_profile(), stage_id="code_review-2"))
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name)


def test_cancelling_removes_the_container_and_ends_the_work(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine
) -> None:
    """Killing the client is not enough, and this is the test that knows it.

    A real client is not the container's parent — the daemon is — so a killed
    client leaves the work running, and ``docker rm --force`` is what actually
    ends it. The fake models that faithfully, which means an implementation that
    stopped at killing the client would leave the agent alive to write its
    second file, and this would fail.
    """
    agent = FakeAgent().append("ran.txt", "before\n").sleep(30).append("ran.txt", "after\n")
    runner = runner_for(fake_engine, agent)
    request = request_for(profile=container_profile())

    async def stop_it() -> RunnerOutcome:
        task = asyncio.create_task(runner.dispatch(request))
        # Waited for rather than slept past: under load the engine client and
        # the agent take longer to start than any sleep worth writing, and a
        # cancel that lands before the agent exists tests the wrong thing.
        await _until(lambda: (repo.path / "ran.txt").is_file())
        assert await runner.cancel(request) is True
        return (await task).outcome

    assert run(stop_it()) is RunnerOutcome.CANCELLED
    assert container_name(request) in fake_engine.removals()
    assert repo.read("ran.txt") == "before\n"


# --------------------------------------------------------------------------- #
# Refusals: the request this tier will not run
# --------------------------------------------------------------------------- #


def test_a_host_profile_is_refused_rather_than_contained(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    with pytest.raises(PermanentError) as caught:
        run(runner_for(fake_engine).dispatch(request_for(profile=host_profile())))
    assert caught.value.kind == "isolation-tier-mismatch"


def test_the_socket_tier_is_not_a_flag_away(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """Testcontainers support is S8 and it is a different tier value on purpose:
    a process that can reach the host daemon is host root by another spelling.

    Two refusals deep, and they are in the right order. The profile does not
    validate without ``docker_socket_acknowledged`` — S8's configuration-time
    gate — and a profile that *has* been acknowledged still gets no socket from
    this runner, because the capability needs a runner built for it rather than
    a repository that asked nicely.
    """
    with pytest.raises(ValidationError):
        container_profile(isolation_tier="container+docker:socket")

    acknowledged = container_profile(
        isolation_tier="container+docker:socket", docker_socket_acknowledged=True
    )
    with pytest.raises(PermanentError) as caught:
        run(runner_for(fake_engine).dispatch(request_for(profile=acknowledged)))
    assert caught.value.kind == "isolation-tier-mismatch"


def test_an_unpinned_image_is_refused(request_for: RequestFactory, fake_engine: FakeEngine) -> None:
    """A tag is a mutable pointer. Resolving one at dispatch means running
    whatever was pushed over it since last time, with the repository open."""
    runner = runner_for(fake_engine, image="registry.invalid/runner:latest")
    with pytest.raises(PermanentError) as caught:
        run(runner.dispatch(request_for(profile=container_profile())))
    assert caught.value.kind == "unpinned-runner-image"


def test_an_unpinned_image_can_be_opted_into(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """Local development against an image you just built has no digest yet.
    A decision with a name attached rather than a check that is simply absent."""
    runner = runner_for(
        fake_engine, image="registry.invalid/runner:latest", allow_unpinned_image=True
    )
    result = run(runner.dispatch(request_for(profile=container_profile())))
    assert result.outcome is RunnerOutcome.SUCCEEDED


def test_a_top_level_worktree_is_refused(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """``worktree_path`` is the one request field that becomes a mount, so a
    wrong value there is not a failed run — it is a container with the
    operator's home directory in it."""
    with pytest.raises(PermanentError) as caught:
        run(
            runner_for(fake_engine).dispatch(
                request_for(profile=container_profile(), worktree=Path("/tmp"))  # noqa: S108
            )
        )
    assert caught.value.kind == "worktree-too-shallow"


def test_a_path_the_mount_parser_would_misread_is_refused(tmp_path: Path) -> None:
    """``--mount`` is CSV to the engine, so a comma in a path is a parser bug
    waiting to become somebody's mount. Refused rather than quoted."""
    with pytest.raises(EngineError):
        Mount(source=tmp_path / "work,dir").argv()


# --------------------------------------------------------------------------- #
# Choosing the image (§3.8)
# --------------------------------------------------------------------------- #


def test_the_repos_own_image_wins(request_for: RequestFactory, fake_engine: FakeEngine) -> None:
    """Corporate adopters have a mandated base image and no way to publish it
    anywhere this project can reach."""
    theirs = "registry.invalid/corp/runner@sha256:" + "1" * 64
    profile = container_profile(runner_image=theirs)
    run(runner_for(fake_engine).dispatch(request_for(profile=profile)))
    assert fake_engine.only_run().image == theirs


def test_an_image_can_be_chosen_per_build_system(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    from clawdence.domain import BuildSystem

    jdk = "registry.invalid/clawdence/jdk@sha256:" + "2" * 64
    runner = ContainerRunner(
        working().command(),
        image=PINNED_IMAGE,
        images={BuildSystem.MAVEN: jdk},
        engine=fake_engine.engine,
    )
    profile = container_profile(build_system=BuildSystem.MAVEN)
    run(runner.dispatch(request_for(profile=profile)))
    assert fake_engine.only_run().image == jdk


def test_the_default_image_is_used_when_nothing_overrides_it(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    run(runner_for(fake_engine).dispatch(request_for(profile=container_profile())))
    assert fake_engine.only_run().image == PINNED_IMAGE


# --------------------------------------------------------------------------- #
# The engine adapter on its own
# --------------------------------------------------------------------------- #


def test_every_restrictive_default_can_be_turned_off() -> None:
    """Somebody's repository will need each of these — an agent that has to
    install a package needs a writable root. A decision with a name attached
    rather than a flag nobody chose."""
    spec = ContainerSpec(
        name="c",
        image="img",
        argv=("true",),
        workdir="/work",
        read_only_rootfs=False,
        user=None,
        interactive=False,
    )
    argv = ContainerEngine().run_argv(spec)
    assert "--read-only" not in argv
    assert "--user" not in argv
    assert "--interactive" not in argv
    # …but never these two, which have no override by design.
    assert "--cap-drop" in argv and "--security-opt" in argv


def test_a_mount_can_be_read_only(tmp_path: Path) -> None:
    """Not used by the worktree — the agent has to write to that — but the
    cache volumes and any config a repo needs to read are a different matter."""
    assert Mount(source=tmp_path, read_only=True).argv()[1].endswith(",readonly")


def test_a_mount_can_land_somewhere_other_than_its_own_path(tmp_path: Path) -> None:
    assert "target=/elsewhere" in Mount(source=tmp_path, target="/elsewhere").argv()[1]


def test_a_daemon_that_stops_answering_does_not_hang_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """These are local metadata calls made after the run's fate is decided. A
    daemon that has stopped answering them must not turn into a runner that
    never returns a result — and killing the client is not enough, because
    whatever it forked still holds the pipes we are waiting on."""
    monkeypatch.setattr(process_module, "REAP_TIMEOUT_SECONDS", 1.0)
    slow = tmp_path / "slow-engine"
    # Backgrounded and then waited on, so there is genuinely a grandchild
    # holding the pipes. A plain `sleep 30` is not the same test: some shells
    # `exec` their last command, and then the thing being killed is the only
    # thing there is — which is the case that was never broken.
    slow.write_text("#!/bin/sh\nsleep 30 &\nwait\n", encoding="utf-8")
    slow.chmod(0o755)
    engine = ContainerEngine(path=str(slow), control_timeout_seconds=0.3)

    started = time.monotonic()
    assert run(engine.state("whatever")) is None
    assert time.monotonic() - started < 5


def test_a_missing_daemon_is_not_available() -> None:
    assert run(ContainerEngine(path="clawdence-engine-that-does-not-exist").available()) is False


def test_inspecting_a_container_that_is_gone_is_not_an_error(fake_engine: FakeEngine) -> None:
    """Gone is the same position the host tier is in permanently: nothing extra
    to say, which is not the same as something having failed."""
    assert run(fake_engine.engine.state("clawdence-never-existed")) is None


def test_removing_something_that_never_existed_is_safe(fake_engine: FakeEngine) -> None:
    """Which is what makes it safe to call from a ``finally``."""
    run(fake_engine.engine.remove("clawdence-never-existed"))


def test_the_fake_engine_answers_a_version_query(fake_engine: FakeEngine) -> None:
    assert run(fake_engine.engine.available()) is True


def test_prices_still_produce_a_cost_entry(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    prices = TokenPrice(input_usd=Decimal("3"), output_usd=Decimal("15"))
    agent = working().tokens(1_000_000)
    result = run(
        runner_for(fake_engine, agent, prices=prices).dispatch(
            request_for(profile=container_profile())
        )
    )
    assert result.cost is not None and result.cost.usd > 0
