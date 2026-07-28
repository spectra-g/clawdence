"""What a step type actually does — and, for three of the four, what it will do.

The executor owns control flow and knows nothing about step types; handlers own
step types and know nothing about control flow. That split is what lets S12
(agent), S6 (runner) and S17 (approval) each arrive as one registration without
reopening the executor — and S6 did arrive exactly that way, as
``clawdence.runners.RunnerHandler``.

The default registry still ships one handler, because a ``runner`` step needs a
repository, a worktree and a branch, and choosing those is triage's job (S11).
The other three fail loudly with an error naming the step that will supply what
they are missing, rather than succeeding vacuously — a stub that
returns success makes a workflow look like it ran, which is the most expensive
possible way to be wrong about an orchestrator. ``StubHandler`` exists for tests
and for S3c's dry-run, and has to be registered deliberately.

Script steps get an environment they were **given**, not the one the control
plane happens to hold. The control plane holds every provider key in the
system (ARCHITECTURE Zone 2); handing ``os.environ`` to a subprocess would put
all of them one ``env | curl`` away from any workflow that can run a script.
The child gets ``env:`` from the stage plus a fixed, boring allowlist.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Protocol

from pydantic import JsonValue

from clawdence.domain import Stage, StepType
from clawdence.domain.workflow import ScriptStage
from clawdence.engine import interpolation
from clawdence.engine.errors import EngineError, InterpolationError, StepFailure
from clawdence.engine.refs import Resolver

#: Variables a subprocess is passed from the control plane's own environment.
#: Deliberately dull: enough for a command to find its interpreter and behave
#: predictably, and nothing that names a credential. Anything else a step needs
#: is declared in the workflow, where it is reviewable.
INHERITED_ENV: Final[tuple[str, ...]] = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")

#: Per stream. v1's processing log reached 300MB; a step that prints a build's
#: entire output should not be able to put it in the run record.
MAX_CAPTURE_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class StepContext:
    """Everything a handler is allowed to see."""

    run_id: str
    stage: Stage
    attempt: int
    resolver: Resolver


@dataclass(frozen=True, slots=True)
class HandlerOutcome:
    """What a successful handler produced.

    ``output`` is what the step *made*; ``response`` is what a *human*
    submitted. Handlers other than approval leave ``response`` alone — the
    separation is what keeps "the model decided" and "a person decided"
    distinguishable in the audit trail (S2).
    """

    output: JsonValue = None
    response: JsonValue = None


class StepHandler(Protocol):
    """Runs one attempt of one stage.

    Returns on success; raises ``StepFailure`` on failure. Exceptions rather
    than a result union because handlers are awaited under ``asyncio.wait_for``,
    where cancellation already arrives as an exception — one path out for
    "did not finish", not two.
    """

    async def __call__(self, ctx: StepContext) -> HandlerOutcome: ...


class UnimplementedHandler:
    """Refuses, naming the step that will make it work.

    ``why`` exists because "not implemented" stopped being the whole truth for
    ``runner`` steps at S6: the runner is built and tested, and what is missing
    is the part that decides which repository, worktree and branch to point it
    at. Saying "not implemented" there would send somebody looking for code that
    is already written.
    """

    __slots__ = ("_owner", "_step_type", "_why")

    def __init__(
        self, step_type: StepType, owner: str, *, why: str = "are not implemented yet"
    ) -> None:
        self._step_type = step_type
        self._owner = owner
        self._why = why

    async def __call__(self, ctx: StepContext) -> HandlerOutcome:
        raise StepFailure(
            "step-type-not-implemented",
            f"{self._step_type.value!r} steps {self._why} — {self._owner} adds them",
            retryable=False,
        )


@dataclass(slots=True)
class StubHandler:
    """Returns a canned result without doing anything.

    For tests and, later, S3c's ``workflow test``. Never in the default
    registry: a stub reachable by accident is a workflow that reports success
    for work nobody did.
    """

    output: JsonValue = None
    response: JsonValue = None
    failure: StepFailure | None = None
    #: Stage ids this handler was asked to run, in order. Tests assert on it.
    calls: list[str] = field(default_factory=list)

    async def __call__(self, ctx: StepContext) -> HandlerOutcome:
        self.calls.append(ctx.stage.id)
        if self.failure is not None:
            raise self.failure
        return HandlerOutcome(output=self.output, response=self.response)


class ScriptHandler:
    """Runs a command as argv. No shell, ever.

    The output is always the same shape, whatever the command did::

        {"exit_code": int, "stdout": str, "stderr": str,
         "parsed": <stdout decoded as JSON, or null>, "truncated": bool}

    One shape rather than "the parsed JSON if it parsed, else an envelope",
    because a condition reading ``$build.json.parsed.verdict`` should mean the
    same thing whether or not that particular run happened to emit JSON. The
    ``json`` facet names the step's structured output; ``parsed`` inside it is
    the part the command chose to say.
    """

    __slots__ = ("_environ",)

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    async def __call__(self, ctx: StepContext) -> HandlerOutcome:
        stage = ctx.stage
        if not isinstance(stage, ScriptStage):  # pragma: no cover - registry routes by type
            raise StepFailure("wrong-handler", f"{stage.id} is not a script stage")

        argv = self._argv(stage, ctx.resolver)
        env = self._env(stage, ctx.resolver)
        cwd = self._expand(stage.cwd, ctx.resolver, "cwd")
        stdin = self._expand(stage.stdin, ctx.resolver, "stdin")

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
        except OSError as exc:
            raise StepFailure(
                "script-not-runnable",
                f"could not run {argv[0]!r}: {exc.strerror or exc}",
                retryable=False,
            ) from exc

        try:
            raw_out, raw_err = await process.communicate(
                stdin.encode("utf-8") if stdin is not None else None
            )
        except asyncio.CancelledError:
            # A timeout cancels the await; the child is still running and would
            # outlive the run. Kill it *and reap it* before letting the
            # cancellation through — a killed process that is never waited on
            # stays a zombie, and its pipes stay open, so the event loop closes
            # underneath a transport that still intends to clean itself up.
            await _kill_and_reap(process)
            raise

        stdout, out_truncated = _capture(raw_out)
        stderr, err_truncated = _capture(raw_err)
        exit_code = process.returncode if process.returncode is not None else -1

        output: JsonValue = {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "parsed": _parse_json(stdout, truncated=out_truncated),
            "truncated": out_truncated or err_truncated,
        }

        if exit_code != 0:
            raise StepFailure(
                "script-exit",
                f"{argv[0]} exited {exit_code}"
                + (f": {stderr.strip().splitlines()[-1]}" if stderr.strip() else ""),
                retryable=True,
            )
        return HandlerOutcome(output=output)

    def _argv(self, stage: ScriptStage, resolver: Resolver) -> list[str]:
        # command[0] is never interpolated — the loader rejects a placeholder
        # there. Which binary runs is a decision the workflow author makes; a
        # value from a prior step choosing it would let step output pick the
        # executable, which is not a thing any workflow needs to do.
        argv = [stage.command[0]]
        argv.extend(
            self._expand(element, resolver, f"command[{index}]") or ""
            for index, element in enumerate(stage.command[1:], start=1)
        )
        return argv

    def _env(self, stage: ScriptStage, resolver: Resolver) -> dict[str, str]:
        env = {name: self._environ[name] for name in INHERITED_ENV if name in self._environ}
        for name, value in stage.env.items():
            env[name] = self._expand(value, resolver, f"env[{name}]") or ""
        return env

    def _expand(self, template: str | None, resolver: Resolver, where: str) -> str | None:
        if template is None:
            return None
        try:
            return interpolation.expand(template, resolver)
        except InterpolationError as exc:
            raise StepFailure("interpolation", f"{where}: {exc}", retryable=False) from exc


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    """Kill a child whose step is over, then wait for it.

    Both halves matter. Without the kill the process outlives the run — v1's
    stale-spawn bug. Without the wait it becomes a zombie whose pipes are still
    open, and the transport's finaliser then runs after the event loop has
    closed, which surfaces as a stray ``RuntimeError: Event loop is closed``
    from a thread nobody is looking at.

    The wait is safe inside a cancellation handler: the process has already
    been killed, so it resolves on the next loop iteration, and ``wait_for``
    cancels its inner coroutine once and then awaits the result — it does not
    re-cancel while this is unwinding.
    """
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:  # pragma: no cover - it exited between checks
        return
    with contextlib.suppress(asyncio.CancelledError):
        await process.wait()


def _capture(raw: bytes) -> tuple[str, bool]:
    truncated = len(raw) > MAX_CAPTURE_BYTES
    text = raw[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace")
    return (text + "\n[truncated]" if truncated else text), truncated


def _parse_json(stdout: str, *, truncated: bool) -> JsonValue:
    """Decode stdout as JSON, or ``None``.

    Truncated output is never parsed: a cut-off document either fails to decode
    or, worse, decodes to a prefix that is valid but wrong.
    """
    if truncated or not stdout.strip():
        return None
    try:
        decoded: JsonValue = json.loads(stdout)
    except (ValueError, RecursionError):
        return None
    return decoded


class HandlerRegistry:
    """Which handler runs which step type."""

    __slots__ = ("_handlers",)

    def __init__(self, handlers: Mapping[StepType, StepHandler]) -> None:
        self._handlers = dict(handlers)

    def for_type(self, step_type: StepType) -> StepHandler:
        handler = self._handlers.get(step_type)
        if handler is None:  # pragma: no cover - default_registry covers every type
            raise EngineError(f"no handler registered for {step_type.value!r} steps")
        return handler


def default_registry(environ: Mapping[str, str] | None = None) -> HandlerRegistry:
    """The M1 registry: script runs, the rest say who will implement them."""
    return HandlerRegistry(
        {
            StepType.SCRIPT: ScriptHandler(environ),
            StepType.AGENT: UnimplementedHandler(StepType.AGENT, "S12"),
            StepType.RUNNER: UnimplementedHandler(
                StepType.RUNNER,
                "S11",
                why=(
                    "have a runner (clawdence.runners, S6) but nothing that chooses a "
                    "repository, worktree and branch to point it at"
                ),
            ),
            StepType.APPROVAL: UnimplementedHandler(StepType.APPROVAL, "S17"),
        }
    )
