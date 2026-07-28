"""Building a fake agent CLI, one behaviour at a time.

The runner's failure taxonomy has eleven values and the plan's verification asks
for each one to be reproducible in a test. That is only possible if the thing
under the runner can be told to time out, to be SIGKILLed, to overspend, to lie
in its verdict, and to change nothing — on demand, deterministically, offline,
for free. So the tests do not run codex-cli; they run a program that behaves like
whichever codex-cli they need, built here.

Fluent because the alternative is a dozen keyword arguments that are mostly
``None``, and because order is load-bearing: reporting tokens *before* a long
sleep is what makes the budget kill observable, and doing it the other way round
tests nothing.

    agent = (
        FakeAgent()
        .say("editing")
        .write("app.py", "print(1)\\n")
        .verdict(status="passed")
        .command()
    )

What this is not: an agent. It does not read the plan, and nothing here should
ever try to interpret one. It is a controllable subprocess, and the value of a
controllable subprocess is that the test says exactly what happens.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from clawdence.runners import AgentCommand, PlanDelivery
from clawdence.runners.host import TokenPrice
from clawdence.runners.stream import Accumulation

#: The script this drives. Beside this module, so moving one moves the other.
SCRIPT: Path = Path(__file__).with_name("_agent_cli.py")


@dataclass(slots=True)
class FakeAgent:
    """A program for ``_agent_cli.py``, and the ``AgentCommand`` that runs it."""

    steps: list[list[Any]] = field(default_factory=list)

    # ------------------------------------------------------------- behaviour

    def say(self, text: str) -> Self:
        """Print a line to stdout."""
        return self._add("say", text)

    def warn(self, text: str) -> Self:
        """Print a line to stderr."""
        return self._add("warn", text)

    def tokens(self, count: int, *, label: str = "tokens used") -> Self:
        """Report token usage the way a CLI does — in prose, on stdout."""
        return self.say(f"{label}: {count}")

    def write(self, path: str, contents: str) -> Self:
        """Write a file inside the worktree. This is 'doing the work'."""
        return self._add("write", [path, contents])

    def append(self, path: str, text: str) -> Self:
        """Append to a file — a side effect that counts how often this ran."""
        return self._add("append", [path, text])

    def verdict(
        self,
        *,
        status: str = "passed",
        summary: str | None = None,
        tests: dict[str, Any] | None = None,
        raw: str | None = None,
        **extra: Any,
    ) -> Self:
        """Write the verdict file.

        ``raw`` writes the text through untouched, which is how a test produces
        a malformed verdict, an oversized one, or one from a protocol this
        version does not know.
        """
        if raw is not None:
            return self._add("verdict", raw)
        body: dict[str, Any] = {"status": status, **extra}
        if summary is not None:
            body["summary"] = summary
        if tests is not None:
            body["tests"] = tests
        return self._add("verdict", json.dumps(body))

    def dump_env(self, path: str) -> Self:
        """Write the child's whole environment to a file, sorted.

        This is how the trust boundary gets an automated assertion rather than a
        claim: the test reads the file and checks that no control-plane
        credential is in it (§3.1, threat model T3).
        """
        return self._add("dump-env", path)

    def read_stdin(self, path: str = "") -> Self:
        """Consume the plan from stdin, optionally saving it."""
        return self._add("read-stdin", path)

    def sleep(self, seconds: float) -> Self:
        """Do nothing for a while — a runner that has stopped making progress."""
        return self._add("sleep", seconds)

    def sigkill(self) -> Self:
        """Die the way the OOM killer kills things."""
        return self._add("sigkill", None)

    def exit_with(self, code: int) -> Self:
        """Stop here with this exit status. Later steps do not run."""
        return self._add("exit", code)

    # --------------------------------------------------------------- command

    def command(
        self,
        *,
        delivery: PlanDelivery = PlanDelivery.STDIN,
        conventions_filename: str = "AGENTS.md",
        extra_env: dict[str, str] | None = None,
        secret_env: dict[str, str] | None = None,
        accumulation: Accumulation = Accumulation.CUMULATIVE,
        prices: TokenPrice | None = None,
        include_stderr_tail: bool = False,
    ) -> AgentCommand:
        return AgentCommand(
            argv=(sys.executable, str(SCRIPT), json.dumps(self.steps)),
            delivery=delivery,
            conventions_filename=conventions_filename,
            extra_env=extra_env or {},
            secret_env=secret_env or {},
            accumulation=accumulation,
            prices=prices,
            include_stderr_tail=include_stderr_tail,
        )

    def _add(self, action: str, value: Any) -> Self:
        self.steps.append([action, value])
        return self


def missing_command() -> AgentCommand:
    """A CLI that is not installed. Produces ``STARTUP_FAILED``."""
    return AgentCommand(argv=("clawdence-agent-that-does-not-exist",))
