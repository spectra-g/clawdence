"""Re-running the evidence, without re-running the agent.

Two callers need this and neither of them wants a model. **The ``pre_verify``
hook** is per-contract config — cache warm, fixture seed, the
``cp -n .env.example .env`` class of repo-specific setup — and until something
executes it, it is a sentence in a prompt asking an agent to please remember.
**S15b's rebase** produces a tree nothing has run against, and the repair is not
another coding attempt: the code is finished and correct, and what is missing is
a test run against the tree that will actually land. Re-dispatching an agent to
get one would spend a coding budget to re-derive an answer already sitting on
disk.

So: ``pre_verify``, then the repository's test command, then parse the reporter
the profile declares. Three steps, in that order, and the order is the hook's
whole meaning — a fixture seeded after the tests ran is not a fixture.

**Nothing here spawns a process.** ``Executor`` is injected, and this module
ships no default, which is a refusal rather than an omission. The commands are
the *repository's* — ``install_command``, ``test_command``, an argv from a
profile a probe wrote by reading somebody's build files — and running those on
the control plane is precisely the thing the container tier exists to prevent. A
default executor here would be a hole in the plane split reachable by editing a
YAML file, and it would be reached by accident rather than by attack. The tier
that owns isolation supplies the executor; this owns the sequence.

The other half of that boundary is the reporter output, which is read from the
worktree by ``verify.reporters`` — bytes, checked and capped, never argv.
"""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from clawdence.domain import RepoProfile, TestEvidence, VerificationContract
from clawdence.verify import reporters
from clawdence.verify.contracts import Attempt
from clawdence.verify.reporters import ReportError


@dataclass(frozen=True, slots=True)
class Command:
    """One argv to run in the worktree, and what it is for.

    ``label`` exists so a failure can say *which* command failed without
    quoting an argv back at a person — and, more to the point, so
    ``pre_verify`` failing is reported as the hook failing rather than as the
    tests failing, which are different repairs.
    """

    label: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What an executor reports back. Deliberately small.

    No stdout. The evidence comes from the reporter file, not from scraping
    output — that is the whole argument of ``verify.reporters``, and a field
    here holding a megabyte of test output would be the thing it exists to
    avoid, arriving through the back door. ``detail`` is for a person and is
    capped by whoever fills it.
    """

    exit_code: int
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Executor(Protocol):
    """Runs one argv in the worktree and says how it went.

    Supplied by the isolation tier. The signature takes the worktree because a
    re-check may happen long after the run that produced the branch, against a
    checkout that has since been rebased — so "the worktree" is not something
    an executor can have closed over at construction.
    """

    #: Positional-only, so a plain ``async def`` satisfies this without having
    #: to match our parameter names. A protocol with named parameters would
    #: quietly require every tier to call its arguments ``worktree`` and
    #: ``command``, which is a coupling to our vocabulary rather than to our
    #: contract.
    def __call__(self, worktree: Path, command: Command, /) -> Awaitable[CommandResult]: ...


@dataclass(frozen=True, slots=True)
class Recheck:
    """The sequence, as data, before anything runs it.

    Separated from the running so the ordering is testable without a
    subprocess, and so a caller can show a person what is about to happen —
    which for a command list read out of somebody's repository profile is worth
    being able to do.
    """

    commands: tuple[Command, ...]
    reporter_source: Path

    @classmethod
    def plan(
        cls,
        profile: RepoProfile,
        contract: VerificationContract,
        worktree: Path,
    ) -> Recheck:
        """Build the sequence for one profile and contract.

        ``exec_prefix`` is applied here rather than left to the executor for the
        reason ``runners.plan`` applies it there: it selects the toolchain
        versions the project pins, and a caller that forgets it runs the tests
        under whatever happens to be on the path.
        """
        prefix = profile.exec_prefix
        commands: list[Command] = []
        if contract.pre_verify:
            commands.append(Command("pre-verify", (*prefix, *contract.pre_verify)))
        if profile.test_command:
            commands.append(Command("test", (*prefix, *profile.test_command)))
        return cls(commands=tuple(commands), reporter_source=worktree)


@dataclass(frozen=True, slots=True)
class Rechecked:
    """What a re-check found. Folded into an ``Attempt`` by ``into_attempt``."""

    evidence: TestEvidence | None
    pre_verify_ok: bool | None
    #: Set when the sequence could not produce an answer — the hook failed, or
    #: the report could not be parsed. Becomes ``Attempt.verification_error``,
    #: which halts rather than counting as a failing test.
    error: str | None = None

    #: The command that stopped the sequence, if one did.
    failed_at: str | None = None


async def run(
    recheck: Recheck,
    profile: RepoProfile,
    execute: Executor,
) -> Rechecked:
    """Run the sequence and parse what it left behind.

    **A failing test command is not an error.** It is the normal way a re-check
    reports that the tests do not pass, and the evidence for it is in the report
    file the run just wrote — so the sequence continues to the parse rather than
    stopping. A failing ``pre_verify`` *is* an error and stops everything, since
    whatever ran after an unprepared workspace proves nothing either way.
    """
    pre_verify_ok: bool | None = None

    for command in recheck.commands:
        result = await execute(recheck.reporter_source, command)
        if command.label == "pre-verify":
            pre_verify_ok = result.ok
            if not result.ok:
                return Rechecked(
                    evidence=None,
                    pre_verify_ok=False,
                    error=result.detail or "the pre-verify command failed",
                    failed_at=command.label,
                )

    try:
        evidence = reporters.collect(recheck.reporter_source, profile.test_reporter)
    except ReportError as exc:
        # The distinction the contract layer turns into a VERIFICATION_ERROR
        # halt: the repository declares a reporter it did not emit, or emitted
        # something we cannot read. Not a failing test, and not the work's fault.
        return Rechecked(
            evidence=None,
            pre_verify_ok=pre_verify_ok,
            error=str(exc),
            failed_at="report",
        )

    return Rechecked(evidence=evidence, pre_verify_ok=pre_verify_ok)


def into_attempt(
    rechecked: Rechecked,
    base: Attempt,
    *,
    tree_hash: str | None,
) -> Attempt:
    """Fold a re-check's findings onto an existing attempt.

    ``base`` carries what the original run observed — the diff, the outcome, the
    red phase — and the re-check replaces only the parts it actually re-derived.
    That split is the point: after a rebase the diff and the red phase are still
    true and only the green run is stale, so re-verification that discarded them
    would report ``NO_RED_PHASE`` against work that did TDD correctly.
    """
    return Attempt(
        tree_hash=tree_hash,
        outcome=base.outcome,
        number=base.number,
        diff=base.diff,
        evidence=rechecked.evidence,
        red_evidence=base.red_evidence,
        build_succeeded=base.build_succeeded,
        full_suite=base.full_suite,
        pre_verify_ok=rechecked.pre_verify_ok,
        verification_error=rechecked.error,
    )


def sequential(results: Sequence[CommandResult]) -> Executor:
    """An executor that replays a fixed list. For tests and dry runs.

    Here rather than in the test suite because the sequence's *contract* — one
    call per command, in order — is the thing a real executor must also honour,
    and a stand-in that documents it belongs next to the protocol it stands in
    for.
    """
    remaining = list(results)

    async def execute(worktree: Path, command: Command) -> CommandResult:
        if not remaining:
            raise AssertionError(f"no scripted result for {command.label}")
        return remaining.pop(0)

    return execute
