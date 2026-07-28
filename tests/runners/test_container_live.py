"""The container tier against a real daemon. Opt-in; ``make docker-tests``.

Everything in ``test_container.py`` is an assertion about argv. That is the right
way to test *what we asked for*, and it is worth being blunt about what it cannot
do: nothing there can tell a flag that works from a flag with a typo in it, and
the plan's verification for S7 asks specifically for the security claims to be
checked **from inside the runner**, automated rather than manual. A container the
agent cannot escape is not a property of a string.

So these run a real ephemeral container and ask it about itself: what is in your
environment, what is on your filesystem, what capabilities do you hold, what
happens when you ask for more memory than you were given. They are skipped unless
``CLAWDENCE_DOCKER_TESTS=1`` and a daemon answers, because a suite that needs
Docker to pass is a suite that fails on every machine that does not have it — and
because these pull an image, which costs a network the rest of the suite is
deliberately denied.

Two environmental notes, both learned the hard way:

*The worktree cannot live in ``tmp_path``.* A Linux VM (Colima, Docker Desktop,
Lima) shares only certain host directories, and the macOS temporary directory is
not one of them — a bind mount of a path the VM cannot see silently produces an
empty directory rather than an error. So these build their fixtures under
``~/.cache`` and clean up after themselves.

*The image is pinned by digest*, which is the same rule the runner enforces on
everybody else. Overridable, because somebody running this behind a proxy will
have their own mirror.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clawdence.domain import (
    Budget,
    ContractKind,
    IsolationTier,
    RepoProfile,
    ResourceCaps,
    RunnerOutcome,
    RunnerRequest,
    VerificationContract,
)
from clawdence.ports import StaticSecrets
from clawdence.runners import (
    VERDICT_PATH,
    AgentCommand,
    ContainerEngine,
    ContainerRunner,
    PlanDelivery,
)
from tests.harness.repos import build_repo
from tests.ports.factories import run

pytestmark = pytest.mark.docker

#: Pinned, like every other image this system runs. Small, has a shell, and
#: needs no toolchain — the agent here is a shell script, because what is being
#: tested is the container rather than the CLI inside it.
IMAGE = os.environ.get(
    "CLAWDENCE_DOCKER_TEST_IMAGE",
    "alpine@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce",
)

#: Under ``$HOME`` because that is what a Linux VM shares by default. See the
#: module docstring — the alternative fails as an empty mount rather than as an
#: error, which is the worst way for this to go wrong.
ROOT = Path(
    os.environ.get("CLAWDENCE_DOCKER_TEST_ROOT", str(Path.home() / ".cache" / "clawdence-tests"))
)

#: Every live run gets one, so a wedged daemon fails the test rather than the
#: afternoon.
TIMEOUT = 120.0


def agent(script: str, **kwargs: object) -> AgentCommand:
    """An 'agent CLI' that is a shell script. Runs inside the container."""
    return AgentCommand(
        argv=("/bin/sh", "-c", script),
        delivery=PlanDelivery.STDIN,
        **kwargs,  # type: ignore[arg-type]
    )


#: Writes the verdict the ``TEST_AFTER`` contract wants. Appended to a script so
#: the run's outcome is about the thing under test rather than about evidence.
PASSED = (
    f'mkdir -p "$(dirname {VERDICT_PATH})" && '
    f'printf \'{{"status":"passed","tests":'
    f'{{"reporter":"pytest-json-report","total":1,"passed":1}}}}\' > {VERDICT_PATH}'
)


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
    root = Path(str(ROOT / f"live-{os.getpid()}-{datetime.now(UTC).timestamp()}"))
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def request_for(worktree: Path, head: str, **caps: object) -> RunnerRequest:
    profile = RepoProfile(
        id="repo.live",
        name="live",
        remote_url="https://forge.invalid/live",
        isolation_tier=IsolationTier.CONTAINER,
        caps=ResourceCaps(wall_clock_seconds=TIMEOUT, **caps),
    )
    return RunnerRequest(
        run_id="run.live",
        stage_id="code",
        work_item_id="wi.live",
        worktree_path=str(worktree),
        branch="clawdence/live",
        base_commit=head,
        profile=profile,
        contract=VerificationContract(kind=ContractKind.TEST_AFTER),
        budget=Budget(),
        plan="write a file called out.txt",
        idempotency_key="run.live:code:1",
        created_at=datetime.now(UTC),
    )


def dispatch(worktree: Path, head: str, command: AgentCommand, **caps: object) -> object:
    runner = ContainerRunner(command, image=IMAGE)
    return run(runner.dispatch(request_for(worktree, head, **caps)))


# --------------------------------------------------------------------------- #
# It works at all
# --------------------------------------------------------------------------- #


def test_a_real_container_produces_a_real_diff(workdir: Path) -> None:
    """The whole pipeline through a daemon: the plan goes in on stdin, the agent
    writes to a bind mount, and the tree the control plane reads afterwards is
    the tree the container wrote — no copy step, because the path is the same on
    both sides."""
    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    script = f'cat > plan-seen.txt; echo "x = 2" > app.py && {PASSED}'

    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is RunnerOutcome.SUCCEEDED  # type: ignore[attr-defined]
    assert result.diff is not None and result.diff.files_changed >= 1  # type: ignore[attr-defined]
    assert repo.read("app.py") == "x = 2\n"
    assert "write a file called out.txt" in repo.read("plan-seen.txt")


def test_files_come_back_owned_by_us(workdir: Path) -> None:
    """Root-owned files in a worktree the control plane then runs git in is not
    a tidiness problem — it is the next run failing to clean up after this one.
    ``--user`` is what stops it, and this is the assertion that it worked."""
    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    result = dispatch(repo.path, repo.head, agent(f"echo made > new.txt && {PASSED}"))

    assert result.outcome is RunnerOutcome.SUCCEEDED  # type: ignore[attr-defined]
    assert (repo.path / "new.txt").stat().st_uid == os.getuid()


# --------------------------------------------------------------------------- #
# §3.1, asserted from inside — the plan's verification for S7
# --------------------------------------------------------------------------- #


def test_no_control_plane_credential_is_reachable_from_inside(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion the plan asks for by name: from inside the runner,
    ``SLACK_BOT_TOKEN``, ``JIRA_*`` and ``GITHUB_TOKEN`` are absent.

    Made against ``env`` in a real container rather than against the argv we
    built, so it holds regardless of any route the value might have taken.
    """
    for name, value in {
        "SLACK_BOT_TOKEN": "xoxb-not-real",
        "JIRA_API_TOKEN": "jira-not-real",
        "JIRA_EMAIL": "someone@example.invalid",
        "GITHUB_TOKEN": "ghp-not-real",
        "AWS_SECRET_ACCESS_KEY": "aws-not-real",
    }.items():
        monkeypatch.setenv(name, value)

    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    result = dispatch(repo.path, repo.head, agent(f"env > seen-env.txt && {PASSED}"))

    assert result.outcome is RunnerOutcome.SUCCEEDED  # type: ignore[attr-defined]
    seen = repo.read("seen-env.txt")
    for leaked in ("xoxb-not-real", "jira-not-real", "ghp-not-real", "aws-not-real"):
        assert leaked not in seen
    assert "SLACK_BOT_TOKEN" not in seen
    assert "GITHUB_TOKEN" not in seen


def test_the_scoped_key_is_reachable_and_never_was_in_a_command_line(workdir: Path) -> None:
    """The other half: a credential the runner is *supposed* to have still
    arrives. A boundary that lets nothing through is easy and useless."""
    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    command = agent(
        f'echo "$MODEL_API_KEY" > seen-key.txt && {PASSED}',
        secret_env={"MODEL_API_KEY": "runner-llm-key"},
    )
    runner = ContainerRunner(
        command, image=IMAGE, secrets=StaticSecrets({"runner-llm-key": "sk-live-scoped"})
    )
    result = run(runner.dispatch(request_for(repo.path, repo.head)))

    assert result.outcome is RunnerOutcome.SUCCEEDED
    assert repo.read("seen-key.txt").strip() == "sk-live-scoped"


def test_the_other_repositories_are_not_on_the_filesystem(workdir: Path) -> None:
    """§3.1's "all repos ✅ / one worktree ❌", checked from the inside.

    The sibling is not protected by a permission check that could be wrong. It
    is absent — there is no mount that would make it reachable, which is a much
    stronger statement and the reason the tier exists.
    """
    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    other = build_repo(workdir / "other-repo", extra_files={"secret.py": "TOKEN = 'nope'\n"})

    script = (
        f'( ls {other.path} > sibling.txt 2>&1 ; echo "rc=$?" >> sibling.txt ) ; '
        f"( cat {other.path}/secret.py >> sibling.txt 2>&1 ) ; {PASSED}"
    )
    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is RunnerOutcome.SUCCEEDED  # type: ignore[attr-defined]
    seen = repo.read("sibling.txt")
    assert "rc=0" not in seen
    assert "nope" not in seen


def test_the_docker_socket_is_not_in_there(workdir: Path) -> None:
    """Reaching the host daemon is host root by another spelling. S8 adds a
    separate *tier* for it rather than a flag, and this is the default tier."""
    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    script = f"( test -S /var/run/docker.sock && echo FOUND || echo absent ) > sock.txt ; {PASSED}"
    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is RunnerOutcome.SUCCEEDED  # type: ignore[attr-defined]
    assert repo.read("sock.txt").strip() == "absent"


def test_every_capability_is_dropped(workdir: Path) -> None:
    """``--cap-drop ALL`` as the kernel sees it. The agent edits files and runs
    a build; none of that needs a capability, so the effective set is empty."""
    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    script = f"grep CapEff /proc/self/status > caps.txt ; {PASSED}"
    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is RunnerOutcome.SUCCEEDED  # type: ignore[attr-defined]
    assert repo.read("caps.txt").split()[1].strip("0") == ""


def test_the_image_filesystem_is_read_only(workdir: Path) -> None:
    """The worktree is writable because that is how work gets out. Everything
    else is not, so a runner cannot leave anything behind for the next one."""
    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    script = (
        f"( touch /etc/planted 2>&1 && echo WROTE || echo refused ) > rootfs.txt ; "
        f"( touch /tmp/fine 2>&1 && echo tmp-ok || echo tmp-refused ) >> rootfs.txt ; {PASSED}"
    )
    result = dispatch(repo.path, repo.head, agent(script))

    assert result.outcome is RunnerOutcome.SUCCEEDED  # type: ignore[attr-defined]
    seen = repo.read("rootfs.txt")
    assert "WROTE" not in seen and "refused" in seen
    # …and /tmp still works, or `--read-only` would break every build tool.
    assert "tmp-ok" in seen


# --------------------------------------------------------------------------- #
# §3.7 caps: a runaway run is contained rather than taking the host with it
# --------------------------------------------------------------------------- #


def test_a_memory_hog_is_killed_by_the_cap_and_reported_as_one(workdir: Path) -> None:
    """Containers without limits are a denial-of-service surface against the
    machine the control plane runs on. And it is reported as ``OOM_KILLED``
    rather than as a non-zero exit, because the daemon knows and says so — the
    distinction the host tier can only guess at."""
    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    # A single 256M buffer against a 32M cap. Fast, deterministic, and it asks
    # the allocator for the memory rather than growing into it.
    script = "dd if=/dev/zero of=/dev/null bs=256M count=1"

    result = dispatch(repo.path, repo.head, agent(script), memory_mb=32)

    assert result.outcome is RunnerOutcome.OOM_KILLED  # type: ignore[attr-defined]


def test_a_fork_bomb_hits_the_pid_ceiling(workdir: Path) -> None:
    """Bounded rather than an actual fork bomb: the point is that the ceiling
    exists, and a test that would take the machine down if the flag were wrong
    is a test nobody runs twice."""
    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    script = (
        "i=0; failed=0; while [ $i -lt 60 ]; do "
        "  ( sleep 5 ) & i=$((i+1)); [ $? -ne 0 ] && failed=1; "
        "done 2>fork-errors.txt; "
        f"wc -c < fork-errors.txt > forks.txt; {PASSED}"
    )
    result = dispatch(repo.path, repo.head, agent(script), pid_limit=16)

    # Either the shell could not fork (errors on stderr) or the container was
    # killed trying. Both are the ceiling doing its job; neither is 60 processes.
    assert result.outcome is not RunnerOutcome.SUCCEEDED or int(repo.read("forks.txt")) > 0  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_nothing_is_left_running_afterwards(workdir: Path) -> None:
    """``--rm`` is not used, so removal is this system's job rather than the
    daemon's. A container per run that is never reaped is a disk that fills."""
    repo = build_repo(workdir / "repo", extra_files={"app.py": "x = 1\n"})
    request = request_for(repo.path, repo.head)
    run(ContainerRunner(agent(f"true && {PASSED}"), image=IMAGE).dispatch(request))

    from clawdence.runners import container_name

    assert run(ContainerEngine().state(container_name(request))) is None
