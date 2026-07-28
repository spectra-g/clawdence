"""A ``RunnerPort`` that runs the agent CLI as a child process on this machine.

**This tier has no isolation and is not a default.** It runs model-generated code
as the user running the control plane, on the filesystem the control plane's
state database is on. The plan calls it "local dev only; never default"; what
enforces that here is a check, not a comment — ``dispatch`` refuses a request
whose ``RepoProfile`` asks for any other tier, because the failure mode being
guarded against is a repository configured for ``container`` quietly executing on
the host after somebody wired the wrong adapter at startup.

Given that, the interesting question is not "what does this stop the agent from
doing" — the answer is nothing — but **"what does the control plane hand over,
and what does it believe on the way back".**

*Handed over*: a worktree, a plan, and an environment built from an allowlist.
Never ``os.environ``. The control plane holds every credential in the system
(ARCHITECTURE Zone 2), and passing its environment to a child would put all of
them one ``env`` away from a process running code from a repository. The Slack
token, the tracker credentials and the push credentials are not merely absent by
convention: ``_forbidden`` refuses to build an environment containing them at
all, so a caller that later fills ``extra_env`` from the wrong dictionary fails
at dispatch rather than leaking quietly.

*Believed on the way back*: the exit status and the tree. The diff is re-derived
with ``git`` rather than taken from the agent's word, the tokens are scraped from
the stream, and the verdict is parsed as untrusted input. The agent claiming
success is not sufficient for ``SUCCEEDED`` — the tree has to have changed, and
``outcome.classify`` is what turns observations into one answer.

Three behaviours exist because of something specific:

**Dispatch is idempotent, including while it is still running.** A step times
out, the watchdog recovers it, the run resumes, and the original process is still
working — so a second dispatch of the same ``idempotency_key`` joins the first
rather than starting a second agent in the same worktree. Two agents editing one
worktree is not a race that produces a bad diff; it is a race that produces a
tree neither of them intended.

**Cancellation is a result, not an exception.** ``cancel`` stops the process and
the awaiting dispatch returns a ``CANCELLED`` result, because an operator
stopping a run should leave a record of what happened rather than an exception
that some layer above renders as "failed". A cancellation the *engine* initiates
— its own step timeout — still propagates as ``CancelledError``, because that is
the executor's contract and it is a different event.

**Budgets abort mid-run.** ``Budget.on_exceeded`` is a one-value ``Literal``
precisely so there is no configuration under which exceeding a cap continues, and
a cap checked after the process exits is an epitaph. Tokens are counted off the
stream as they are reported and the process is killed when the count passes the
limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

from clawdence.domain import (
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
from clawdence.runners.outcome import Completion, classify
from clawdence.runners.stream import (
    Accumulation,
    LogLine,
    LogSink,
    Stream,
    Tail,
    TokenTally,
    pump,
)

#: Directory the runner owns inside the worktree. One entry in git's exclude file
#: covers everything installed into it, so nothing the runner writes can reach a
#: pull request.
WORK_DIR: Final = ".clawdence"

#: Where the plan is written when the CLI reads it from a file.
PLAN_PATH: Final = f"{WORK_DIR}/plan.md"

#: Passed through from the control plane's own environment. The engine's
#: ``ScriptHandler`` allowlist, plus what a build needs to find its toolchain and
#: somewhere to write temporary files. Deliberately dull: nothing here names a
#: credential.
INHERITED_ENV: Final[tuple[str, ...]] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "SHELL",
    "USER",
    "XDG_CACHE_HOME",
)

#: Names, and prefixes, that must never reach a runner. This is §3.1's trust
#: boundary written as a check. A denylist *on top of* an allowlist — the
#: allowlist stops inheritance, this stops a caller passing them in on purpose,
#: which is the mistake that reads as reasonable in a diff.
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


class HostRunner:
    """Runs the agent CLI on this machine. ``IsolationTier.HOST`` only.

    Settled results are remembered for the life of the instance, which is what
    makes a redelivery return the original answer rather than running the agent
    again. It is deliberately only an in-process cache: the durable answer to
    "has this attempt already happened" is the ledger's unique constraint (S4),
    which survives the process, and duplicating that here would be a second
    source of truth. For a long-lived control plane the map is one small record
    per attempt — bounded by the run, not by time — and S7's scheduler is where
    it gets an eviction policy if it ever needs one.
    """

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
        if tier is not IsolationTier.HOST:
            raise PermanentError(
                "isolation-tier-mismatch",
                f"{request.profile.name!r} asks for {tier.value!r} isolation and this runner "
                f"executes on the host with none — refusing rather than quietly downgrading it",
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
            completion = await self._run_agent(request, worktree, tally)
        except asyncio.CancelledError:
            if request.idempotency_key not in self._stopping:
                # The engine's own timeout, unwinding. One path out for "did not
                # finish" is the executor's contract, and it is a different event
                # from an operator pressing stop.
                raise
            completion = Completion(cancelled=True, killed_by_us=True)
        finally:
            self._tidy(worktree, installed)

        completion = await self._inspect(request, worktree, completion)
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

    async def _prepare(self, request: RunnerRequest, worktree: Path) -> str | None:
        """Install what the agent needs and hide all of it from git.

        The exclusion is not housekeeping. Every file installed here would
        otherwise be picked up by ``git add --all`` and land in the pull request —
        the conventions file, the plan, the verdict — which is changes nobody
        asked for appearing in somebody's repository under our name.

        Returns the conventions filename if one was installed, so the cleanup
        removes only what this run put there.
        """
        (worktree / WORK_DIR).mkdir(parents=True, exist_ok=True)
        verdict_file.clear(worktree)

        installed = self._install_conventions(request, worktree)
        excluded = [f"/{WORK_DIR}/"] + ([f"/{installed}"] if installed else [])
        await wt.exclude(worktree, *excluded)

        if self._command.delivery is PlanDelivery.FILE:
            (worktree / PLAN_PATH).write_text(plan_text.build(request), encoding="utf-8")
        return installed

    def _install_conventions(self, request: RunnerRequest, worktree: Path) -> str | None:
        """Copy the repo's conventions file to where this CLI looks for it.

        v1's ``agentsMd``. The source is a control-plane path and the destination
        is the CLI's filename, which is why both ends are configured: the same
        file is ``AGENTS.md`` to one CLI and ``CLAUDE.md`` to another, and a
        repository should not have to keep two.

        A file the repository already has is left alone. Copying over it would
        show up as a modification to a tracked file — the pull request containing
        changes nobody asked for, again.
        """
        source_path = request.profile.agents_md_path
        if source_path is None:
            return None
        source = Path(source_path)
        if not source.is_file():
            return None

        destination = worktree / self._command.conventions_filename
        if destination.exists():
            return None
        shutil.copyfile(source, destination)
        return self._command.conventions_filename

    def _tidy(self, worktree: Path, installed: str | None) -> None:
        """Take back what this run installed. Runs even when it was cancelled.

        The verdict is deliberately *not* removed: it is the only account of what
        the agent thought it was doing, it is excluded from git, and the next
        attempt clears it before starting. Removing it here would delete the
        evidence at exactly the moment somebody wants to read it.
        """
        with contextlib.suppress(OSError):
            (worktree / PLAN_PATH).unlink(missing_ok=True)
        if installed is not None:
            with contextlib.suppress(OSError):
                (worktree / installed).unlink(missing_ok=True)

    async def _run_agent(
        self,
        request: RunnerRequest,
        worktree: Path,
        tally: TokenTally,
    ) -> Completion:
        """Spawn the CLI, stream it, and stop it when it runs out of something."""
        prompt = plan_text.build(request)
        argv = self._argv(request, prompt)
        feed = prompt if self._command.delivery is PlanDelivery.STDIN else None

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=worktree,
                env=self._environment(request),
                stdin=asyncio.subprocess.PIPE if feed else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            # Missing, or not executable. Nothing ran, so nothing about the
            # repository is implied — which is why this is `startup-failed` and
            # not `non-zero-exit`.
            return Completion(startup_error=f"could not run {argv[0]!r}: {exc.strerror or exc}")

        watcher = _Watcher(
            tally=tally,
            sink=self._sink,
            budget=request.budget,
            prices=self._command.prices,
            stop=lambda: _kill(process),
        )
        limit, limit_is_budget = _wall_clock(request)

        timed_out = False
        try:
            await asyncio.wait_for(self._drive(process, feed, watcher), timeout=limit)
        except TimeoutError:
            timed_out = True
            await _kill_and_reap(process)
        except asyncio.CancelledError:
            await _kill_and_reap(process)
            raise

        return Completion(
            exit_code=process.returncode,
            timed_out=timed_out and not limit_is_budget,
            budget_exceeded=watcher.overspent or (timed_out and limit_is_budget),
            killed_by_us=timed_out or watcher.overspent,
            stderr_tail=watcher.stderr.last(),
        )

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

    async def _inspect(
        self,
        request: RunnerRequest,
        worktree: Path,
        completion: Completion,
    ) -> Completion:
        """Find out what happened to the tree, and what the agent said about it.

        Re-derived rather than reported: the diff comes from ``git``, not from
        the agent, because the number that decides whether a pull request gets
        opened should not come from the process being judged.
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
            requires_evidence=request.contract.kind in _NEEDS_EVIDENCE,
            stderr_tail=problem or completion.stderr_tail,
        )

    # -------------------------------------------------------------- plumbing

    def _argv(self, request: RunnerRequest, prompt: str) -> tuple[str, ...]:
        """``exec_prefix`` + the CLI + the plan, when the plan is an argument.

        The prefix comes from the profile (v1's ``mise exec node@24.5 --``) and
        goes in front of everything, because the point of a toolchain wrapper is
        that the CLI itself runs under the versions the repository pins.
        """
        argv = (*request.profile.exec_prefix, *self._command.argv)
        if self._command.delivery is PlanDelivery.ARGUMENT:
            return (*argv, prompt)
        if self._command.delivery is PlanDelivery.FILE:
            return (*argv, PLAN_PATH)
        return argv

    def _environment(self, request: RunnerRequest) -> dict[str, str]:
        """The child's whole environment. Built up, never filtered down.

        Order matters only in that the check comes last: everything assembled
        here goes through ``_forbidden``, so a control-plane credential cannot
        arrive by any of the three routes — inheritance, ``extra_env``, or a
        repository's MCP configuration naming one.
        """
        env = {name: self._environ[name] for name in INHERITED_ENV if name in self._environ}

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

        # The honest exception to "the runner holds no secrets" (§3.1): a repo
        # that configures MCP hands the runner a credential. Scoped per repo,
        # injected per run, resolved by name — never carried in the request.
        for server in request.profile.mcp_servers:
            if server.bearer_token_env_var is None:
                continue
            found = self._secrets.find(server.bearer_token_env_var)
            if found is not None:
                env[server.bearer_token_env_var] = found.reveal()

        forbidden = _forbidden(env)
        if forbidden:
            raise PermanentError(
                "control-plane-secret-in-runner-env",
                f"refusing to start a runner with {', '.join(sorted(forbidden))} in its "
                f"environment — a runner receives no control-plane credentials (§3.1)",
            )
        return env

    def _result(
        self,
        request: RunnerRequest,
        completion: Completion,
        *,
        started_at: datetime,
        tally: TokenTally,
    ) -> RunnerResult:
        outcome = classify(completion)
        produced = outcome in (RunnerOutcome.SUCCEEDED, RunnerOutcome.TESTS_FAILED)
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
        if completion.verdict is not None and completion.verdict.summary:
            parts.append(completion.verdict.summary)
        if self._command.include_stderr_tail and completion.stderr_tail:
            parts.append(completion.stderr_tail)
        return " · ".join(parts)


#: Contracts whose definition of done is passing tests. For these, and only
#: these, an absent verdict means the same thing as a failing one.
_NEEDS_EVIDENCE: Final = frozenset({ContractKind.OUTSIDE_IN_TDD, ContractKind.TEST_AFTER})


@dataclass(slots=True)
class _Watcher:
    """Per-run stream state: the tally, the stderr tail, and the spend check.

    Separate from ``HostRunner`` because it is the only mutable thing in a
    dispatch, and because the budget check has to run on the line that reports
    the number rather than after the process ends.
    """

    tally: TokenTally
    sink: LogSink | None
    budget: Budget
    prices: TokenPrice | None
    stop: Callable[[], None]
    stderr: Tail = field(default_factory=Tail)
    overspent: bool = False

    def line(self, line: LogLine) -> None:
        if line.stream is Stream.STDERR:
            self.stderr.add(line.text)
        if self.sink is not None:
            self.sink(line)

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


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    """Kill a child whose attempt is over, then wait for it.

    Both halves, for the reason ``engine.handlers`` spells out: without the kill
    the process outlives the run, and without the wait it is a zombie whose pipes
    stay open, so a transport finalises after the event loop has closed and the
    suite reports a warning from a thread nobody is looking at.
    """
    if process.returncode is not None:
        return
    _kill(process)
    with contextlib.suppress(asyncio.CancelledError):
        await process.wait()


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
