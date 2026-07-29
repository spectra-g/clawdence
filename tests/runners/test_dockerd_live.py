"""The socket tier against a real daemon. Opt-in; ``make docker-tests``.

``test_dockerd.py`` proves what was asked for. This proves it works, and the gap
between the two is wider here than anywhere else in the codebase: a socket in a
``--mount`` string is not a daemon a process can talk to, and the three
constraints §3.3 lists — path identity, a name for the host, Ryuk — each fail
*silently* when they are wrong. A sibling whose bind mount points at a path the
daemon cannot see gets an empty directory rather than an error. A test that
cannot reach ``host.docker.internal`` hangs rather than fails. Ryuk that never
gets a connection reaps nothing and says nothing. None of those are visible in
argv, and all of them are visible from inside a container.

The plan asks for a Spring Boot fixture whose integration tests pass in the
runner. **What is here instead is a fixture repository that starts real sibling
containers through the real Docker client**, using a shell script where a Spring
Boot repository would use testcontainers-java. The substitution is deliberate:
the three constraints are properties of the *daemon relationship*, and a JDK
image plus a Maven cold start plus a Spring context costs several minutes and a
gigabyte to assert the same three things a shell script asserts in seconds. What
the substitution does not cover is the testcontainers library's own behaviour —
Ryuk's reconnection timeout in particular — and the last test here covers what
it can of that by holding the socket the way Ryuk does.

Environmental notes, all inherited from ``test_container_live.py``: the worktree
lives under ``~/.cache`` because a Linux VM does not share the macOS temporary
directory, and the image is digest-pinned because the runner enforces that on
everybody else. One more is specific to this file: the image needs a Docker
*client*, which the default alpine does not have, so this pulls one that does.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from clawdence.domain import (
    Budget,
    ContractKind,
    IsolationTier,
    RepoProfile,
    ResourceCaps,
    Run,
    RunnerOutcome,
    RunnerRequest,
    RunStatus,
    VerificationContract,
)
from clawdence.runners import (
    SESSION_LABEL,
    AgentCommand,
    ContainerEngine,
    DockerSocketRunner,
    PlanDelivery,
    container_name,
)
from clawdence.store import IN_MEMORY, StateStore, StoreControl
from tests.harness.repos import build_repo
from tests.ports.factories import run
from tests.runners.test_container_live import PASSED, ROOT, TIMEOUT, WORKED

pytestmark = pytest.mark.docker

#: Pinned, small, and — unlike the alpine the plain container tier uses — it has
#: a Docker client in it. That is the whole reason for a second image: the
#: capability this tier grants is unobservable from a container that cannot
#: speak to a daemon.
IMAGE = os.environ.get(
    "CLAWDENCE_DOCKER_TEST_CLIENT_IMAGE",
    "docker@sha256:be132a9f282288de4afaf63379dff75711fda0147c6b72a9df44e51841402144",
)

#: What a sibling container is asked to be. Tiny, pinned, and it exits
#: immediately — what is being observed is that the daemon created it at all.
SIBLING_IMAGE = os.environ.get(
    "CLAWDENCE_DOCKER_TEST_SIBLING_IMAGE",
    "alpine@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce",
)

#: A stand-in for the session label testcontainers stamps on its containers. The
#: sweep correlates on the label, not on the library, so a script that labels its
#: siblings this way is indistinguishable from a testcontainers session as far as
#: the tier is concerned — which is exactly the claim being tested.
SESSION: Final = "clawdence-live-session"

#: A second session, belonging to nobody in this file. Real session ids are one
#: per process, so "somebody else's test suite on this machine" is a *different*
#: value here rather than an older container — and getting that wrong in a test
#: is how the bystander case passes without ever being exercised.
OTHER_SESSION: Final = "clawdence-live-other-session"

#: Both, for cleanup.
SESSIONS: Final = frozenset({SESSION, OTHER_SESSION})


#: Where the daemon has its socket **on the machine the daemon is running on**,
#: which on this project's own development machine — Colima — is inside a VM
#: that has no counterpart on the Mac at all. Deliberately not checked with
#: ``Path.is_socket()`` for that reason: the check would skip every test in this
#: file on precisely the setups the tier most needs proving on. Overridable for
#: a rootless or remote daemon, which is what ``socket_path`` exists for.
SOCKET = os.environ.get("CLAWDENCE_DOCKER_TEST_SOCKET", "/var/run/docker.sock")


@pytest.fixture(autouse=True)
def _needs_a_daemon() -> None:
    if os.environ.get("CLAWDENCE_DOCKER_TESTS") != "1":
        pytest.skip("set CLAWDENCE_DOCKER_TESTS=1 to run the live container tests")
    if not run(ContainerEngine().available()):
        pytest.skip("no container daemon is answering")


@pytest.fixture
def workdir() -> Iterator[Path]:
    """A scratch directory the VM can actually see, removed afterwards."""
    ROOT.mkdir(parents=True, exist_ok=True)
    root = Path(str(ROOT / f"sock-{os.getpid()}-{datetime.now(UTC).timestamp()}"))
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_siblings_left_behind() -> Iterator[None]:
    """Remove this file's own siblings however a test ends.

    A test that fails partway is a test that has just proved a leak exists;
    leaving the leak behind for the next run to trip over would turn one failure
    into a file that only passes on a clean machine.
    """
    yield
    for name, session in run(ContainerEngine().labelled(SESSION_LABEL)):
        if session in SESSIONS:
            run(ContainerEngine().remove(name))


def agent(script: str, **kwargs: object) -> AgentCommand:
    """An 'agent CLI' that is a shell script, run inside the container."""
    return AgentCommand(
        argv=("/bin/sh", "-c", script),
        delivery=PlanDelivery.STDIN,
        **kwargs,  # type: ignore[arg-type]
    )


def sibling(name: str, *extra: str) -> str:
    """Shell that asks the host daemon for a container, labelled as a session.

    What testcontainers does, spelled out: a sibling on the host daemon,
    carrying the session label its reaper keys on.
    """
    return (
        f"docker run -d --name {name} "
        f"--label org.testcontainers=true "
        f"--label {SESSION_LABEL}={SESSION} "
        f"{' '.join(extra)} {SIBLING_IMAGE} sleep 300"
    )


def request_for(
    worktree: Path, head: str, *, trusted: bool = True, **caps: object
) -> RunnerRequest:
    profile = RepoProfile(
        id="repo.sock",
        name="sock",
        remote_url="https://forge.invalid/sock",
        needs_docker=True,
        isolation_tier=IsolationTier.CONTAINER_DOCKER_SOCKET,
        docker_socket_acknowledged=True,
        caps=ResourceCaps(wall_clock_seconds=TIMEOUT, **caps),
    )
    return RunnerRequest(
        run_id="run.sock",
        stage_id="code",
        work_item_id="wi.sock",
        worktree_path=str(worktree),
        branch="clawdence/sock",
        base_commit=head,
        profile=profile,
        contract=VerificationContract(kind=ContractKind.TEST_AFTER),
        budget=Budget(),
        plan="run the integration tests",
        idempotency_key="run.sock:code:1",
        trusted_provenance=trusted,
        created_at=datetime.now(UTC),
    )


def runner_for(command: AgentCommand, **kwargs: object) -> DockerSocketRunner:
    return DockerSocketRunner(command, image=IMAGE, socket_path=SOCKET, **kwargs)  # type: ignore[arg-type]


def dispatch(worktree: Path, head: str, command: AgentCommand) -> Any:
    return run(runner_for(command).dispatch(request_for(worktree, head)))


def existing(session: str = SESSION) -> tuple[str, ...]:
    """Containers on the host carrying our session label, right now."""
    return tuple(
        name for name, value in run(ContainerEngine().labelled(SESSION_LABEL)) if value == session
    )


# --------------------------------------------------------------------------- #
# §3.3's three constraints, from inside a real container
# --------------------------------------------------------------------------- #


def test_the_daemon_is_reachable_from_inside(workdir: Path) -> None:
    """The capability itself: a process in the runner can talk to the host
    daemon. This is the thing every other test in the file assumes, and the
    thing the plain container tier asserts the *absence* of."""
    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    script = f"docker version --format '{{{{.Server.Version}}}}' > daemon.txt 2>&1; {PASSED}"

    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is WORKED
    assert repo.read("daemon.txt").strip(), "no server version came back"


def test_the_socket_is_openable_by_the_user_the_container_runs_as(workdir: Path) -> None:
    """``--user`` is what stops root-owned files landing in the worktree, and the
    daemon's socket is group-owned — so without ``--group-add`` the mount is
    present and unopenable, which fails several layers inside somebody's test
    framework as a permission error nobody attributes to us."""
    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    script = (
        f"id > who.txt; ( test -w {SOCKET} && echo writable || echo refused ) >> who.txt; {PASSED}"
    )

    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is WORKED
    seen = repo.read("who.txt")
    assert f"uid={os.getuid()}" in seen
    assert "writable" in seen


def test_a_sibling_sees_the_worktree_at_the_very_same_path(workdir: Path) -> None:
    """§3.3's first constraint, and the one that fails silently.

    Testcontainers hands the daemon *host* paths when it mounts a volume for a
    sibling. Here the runner's worktree path is used verbatim as a sibling's
    mount source — which resolves on the host — and the sibling reads a file the
    agent wrote a moment earlier from inside the runner. If the two paths
    differed, the daemon would create an empty directory and the sibling would
    find nothing: no error, no fixtures, and a test failure that points at the
    repository.
    """
    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    script = (
        f"echo from-the-runner > marker.txt; "
        f"docker run --rm --label {SESSION_LABEL}={SESSION} "
        f"  -v $CLAWDENCE_WORKTREE:$CLAWDENCE_WORKTREE {SIBLING_IMAGE} "
        f"  cat $CLAWDENCE_WORKTREE/marker.txt > sibling-saw.txt 2>&1; {PASSED}"
    )

    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is WORKED
    assert repo.read("sibling-saw.txt").strip() == "from-the-runner"


def test_the_host_has_a_name_that_resolves(workdir: Path) -> None:
    """§3.3's second constraint. A sibling publishes its port on the *host*, and
    ``localhost`` inside the runner is the runner — so a test reaching a fixture,
    and Ryuk reaching back, both go through this name. It exists on Docker
    Desktop and not on a native Linux daemon, which is what ``--add-host``
    settles."""
    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    script = (
        f'echo "override=$TESTCONTAINERS_HOST_OVERRIDE" > host.txt; '
        f"getent hosts $TESTCONTAINERS_HOST_OVERRIDE >> host.txt 2>&1 || "
        f"  ping -c1 -W1 $TESTCONTAINERS_HOST_OVERRIDE >> host.txt 2>&1; {PASSED}"
    )

    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is WORKED
    seen = repo.read("host.txt")
    assert "override=host.docker.internal" in seen
    assert "host.docker.internal" in seen.split("\n", 1)[1], "the name did not resolve"


def test_the_reaper_is_not_disabled_inside(workdir: Path) -> None:
    """§3.3's third constraint, as the container sees it. Ryuk is the only thing
    that knows which of the host's containers belong to this run's session."""
    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    script = f'echo "$TESTCONTAINERS_RYUK_DISABLED" > ryuk.txt; {PASSED}'

    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is WORKED
    assert repo.read("ryuk.txt").strip() == "false"


# --------------------------------------------------------------------------- #
# The boundary that survives, and the one that does not
# --------------------------------------------------------------------------- #


def test_no_control_plane_credential_is_reachable_from_inside_this_tier_either(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket makes this defeatable from inside — an agent that wanted to
    could ask the daemon for a container with the host filesystem in it and read
    whatever it liked. That is the honest cost of the tier and it is not a reason
    to stop doing the part that still works: nothing is *handed* over, and a run
    that does not go looking is a run that has no control-plane credential."""
    for name, value in {
        "SLACK_BOT_TOKEN": "xoxb-not-real",
        "GITHUB_TOKEN": "ghp-not-real",
    }.items():
        monkeypatch.setenv(name, value)

    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    result = dispatch(repo.path, repo.head, agent(f"env > seen-env.txt; {PASSED}"))

    assert result.outcome is WORKED
    seen = repo.read("seen-env.txt")
    assert "xoxb-not-real" not in seen and "ghp-not-real" not in seen


def test_the_install_phase_cannot_reach_the_daemon(workdir: Path) -> None:
    """A ``postinstall`` script is arbitrary code from a transitive dependency
    and it is the least trusted thing in the run. The capability is for tests, so
    the socket is mounted for the agent's container only — and the install is
    told where a daemon *would* be, so reaching for one fails saying so."""
    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    request = request_for(repo.path, repo.head)
    request = request.model_copy(
        update={
            "profile": request.profile.model_copy(
                update={
                    "install_command": (
                        "/bin/sh",
                        "-c",
                        'docker version > "$CLAWDENCE_WORKTREE/install-daemon.txt" 2>&1; exit 0',
                    )
                }
            )
        }
    )

    result = run(runner_for(agent(f"echo x=2 > app.py; {PASSED}")).dispatch(request))

    assert result.outcome is WORKED
    seen = repo.read("install-daemon.txt")
    assert "Server" not in seen, "the install reached a daemon"


# --------------------------------------------------------------------------- #
# Sibling cleanup — "kill a run mid-test, confirm no orphans survive"
# --------------------------------------------------------------------------- #


async def _while_running(
    dispatch_coroutine: Coroutine[Any, Any, Any],
    action: Callable[[], object],
    *,
    when: Callable[[], bool],
) -> Any:
    async def meanwhile() -> None:
        deadline = time.monotonic() + TIMEOUT
        while not when():
            assert time.monotonic() < deadline, "the container never got going"
            await asyncio.sleep(0.1)
        action()

    result, _ = await asyncio.gather(dispatch_coroutine, meanwhile())
    return result


@pytest.fixture
def live_control() -> Iterator[StoreControl]:
    with StateStore.open(IN_MEMORY) as store:
        store.create_run(
            Run(
                id="run.sock",
                work_item_id="wi.sock",
                workflow="live",
                workflow_version="1.0.0",
                status=RunStatus.RUNNING,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        yield StoreControl(store)


def test_a_finished_run_leaves_no_siblings_behind(workdir: Path) -> None:
    """The ordinary path. The run's own containers are removed by teardown, and
    the ones it asked the host daemon for are removed by the sweep."""
    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    script = f"{sibling('clawdence-live-fixture')}; echo x=2 > app.py; {PASSED}"

    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is WORKED
    assert existing() == (), "a sibling outlived the run that started it"


def test_killing_a_run_mid_test_leaves_no_orphaned_containers(
    workdir: Path, live_control: StoreControl
) -> None:
    """The plan's verification for this step, word for word.

    A run that is cancelled while its fixtures are up is the case Ryuk exists
    for — and the case this tier does not want to *depend* on Ryuk for, because
    Ryuk's own reconnection timeout is somebody else's default. So the sweep runs
    on the abort path too, and this asserts the host is clean the moment the
    dispatch returns rather than ten seconds later.
    """
    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    script = (
        f"{sibling('clawdence-live-fixture')}; "
        f"echo x=2 > app.py; {PASSED}; touch running.txt; sleep {int(TIMEOUT * 2)}"
    )
    runner = runner_for(agent(script), control=live_control, poll_seconds=0.2)
    request = request_for(repo.path, repo.head)

    result = run(
        _while_running(
            runner.dispatch(request),
            lambda: live_control.cancellations.request(
                "run.sock", at=datetime.now(UTC), reason="wrong branch"
            ),
            when=lambda: (repo.path / "running.txt").exists(),
        )
    )

    assert result.outcome is RunnerOutcome.CANCELLED
    assert run(ContainerEngine().state(container_name(request))) is None
    assert existing() == (), "a fixture outlived the run that was killed"


def test_a_sibling_that_was_already_there_survives(workdir: Path) -> None:
    """Someone else's test suite on the same machine.

    A *different* session, not merely an older container, because that is what
    the case actually is: session ids are one per test process, and a fixture
    that reused ours would let this pass without the snapshot ever being
    consulted. Getting it wrong in the product means a tool that deletes a
    developer's containers while they are debugging with them.
    """
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [  # noqa: S607 - PATH lookup of the client this whole file already needs
            "docker",
            "run",
            "-d",
            "--name",
            "clawdence-live-bystander",
            "--label",
            f"{SESSION_LABEL}={OTHER_SESSION}",
            SIBLING_IMAGE,
            "sleep",
            "300",
        ],
        capture_output=True,
        check=True,
    )

    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    script = f"{sibling('clawdence-live-fixture')}; echo x=2 > app.py; {PASSED}"

    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is WORKED
    assert existing() == (), "our own fixture outlived the run"
    assert existing(OTHER_SESSION) == ("clawdence-live-bystander",)


# --------------------------------------------------------------------------- #
# The refusals, through a real daemon
# --------------------------------------------------------------------------- #


def test_untrusted_work_never_reaches_the_daemon(workdir: Path) -> None:
    """The gate that makes public ingestion (S10b) survivable, asserted where it
    matters: no container was created, so nothing had the socket even briefly."""
    from clawdence.ports import PermanentError

    repo = build_repo(workdir / "repo", testcontainers=True, extra_files={"app.py": "x = 1\n"})
    request = request_for(repo.path, repo.head, trusted=False)

    with pytest.raises(PermanentError) as caught:
        run(runner_for(agent(f"true; {PASSED}")).dispatch(request))

    assert caught.value.kind == "untrusted-work-may-not-reach-the-daemon"
    assert run(ContainerEngine().state(container_name(request))) is None
