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

**A run is two phases, not one** (added with S7's caching). Before the agent
starts, the repository's own ``install_command`` runs — v1's ``install_cmd`` /
``pre_coding_cmd`` — against the warm cache in ``cache``. It is a *phase* rather
than a special case because everything the agent phase needs, it needs too: the
same environment, the same wall clock, the same stop button, and the same
heartbeats. The last one is not decoration. §3.11's silence detector keys on the
timestamp of the newest thing a run said, and a fifteen-minute ``mvn install``
that nobody was streaming would look exactly like a wedged agent to it — so the
attend loop runs across both phases and the detector sees an install as the busy
thing it is. Consequently ``_launch``, ``_observe`` and ``_halt`` all take the
phase: the tier is told *which* process it is being asked about, rather than
assuming there is only one.

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
from enum import StrEnum
from pathlib import Path
from time import monotonic
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
from clawdence.ports.control import (
    DEFAULT_POLL_SECONDS,
    Cancellation,
    ControlPort,
    NoControl,
    Signal,
)
from clawdence.ports.errors import PermanentError
from clawdence.ports.model import TokenPrice
from clawdence.ports.runner import validate_result
from clawdence.ports.secrets import NullSecrets, SecretProvider
from clawdence.runners import plan as plan_text
from clawdence.runners import steering
from clawdence.runners import verdict as verdict_file
from clawdence.runners import worktree as wt
from clawdence.runners.cache import Cache, CachePlan
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


class Phase(StrEnum):
    """Which of a run's two processes a tier is being asked about.

    ``SETUP`` is the repository's ``install_command``; ``AGENT`` is the CLI. The
    hooks take this rather than the tiers keeping state about which one is
    running, because a runner instance serves many dispatches at once under the
    scheduler and per-instance "current phase" would be a field two concurrent
    runs disagree about.
    """

    SETUP = "setup"
    AGENT = "agent"


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

    #: Ceiling on the repository's ``install_command``, on top of whatever the
    #: run's own wall clock leaves. Generous, because a cold monorepo install is
    #: the thing the cache exists to stop being slow and the *first* one still
    #: pays for it — and finite, because an install that hangs on a private
    #: registry prompt would otherwise consume the whole run before the agent
    #: got a turn.
    setup_timeout_seconds: float = 1800.0


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
        "_cache",
        "_clock",
        "_command",
        "_control",
        "_environ",
        "_identity",
        "_inflight",
        "_poll_seconds",
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
        control: ControlPort | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        cache: Cache | None = None,
    ) -> None:
        self._command = command
        # ``Cache(enabled=False)`` rather than ``None`` for "no cache", so the
        # code path is one shape. A runner given nothing gets the machine's
        # default cache home, because a cache that has to be wired up is a cache
        # that is off in every deployment nobody read the docs for — and the
        # thing it protects against, a cold install per run, is the single
        # largest cost in the tier.
        self._cache = Cache.default(environ) if cache is None else cache
        self._secrets: SecretProvider = NullSecrets() if secrets is None else secrets
        self._sink = sink
        self._clock = clock
        self._identity = identity
        self._environ = os.environ if environ is None else environ
        self._settled: dict[str, RunnerResult] = {}
        self._inflight: dict[str, asyncio.Task[RunnerResult]] = {}
        self._stopping: set[str] = set()
        # ``NoControl`` rather than ``None`` so the poll loop has one shape: a
        # runner nobody wired a control plane to still runs it, still learns
        # nothing, and does not grow a branch that only the wired case exercises.
        self._control: ControlPort = NoControl() if control is None else control
        self._poll_seconds = poll_seconds

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
                f"the repository profile for {request.profile.name!r} sets isolation_tier to "
                f"{tier.value!r}, but this deployment's runner.tier (in the deployment config) "
                f"is {self.tier.value!r} — refusing rather than quietly running it under "
                f"stronger or weaker containment than its profile asks for. The two must agree: "
                f"either change runner.tier to {tier.value!r} in the deployment config"
                + (
                    " (a container tier also needs runner.image set)"
                    if tier is not IsolationTier.HOST
                    else ""
                )
                + f", or change isolation_tier to {self.tier.value!r} in this repository's "
                f"profile"
                + (
                    " — which means its agent runs unsandboxed, as the control-plane user, "
                    "on this machine"
                    if self.tier is IsolationTier.HOST
                    else ""
                )
                + ".",
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
        # One deadline for the whole attempt, not one per phase. A repository
        # whose install eats twenty-nine minutes of a thirty-minute cap leaves
        # the agent one minute and then times out, which is the honest report;
        # giving each phase its own copy of the limit would let a run declared
        # to take half an hour take an hour and report success.
        deadline = _Deadline.of(request)
        try:
            try:
                completion = await self._run_setup(request, worktree, deadline)
                if completion is None:
                    completion = await self._run_agent(request, worktree, tally, deadline)
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
        plan = self._cache_plan(request)
        if plan is not None:
            # As the invoking user, and before anything is mounted — see
            # ``CachePlan.prepare``. A directory the daemon has to create is a
            # directory the daemon owns, and a container running as somebody
            # else then cannot write to its own cache.
            plan.prepare()
        verdict_file.clear(worktree)
        # Empty, and before the agent starts: the plan tells it to look here
        # every turn, and an instruction about a path that does not exist is one
        # the agent spends a turn interpreting.
        steering.prepare(worktree)

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

        ``agents_md_path`` is repo-relative — it names a path inside the
        checkout, the same way ``profile.install_command`` names a command run
        inside it, not a path on the control plane's own filesystem. It has to
        be resolved against *this run's* worktree rather than the runner
        process's working directory: those are different directories on every
        run, and reading it relative to the latter is reading whatever
        unrelated file happens to sit at that path in wherever the operator
        invoked ``clawdence`` from — silently installing nothing, or worse,
        installing something that is not this repository's.
        """
        source_path = request.profile.agents_md_path
        if source_path is None:
            return None
        source = installed.worktree / source_path
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

    async def _run_setup(
        self,
        request: RunnerRequest,
        worktree: Path,
        deadline: _Deadline,
    ) -> Completion | None:
        """Install the repository's dependencies. ``None`` when that went fine.

        v1's ``install_cmd``, finally run by something. Returning ``None`` for
        success is what keeps the caller readable — the only reason this phase
        exists is to *not* be the thing that produced the result.

        **A failing install is ``BLOCKED``, and that is a decision.** The two
        near-misses are ``STARTUP_FAILED``, which is retryable and means the
        environment was wrong rather than the repository, and ``NON_ZERO_EXIT``,
        which belongs to the agent and implies an agent ran. A repository whose
        own install command fails is a repository the agent cannot work in, and
        three more attempts re-run the same failing install at full price —
        which is the exact budget burn ``BLOCKED`` was added for. What still
        outranks it, because ``classify`` puts them above, is every way the
        install could have been stopped rather than failed: a cancel, the wall
        clock, the money cap, an OOM kill.
        """
        argv = self._setup_argv(request)
        if not argv:
            return None

        completion = await self._run_phase(
            request,
            worktree,
            Phase.SETUP,
            argv,
            feed=None,
            tally=TokenTally(accumulation=self._command.accumulation),
            # No budget. The install spends time and disk, not tokens, and a
            # tally run over `npm`'s output would be a regular expression
            # written for an agent's event stream deciding a build's fate.
            budget=Budget(),
            limit=deadline.remaining(self._command.setup_timeout_seconds),
            limit_is_budget=deadline.is_budget,
        )
        if completion.exit_code == 0 and completion.startup_error is None:
            return None
        return replace(
            completion,
            setup_error=(
                f"{request.profile.name!r} could not be prepared: "
                f"{' '.join(argv)} exited {completion.exit_code}"
            ),
        )

    async def _run_agent(
        self,
        request: RunnerRequest,
        worktree: Path,
        tally: TokenTally,
        deadline: _Deadline,
    ) -> Completion:
        """Spawn whatever this tier spawns, stream it, and stop it when it runs
        out of something."""
        prompt = plan_text.build(request)
        return await self._run_phase(
            request,
            worktree,
            Phase.AGENT,
            self._cli_argv(request, prompt),
            feed=prompt if self._command.delivery is PlanDelivery.STDIN else None,
            tally=tally,
            budget=request.budget,
            limit=deadline.remaining(),
            limit_is_budget=deadline.is_budget,
        )

    async def _run_phase(
        self,
        request: RunnerRequest,
        worktree: Path,
        phase: Phase,
        argv: tuple[str, ...],
        *,
        feed: str | None,
        tally: TokenTally,
        budget: Budget,
        limit: float | None,
        limit_is_budget: bool,
    ) -> Completion:
        """Run one of the attempt's processes to a conclusion.

        Both phases want the same six things — a spawn, two streams read at
        once, a wall clock, a stop button, the control-plane exchange, and a
        report of what the tier saw — so they are written once and the phase is
        passed to the three hooks that care which process this is.
        """
        launch = self._launch(request, worktree, phase, argv)

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
            halting = asyncio.create_task(self._halt(request, phase, process))

        watcher = _Watcher(
            tally=tally,
            sink=self._sink,
            budget=budget,
            prices=self._command.prices,
            stop=stop,
        )

        # The channel *into* the run (§3.11), and the only thing in this method
        # that is not about the process itself. It runs beside the drive rather
        # than inside it because both halves of what it does are periodic and
        # neither is triggered by output arriving: a message waiting in the
        # inbox has to be picked up by a run that is saying nothing, and a run
        # that is saying nothing is precisely the case the heartbeat exists to
        # make visible.
        attending = asyncio.create_task(self._attend(request, worktree, phase, process, watcher))

        timed_out = False
        try:
            await asyncio.wait_for(self._drive(process, feed, watcher, phase), timeout=limit)
        except TimeoutError:
            timed_out = True
            await self._halt(request, phase, process)
        except asyncio.CancelledError:
            await self._halt(request, phase, process)
            raise
        finally:
            attending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await attending
            if halting is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await halting

        stopped = watcher.cancelled
        completion = Completion(
            exit_code=process.returncode,
            # A stop from outside outranks everything the process did on its way
            # down. It also outranks the timeout: a run cancelled at 29 minutes
            # of a 30-minute limit exits killed, and reporting that as a timeout
            # would send it back through a retry policy written for a run nobody
            # stopped.
            cancelled=stopped is not None,
            cancelled_because=None if stopped is None else stopped.reason,
            timed_out=timed_out and not limit_is_budget,
            budget_exceeded=watcher.overspent or (timed_out and limit_is_budget),
            killed_by_us=timed_out or watcher.overspent or stopped is not None,
            stderr_tail=watcher.stderr.last(),
            # §3.7a: the two things the exit status cannot say. Both come off the
            # stream that was already being read for tokens — reading it twice
            # would be two chances to disagree about what arrived.
            #
            # **Agent phase only.** These read the *agent's* event stream, and an
            # install emitting structured output — npm and gradle both can —
            # would prove "this process emits events" while never emitting a
            # model turn, which is precisely the shape ``NO_MODEL_RESPONSE`` is
            # keyed on. A build tool would then report the credential as
            # rejected.
            provider_error=watcher.turns.terminal_error if phase is Phase.AGENT else None,
            model_turn_seen=watcher.turns.model_turn_seen if phase is Phase.AGENT else None,
        )
        return await self._observe(request, phase, completion)

    async def _attend(
        self,
        request: RunnerRequest,
        worktree: Path,
        phase: Phase,
        process: asyncio.subprocess.Process,
        watcher: _Watcher,
    ) -> None:
        """Carry messages in and liveness out, while the agent works (§3.11).

        Runs across **both** phases, and the setup phase is the one that makes it
        matter rather than the one that gets it for free: a cold ``mvn install``
        says plenty but says none of it to a control plane that is not listening,
        and §3.11's silence detector cannot tell a run nobody is streaming from a
        run that has wedged. A steer delivered during setup is simply waiting for
        the agent when it starts, which is where it was going anyway.

        Cancelled by ``_run_phase`` when the drive finishes, so this loops
        forever on purpose and has no exit condition of its own — except a stop,
        which is the one thing it is allowed to decide.

        **A heartbeat only when something was actually heard.** The instant
        reported is the arrival time of the newest line, not now, and a poll that
        has heard nothing new since the last one reports nothing at all. A timer
        that beat regardless would make a wedged run look healthy for as long as
        it stayed wedged, which is the exact failure mode §3.11 asks for a second
        detector to catch, rather than a second budget.

        **A control plane that cannot be reached does not fail a run.** Steering
        is an improvement on a run that is otherwise working; killing work in
        progress because the database was busy would make the feature more
        dangerous than its absence.
        """
        while True:
            await asyncio.sleep(self._poll_seconds)
            signal = await self._exchange(request, watcher)
            if signal.messages:
                steering.deliver(worktree, signal.messages)
            if signal.cancel is not None:
                # Latched on the watcher rather than returned, because the drive
                # is what finishes the run and it needs to find this afterwards.
                watcher.cancelled = signal.cancel
                await self._halt(request, phase, process)
                return

    async def _exchange(self, request: RunnerRequest, watcher: _Watcher) -> Signal:
        """One round trip to the control plane, or nothing if it did not answer.

        The swallowed exception is the point of the separation: a control plane
        that is unreachable, busy or misconfigured must not take a working run
        down with it, and an empty ``Signal`` is exactly what "nothing was said
        to this run" already means everywhere else in the loop. The cost is that
        a persistently broken control source is silent, and the thing that
        surfaces it is the *absence* of heartbeats — which the silence detector
        is already watching for.
        """
        try:
            heard = watcher.heard_at
            if heard is not None and heard != watcher.reported_at:
                await self._control.heartbeat(request.run_id, at=heard)
                watcher.reported_at = heard
            return await self._control.poll(request.run_id)
        except Exception:
            return Signal()

    async def _drive(
        self,
        process: asyncio.subprocess.Process,
        feed: str | None,
        watcher: _Watcher,
        phase: Phase,
    ) -> None:
        """Write the prompt and read both streams, all at once.

        Concurrently, and that is the whole reason this is a separate method: a
        plan large enough to fill the pipe buffer blocks the write until the
        child reads it, and a child that does not read until it has printed
        something blocks on the write. Sequential code deadlocks on a long plan
        and works on every short one, which is the worst way for a bug to behave.

        ``phase`` is carried onto every line this reads, not just kept beside
        the call. A repository's own install command prints its own success
        banner — Maven says ``BUILD SUCCESS`` for a dependency-resolution goal
        that never touched the agent — and a sink that cannot tell setup output
        from agent output reads that banner as the coding CLI's.
        """
        readers = [
            pump(reader, name, on_line=watcher.line, clock=self._clock, phase=phase.value)
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
    def _launch(
        self, request: RunnerRequest, worktree: Path, phase: Phase, argv: tuple[str, ...]
    ) -> Launch:
        """argv, environment and cwd of the process to spawn.

        ``argv`` is the *inner* command — the agent CLI, or the repository's
        install command — already carrying ``exec_prefix``. The tier decides what
        actually gets executed to make that happen, which on the host is the
        command itself and in a container is an engine client with it on the end.
        """

    # The three below are *optional* hooks, not abstract ones, and doing nothing
    # is a correct implementation of each: the host tier has no preflight of its
    # own, nothing to ask a supervisor, and nothing to give back. Making them
    # abstract would force every tier to write three empty methods to say so.
    def _check(self, request: RunnerRequest) -> None:  # noqa: B027
        """Tier-specific preflight. Raises ``PermanentError``, or does nothing."""

    async def _halt(
        self, request: RunnerRequest, phase: Phase, process: asyncio.subprocess.Process
    ) -> None:
        """Stop the work. Called for a timeout, a budget kill, and a cancel.

        The default — kill the child and reap it — is right only where the child
        *is* the work. A tier that spawns the agent somewhere else has to stop it
        there first, because the process we hold is a client and killing a client
        does not stop what it asked for. ``phase`` is how such a tier knows
        *which* of the run's two processes it is being asked to end.
        """
        await kill_and_reap(process)

    async def _observe(
        self, request: RunnerRequest, phase: Phase, completion: Completion
    ) -> Completion:
        """Anything the tier knows that the exit status does not say."""
        return completion

    async def _teardown(self, request: RunnerRequest) -> None:  # noqa: B027
        """Release whatever the tier allocated. Must be safe to call twice.

        Called once per attempt, after both phases, so a tier that allocates per
        phase gives back *every* phase's allocation here — the setup container
        outlives its phase if nothing removes it, and a leaked container is a
        leaked CPU whether or not an agent was in it.
        """

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

    def _setup_argv(self, request: RunnerRequest) -> tuple[str, ...]:
        """The repository's install command, under its toolchain wrapper.

        argv, never a shell string, for the reason ``ScriptStage.command`` gives
        and one more that is specific to this: the value comes from a profile the
        probe (S9) proposes, and a profile that could carry a shell string is a
        profile that could carry a pipeline into ``curl``.
        """
        install = request.profile.install_command
        if not install:
            return ()
        return (*request.profile.exec_prefix, *install)

    def _cache_plan(self, request: RunnerRequest) -> CachePlan | None:
        return self._cache.plan(request.profile)

    def _environment(self, request: RunnerRequest) -> Environment:
        """The agent's whole environment. Built up, never filtered down.

        Order matters only in that the check comes last: everything assembled
        here goes through ``_forbidden``, so a control-plane credential cannot
        arrive by any of the three routes — inheritance, ``extra_env``, or a
        repository's MCP configuration naming one.
        """
        env = self._inherited(request)
        secret_names: set[str] = set()

        # Before everything else, so a repository that wants its own cache
        # location can still say so through ``extra_env``. Absolute paths, and
        # the same ones on both sides of a container boundary — see ``cache``.
        plan = self._cache_plan(request)
        if plan is not None:
            env.update(plan.env)

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

        The first part explains the outcome rather than just naming it, for the
        band where the name alone does not say what to conclude: the process
        exited cleanly, so the question is what it left behind, and "dropped
        commit" or "empty diff" is a verdict a reader has to already know this
        taxonomy to parse. The caller prefixes this with ``runner-{outcome}``
        (``handler.py``), so repeating the bare enum value here would say the
        same thing twice and explain nothing extra.
        """
        parts = [_EXPLANATIONS.get(outcome, outcome.value)]
        if completion.startup_error is not None:
            parts.append(completion.startup_error)
        elif completion.setup_error is not None:
            # Even when the outcome is not ``BLOCKED``. A setup killed by the
            # wall clock reports ``timed-out``, and "which of the two processes
            # ran out of time" is the first thing anybody reading it asks.
            parts.append(completion.setup_error)
        elif completion.exit_code is not None:
            parts.append(f"exit {completion.exit_code}")
        if completion.cancelled_because is not None:
            parts.append(completion.cancelled_because)
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

#: Explanations for band 5 — see ``outcome.classify`` — where the process
#: exited zero and the outcome is a judgement about what it produced rather
#: than a report of what went wrong. Every other band's name (``timed-out``,
#: ``budget-exceeded``, ``oom-killed``, ...) already says what happened; these
#: four don't, without already knowing the taxonomy that names them.
_EXPLANATIONS: Final[Mapping[RunnerOutcome, str]] = {
    RunnerOutcome.DROPPED_COMMIT: (
        "the agent finished and left changes in the tree but never ran `git commit` "
        "on them; clawdence committed them anyway so the work is not lost — that commit "
        "is what any pull request here is built from — but an agent that does not commit "
        "its own work cannot be trusted to have considered it finished, so this is "
        "flagged rather than treated as a plain success. Read the diff yourself"
    ),
    RunnerOutcome.EMPTY_DIFF: (
        "the agent left the working tree clean and committed nothing — as far as git is "
        "concerned, this run made no changes"
    ),
    RunnerOutcome.BLOCKED: (
        "the agent (or its setup step) reported it could not proceed, and retrying with "
        "the same request is expected to fail the same way"
    ),
    RunnerOutcome.TESTS_FAILED: "the evidence attached to this run says the tests do not pass",
}

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

    #: When the newest line arrived, on either stream. The liveness signal
    #: §3.11's detector keys on — a run that is thinking, compiling or waiting
    #: on a provider is still saying something, and one that has stopped saying
    #: anything is the case a declared timeout cannot see.
    heard_at: datetime | None = None

    #: The instant last reported to the control plane, so an unchanged heartbeat
    #: is not written again. Kept beside ``heard_at`` rather than in ``_attend``
    #: because the two are one fact and splitting them across the loop's local
    #: state is how they drift.
    reported_at: datetime | None = None

    #: Set when a stop arrives from outside, and read after the drive ends.
    cancelled: Cancellation | None = None

    def line(self, line: LogLine) -> None:
        self.heard_at = line.at
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


@dataclass(frozen=True, slots=True)
class _Deadline:
    """One wall clock, shared by both phases of an attempt.

    Monotonic rather than the injected clock, because this measures elapsed time
    against a limit and the injected clock exists so tests can make timestamps
    predictable — a frozen clock would mean an attempt whose deadline never
    arrives.
    """

    limit: float | None
    is_budget: bool
    started: float

    @classmethod
    def of(cls, request: RunnerRequest) -> _Deadline:
        limit, is_budget = _wall_clock(request)
        return cls(limit=limit, is_budget=is_budget, started=monotonic())

    def remaining(self, ceiling: float | None = None) -> float | None:
        """What is left, under an optional per-phase ceiling of its own.

        Never zero or negative: a phase given a non-positive timeout would be
        spawned and killed rather than reported as out of time, and the smallest
        positive limit produces the timeout the caller is actually asking for.
        """
        limits = [value for value in (self._left(), ceiling) if value is not None]
        return min(limits) if limits else None

    def _left(self) -> float | None:
        if self.limit is None:
            return None
        return max(self.limit - (monotonic() - self.started), _MINIMUM_LIMIT_SECONDS)


#: Floor on a phase's timeout. Small enough that an attempt already out of time
#: fails immediately, and positive because ``asyncio.wait_for`` treats zero as
#: "already expired" and would report a timeout before the process existed —
#: which is a timeout the tier could not have halted anything for.
_MINIMUM_LIMIT_SECONDS: Final = 0.001


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
