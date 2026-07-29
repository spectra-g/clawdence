"""Driving the fake container engine, and reading back what it was asked for.

The awkward part this hides: ``ContainerEngine.path`` is one string, so there is
nowhere to pass "and write your state here". The fix is a generated wrapper — a
one-line executable named ``docker`` with the state directory baked into it —
which also means the runner is exercised through a genuine ``PATH``-less absolute
executable rather than through a patched function, and so the argv assertions are
assertions about a real ``execve``.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from clawdence.runners.engine import ContainerEngine

SCRIPT: Path = Path(__file__).with_name("_engine_cli.py")


@dataclass(frozen=True, slots=True)
class Call:
    """One invocation of the engine client, parsed enough to ask questions of."""

    argv: tuple[str, ...]

    @property
    def command(self) -> str:
        return self.argv[0] if self.argv else ""

    def value(self, flag: str) -> str | None:
        """The first value given for ``flag``, in either spelling."""
        for index, token in enumerate(self.argv):
            if token == flag and index + 1 < len(self.argv):
                return self.argv[index + 1]
            if token.startswith(f"{flag}="):
                return token.split("=", 1)[1]
        return None

    def values(self, flag: str) -> tuple[str, ...]:
        """Every value given for ``flag`` — ``--env`` and ``--mount`` repeat."""
        found = [
            self.argv[index + 1]
            for index, token in enumerate(self.argv)
            if token == flag and index + 1 < len(self.argv)
        ]
        return tuple(found)

    def has(self, flag: str) -> bool:
        return flag in self.argv

    @property
    def image(self) -> str:
        """The image name: the first bare token that is not somebody's value."""
        from tests.harness._engine_cli import _parse

        _, image, _ = _parse(list(self.argv[1:]))
        return image

    @property
    def container_argv(self) -> tuple[str, ...]:
        from tests.harness._engine_cli import _parse

        _, _, command = _parse(list(self.argv[1:]))
        return tuple(command)


@dataclass(frozen=True, slots=True)
class FakeEngine:
    """A container engine the test writes the interesting answers for."""

    root: Path

    @property
    def engine(self) -> ContainerEngine:
        return ContainerEngine(path=str(self._wrapper()))

    # ------------------------------------------------------------- scripting

    def oom(self) -> FakeEngine:
        """Report the next container as OOM-killed by the kernel."""
        return self._script("oom", "1")

    def created_at(self, when: datetime) -> FakeEngine:
        """Backdate the next container. The alternative is waiting a week."""
        return self._script("created", when.isoformat().replace("+00:00", "Z"))

    def refuse_to_start(self, code: int = 125) -> FakeEngine:
        """Fail the way a client fails when the image is not there.

        No container is created, which is the part that matters: the tier has to
        tell "the agent exited non-zero" from "there was never an agent", and the
        absence of a container is the only signal that says so.
        """
        return self._script("client-failure", str(code))

    # --------------------------------------------------------------- reading

    def calls(self) -> tuple[Call, ...]:
        log = self.root / "calls.jsonl"
        if not log.is_file():
            return ()
        return tuple(
            Call(argv=tuple(json.loads(line)))
            for line in log.read_text(encoding="utf-8").splitlines()
            if line
        )

    def runs(self) -> tuple[Call, ...]:
        return tuple(call for call in self.calls() if call.command == "run")

    def only_run(self) -> Call:
        runs = self.runs()
        assert len(runs) == 1, f"expected one `run`, got {len(runs)}"
        return runs[0]

    def removals(self) -> tuple[str, ...]:
        return tuple(call.argv[-1] for call in self.calls() if call.command == "rm")

    # -------------------------------------------------------------- plumbing

    def _script(self, name: str, value: str) -> FakeEngine:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(value, encoding="utf-8")
        return self

    def _wrapper(self) -> Path:
        """An executable called ``docker`` that knows where to keep its state."""
        self.root.mkdir(parents=True, exist_ok=True)
        wrapper = self.root / "docker"
        if not wrapper.is_file():
            wrapper.write_text(
                f"#!{sys.executable}\n"
                "import os, runpy\n"
                f"os.environ['CLAWDENCE_FAKE_ENGINE_STATE'] = {str(self.root)!r}\n"
                f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n",
                encoding="utf-8",
            )
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        return wrapper


def container_environment(call: Call) -> dict[str, str]:
    """The environment the container would have had, from the ``run`` argv.

    Mirrors the engine's own rule, which is the rule the whole secret-handling
    design rests on: ``NAME=value`` carries its value in the command line, and a
    bare ``NAME`` does not — the engine reads that one from its own environment.
    A test asserting a credential is absent from ``env=`` and present in the
    container is asserting exactly that difference.
    """
    env: dict[str, str] = {}
    for value in call.values("--env"):
        name, separator, literal = value.partition("=")
        if separator:
            env[name] = literal
        elif name in os.environ:  # pragma: no cover - the runner sets these itself
            env[name] = os.environ[name]
    return env


def passthrough_names(call: Call) -> tuple[str, ...]:
    """Names passed by reference — the ones whose values never hit argv."""
    return tuple(value for value in call.values("--env") if "=" not in value)
