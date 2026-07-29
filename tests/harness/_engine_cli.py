"""A container engine that is not one. Driven by ``tests/harness/engine.py``.

This stands in for ``docker`` the way ``_agent_cli`` stands in for ``codex``, and
for the same reason: the questions the container tier has to answer — is the
credential out of argv, is the mount the only one, does an OOM kill get reported
as an OOM kill — are questions about *what we asked the engine for*, and asking a
real daemon costs the suite a daemon, an image pull, and a network.

It is deliberately more than a recorder. ``run`` really does execute the command
after the image name, in the workdir it was given, **with exactly the environment
the ``--env`` flags describe and nothing else** — which is what makes
"no control-plane secret reaches the container" an assertion about a process's
real environment rather than about a list of strings. A recorder would pass that
test while a bug that leaked the whole environment through went unnoticed.

What it does not simulate: isolation. Nothing here drops a capability or enforces
a memory cap, because a Python script cannot. Those claims are asserted against a
real daemon in ``tests/runners/test_container_live.py``; here they are asserted
as argv, which is the honest division — construction is testable offline, and
enforcement is not testable at all without the thing that enforces.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Flags this fake understands as taking a separate value argument. Anything the
#: runner emits that is missing here would be mistaken for the image name, so the
#: list failing to keep up shows as a loud, immediate test failure.
VALUED_FLAGS = frozenset(
    {
        "--name",
        "--workdir",
        "--network",
        "--cap-drop",
        "--security-opt",
        "--user",
        "--tmpfs",
        "--mount",
        "--label",
        "--env",
        "--cpus",
        "--memory",
        "--memory-swap",
        "--pids-limit",
        "--storage-opt",
        "--entrypoint",
        "--pull",
    }
)

BOOLEAN_FLAGS = frozenset({"--init", "--read-only", "--interactive", "--rm", "--tty"})


def main(argv: list[str]) -> int:
    state_dir = Path(os.environ["CLAWDENCE_FAKE_ENGINE_STATE"])
    state_dir.mkdir(parents=True, exist_ok=True)
    _record(state_dir, argv)

    if not argv:
        return 125
    command, rest = argv[0], argv[1:]
    if command == "run":
        return _run(state_dir, rest)
    if command == "inspect":
        return _inspect(state_dir, rest)
    if command == "rm":
        return _remove(state_dir, rest)
    if command == "ps":
        return _ps(state_dir, rest)
    if command == "version":
        sys.stdout.write("0.0.0-fake\n")
        return 0
    return 125


def _ps(state_dir: Path, argv: list[str]) -> int:
    """``--filter label=<key>`` and ``--format {{.Names}}\\t{{.Label "<key>"}}``.

    Only the one shape the reaper asks for, and it reads the labels back out of
    the recorded ``run`` argv rather than keeping a second copy of them — which
    is what makes "the reaper finds a container by the label the runner set" a
    claim about the label the runner actually set.
    """
    wanted = (_value(argv, "--filter") or "label=").partition("=")[2]
    lines = []
    for name, labels in _containers(state_dir).items():
        if wanted in labels:
            lines.append(f"{name}\t{labels[wanted]}")
    sys.stdout.write("".join(f"{line}\n" for line in sorted(lines)))
    return 0


def _containers(state_dir: Path) -> dict[str, dict[str, str]]:
    """Every container that still exists, and the labels it was created with."""
    found: dict[str, dict[str, str]] = {}
    for call in _calls(state_dir):
        if not call or call[0] != "run":
            continue
        flags, _, _ = _parse(call[1:])
        name = _one(flags, "--name")
        if name is None or not _state_path(state_dir, name).is_file():
            continue
        found[name] = dict(value.partition("=")[::2] for flag, value in flags if flag == "--label")
    return found


def _run(state_dir: Path, argv: list[str]) -> int:
    flags, _image, command = _parse(argv)
    name = _one(flags, "--name") or "unnamed"

    # Scripted before the run rather than after, because these are the two
    # things a real daemon decides and this fake cannot: whether it could start
    # a container at all, and whether the kernel took it.
    forced = _scripted(state_dir, "client-failure")
    if forced is not None:
        sys.stderr.write("fake engine: refusing to start a container\n")
        return int(forced)

    env = _container_env(flags)
    interactive = any(flag == "--interactive" for flag, _ in flags)
    try:
        child = subprocess.Popen(  # noqa: S603 - argv, no shell, and the point
            command,
            cwd=_one(flags, "--workdir") or None,
            env=env,
            # Inherited when interactive, which is how the plan crosses the
            # second boundary: our stdin is the client's, the client's is the
            # container's.
            stdin=None if interactive else subprocess.DEVNULL,
        )
    except OSError:
        # The image's command is not executable. A real engine answers 126/127
        # for this without ever creating a container, and the container tier
        # reads those as "nothing ran".
        sys.stderr.write("fake engine: cannot execute the command\n")
        return 127

    # Written before the wait, because the interesting case is that this process
    # is killed during it. A real client being killed leaves the container
    # running — the daemon owns it, not the client — and `docker rm --force` is
    # what actually ends it. Recording the pid is how that is modelled here, and
    # without it the tier's teardown would look correct while leaving the work
    # running.
    _pid_path(state_dir, name).write_text(str(child.pid), encoding="utf-8")
    code = child.wait()
    _pid_path(state_dir, name).unlink(missing_ok=True)

    _write_state(
        state_dir,
        name,
        {
            "ExitCode": 137 if code < 0 else code,
            "OOMKilled": _scripted(state_dir, "oom") is not None,
            "Error": "",
            # Not part of `.State`, and separated on the way out — the reaper
            # asks for `{{.Created}}`, which is a field of the container rather
            # than of its state. Scriptable, because the alternative way to test
            # a seven-day retention is to wait a week.
            "Created": _scripted(state_dir, "created") or _now(),
        },
    )
    return 137 if code < 0 else code


def _inspect(state_dir: Path, argv: list[str]) -> int:
    name = [value for value in argv if not value.startswith("-")][-1]
    # The format string arrives as a value of `--format`; skipping it is why the
    # name is taken from the end rather than the start.
    path = _state_path(state_dir, name)
    if not path.is_file():
        sys.stderr.write(f"Error: No such object: {name}\n")
        return 1
    state = json.loads(path.read_text(encoding="utf-8"))
    if _value(argv, "--format") == "{{.Created}}":
        sys.stdout.write(f"{state['Created']}\n")
        return 0
    # `--format {{json .State}}`, which is what the tier asks for after a run.
    sys.stdout.write(json.dumps({key: value for key, value in state.items() if key != "Created"}))
    return 0


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _remove(state_dir: Path, argv: list[str]) -> int:
    """``--force``, so this kills before it deletes — as the real one does."""
    name = [value for value in argv if not value.startswith("-")][-1]
    pid_path = _pid_path(state_dir, name)
    if pid_path.is_file():
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid_path.read_text(encoding="utf-8")), signal.SIGKILL)
        pid_path.unlink(missing_ok=True)

    path = _state_path(state_dir, name)
    if not path.is_file():
        sys.stderr.write(f"Error: No such container: {name}\n")
        return 1
    path.unlink()
    return 0


def _container_env(flags: list[tuple[str, str]]) -> dict[str, str]:
    """Exactly what the ``--env`` flags describe, and nothing else.

    Two forms, and the difference is the whole point of the split: ``NAME=value``
    carries its own value, and a bare ``NAME`` means "take it from my
    environment" — which is how a credential reaches a container without passing
    through a command line anything can read.
    """
    env: dict[str, str] = {}
    for flag, value in flags:
        if flag != "--env":
            continue
        name, separator, literal = value.partition("=")
        if separator:
            env[name] = literal
        elif name in os.environ:
            env[name] = os.environ[name]
    return env


def _parse(argv: list[str]) -> tuple[list[tuple[str, str]], str, list[str]]:
    """Split ``run``'s arguments into flags, the image, and the command.

    Positional-after-flags, the way the real client does it: the first bare token
    that is not somebody's value is the image, and everything after it belongs to
    the container.
    """
    flags: list[tuple[str, str]] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in BOOLEAN_FLAGS:
            flags.append((token, ""))
            index += 1
        elif token in VALUED_FLAGS:
            flags.append((token, argv[index + 1]))
            index += 2
        elif token.startswith("--") and "=" in token:
            name, _, value = token.partition("=")
            flags.append((name, value))
            index += 1
        else:
            break
    if index >= len(argv):
        return flags, "", []
    return flags, argv[index], argv[index + 1 :]


def _one(flags: list[tuple[str, str]], name: str) -> str | None:
    for flag, value in flags:
        if flag == name:
            return value
    return None


def _value(argv: list[str], flag: str) -> str | None:
    """The value after ``flag`` in a raw argument list. For the sub-commands
    that are flat enough not to need ``_parse``."""
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _calls(state_dir: Path) -> list[list[str]]:
    log = state_dir / "calls.jsonl"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _scripted(state_dir: Path, name: str) -> str | None:
    path = state_dir / name
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def _state_path(state_dir: Path, name: str) -> Path:
    return state_dir / f"state-{name}.json"


def _pid_path(state_dir: Path, name: str) -> Path:
    return state_dir / f"pid-{name}"


def _write_state(state_dir: Path, name: str, state: dict[str, Any]) -> None:
    _state_path(state_dir, name).write_text(json.dumps(state), encoding="utf-8")


def _record(state_dir: Path, argv: list[str]) -> None:
    """Append this invocation to the call log the test reads."""
    with (state_dir / "calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
