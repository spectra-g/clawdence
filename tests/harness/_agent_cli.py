"""A stand-in for codex-cli, driven by a script. Runs as a subprocess.

The runner's contract with an agent CLI is: we spawn it, it writes to a worktree,
it prints things, it exits. Everything interesting about ``HostRunner`` is what
happens around the edges of that — a process that never exits, one that is
SIGKILLed, one that reports more tokens than the budget allows, one that writes a
verdict claiming success while changing nothing. A real CLI can be made to do
approximately none of those on demand, and needs a network and a key to do
anything at all.

So this is the thing under the runner in every test: a small program that does
exactly what a JSON program tells it to, in order. ``tests.harness.agent``
builds those programs; this executes them.

Not named ``test_*`` so pytest does not collect it, and invoked through
``sys.executable`` so it needs nothing on ``PATH``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


def main(argv: list[str]) -> int:
    program: list[list[Any]] = json.loads(argv[1]) if len(argv) > 1 else []

    for step in program:
        action, value = step[0], step[1]

        if action == "say":
            print(value, flush=True)
        elif action == "warn":
            print(value, file=sys.stderr, flush=True)
        elif action == "write":
            target = Path(value[0])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value[1], encoding="utf-8")
        elif action == "append":
            target = Path(value[0])
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(value[1])
        elif action == "verdict":
            # Written as raw text, never re-serialised, so a test can hand this
            # malformed JSON and see what the reader does with it.
            path = Path(os.environ.get("CLAWDENCE_VERDICT_PATH", ".clawdence/verdict.json"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        elif action == "dump-env":
            Path(value).write_text(
                "\n".join(f"{name}={os.environ[name]}" for name in sorted(os.environ)),
                encoding="utf-8",
            )
        elif action == "read-stdin":
            data = sys.stdin.read()
            print(f"plan bytes: {len(data.encode('utf-8'))}", flush=True)
            if value:
                Path(value).write_text(data, encoding="utf-8")
        elif action == "sleep":
            time.sleep(float(value))
        elif action == "sigkill":
            # What the OOM killer does, and the only way to produce that
            # signature without actually exhausting the machine's memory.
            os.kill(os.getpid(), signal.SIGKILL)
        elif action == "exit":
            return int(value)
        else:  # pragma: no cover - a typo in a test's program
            raise SystemExit(f"unknown fake-agent action: {action!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
