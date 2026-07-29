"""Running an agent CLI, in whatever it is that a tier gives us to run it in.

S6 wrote this as one class because there was one tier. S7 has two, and almost
none of the difference is interesting: the plan is still built the same way, the
conventions file is still installed and still hidden from git, the tree is still
re-derived with git afterwards, the tokens are still scraped off the stream, and
the result is still assembled by the same rules. What actually differs between
running the CLI as a child process and running it inside a container is **four
things**, and they are the four hooks at the bottom of ``AgentRunner``:

``_inherited``
    What the agent's environment starts from. On the host that is an allowlist
    of the control plane's own variables; in a container it is nearly nothing,
    because the image supplies ``PATH`` and ``HOME`` and passing the host's would
    describe a filesystem that is not there.

``_launch``
    argv, environment and working directory for the process we actually spawn.
    The host spawns the CLI. The container spawns the engine client, which is a
    different program with the CLI's argv on the end of it.

``_observe``
    What the tier can say about the attempt that the exit status does not. This
    is where the container asks the daemon whether the kernel OOM-killed the
    thing — the one signal ``outcome.Completion`` reserved a field for and the
    host tier can only guess at.

``_teardown``
    Giving back whatever was allocated. Nothing on the host; a container.

Everything above those four is here, once. The alternative — a second runner
class that reimplements idempotent dispatch, budget aborts and verdict handling
"for containers" — is how two tiers acquire two different bugs in the same
feature, and v1's two integrations diverging on idempotency is the same shape of
mistake one layer up.

The base class is deliberately not a ``RunnerPort`` implementation detail that
tiers extend by overriding ``dispatch``. ``dispatch`` is final in spirit: it is
where the port's obligations live (redelivery returns the first answer, an
in-flight duplicate joins rather than races) and a tier that reimplemented it
would be a tier those obligations were no longer proven for.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Final

from clawdence.domain import (
    MAX_DIRTY_PATHS,
    Budget,
    ContractKind,
    CostEntry,
    IsolationTier,
    RunnerOutcome,
    RunnerRequest,
    RunnerResult,
    TokenUsage,
)
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.errors import PermanentError
from clawdence.ports.runner import validate_result
from clawdence.ports.secrets import NullSecrets, SecretProvider
from clawdence.runners import plan as plan_text
from clawdence.runners import verdict as verdict_file
from clawdence.runners import worktree as wt
from clawdence.runners.installed import HOME_DIR, PLAN_PATH, WORK_DIR, Installed
from clawdence.runners.outcome import Completion, classify
from clawdence.runners.process import kill_and_reap
from clawdence.runners.stream import (
    Accumulation,
    LogLine,
    LogSink,
    Stream,
    Tail,
    TokenTally,
    pump,
)
from clawdence.runners.turns import TurnTracker

#: Names, and prefixes, that must never reach a runner. This is §3.1's trust
#: boundary written as a check. A denylist *on top of* each tier's allowlist —
#: the allowlist stops inheritance, this stops a caller passing them in on
#: purpose, which is the mistake that reads as reasonable in a diff.
FORBIDDEN_ENV: Final[tuple[str, ...]] = (
    "SLACK_",
    "JIRA_",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_APP_",
    "AWS_",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLAWDENCE_HOME",
)


class PlanDelivery(StrEnum):
    """How the agent CLI is given the plan.

    Three, because the CLIs anybody will wire this to each want a different one,
    and guessing wrong produces an agent that runs with an empty prompt and
    truthfully reports that there was nothing to do.
    """

    STDIN = "stdin"
    ARGUMENT = "argument"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class TokenPrice:
    """What tokens cost, per million, for the model this CLI runs.

    Configured rather than derived, because the runner cannot see which model the
    CLI chose. Required whenever a request sets ``Budget.max_usd``: a runner that
    accepted a dollar cap it had no way to evaluate would report a budget as
    enforced while enforcing nothing.
    """

    input_usd: Decimal
    output_usd: Decimal
    cached_input_usd: Decimal = Decimal("0")

    def usd(self, usage: TokenUsage, *, unattributed: int = 0) -> Decimal:
        """Cost of this usage, plus tokens reported without a breakdown.

        Unattributed tokens are priced at the output rate — the more expensive of
        the two. A cap that errs towards firing early is still a cap; one that
        errs the other way is decoration.
        """
        return (
            self.input_usd * usage.input_tokens
            + self.output_usd * (usage.output_tokens + usage.reasoning_tokens + unattributed)
            + self.cached_input_usd * usage.cached_input_tokens
        ) / Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class AgentCommand:
    """Which CLI runs, and how it is spoken to.

    Configuration rather than code, because the runner CLIs move faster than this
    system will and a hardcoded ``codex exec --full-auto`` is a dependency on
    somebody else's flag names. S22 pins the versions this has been tested
    against.
    """

    #: argv of the CLI. ``RepoProfile.exec_prefix`` is prepended at dispatch, so
    #: the toolchain wrapper is applied without this knowing about it.
    argv: tuple[str, ...]

    delivery: PlanDelivery = PlanDelivery.STDIN

    #: Filename the CLI reads repository conventions from — ``AGENTS.md`` for
    #: codex, ``CLAUDE.md`` for claude-code. v1's ``agentsMd``, with the
    #: destination named instead of assumed.
    conventions_filename: str = "AGENTS.md"

    #: Extra environment for the child. Checked against ``FORBIDDEN_ENV``.
    extra_env: Mapping[str, str] = field(default_factory=dict)

    #: Secrets to resolve by name and pass on, as ``{env var: secret name}``.
    #: Where the *scoped* LLM key goes — §3.1 gives the runner a budgeted key of
    #: its own, separate from the control plane's.
    secret_env: Mapping[str, str] = field(default_factory=dict)

    #: How this CLI's token reports combine. Wrong in either direction defeats
    #: the budget, so it is declared per CLI rather than inferred.
    accumulation: Accumulation = Accumulation.CUMULATIVE

    prices: TokenPrice | None = None

    #: Off by default. The result's ``message`` is persisted with the step, and
    #: stderr is exactly where a provider's echo of a rejected request — and so a
    #: pasted key — ends up. Turn it on locally; never where runs are stored
    #: somewhere shared.
    include_stderr_tail: bool = False


@dataclass(frozen=True, slots=True)
class Environment:
    """The agent's environment, with the secret-valued names called out.

    The split exists for one tier: a container gets its environment through an
    engine client, and ``-e NAME=value`` puts the value in that client's argv,
    where the process list can read it. Knowing which names hold credentials is
    what lets the container tier pass those by name alone. The host tier ignores
    the distinction, because there is no intermediate process to hide them from.
    """

    values: Mapping[str, str]
    secret_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class Launch:
    """What to spawn. The one thing every tier answers differently."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path


class AgentRunner(ABC):
    """A ``RunnerPort`` that runs an agent CLI. Tier supplied by the subclass.

    Settled results are remembered for the life of the instance, which is what
    makes a redelivery return the original answer rather than running the agent
    again. It is deliberately only an in-process cache: the durable answer to
    "has this attempt already happened" is the ledger's unique constraint (S4),
    which survives the process, and duplicating that here would be a second
    source of truth. For a long-lived control plane the map is one small record
    per attempt — bounded by the run, not by time — and the scheduler in the rest
    of S7 is where
    it gets an eviction policy if it ever needs one.
    """

    #: The one tier this runner will accept a request for. Never a set: a runner
    #: that accepted two tiers would be one that silently downgrades.
    tier: ClassVar[IsolationTier]

    __slots__ = (
        "_clock",
        "_command",
        "_environ",
        "_identity",
        "_inflight",
        "_secrets",
        "_settled",
        "_sink",
        "_stopping",
    )

    def __init__(
        self,
        command: AgentCommand,
        *,
        secrets: SecretProvider | None = None,
        sink: LogSink | None = None,
        clock: Clock = utc_now,
        identity: wt.GitIdentity = wt.DEFAULT_IDENTITY,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._command = command
        self._secrets: SecretProvider = NullSecrets() if secrets is None else secrets
        self._sink = sink
        self._clock = clock
        self._identity = identity
        self._environ = os.environ if environ is None else environ
        self._settled: dict[str, RunnerResult] = {}
        self._inflight: dict[str, asyncio.Task[RunnerResult]] = {}
        self._stopping: set[str] = set()

    # ------------------------------------------------------------------ port

    async def dispatch(self, request: RunnerRequest) -> RunnerResult:
        key = request.idempotency_key
        settled = self._settled.get(key)
        if settled is not None:
            return settled

        task = self._inflight.get(key)
        mine = task is None
        if task is None:
            self._preflight(request)
            task = asyncio.create_task(self._execute(request))
            self._inflight[key] = task

        try:
            # Shielded so a *redelivery* being cancelled does not stop work the
            # original dispatcher is still waiting on. The original's own
            # cancellation is handled here, where the two can be told apart.
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if mine and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise
        finally:
            if mine:
                self._inflight.pop(key, None)
                self._stopping.discard(key)

        self._settled[key] = result
        return result

    async def cancel(self, request: RunnerRequest) -> bool:
        key = request.idempotency_key
        task = self._inflight.get(key)
        if task is None or task.done():
            # Already finished, or never dispatched here. The watchdog deciding a
            # step is overdue races the step reporting; that race is normal and
            # must not itself produce a failure.
            return False
        self._stopping.add(key)
        task.cancel()
        return True

    # -------------------------------------------------------------- the work

    def _preflight(self, request: RunnerRequest) -> None:
        """Refuse a request that cannot honestly be run, before anything starts.

        ``PermanentError`` rather than a failing result, and the line is the
        port's: a result describes a run that happened, an exception describes a
        request that could not be dispatched. Retrying any of these produces the
        same refusal with the budget one attempt smaller.
        """
        tier = request.profile.isolation_tier
        if tier is not self.tier:
            raise PermanentError(
                "isolation-tier-mismatch",
                f"{request.profile.name!r} asks for {tier.value!r} isolation and this runner "
                f"provides {self.tier.value!r} — refusing rather than quietly substituting one "
                f"for the other",
            )

        if request.budget.max_usd is not None and self._command.prices is None:
            raise PermanentError(
                "no-token-prices",
                "the request sets a dollar budget and this runner has no prices configured, "
                "so the cap could not fire — set AgentCommand.prices, or drop max_usd",
            )

        if not Path(request.worktree_path).is_dir():
            raise PermanentError(
                "worktree-missing",
                f"{request.worktree_path} is not a directory on this machine",
            )

        self._check(request)

        # Built and thrown away: the point is that a forbidden variable or an
        # unresolvable secret fails now rather than after an agent has started.
        self._environment(request)

    async def _execute(self, request: RunnerRequest) -> RunnerResult:
        worktree = Path(request.worktree_path)
        started_at = self._clock()
        tally = TokenTally(accumulation=self._command.accumulation)

        blocked = await self._not_runnable(request, worktree)
        if blocked is not None:
            return self._result(request, blocked, started_at=started_at, tally=tally)

        installed = await self._prepare(request, worktree)
        try:
            try:
                completion = await self._run_agent(request, worktree, tally)
            except asyncio.CancelledError:
                if request.idempotency_key not in self._stopping:
                    # The engine's own timeout, unwinding. One path out for "did
                    # not finish" is the executor's contract, and it is a
                    # different event from an operator pressing stop.
                    raise
                completion = Completion(cancelled=True, killed_by_us=True)
            # Inside the try, and so **before teardown** (§3.10). The artifacts
            # are only true at the moment the work is collected: afterwards the
            # container is gone. On this tier they happen to survive teardown —
            # the worktree is a host bind mount — but ordering the collection
            # around that would be depending on a coincidence of the one tier
            # where the assumption holds, which is the assumption §3.10 removes.
            completion = await self._collect(request, worktree, completion, installed)
        finally:
            # Teardown before tidy, and both before anything can fail: whatever
            # the tier allocated outlives this process otherwise, and a leaked
            # container is a leaked CPU.
            await self._teardown(request)
            self._tidy(worktree, installed)

        return self._result(request, completion, started_at=started_at, tally=tally)

    async def _not_runnable(self, request: RunnerRequest, worktree: Path) -> Completion | None:
        """Checks that need git, and so cannot happen in ``_preflight``.

        A ``STARTUP_FAILED`` result rather than a raise, because by this point
        the attempt has been recorded as started and the taxonomy has a value for
        exactly this: something was wrong with the environment, nothing about the
        repository is implied, and a second attempt may well work.
        """
        if not await wt.is_repository(worktree):
            return Completion(startup_error=f"{worktree} is not a git repository")
        if not await wt.has_commit(worktree, request.base_commit):
            return Completion(
                startup_error=f"this repository does not have commit {request.base_commit}"
            )
        return None

    async def _prepare(self, request: RunnerRequest, worktree: Path) -> Installed:
        """Install what the agent needs, hide all of it from git, and record it.

        The exclusion is not housekeeping. Every file installed here would
        otherwise be picked up by ``git add --all`` and land in the pull request —
        the conventions file, the plan, the verdict — which is changes nobody
        asked for appearing in somebody's repository under our name.

        The *record* is the S6b half, and it exists because the exclusion is not
        enough on its own: ``$GIT_DIR/info/exclude`` has no effect on a path the
        repository already tracks (§3.9). Knowing the bytes is what lets
        ``_collect`` put such a path back, and it is what makes "the tree is
        dirty" mean the agent's work rather than ours.
        """
        installed = Installed(worktree=worktree)
        (worktree / HOME_DIR).mkdir(parents=True, exist_ok=True)
        verdict_file.clear(worktree)

        conventions = self._install_conventions(request, installed)
        excluded = [f"/{WORK_DIR}/"] + ([f"/{conventions}"] if conventions else [])
        await wt.exclude(worktree, *excluded)

        if self._command.delivery is PlanDelivery.FILE:
            installed.write(PLAN_PATH, plan_text.build(request))
        return installed

    def _install_conventions(self, request: RunnerRequest, installed: Installed) -> str | None:
        """Copy the repo's conventions file to where this CLI looks for it.

        v1's ``agentsMd``. The source is a control-plane path and the destination
        is the CLI's filename, which is why both ends are configured: the same
        file is ``AGENTS.md`` to one CLI and ``CLAUDE.md`` to another, and a
        repository should not have to keep two.

        **S6 skipped a destination that already existed; S6b writes over it.**
        The skip avoided one bug by causing a quieter one: a repository that
        tracks its own ``AGENTS.md`` — the common case in the repositories this
        is pointed at — silently ignored the conventions file an operator had
        configured for it, and the field went dead exactly where it was needed.
        What makes overwriting safe is the record: ``_collect`` reverts the path
        wherever it still holds our bytes, so the repository's own copy comes
        back and nothing reaches a pull request. An agent that *deliberately*
        edited the file has its edit survive, which is the case the byte
        comparison exists to protect.

        The residue, stated because it is real: an agent editing that file edits
        *our* version of it, so its diff is against our content rather than the
        repository's. That is the price of the conventions file being installed
        at all, and it is paid only on repositories that track a file at the
        same path.
        """
        source_path = request.profile.agents_md_path
        if source_path is None:
            return None
        source = Path(source_path)
        if not source.is_file():
            return None

        destination = self._command.conventions_filename
        try:
            installed.copy(source, destination)
        except OSError:
            # A conventions file we cannot install is not a reason to fail a run
            # that has not started. The agent works without it, slightly worse.
            return None
        return destination

    def _tidy(self, worktree: Path, installed: Installed) -> None:
        """Take back what this run installed. Runs even when it was cancelled.

        Only paths this run wrote, and only where they still hold what it wrote:
        an agent that replaced the conventions file with something of its own has
        made that file the agent's, and deleting it here would delete work.

        The verdict is deliberately *not* removed — it is not written by us and
        so is not in the record. It is the only account of what the agent thought
        it was doing, it is excluded from git, and the next attempt clears it
        before starting. Removing it here would delete the evidence at exactly
        the moment somebody wants to read it.
        """
        for path in installed.paths():
            if not installed.owns(path):
                continue
            with contextlib.suppress(OSError):
                (worktree / path).unlink(missing_ok=True)

    async def _run_agent(
        self,
        request: RunnerRequest,
        worktree: Path,
        tally: TokenTally,
    ) -> Completion:
        """Spawn whatever this tier spawns, stream it, and stop it when it runs
        out of something."""
        prompt = plan_text.build(request)
        launch = self._launch(request, worktree, prompt)
        feed = prompt if self._command.delivery is PlanDelivery.STDIN else None

        try:
            process = await asyncio.create_subprocess_exec(
                *launch.argv,
                cwd=launch.cwd,
                env=dict(launch.env),
                stdin=asyncio.subprocess.PIPE if feed else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            # Missing, or not executable. Nothing ran, so nothing about the
            # repository is implied — which is why this is `startup-failed` and
            # not `non-zero-exit`.
            return Completion(
                startup_error=f"could not run {launch.argv[0]!r}: {exc.strerror or exc}"
            )

        # The budget check runs on the line that reports the number, which is a
        # synchronous callback, and stopping a run is asynchronous on any tier
        # that has something to tell. So it starts a task and everything below
        # waits for it — a stop that is still in flight when the completion is
        # assembled is a container still running.
        halting: asyncio.Task[None] | None = None

        def stop() -> None:
            # Called at most once: ``_Watcher`` latches ``overspent`` before it
            # gets here, so this does not need a guard of its own.
            nonlocal halting
            halting = asyncio.create_task(self._halt(request, process))

        watcher = _Watcher(
            tally=tally,
            sink=self._sink,
            budget=request.budget,
            prices=self._command.prices,
            stop=stop,
        )
        limit, limit_is_budget = _wall_clock(request)

        timed_out = False
        try:
            await asyncio.wait_for(self._drive(process, feed, watcher), timeout=limit)
        except TimeoutError:
            timed_out = True
            await self._halt(request, process)
        except asyncio.CancelledError:
            await self._halt(request, process)
            raise
        finally:
            if halting is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await halting

        completion = Completion(
            exit_code=process.returncode,
            timed_out=timed_out and not limit_is_budget,
            budget_exceeded=watcher.overspent or (timed_out and limit_is_budget),
            killed_by_us=timed_out or watcher.overspent,
            stderr_tail=watcher.stderr.last(),
            # §3.7a: the two things the exit status cannot say. Both come off the
            # stream that was already being read for tokens — reading it twice
            # would be two chances to disagree about what arrived.
            provider_error=watcher.turns.terminal_error,
            model_turn_seen=watcher.turns.model_turn_seen,
        )
        return await self._observe(request, completion)

    async def _drive(
        self,
        process: asyncio.subprocess.Process,
        feed: str | None,
        watcher: _Watcher,
    ) -> None:
        """Write the prompt and read both streams, all at once.

        Concurrently, and that is the whole reason this is a separate method: a
        plan large enough to fill the pipe buffer blocks the write until the
        child reads it, and a child that does not read until it has printed
        something blocks on the write. Sequential code deadlocks on a long plan
        and works on every short one, which is the worst way for a bug to behave.
        """
        readers = [
            pump(reader, name, on_line=watcher.line, clock=self._clock)
            for reader, name in ((process.stdout, Stream.STDOUT), (process.stderr, Stream.STDERR))
            if reader is not None
        ]
        await asyncio.gather(_feed(process, feed), *readers)
        await process.wait()

    async def _collect(
        self,
        request: RunnerRequest,
        worktree: Path,
        completion: Completion,
        installed: Installed,
    ) -> Completion:
        """Take the work, and the artifacts that say what it is (§3.10).

        Re-derived rather than reported: the diff comes from ``git``, not from
        the agent, because the number that decides whether a pull request gets
        opened should not come from the process being judged.

        **The order is the design.** Each of the first three steps is only
        answerable before the one after it has run:

        1. *What did the agent leave uncommitted* — asked before anything is
           committed, and with the runner's own installed files taken out,
           because our plan and conventions file are in that tree on every run
           and a naive probe would call every run dirty.
        2. *How many commits did the agent make* — asked before the safety
           commit below, which would otherwise be counted as one of them and
           make ``DROPPED_COMMIT`` inexpressible.
        3. *Put our files back* — the §3.9 repair. It reads ``owns`` per path,
           so it has to run after step 1 has already decided whose each path is:
           reverting first would turn every installed path into agent dirt.

        Only then the safety commit. It stays, and it is why a dropped commit is
        reported rather than lost: the work is preserved on a real tree that a
        person can look at, and the *outcome* is what says the agent never
        claimed it.
        """
        verdict = None
        problem = None
        try:
            verdict = verdict_file.read(worktree)
        except verdict_file.VerdictError as exc:
            # A verdict that does not validate is an absent verdict plus a
            # complaint. It is not fatal — what an absence *means* is the
            # contract's business, and `_requires_evidence` is what says so.
            problem = str(exc)

        try:
            pending = await wt.pending_changes(worktree)
            ours = tuple(path for path in pending if installed.owns(path))
            theirs = tuple(path for path in pending if path not in set(ours))

            commits = await wt.commits_ahead(worktree, request.base_commit)
            await self._reclaim(worktree, request.base_commit, installed)

            await wt.commit_all(
                worktree,
                f"clawdence: {request.stage_id} for {request.work_item_id}",
                identity=self._identity,
            )
            head = await wt.head(worktree)
            diff = await wt.diff_stat(worktree, request.base_commit)
        except (wt.GitError, OSError) as exc:
            # The tree cannot be read, so nothing downstream can be told what
            # changed. Reported rather than invented as an empty diff, which
            # would look exactly like an agent that did nothing.
            return replace(completion, startup_error=f"the worktree could not be read: {exc}")

        return replace(
            completion,
            verdict=verdict,
            tree_hash=head,
            diff=diff,
            files_changed=diff.files_changed,
            commits_ahead=commits,
            # Truncated rather than refused: the domain caps this field, and a
            # result that failed validation for having too many paths in it
            # would be the reporting destroying the report.
            dirty_paths=theirs[:MAX_DIRTY_PATHS],
            requires_evidence=request.contract.kind in _NEEDS_EVIDENCE,
            stderr_tail=problem or completion.stderr_tail,
        )

    async def _reclaim(self, worktree: Path, base: str, installed: Installed) -> None:
        """Undo the runner's own installs, wherever they are still the runner's.

        The condition is the entire control. A path that still holds the bytes we
        wrote was never touched by the agent and has no business in a pull
        request; a path whose contents have changed is the agent's work, whatever
        we originally put there, and reverting it would delete a deliberate edit
        by an agent that was asked to make one.

        Reverting to the **base** rather than undoing a modification is what
        makes this work against a real CLI: agents run ``git add --all``, and by
        the time this runs our conventions file is usually already committed.
        ``wt.revert_to`` explains the four cases.
        """
        for path in installed.paths():
            if installed.owns(path):
                await wt.revert_to(worktree, base, path)

    # ----------------------------------------------------------------- hooks

    @abstractmethod
    def _inherited(self, request: RunnerRequest) -> dict[str, str]:
        """What the agent's environment starts from, before the run's own."""

    @abstractmethod
    def _launch(self, request: RunnerRequest, worktree: Path, prompt: str) -> Launch:
        """argv, environment and cwd of the process to spawn."""

    # The three below are *optional* hooks, not abstract ones, and doing nothing
    # is a correct implementation of each: the host tier has no preflight of its
    # own, nothing to ask a supervisor, and nothing to give back. Making them
    # abstract would force every tier to write three empty methods to say so.
    def _check(self, request: RunnerRequest) -> None:  # noqa: B027
        """Tier-specific preflight. Raises ``PermanentError``, or does nothing."""

    async def _halt(self, request: RunnerRequest, process: asyncio.subprocess.Process) -> None:
        """Stop the work. Called for a timeout, a budget kill, and a cancel.

        The default — kill the child and reap it — is right only where the child
        *is* the work. A tier that spawns the agent somewhere else has to stop it
        there first, because the process we hold is a client and killing a client
        does not stop what it asked for.
        """
        await kill_and_reap(process)

    async def _observe(self, request: RunnerRequest, completion: Completion) -> Completion:
        """Anything the tier knows that the exit status does not say."""
        return completion

    async def _teardown(self, request: RunnerRequest) -> None:  # noqa: B027
        """Release whatever the tier allocated. Must be safe to call twice."""

    # -------------------------------------------------------------- plumbing

    def _cli_argv(self, request: RunnerRequest, prompt: str) -> tuple[str, ...]:
        """``exec_prefix`` + the CLI + the plan, when the plan is an argument.

        The prefix comes from the profile (v1's ``mise exec node@24.5 --``) and
        goes in front of everything, because the point of a toolchain wrapper is
        that the CLI itself runs under the versions the repository pins.

        This is the *inner* command on every tier. The host spawns it directly;
        the container puts it after the image name.
        """
        argv = (*request.profile.exec_prefix, *self._command.argv)
        if self._command.delivery is PlanDelivery.ARGUMENT:
            return (*argv, prompt)
        if self._command.delivery is PlanDelivery.FILE:
            return (*argv, PLAN_PATH)
        return argv

    def _environment(self, request: RunnerRequest) -> Environment:
        """The agent's whole environment. Built up, never filtered down.

        Order matters only in that the check comes last: everything assembled
        here goes through ``_forbidden``, so a control-plane credential cannot
        arrive by any of the three routes — inheritance, ``extra_env``, or a
        repository's MCP configuration naming one.
        """
        env = self._inherited(request)
        secret_names: set[str] = set()

        # The run's identity, so an agent — and anything a repository's own
        # tooling reads — can tell it is under an orchestrator, not a person.
        env["CLAWDENCE_RUN_ID"] = request.run_id
        env["CLAWDENCE_STAGE_ID"] = request.stage_id
        env["CLAWDENCE_WORKTREE"] = request.worktree_path
        env["CLAWDENCE_VERDICT_PATH"] = verdict_file.VERDICT_PATH

        # Commits need an author *inside* the runner (§3.9): an agent that
        # commits its own work with no identity configured fails outright on a
        # machine with no global git config, which is every container.
        env["GIT_AUTHOR_NAME"] = self._identity.name
        env["GIT_AUTHOR_EMAIL"] = self._identity.email
        env["GIT_COMMITTER_NAME"] = self._identity.name
        env["GIT_COMMITTER_EMAIL"] = self._identity.email

        env.update(self._command.extra_env)

        for variable, secret_name in self._command.secret_env.items():
            env[variable] = self._secrets.resolve(secret_name).reveal()
            secret_names.add(variable)

        # The honest exception to "the runner holds no secrets" (§3.1): a repo
        # that configures MCP hands the runner a credential. Scoped per repo,
        # injected per run, resolved by name — never carried in the request.
        for server in request.profile.mcp_servers:
            if server.bearer_token_env_var is None:
                continue
            found = self._secrets.find(server.bearer_token_env_var)
            if found is not None:
                env[server.bearer_token_env_var] = found.reveal()
                secret_names.add(server.bearer_token_env_var)

        forbidden = _forbidden(env)
        if forbidden:
            raise PermanentError(
                "control-plane-secret-in-runner-env",
                f"refusing to start a runner with {', '.join(sorted(forbidden))} in its "
                f"environment — a runner receives no control-plane credentials (§3.1)",
            )
        return Environment(values=env, secret_names=frozenset(secret_names))

    def _result(
        self,
        request: RunnerRequest,
        completion: Completion,
        *,
        started_at: datetime,
        tally: TokenTally,
    ) -> RunnerResult:
        outcome = classify(completion)
        produced = outcome in _PRODUCED_A_TREE
        verdict = completion.verdict

        # A CLI that reports its own usage is believed over a regular expression
        # run across its prose, which is what the scraper is.
        usage = verdict.usage if verdict is not None and verdict.usage is not None else tally.usage
        unattributed = max(tally.reported_total - _total(usage), 0)
        prices = self._command.prices

        result = RunnerResult(
            run_id=request.run_id,
            stage_id=request.stage_id,
            outcome=outcome,
            tree_hash=completion.tree_hash if produced else None,
            exit_code=completion.exit_code,
            diff=completion.diff,
            test_evidence=verdict.tests if verdict is not None else None,
            commits_ahead=completion.commits_ahead,
            dirty=bool(completion.dirty_paths),
            dirty_paths=completion.dirty_paths,
            usage=usage,
            cost=(
                CostEntry(
                    run_id=request.run_id,
                    stage_id=request.stage_id,
                    usage=usage,
                    usd=prices.usd(usage, unattributed=unattributed),
                    at=self._clock(),
                )
                if prices is not None
                else None
            ),
            discovery_notes=verdict.discovery_notes if verdict is not None else (),
            unresolved_stubs=verdict.unresolved_stubs if verdict is not None else (),
            started_at=started_at,
            finished_at=self._clock(),
            message=self._message(completion, outcome),
        )
        return validate_result(request, result)

    def _message(self, completion: Completion, outcome: RunnerOutcome) -> str:
        """A diagnostic line. Deliberately not the agent's output.

        This string is persisted with the step result. Stderr is where a
        provider's echo of a rejected request ends up, and an echoed request
        contains whatever was in it — so the tail is opt-in, and off.
        """
        parts = [outcome.value]
        if completion.startup_error is not None:
            parts.append(completion.startup_error)
        elif completion.exit_code is not None:
            parts.append(f"exit {completion.exit_code}")
        # Not gated on `include_stderr_tail`, and the difference is the point: a
        # `provider-error` that does not say which provider error is a value
        # nobody can act on, and this is a bounded field from a structured event
        # rather than an unbounded echo of whatever the process printed. The
        # residue is named in `turns.MAX_ERROR_CHARS`.
        if completion.provider_error is not None:
            parts.append(completion.provider_error)
        if completion.verdict is not None and completion.verdict.summary:
            parts.append(completion.verdict.summary)
        if self._command.include_stderr_tail and completion.stderr_tail:
            parts.append(completion.stderr_tail)
        return " · ".join(parts)


#: Contracts whose definition of done is passing tests. For these, and only
#: these, an absent verdict means the same thing as a failing one.
_NEEDS_EVIDENCE: Final = frozenset({ContractKind.OUTSIDE_IN_TDD, ContractKind.TEST_AFTER})

#: Outcomes that leave a tree worth naming. ``DROPPED_COMMIT`` is here because
#: the runner's safety commit means the work exists: the agent never claimed it,
#: which is what the outcome says, but somebody looking into the failure needs
#: the hash to see what was nearly done. Every other failure gets ``None``, and
#: ``ports.runner.validate_result`` enforces that from the other side — a hash on
#: a timeout is a hash something eventually tries to merge.
_PRODUCED_A_TREE: Final = frozenset(
    {RunnerOutcome.SUCCEEDED, RunnerOutcome.TESTS_FAILED, RunnerOutcome.DROPPED_COMMIT}
)


@dataclass(slots=True)
class _Watcher:
    """Per-run stream state: the tally, the stderr tail, and the spend check.

    Separate from the runner because it is the only mutable thing in a dispatch,
    and because the budget check has to run on the line that reports the number
    rather than after the process ends.
    """

    tally: TokenTally
    sink: LogSink | None
    budget: Budget
    prices: TokenPrice | None
    stop: Callable[[], None]
    stderr: Tail = field(default_factory=Tail)
    turns: TurnTracker = field(default_factory=TurnTracker)
    overspent: bool = False

    def line(self, line: LogLine) -> None:
        if line.stream is Stream.STDERR:
            self.stderr.add(line.text)
        if self.sink is not None:
            self.sink(line)

        # Stdout only. A CLI's event stream is its stdout; its stderr is where
        # warnings and a provider's echo of a rejected request go, and reading
        # turns out of *that* would let a diagnostic decide the outcome.
        if line.stream is Stream.STDOUT:
            self.turns.observe(line.text)

        self.tally.observe(line.text)
        if self.overspent or not self._over_budget():
            return
        self.overspent = True
        self.stop()

    def _over_budget(self) -> bool:
        if self.budget.max_tokens is not None and self.tally.spent() > self.budget.max_tokens:
            return True
        if self.budget.max_usd is None or self.prices is None:
            return False
        unattributed = max(self.tally.reported_total - _total(self.tally.usage), 0)
        spend = self.prices.usd(self.tally.usage, unattributed=unattributed)
        return spend > self.budget.max_usd


async def _feed(process: asyncio.subprocess.Process, text: str | None) -> None:
    """Write the prompt to the child's stdin and close it.

    A CLI that reads its prompt and exits closes the pipe under us; that is its
    answer, not an error in the write, so the broken pipe is suppressed rather
    than failing a run that has already produced its result.
    """
    if text is None or process.stdin is None:
        return
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        process.stdin.write(text.encode("utf-8"))
        await process.stdin.drain()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
        process.stdin.close()


def _wall_clock(request: RunnerRequest) -> tuple[float | None, bool]:
    """The deadline, and whether it came from the budget or the resource caps.

    Both can be set and the smaller wins, but *which* one won decides what the
    failure is called: stopped by the money cap is ``budget-exceeded``, stopped
    by the resource cap is ``timed-out``. They are handled differently — one is
    worth retrying with a larger budget, the other is worth asking why a
    twenty-minute job took an hour.
    """
    budget = request.budget.max_wall_clock_seconds
    caps = request.profile.caps.wall_clock_seconds
    if budget is None:
        return caps, False
    if caps is None:
        return budget, True
    return (budget, True) if budget <= caps else (caps, False)


def _total(usage: TokenUsage) -> int:
    return (
        usage.input_tokens
        + usage.output_tokens
        + usage.cached_input_tokens
        + usage.reasoning_tokens
    )


def _forbidden(env: Mapping[str, str]) -> Sequence[str]:
    return [
        name
        for name in env
        if any(
            name == pattern or (pattern.endswith("_") and name.startswith(pattern))
            for pattern in FORBIDDEN_ENV
        )
    ]
