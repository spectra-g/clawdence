"""Suite-wide guarantees: no network, no leaks, and the shared fixtures.

The network guard is the one worth reading. S5's verification says a full
workflow runs "with **zero network calls** and zero LLM spend", and the only
honest way to have that is to make a network call impossible rather than to
assert afterwards that none happened. So TCP is blocked for every test, and a
test that genuinely needs a socket says so with ``@pytest.mark.allow_network``
— which makes the exceptions countable, and there are currently none.

What it covers and what it does not, stated plainly because a security-adjacent
control that overstates itself is worse than none:

- **Covered**: anything in the pytest process that opens an AF_INET or AF_INET6
  socket, and DNS resolution, which is where most accidental egress starts.
- **Not covered**: subprocesses. ``ScriptHandler`` spawns children, and a child
  has its own libc. Constraining what a *runner* can reach is the egress
  allowlist's job (S7b) and it is a different mechanism for a different threat.
- **Deliberately allowed**: AF_UNIX. Local IPC is not egress, and from S7 the
  Docker daemon socket is how a container gets started at all.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from clawdence.domain import BuildSystem
from tests.harness.cassette import Cassette, Mode
from tests.harness.cleanup import Reaper
from tests.harness.repos import FixtureRepo, build_repo, git_available

#: Address families a test may not open. AF_UNIX is absent on purpose.
_BLOCKED_FAMILIES = frozenset({socket.AF_INET, socket.AF_INET6})


class NetworkBlocked(RuntimeError):
    """A test tried to reach the network.

    Its own type so the failure reads as what it is. A ``ConnectionRefused``
    from the guard would look like a service being down, and somebody would
    eventually "fix" it by adding a retry.
    """


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_network: this test may open TCP sockets. Justify it in the test's docstring.",
    )
    config.addinivalue_line(
        "markers",
        "contract: part of the port contract suite — every adapter must pass it (`make contract`).",
    )


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Block TCP unless the test is marked ``allow_network``.

    Patching the *methods* rather than the class means an object created before
    the patch is still constrained, which matters for anything holding a module
    level session.
    """
    if request.node.get_closest_marker("allow_network") is not None:
        return

    def refuse(where: str) -> Callable[..., Any]:
        def blocked(*args: Any, **kwargs: Any) -> Any:
            raise NetworkBlocked(
                f"{where} was called, but this suite runs with no network. "
                f"Use a fake from clawdence.ports, or a cassette from tests.harness, "
                f"or mark the test @pytest.mark.allow_network and say why."
            )

        return blocked

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if self.family in _BLOCKED_FAMILIES:
            refuse("socket.connect")()
        original_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if self.family in _BLOCKED_FAMILIES:
            refuse("socket.connect_ex")()
        return original_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    # DNS as well: a name lookup is the first observable step of most accidental
    # egress, and blocking it makes the failure point at the call that meant to
    # go out rather than at whatever it was going to do next.
    monkeypatch.setattr(socket, "getaddrinfo", refuse("socket.getaddrinfo"))


@pytest.fixture
def reaper() -> Iterator[Reaper]:
    """Releases whatever a test registers, even when the test fails.

    The assertion at the end is the part that matters: a teardown that silently
    swallowed its own failures would be indistinguishable from one with nothing
    to do.
    """
    keeper = Reaper()
    yield keeper
    leaks = keeper.release_all()
    assert not leaks, "cleanup failed:\n  " + "\n  ".join(leak.describe() for leak in leaks)


@pytest.fixture
def workspace(tmp_path: Path, reaper: Reaper) -> Path:
    """A scratch directory that is registered for cleanup.

    ``tmp_path`` already gets removed by pytest eventually — after three runs,
    by default. Registering it means a test can assert the directory is gone
    *now*, which is what the container fixtures from S7 will need.
    """
    root = tmp_path / "workspace"
    root.mkdir()

    def release() -> None:
        import shutil

        shutil.rmtree(root, ignore_errors=True)

    reaper.register(f"workspace {root}", release)
    return root


RepoFactory = Callable[..., FixtureRepo]


@pytest.fixture
def repos(workspace: Path) -> RepoFactory:
    """Builds fixture repositories. Skips the test if ``git`` is missing.

    A factory rather than one repo because the interesting tests need two — a
    source and a fork, or two build systems compared.
    """
    if not git_available():
        pytest.skip("git is not on PATH, so a real fixture repository cannot be built")

    built = 0

    def build(
        build_system: BuildSystem = BuildSystem.UV,
        *,
        testcontainers: bool = False,
        extra_files: dict[str, str] | None = None,
    ) -> FixtureRepo:
        nonlocal built
        built += 1
        return build_repo(
            workspace / f"repo{built}",
            build_system=build_system,
            testcontainers=testcontainers,
            extra_files=extra_files,
        )

    return build


CassetteFactory = Callable[..., Cassette]


@pytest.fixture
def cassettes(tmp_path: Path) -> CassetteFactory:
    """Cassettes under a temporary path, in replay mode.

    Tests of the cassette *machinery* build their own; this is for tests that
    consume one. It defaults to a temporary path rather than the committed
    fixture directory so that a test cannot rewrite a fixture by accident.
    """

    def make(name: str = "default", *, mode: Mode = Mode.REPLAY) -> Cassette:
        return Cassette(tmp_path / "cassettes" / f"{name}.json", mode=mode)

    return make
