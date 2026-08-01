"""The socket tier, against an engine that records what it was asked for.

Same division as ``test_container.py``, and it matters more here. What is
testable offline is **who is refused** and **what the container is given** — the
four gates, the mount, the group, the hosts entry, the four environment
variables. All of that is argv and control flow, and all of it is the entire
security story of a tier whose whole purpose is to hand out a capability that
defeats every other control in the system.

What is not testable here: that a container with that socket mounted can
actually reach a daemon, that a sibling's bind mount resolves to the same files,
that Ryuk collects a killed run's fixtures. Those need a daemon and live in
``test_dockerd_live.py``.

The sibling sweep is the one thing in between. The *policy* — what a run may
claim, and what it must leave for Ryuk — is decided in this process against a
fake engine that really creates and really removes containers, so the
overlapping-runs case can be set up deterministically instead of raced against a
real test suite.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from clawdence.domain import IsolationTier, RepoProfile, RunnerOutcome, RunnerRequest
from clawdence.ports import PermanentError
from clawdence.runners import (
    DOCKER_SOCKET,
    HOST_ALIAS,
    HOST_OVERRIDE_ENV,
    RYUK_DISABLED_ENV,
    ContainerRunner,
    DockerSocketRunner,
    Phase,
    container_name,
)
from tests.harness.agent import FakeAgent
from tests.harness.engine import FakeEngine, container_environment
from tests.harness.repos import FixtureRepo
from tests.ports.contract import RunnerContract
from tests.ports.factories import run
from tests.runners.conftest import (
    PINNED_IMAGE,
    RequestFactory,
    container_profile,
    socket_profile,
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
    fake_socket: Path,
    agent: FakeAgent | None = None,
    **kwargs: object,
) -> DockerSocketRunner:
    command = (agent or working()).command(
        **{name: value for name, value in kwargs.items() if name in _COMMAND_FIELDS}  # type: ignore[arg-type]
    )
    options = {name: value for name, value in kwargs.items() if name not in _COMMAND_FIELDS}
    options.setdefault("socket_path", str(fake_socket))
    return DockerSocketRunner(
        command,
        image=str(options.pop("image", PINNED_IMAGE)),
        engine=fake_engine.engine,
        **options,  # type: ignore[arg-type]
    )


_COMMAND_FIELDS = frozenset(
    {"delivery", "conventions_filename", "extra_env", "secret_env", "prices", "accumulation"}
)


def trusted(request_for: RequestFactory, **overrides: object) -> RunnerRequest:
    """A request this tier will actually accept: right profile, right
    provenance. Both, every time, because either alone is a refusal."""
    overrides.setdefault("profile", socket_profile())
    overrides.setdefault("trusted_provenance", True)
    return request_for(**overrides)


async def _until(ready: Callable[[], bool], *, limit: float = 20.0) -> None:
    deadline = time.monotonic() + limit
    while not ready():
        assert time.monotonic() < deadline, "the agent never started"
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- #
# The contract every adapter is held to
# --------------------------------------------------------------------------- #


class TestDockerSocketRunner(RunnerContract):
    """The third real adapter. A tier that is a subclass is still a tier, and
    the obligations — a redelivery returns the first answer, a duplicate joins
    rather than races — are not inherited by assertion."""

    @pytest.fixture
    def runner(self, fake_engine: FakeEngine, fake_socket: Path) -> DockerSocketRunner:
        return runner_for(fake_engine, fake_socket)

    @pytest.fixture
    def make_request(self, request_for: RequestFactory) -> RequestFactory:
        def build(*args: object, **kwargs: object) -> object:
            kwargs.setdefault("profile", socket_profile())
            kwargs.setdefault("trusted_provenance", True)
            return request_for(*args, **kwargs)

        return build  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Gate 1: the profile cannot be written without saying so
# --------------------------------------------------------------------------- #


def test_the_tier_cannot_be_named_without_acknowledging_it() -> None:
    """§3.2 asks for socket mode to be opt-in and loudly documented. A warning
    in a README is a warning nobody read, so the acknowledgement is a field and
    the profile does not validate without it."""
    with pytest.raises(ValueError, match="docker_socket_acknowledged"):
        RepoProfile(
            id="repo.x",
            name="x",
            remote_url="https://forge.invalid/x",
            isolation_tier=IsolationTier.CONTAINER_DOCKER_SOCKET,
        )


def test_the_acknowledgement_alone_changes_no_tier() -> None:
    """The field is a permission, not a request. A repository that sets it and
    stays on the default tier gets the default tier — otherwise the flag would
    be a second, quieter way to select the dangerous one."""
    profile = RepoProfile(
        id="repo.x",
        name="x",
        remote_url="https://forge.invalid/x",
        docker_socket_acknowledged=True,
    )
    assert profile.isolation_tier is IsolationTier.CONTAINER


def test_needs_docker_does_not_imply_the_socket() -> None:
    """The probe's inference (S9) says the repository's tests want a daemon. It
    does not say the operator agreed to hand one over, and collapsing the two
    would let a lockfile select the tier."""
    profile = RepoProfile(
        id="repo.x", name="x", remote_url="https://forge.invalid/x", needs_docker=True
    )
    assert profile.isolation_tier is IsolationTier.CONTAINER


# --------------------------------------------------------------------------- #
# Gate 2: the provenance of the work, not just the configuration of the repo
# --------------------------------------------------------------------------- #


def test_untrusted_work_is_refused_even_on_an_acknowledged_repo(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """§3.3's table, as the line that implements it. Two facts — what the
    repository needs and who asked — and this tier requires both."""
    with pytest.raises(PermanentError) as caught:
        run(
            runner_for(fake_engine, fake_socket).dispatch(
                request_for(profile=socket_profile(), trusted_provenance=False)
            )
        )
    assert caught.value.kind == "untrusted-work-may-not-reach-the-daemon"


def test_provenance_is_deny_by_default(request_for: RequestFactory) -> None:
    """Nobody has to remember to mark work untrusted. ``Submitter.trusted`` is
    false by default and so is this, all the way down."""
    assert request_for(profile=socket_profile()).trusted_provenance is False


def test_the_refusal_is_not_a_downgrade(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """Quietly running on the plain container tier would run a testcontainers
    suite without a daemon and report the failure as the agent's — a wrong
    answer that costs a full run to produce."""
    with pytest.raises(PermanentError):
        run(
            runner_for(fake_engine, fake_socket).dispatch(
                request_for(profile=socket_profile(), trusted_provenance=False)
            )
        )
    assert not fake_engine.runs()


# --------------------------------------------------------------------------- #
# Gate 3: no other runner will take this tier, and this one takes no other
# --------------------------------------------------------------------------- #


def test_the_default_runner_still_refuses_the_socket_tier(
    request_for: RequestFactory, fake_engine: FakeEngine
) -> None:
    """The capability needs a runner that was constructed for it. No amount of
    profile editing turns the default one into this."""
    runner = ContainerRunner(working().command(), image=PINNED_IMAGE, engine=fake_engine.engine)
    with pytest.raises(PermanentError) as caught:
        run(runner.dispatch(request_for(profile=socket_profile(), trusted_provenance=True)))
    assert caught.value.kind == "isolation-tier-mismatch"


def test_this_runner_refuses_a_plain_container_profile(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """And it does not silently *upgrade* either: a repository that asked for
    the safe tier gets the safe tier, or an error, never a socket it did not
    ask for."""
    with pytest.raises(PermanentError) as caught:
        run(
            runner_for(fake_engine, fake_socket).dispatch(
                request_for(profile=container_profile(), trusted_provenance=True)
            )
        )
    assert caught.value.kind == "isolation-tier-mismatch"


def test_a_socket_the_daemon_does_not_have_is_refused_before_the_run(
    request_for: RequestFactory, fake_engine: FakeEngine, tmp_path: Path
) -> None:
    """Otherwise the run reaches a test suite that dies on connection refused
    several minutes in, having paid for an install first."""
    runner = runner_for(fake_engine, tmp_path / "not-a-socket")
    with pytest.raises(PermanentError) as caught:
        run(runner.dispatch(trusted(request_for)))
    assert caught.value.kind == "docker-socket-unreadable"


def test_the_group_is_asked_of_the_daemon_rather_than_of_this_machine(
    request_for: RequestFactory, fake_socket: Path, fake_engine: FakeEngine
) -> None:
    """The finding that made this a container run rather than a ``stat``.

    On Docker Desktop, Colima, Lima and Rancher the daemon is in a VM: the
    socket the container mounts is the VM's, its gid is the VM's, and the path
    on the developer's machine either has an unrelated owner or does not exist.
    A local ``stat`` answers about the wrong filesystem, and the divergence is
    invisible until a non-root container cannot open a mount that is there.
    """
    run(runner_for(fake_engine, fake_socket).dispatch(trusted(request_for)))
    probes = fake_engine.probes()
    assert len(probes) == 1
    assert any(str(fake_socket) in value for value in probes[0].values("--mount"))
    assert probes[0].has("--rm")


def test_the_daemon_is_asked_once_per_image(
    request_for: RequestFactory, fake_socket: Path, fake_engine: FakeEngine
) -> None:
    """It costs a container start, and the answer does not change while the
    process is alive."""
    runner = runner_for(fake_engine, fake_socket)
    run(runner.dispatch(trusted(request_for, attempt=1)))
    run(runner.dispatch(trusted(request_for, attempt=2)))
    assert len(fake_engine.probes()) == 1


def test_a_configured_group_skips_the_question_entirely(
    request_for: RequestFactory, fake_socket: Path, fake_engine: FakeEngine
) -> None:
    """The escape hatch, and the reason it exists: an image with no shell in it
    cannot answer, and a tier that only worked on images with a shell would be
    one that failed on the hardened base a corporate adopter is required to
    use."""
    runner = runner_for(fake_engine, fake_socket, socket_group="991")
    run(runner.dispatch(trusted(request_for)))
    assert fake_engine.probes() == ()
    assert fake_engine.only_run().value("--group-add") == "991"


# --------------------------------------------------------------------------- #
# Gate 4: Ryuk stays on
# --------------------------------------------------------------------------- #


def test_the_reaper_is_switched_on_for_every_run(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """Explicitly false rather than merely unset: an image or a base
    configuration that turned it off would otherwise be inherited, and this is
    the variable whose default matters most on this tier."""
    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    run(runner_for(fake_engine, fake_socket, agent).dispatch(trusted(request_for)))
    assert f"{RYUK_DISABLED_ENV}=false" in repo.read("env.txt")


def test_disabling_the_reaper_is_refused_rather_than_obeyed(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """Without Ryuk every fixture a run starts outlives it, on the host,
    invisibly until the disk fills — and this system has no way of its own to
    tell which of the host's containers were that run's."""
    runner = runner_for(fake_engine, fake_socket, extra_env={RYUK_DISABLED_ENV: "true"})
    with pytest.raises(PermanentError) as caught:
        run(runner.dispatch(trusted(request_for)))
    assert caught.value.kind == "testcontainers-reaper-disabled"


@pytest.mark.parametrize("spelling", ["true", "TRUE", "1", "yes", " on "])
def test_every_spelling_of_true_is_caught(
    spelling: str, request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """A shell, a CI config and a ``.env`` file each produce a different one,
    and getting this wrong in the permissive direction accepts a run with no
    reaper."""
    runner = runner_for(fake_engine, fake_socket, extra_env={RYUK_DISABLED_ENV: spelling})
    with pytest.raises(PermanentError) as caught:
        run(runner.dispatch(trusted(request_for)))
    assert caught.value.kind == "testcontainers-reaper-disabled"


# --------------------------------------------------------------------------- #
# What the container is given — §3.3's three constraints
# --------------------------------------------------------------------------- #


def test_the_socket_is_mounted_at_its_own_path(
    request_for: RequestFactory, fake_socket: Path, fake_engine: FakeEngine
) -> None:
    run(runner_for(fake_engine, fake_socket).dispatch(trusted(request_for)))
    mounts = fake_engine.only_run().values("--mount")
    assert f"type=bind,source={fake_socket},target={fake_socket}" in mounts


def test_the_socket_is_not_read_only(
    request_for: RequestFactory, fake_socket: Path, fake_engine: FakeEngine
) -> None:
    """A read-only bind of a socket stops nothing that matters — the danger is
    the conversation, not the inode — so it is not claimed as a control."""
    run(runner_for(fake_engine, fake_socket).dispatch(trusted(request_for)))
    socket_mount = next(
        value for value in fake_engine.only_run().values("--mount") if str(fake_socket) in value
    )
    assert "readonly" not in socket_mount


def test_the_worktree_still_keeps_its_path_inside(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """§3.3's first constraint, and the reason ``Mount.target`` defaults to
    ``Mount.source``. Testcontainers sends *host* paths to the daemon when it
    mounts volumes for a sibling; a differing path resolves to a directory the
    daemon creates empty, so nothing errors and the fixtures are simply
    missing."""
    run(runner_for(fake_engine, fake_socket).dispatch(trusted(request_for)))
    call = fake_engine.only_run()
    assert f"type=bind,source={repo.path},target={repo.path}" in call.values("--mount")
    assert call.value("--workdir") == str(repo.path)


def test_the_container_is_told_where_the_host_is(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """§3.3's second constraint. Siblings publish their ports on the *host*, and
    ``localhost`` inside this container is this container — so every connection
    a test makes to a fixture, and the one Ryuk needs, goes through this name."""
    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    run(runner_for(fake_engine, fake_socket, agent).dispatch(trusted(request_for)))
    assert f"{HOST_OVERRIDE_ENV}={HOST_ALIAS}" in repo.read("env.txt")


def test_the_host_name_is_made_to_resolve_on_a_native_daemon(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """``host.docker.internal`` exists by default on Docker Desktop and does not
    on a native Linux daemon. Without this the tier works on a laptop and hangs
    on a Linux host, which is the worst place for the difference to show up."""
    run(runner_for(fake_engine, fake_socket).dispatch(trusted(request_for)))
    assert fake_engine.only_run().value("--add-host") == f"{HOST_ALIAS}:host-gateway"


def test_the_container_joins_the_sockets_group(
    request_for: RequestFactory, fake_socket: Path, fake_engine: FakeEngine
) -> None:
    """The container runs as the invoking user, because root-owned files on a
    bind mount are the next run's problem — and the daemon's socket is
    group-owned, so ``--user`` without this is a mount the process cannot
    open."""
    run(runner_for(fake_engine, fake_socket).dispatch(trusted(request_for)))
    call = fake_engine.only_run()
    assert call.value("--group-add") == str(fake_socket.stat().st_gid)
    assert call.value("--user") is not None


def test_a_client_inside_is_pointed_at_the_mounted_socket(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """Explicit rather than left to the default, because the default is only
    right while the socket is at the standard path — and ``socket_path`` exists
    so that it need not be."""
    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    run(runner_for(fake_engine, fake_socket, agent).dispatch(trusted(request_for)))
    assert f"DOCKER_HOST=unix://{fake_socket}" in repo.read("env.txt")


def test_the_default_socket_path_is_the_standard_one() -> None:
    assert DOCKER_SOCKET == "/var/run/docker.sock"


# --------------------------------------------------------------------------- #
# The setup phase does not get the daemon
# --------------------------------------------------------------------------- #


def test_the_install_phase_has_no_socket(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """The setup phase runs whatever a lockfile asks it to — a ``postinstall``
    script is arbitrary code from a transitive dependency, and it is the least
    trusted thing in the run. The capability this tier grants is for tests."""
    profile = socket_profile(install_command=("/bin/sh", "-c", "true"))
    run(runner_for(fake_engine, fake_socket).dispatch(trusted(request_for, profile=profile)))

    runs = fake_engine.runs()
    assert len(runs) == 2
    setup, agent = runs
    assert setup.value(
        "--name",
    ) and setup.value("--name").endswith(Phase.SETUP.value)  # type: ignore[union-attr]
    assert not any(str(fake_socket) in value for value in setup.values("--mount"))
    assert any(str(fake_socket) in value for value in agent.values("--mount"))


def test_the_install_phase_is_still_told_where_a_daemon_would_be(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """So an install that reaches for one fails saying it could not find it,
    rather than failing while saying nothing at all."""
    profile = socket_profile(install_command=("/bin/sh", "-c", "true"))
    run(runner_for(fake_engine, fake_socket).dispatch(trusted(request_for, profile=profile)))
    setup = fake_engine.runs()[0]
    assert container_environment(setup)["DOCKER_HOST"] == f"unix://{fake_socket}"


# --------------------------------------------------------------------------- #
# Everything the container tier decided is still decided
# --------------------------------------------------------------------------- #


def test_the_restrictive_flags_survive_the_subclass(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """The socket defeats these, and they are still all there. A tier that
    dropped them as well would be handing out two problems for the price of the
    one that was actually asked for."""
    run(runner_for(fake_engine, fake_socket).dispatch(trusted(request_for)))
    call = fake_engine.only_run()
    assert call.value("--cap-drop") == "ALL"
    assert call.value("--security-opt") == "no-new-privileges"
    assert call.has("--read-only")
    assert call.has("--init")
    assert not call.has("--rm")


def test_no_control_plane_credential_reaches_this_tier_either(
    request_for: RequestFactory,
    repo: FixtureRepo,
    fake_engine: FakeEngine,
    fake_socket: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plane split is voidable from inside this container, which is exactly
    why it is not also voided on the way in: a tier is not an excuse to stop
    doing the things that still work."""
    for name, value in {
        "SLACK_BOT_TOKEN": "xoxb-not-real",
        "GITHUB_TOKEN": "ghp-not-real",
    }.items():
        monkeypatch.setenv(name, value)

    agent = FakeAgent().dump_env("env.txt").write("app.py", CHANGED).verdict()
    run(runner_for(fake_engine, fake_socket, agent).dispatch(trusted(request_for)))

    seen = repo.read("env.txt")
    assert "xoxb-not-real" not in seen and "ghp-not-real" not in seen


def test_a_diff_and_a_result_still_come_back(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    runner = runner_for(fake_engine, fake_socket, working(summary="added mul"))
    result = run(runner.dispatch(trusted(request_for)))

    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert result.diff is not None and result.diff.files_changed == 1


def test_the_containers_are_still_removed_afterwards(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    request = trusted(request_for)
    run(runner_for(fake_engine, fake_socket).dispatch(request))
    assert container_name(request) in fake_engine.removals()


# --------------------------------------------------------------------------- #
# The sibling sweep
# --------------------------------------------------------------------------- #


def sweeping_runner(
    fake_engine: FakeEngine, fake_socket: Path, **kwargs: object
) -> DockerSocketRunner:
    """A runner whose agent starts a sibling container mid-run.

    The sibling is created from *outside* the fake container, because the fake
    agent is a Python program rather than a test framework — but it is created
    while the dispatch is in flight, which is the only property the sweep
    depends on. What is being tested is which sessions the tier decides are its
    own, not who typed ``docker run``.
    """
    agent = FakeAgent().append("ran.txt", "started\n").sleep(30)
    agent.write("app.py", CHANGED).verdict(tests=TESTS_PASSED)
    return runner_for(fake_engine, fake_socket, agent, **kwargs)


async def _dispatch_with_a_sibling(
    runner: DockerSocketRunner,
    request: RunnerRequest,
    fake_engine: FakeEngine,
    session: str,
    repo: FixtureRepo,
) -> None:
    task = asyncio.create_task(runner.dispatch(request))
    await _until(lambda: (repo.path / "ran.txt").is_file())
    fake_engine.sibling(session)
    assert await runner.cancel(request) is True
    await task


def test_a_sibling_started_during_the_run_is_swept(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """The backstop for the case Ryuk cannot cover — Ryuk itself killed, or the
    daemon restarted. A cancelled run is the one the plan asks about by name."""
    runner = sweeping_runner(fake_engine, fake_socket)
    request = trusted(request_for)

    run(_dispatch_with_a_sibling(runner, request, fake_engine, "sess-ours", repo))

    assert "testcontainers-sess-ours" not in fake_engine.alive()


def test_a_sibling_that_was_already_there_is_left_alone(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """It may be a developer's own test suite on the same machine, and removing
    it costs them a debugging session they were in the middle of. The snapshot
    is what tells the two apart."""
    fake_engine.sibling("sess-theirs")
    runner = sweeping_runner(fake_engine, fake_socket)
    request = trusted(request_for)

    run(_dispatch_with_a_sibling(runner, request, fake_engine, "sess-ours", repo))

    assert "testcontainers-sess-theirs" in fake_engine.alive()
    assert "testcontainers-sess-ours" not in fake_engine.alive()


def test_a_session_two_runs_could_each_claim_is_left_for_ryuk(
    request_for: RequestFactory, repo: FixtureRepo, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """The rule that costs precision and buys safety.

    Sibling containers carry no label naming the run that caused them, so with
    two dispatches overlapping, ownership of a session created during both is
    genuinely unknowable. Claiming it anyway would mean one run tearing down
    another run's database halfway through its test suite; Ryuk collects it a
    little later instead, which is the cheaper way to be wrong.
    """
    runner = sweeping_runner(fake_engine, fake_socket)
    first = trusted(request_for, run_id="run.one", attempt=1)
    second = trusted(request_for, run_id="run.two", attempt=2)

    async def both() -> None:
        one = asyncio.create_task(runner.dispatch(first))
        await _until(lambda: (repo.path / "ran.txt").is_file())
        two = asyncio.create_task(runner.dispatch(second))
        # Two "started" lines, so the second dispatch is genuinely in flight
        # before the ambiguous session exists.
        await _until(lambda: repo.read("ran.txt").count("started") == 2)

        fake_engine.sibling("sess-ambiguous")
        assert await runner.cancel(first) is True
        await one
        assert await runner.cancel(second) is True
        await two

    run(both())

    assert "testcontainers-sess-ambiguous" in fake_engine.alive()


def test_nothing_is_swept_when_nothing_appeared(
    request_for: RequestFactory, fake_engine: FakeEngine, fake_socket: Path
) -> None:
    """The common case: a repository that uses the tier and, this run, started
    no fixtures. A sweep that removed something here would be removing the
    host's."""
    fake_engine.sibling("sess-theirs")
    run(runner_for(fake_engine, fake_socket).dispatch(trusted(request_for)))
    assert "testcontainers-sess-theirs" in fake_engine.alive()
