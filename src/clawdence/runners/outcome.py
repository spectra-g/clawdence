"""Deciding which of the eleven things that can happen, happened.

``RunnerOutcome`` has eleven values because v1 had one: "runner failed". With one
value the retry policy cannot tell a flaky integration test from an OOM kill from
a budget cap, so it treats them the same — and treating them the same means
either retrying things that will never work, or refusing to retry things that
would have worked second time. Every value in the taxonomy costs a branch
somewhere; this module is where they are earned.

The classifier is a **pure function over a record of observations**, separate
from the code that gathers them, and that split is what makes the taxonomy
testable. Some of these signals are only available to some tiers — a container
knows it was OOM-killed because the daemon says so, a host process can only infer
it from a ``SIGKILL`` it did not send; egress denial is not observable at all
until S7b exists to deny anything. Rather than let the untestable modes go
unwritten until the tier that produces them arrives, ``Completion`` has a field
for each, S6 fills in the ones the host can see, and S7 and S7b fill in the rest
without touching the ranking below.

**The ranking is the interesting part**, because several signals are true at
once and only one outcome is reported. It goes: things we did (cancel, budget,
timeout) before things that happened to it (OOM, disk), before what it said
(exit code), before what it produced (tests, diff). A run killed for exceeding
its budget also exits non-zero; reporting ``non-zero-exit`` for it would send a
retry after work that was stopped on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from clawdence.domain import DiffStat, RunnerOutcome
from clawdence.runners.verdict import RunnerVerdict, VerdictStatus

#: Exit statuses that mean the process was killed with ``SIGKILL``. Python
#: reports a signal death as a negative return code; 137 is the same thing seen
#: through a shell or a container runtime (128 + 9).
_SIGKILL_STATUSES: Final = frozenset({-9, 137})

#: Substrings that mean the filesystem gave out. Matched against stderr because
#: on the host tier there is no other channel — the container tier gets this from
#: the runtime instead and sets the flag directly.
_DISK_FULL_SIGNATURES: Final = ("no space left on device", "enospc", "disk quota exceeded")


@dataclass(frozen=True, slots=True)
class Completion:
    """Everything observed about one attempt, before it is named.

    A record rather than arguments, because the number of signals only grows —
    S7 adds the daemon's OOM flag, S7b adds egress denial — and a function
    signature that grows to eleven parameters is one whose call sites disagree
    about the order.
    """

    #: ``None`` when the process never got as far as an exit — killed, or never
    #: started at all.
    exit_code: int | None = None

    #: Set when the attempt did not start: the binary is missing, the worktree
    #: vanished, the toolchain wrapper is not installed. Distinct from a non-zero
    #: exit, because nothing ran and so nothing about the repository is implied.
    startup_error: str | None = None

    #: We stopped it. Each is a decision the control plane made, and each ranks
    #: above anything the process itself reported.
    cancelled: bool = False
    timed_out: bool = False
    budget_exceeded: bool = False

    #: Whether the kill was ours. Without this, a process we killed for a timeout
    #: is indistinguishable from one the kernel's OOM killer took.
    killed_by_us: bool = False

    #: Filled by the container tier (S7), which is told by the runtime. On the
    #: host this stays ``False`` and the ``SIGKILL`` heuristic below is all there
    #: is — stated rather than hidden, because "probably OOM" is what the host
    #: tier can honestly say.
    oom_killed: bool = False
    disk_full: bool = False

    #: Filled by the egress allowlist (S7b). Nothing denies egress before it
    #: exists, so on every tier shipping today this is ``False``.
    egress_denied: bool = False

    #: What the tree says, after the runner committed whatever was left. Read
    #: with git rather than reported by the agent: the number that decides
    #: whether a pull request gets opened does not come from the process being
    #: judged. ``files_changed`` is duplicated out of ``diff`` because the
    #: classifier only ever asks that one question of it.
    files_changed: int = 0
    diff: DiffStat | None = None
    tree_hash: str | None = None

    #: What the agent said. ``None`` means it wrote no verdict, or wrote one that
    #: did not validate.
    verdict: RunnerVerdict | None = None

    #: Whether the contract asked for passing tests. Decides what an absent
    #: verdict means, which is the one place absence is not neutral.
    requires_evidence: bool = False

    #: Last line of stderr, lowercased for matching. Never propagated into a
    #: result — see ``host.AgentCommand.include_stderr_tail``.
    stderr_tail: str = ""


def classify(completion: Completion) -> RunnerOutcome:
    """Name what happened. One outcome, chosen by rank, never a combination."""
    if completion.cancelled:
        return RunnerOutcome.CANCELLED
    if completion.startup_error is not None:
        return RunnerOutcome.STARTUP_FAILED
    if completion.budget_exceeded:
        return RunnerOutcome.BUDGET_EXCEEDED
    if completion.timed_out:
        return RunnerOutcome.TIMED_OUT
    if completion.egress_denied:
        return RunnerOutcome.NETWORK_DENIED
    if completion.disk_full or _looks_disk_full(completion.stderr_tail):
        return RunnerOutcome.DISK_FULL
    if completion.oom_killed or _looks_oom_killed(completion):
        return RunnerOutcome.OOM_KILLED
    if completion.exit_code != 0:
        return RunnerOutcome.NON_ZERO_EXIT

    # Exited cleanly. From here the question stops being "what went wrong with
    # the process" and becomes "is what it produced any good".
    if _blocked(completion):
        return RunnerOutcome.BLOCKED
    if _tests_failed(completion):
        return RunnerOutcome.TESTS_FAILED
    if completion.files_changed == 0:
        return RunnerOutcome.EMPTY_DIFF
    return RunnerOutcome.SUCCEEDED


def _looks_disk_full(stderr_tail: str) -> bool:
    lowered = stderr_tail.lower()
    return any(signature in lowered for signature in _DISK_FULL_SIGNATURES)


def _looks_oom_killed(completion: Completion) -> bool:
    """A ``SIGKILL`` nobody admits to sending.

    Best effort, and only on the host tier. Nothing else routinely ``SIGKILL``s a
    process it is not supervising, so the inference is usually right; an operator
    running ``kill -9`` by hand will see it reported as an OOM kill, which is the
    known cost of not having a runtime to ask.
    """
    return completion.exit_code in _SIGKILL_STATUSES and not completion.killed_by_us


def _blocked(completion: Completion) -> bool:
    """The agent stopped on something a retry will not change.

    Ranked above failing tests because the two are handled oppositely: failing
    tests are worth another attempt, and a missing dependency is worth a human.
    An agent that could report only "failed" gets the second one retried until
    the attempts run out, which is v1's behaviour and where its budget went.
    """
    return completion.verdict is not None and completion.verdict.status is VerdictStatus.BLOCKED


def _tests_failed(completion: Completion) -> bool:
    """Whether the evidence says the work is not done.

    Three cases collapse to one answer, and the collapse is deliberate:

    - the agent said ``failed``;
    - the agent said ``passed`` and its own test counts disagree — a claim
      contradicted by the numbers attached to it is not a claim;
    - the contract requires evidence and there is none, because "the tests
      passed" and "nothing shows the tests passed" are the same thing to a
      contract, and both are worth another attempt.
    """
    verdict = completion.verdict
    if verdict is None:
        return completion.requires_evidence
    if verdict.status is VerdictStatus.FAILED:
        return True
    if verdict.tests is not None and verdict.tests.failed > 0:
        return True
    return completion.requires_evidence and verdict.tests is None
