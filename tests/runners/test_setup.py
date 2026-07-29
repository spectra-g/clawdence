"""The setup phase: installing a repository's dependencies before the agent runs.

v1's ``install_cmd``, finally run by something — and the plan's judgement that
this, rather than the container, is the hard part of S7. A cold ``yarn install``
on a large monorepo dominates a run and is paid again next time, because the
worktree is ephemeral by design.

The install command here is ``python -c`` rather than a package manager. Not to
avoid a network — the suite blocks that anyway — but because the claims are
about the *phase*: that it runs before the agent, that its failure is reported
as the repository's rather than the agent's, that it shares the wall clock, and
that the cache directory it is pointed at still has last run's contents in it. A
real ``npm`` would answer those questions much more slowly, and only where npm
was installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from clawdence.domain import BuildSystem, RepoProfile, RunnerOutcome
from clawdence.runners import Cache, HostRunner, Phase, container_name
from tests.harness.agent import FakeAgent
from tests.harness.engine import FakeEngine
from tests.ports.factories import run
from tests.runners.conftest import RequestFactory, container_profile, host_profile
from tests.runners.test_container import runner_for as container_runner_for
from tests.runners.test_container import working

CHANGED = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
TESTS_PASSED = {"reporter": "pytest-json-report", "total": 4, "passed": 4}


def agent() -> FakeAgent:
    return FakeAgent().say("working").write("app.py", CHANGED).commit().verdict(tests=TESTS_PASSED)


def install(*statements: str) -> tuple[str, ...]:
    """An install command, as argv. Never a shell string — see
    ``ScriptStage.command``, and note that this value comes from a profile the
    probe proposes, so a profile that could hold a shell string is one that
    could hold a pipeline into ``curl``."""
    return (sys.executable, "-c", "\n".join(("import os, sys", *statements)))


def profile_with(install_command: tuple[str, ...], **overrides: object) -> RepoProfile:
    return host_profile(build_system=BuildSystem.UV, install_command=install_command, **overrides)


def host_runner_for(agent_program: FakeAgent, cache: Cache, **kwargs: object) -> HostRunner:
    return HostRunner(agent_program.command(), cache=cache, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The phase runs, and it runs first
# --------------------------------------------------------------------------- #


def test_the_install_command_runs_before_the_agent(
    request_for: RequestFactory, repo: object, tmp_path: Path
) -> None:
    """Ordering, observed rather than assumed.

    Both phases append to one file, so the file itself records which went
    first. An install that runs *after* the agent is an install that did
    nothing for it, which is a bug that reports success.
    """
    log = tmp_path / "order.txt"
    profile = profile_with(install(f"open({str(log)!r}, 'a').write('setup\\n')"))
    program = agent().append(str(log), "agent\n")

    runner = host_runner_for(program, Cache(root=tmp_path / "cache"))
    result = run(runner.dispatch(request_for("code", profile=profile)))

    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert log.read_text(encoding="utf-8").split() == ["setup", "agent"]


def test_a_repository_with_no_install_command_has_no_setup_phase(
    request_for: RequestFactory, tmp_path: Path
) -> None:
    """Most repositories have none, and paying for a spawned process to run
    nothing would be a cost on every run of every one of them."""
    marker = tmp_path / "ran.txt"
    runner = host_runner_for(agent().append(str(marker), "agent\n"), Cache(root=tmp_path / "c"))

    result = run(runner.dispatch(request_for("code", profile=host_profile())))

    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert marker.read_text(encoding="utf-8") == "agent\n"


def test_the_toolchain_wrapper_is_applied_to_the_install_command(
    request_for: RequestFactory, tmp_path: Path
) -> None:
    """``exec_prefix`` is v1's ``mise exec node@24.5 --``, and the whole point of
    it is that the repository's *toolchain* runs the commands — which includes
    the one that installs the toolchain's packages."""
    seen = tmp_path / "prefix.txt"
    profile = profile_with(
        install(f"open({str(seen)!r}, 'w').write(' '.join(sys.argv))"),
        exec_prefix=(sys.executable, "-c", "import os,sys; os.execv(sys.argv[1], sys.argv[1:])"),
    )

    runner = host_runner_for(agent(), Cache(root=tmp_path / "cache"))
    result = run(runner.dispatch(request_for("code", profile=profile)))

    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert seen.is_file()


# --------------------------------------------------------------------------- #
# When it fails
# --------------------------------------------------------------------------- #


def test_a_failing_install_is_blocked_and_the_agent_never_runs(
    request_for: RequestFactory, tmp_path: Path
) -> None:
    """``BLOCKED``, and that is a decision rather than a default.

    ``STARTUP_FAILED`` would be retryable and would mean the machine was wrong;
    ``NON_ZERO_EXIT`` belongs to the agent and implies one ran. A repository
    whose own install command fails is a repository the agent cannot work in,
    and three more attempts re-run the same failing install at full price.
    """
    marker = tmp_path / "agent-ran.txt"
    profile = profile_with(install("sys.exit(3)"))
    runner = host_runner_for(agent().append(str(marker), "agent\n"), Cache(root=tmp_path / "c"))

    result = run(runner.dispatch(request_for("code", profile=profile)))

    assert result.outcome is RunnerOutcome.BLOCKED
    assert not marker.exists()


def test_the_failure_message_names_the_command_that_failed(
    request_for: RequestFactory, tmp_path: Path
) -> None:
    """Otherwise ``blocked`` is a value nobody can act on: the operator has to
    guess whether the agent gave up or the repository would not install."""
    profile = profile_with(install("sys.exit(3)"))
    runner = host_runner_for(agent(), Cache(root=tmp_path / "cache"))

    result = run(runner.dispatch(request_for("code", profile=profile)))

    assert result.message is not None
    assert "could not be prepared" in result.message
    assert "exited 3" in result.message


def test_an_install_command_that_is_not_installed_is_a_startup_failure(
    request_for: RequestFactory, tmp_path: Path
) -> None:
    """The other side of the line. Nothing ran at all, so nothing about the
    repository is implied and a second attempt on a fixed machine may work."""
    profile = profile_with(("clawdence-installer-that-does-not-exist",))
    runner = host_runner_for(agent(), Cache(root=tmp_path / "cache"))

    result = run(runner.dispatch(request_for("code", profile=profile)))

    assert result.outcome is RunnerOutcome.STARTUP_FAILED


def test_an_install_that_runs_out_of_wall_clock_is_a_timeout_not_a_block(
    request_for: RequestFactory, tmp_path: Path
) -> None:
    """The ranking, cashed. ``classify`` puts every way a phase can be *stopped*
    above the fact that it was the setup phase — a run halted by its own
    declared limit is a timeout wherever it was when the limit arrived.
    """
    profile = profile_with(install("import time; time.sleep(30)"))
    runner = host_runner_for(agent(), Cache(root=tmp_path / "cache"))

    result = run(runner.dispatch(request_for("code", profile=profile, wall_clock_seconds=0.4)))

    assert result.outcome is RunnerOutcome.TIMED_OUT


def test_the_two_phases_share_one_wall_clock(request_for: RequestFactory, tmp_path: Path) -> None:
    """A run declared to take thirty seconds takes thirty seconds, not sixty.

    The install here eats most of the limit and the agent is given what is left,
    so the *agent* is what the deadline catches — which is only possible if the
    deadline was started once, for the attempt, rather than once per phase.
    """
    profile = profile_with(install("import time; time.sleep(0.5)"))
    runner = host_runner_for(agent().sleep(30), Cache(root=tmp_path / "cache"))

    result = run(runner.dispatch(request_for("code", profile=profile, wall_clock_seconds=1.0)))

    assert result.outcome is RunnerOutcome.TIMED_OUT


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #


def test_the_install_command_is_pointed_at_the_warm_cache(
    request_for: RequestFactory, tmp_path: Path
) -> None:
    cache = Cache(root=tmp_path / "cache")
    seen = tmp_path / "cache-dir.txt"
    profile = profile_with(install(f"open({str(seen)!r}, 'w').write(os.environ['UV_CACHE_DIR'])"))

    runner = host_runner_for(agent(), cache)
    run(runner.dispatch(request_for("code", profile=profile)))

    directory = Path(seen.read_text(encoding="utf-8"))
    assert directory.is_dir()
    assert directory.is_relative_to(cache.directory(profile))


def test_the_second_run_finds_what_the_first_one_downloaded(
    request_for: RequestFactory, tmp_path: Path
) -> None:
    """The plan's "second run is materially faster", stated as the mechanism
    rather than as a stopwatch.

    A timing assertion against a fake install would measure the harness. What
    actually decides whether the second run is faster is whether the artefacts
    the first one fetched are still there, so that is what this asserts: run one
    reports a cold cache and populates it, run two reports a warm one and does
    no work. A cache keyed wrongly, created too late, or thrown away with the
    worktree fails here and passes every timing test on a fast machine.
    """
    cache = Cache(root=tmp_path / "cache")
    log = tmp_path / "installs.txt"
    profile = profile_with(
        install(
            "marker = os.path.join(os.environ['UV_CACHE_DIR'], 'package.tar')",
            "warm = os.path.exists(marker)",
            f"open({str(log)!r}, 'a').write(('warm' if warm else 'cold') + '\\n')",
            "open(marker, 'w').write('downloaded')",
        )
    )
    runner = host_runner_for(agent(), cache)

    run(runner.dispatch(request_for("code", profile=profile, attempt=1)))
    run(runner.dispatch(request_for("code", profile=profile, attempt=2)))

    assert log.read_text(encoding="utf-8").split() == ["cold", "warm"]


def test_a_disabled_cache_leaves_the_environment_alone(
    request_for: RequestFactory, tmp_path: Path
) -> None:
    """Off is a supported configuration. What it must not do is leave a variable
    pointing at a directory nothing created."""
    seen = tmp_path / "env.txt"
    profile = profile_with(
        install(f"open({str(seen)!r}, 'w').write(os.environ.get('UV_CACHE_DIR', 'unset'))")
    )

    runner = host_runner_for(agent(), Cache(root=tmp_path / "cache", enabled=False))
    run(runner.dispatch(request_for("code", profile=profile)))

    assert seen.read_text(encoding="utf-8") == "unset"


# --------------------------------------------------------------------------- #
# The container tier
# --------------------------------------------------------------------------- #


def container_profile_with(**overrides: object) -> RepoProfile:
    return container_profile(build_system=BuildSystem.UV, **overrides)


def test_each_phase_gets_its_own_container(
    fake_engine: FakeEngine, request_for: RequestFactory, tmp_path: Path
) -> None:
    """One name for both would mean creating the agent's container collides with
    the setup container's exit state — which is the evidence ``_observe`` reads
    to decide whether the kernel took the install."""
    profile = container_profile_with(install_command=install("pass"))
    request = request_for("code", profile=profile)
    runner = container_runner_for(fake_engine, working(), cache=Cache(root=tmp_path / "c"))

    run(runner.dispatch(request))

    names = [call.value("--name") for call in fake_engine.runs()]
    assert names == [container_name(request, Phase.SETUP), container_name(request, Phase.AGENT)]


def test_both_phases_containers_are_removed_however_far_the_run_got(
    fake_engine: FakeEngine, request_for: RequestFactory, tmp_path: Path
) -> None:
    """A run that failed during setup never created an agent container, and one
    that succeeded created both. Teardown removes every phase's, so neither way
    of ending leaves the other behind."""
    profile = container_profile_with(install_command=install("sys.exit(1)"))
    request = request_for("code", profile=profile)
    runner = container_runner_for(fake_engine, working(), cache=Cache(root=tmp_path / "c"))

    result = run(runner.dispatch(request))

    assert result.outcome is RunnerOutcome.BLOCKED
    removed = set(fake_engine.removals())
    assert container_name(request, Phase.SETUP) in removed
    assert container_name(request, Phase.AGENT) in removed


def test_an_install_the_kernel_killed_is_reported_as_an_oom_kill(
    fake_engine: FakeEngine, request_for: RequestFactory, tmp_path: Path
) -> None:
    """The tier's one real advantage, applied to the phase where a build tool
    is the thing most likely to exhaust the cap. ``BLOCKED`` here would send an
    operator looking at a lockfile instead of at ``memory_mb``."""
    profile = container_profile_with(install_command=install("sys.exit(137)"))
    runner = container_runner_for(fake_engine.oom(), working(), cache=Cache(root=tmp_path / "c"))

    result = run(runner.dispatch(request_for("code", profile=profile)))

    assert result.outcome is RunnerOutcome.OOM_KILLED


def test_the_cache_is_mounted_into_both_containers(
    fake_engine: FakeEngine, request_for: RequestFactory, tmp_path: Path
) -> None:
    """At the same absolute path it has on the host, like the worktree: the
    environment variables name absolute paths, and a cache appearing somewhere
    else would need a second spelling of every one of them."""
    cache = Cache(root=tmp_path / "cache")
    profile = container_profile_with(install_command=install("pass"))
    runner = container_runner_for(fake_engine, working(), cache=cache)

    run(runner.dispatch(request_for("code", profile=profile)))

    directory = str(cache.directory(profile))
    for call in fake_engine.runs():
        mounts = call.values("--mount")
        assert any(f"source={directory},target={directory}" in mount for mount in mounts)


def test_the_setup_container_is_never_given_a_stdin(
    fake_engine: FakeEngine, request_for: RequestFactory, tmp_path: Path
) -> None:
    """An attached stdin nothing writes to is a package manager waiting on a
    prompt — a private registry asking for credentials — until the wall clock
    ends the run. The agent's, on this delivery, genuinely is written to."""
    profile = container_profile_with(install_command=install("pass"))
    runner = container_runner_for(fake_engine, working(), cache=Cache(root=tmp_path / "c"))

    run(runner.dispatch(request_for("code", profile=profile)))

    setup, agent_run = fake_engine.runs()
    assert not setup.has("--interactive")
    assert agent_run.has("--interactive")
