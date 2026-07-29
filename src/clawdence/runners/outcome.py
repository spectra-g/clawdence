"""Deciding which of the fifteen things that can happen, happened.

``RunnerOutcome`` has fifteen values because v1 had one: "runner failed". With
one value the retry policy cannot tell a flaky integration test from an OOM kill
from a budget cap, so it treats them the same — and treating them the same means
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
for each, S6 filled in the ones the host can see, and S7 and S7b fill in the rest
without touching the ranking below.

**The ranking is the interesting part**, because several signals are true at
once and only one outcome is reported. In five bands, most specific first:

1. **Things we did** — cancel, startup, budget, timeout. A run killed for
   exceeding its budget also exits non-zero, and reporting ``non-zero-exit`` for
   it would send a retry after work that was stopped on purpose.
2. **Things that happened to it** — denied egress, a full disk, an OOM kill.
3. **What its event stream said** — §3.7a's band, added in S6b.
4. **What the process said** — its exit status.
5. **What it produced** — a verdict, a diff, a dirty tree.

**What S6b changed, and what it cost.** The old ranking was anchored on band 4:
everything above it was a reason to disbelieve the exit status, everything below
it only ran once the process had exited cleanly. Band 3 is new and sits *above*
the exit status, which is the whole point of it — ``PROVIDER_ERROR`` exists
because a provider failure arrives as an exit code of **zero**, and a taxonomy
that reads the exit status first reports that run as a success. A false success
is a different severity of bug from a misclassified failure: verification, the
merge gate and the epic aggregator are all built to trust that one value.

Three orderings inside the new work are decisions rather than consequences:

- **``NO_MODEL_RESPONSE`` outranks ``PROVIDER_ERROR``.** A rejected credential
  produces both — an error frame, and no model turn anywhere in the stream — and
  the more specific of the two is the one that says nothing happened at all.
  ``PROVIDER_ERROR`` is then what it should be: the model *was* working and the
  provider stopped it.
- **``TESTS_FAILED`` still outranks ``DROPPED_COMMIT``.** Band 5 keeps S6's
  internal order, what it *said* before what it *produced*. The cost is real: an
  agent that wrote a failing verdict and also forgot to commit is reported as
  failing tests, and the dropped commit is visible only in ``commits_ahead`` on
  the result. Both are retried the same way, so nothing downstream turns on it.
- **``DROPPED_COMMIT`` outranks ``EMPTY_DIFF``** and takes half of what
  ``EMPTY_DIFF`` used to own. An empty diff over a **clean** tree is still an
  empty diff — the agent read the plan and concluded there was nothing to do.
  An empty diff over a tree the agent *left dirty* is not a no-op, it is work
  that was never claimed. The third case, a tree dirty only where the runner
  itself installed files, stays an empty diff: our conventions file, plan and
  verdict are in that tree on every run, and a naive dirtiness probe would report
  every single run as a dropped commit. Telling those apart is ``installed``'s
  job, and it is why the split cannot be a one-line ``is_dirty`` check.
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

    #: Why, when the stop came from outside the process (§3.11). Diagnostic
    #: only — ``cancelled`` is what the classifier reads — but a ``cancelled``
    #: result that cannot say whether a person or the silence detector stopped
    #: it is one somebody has to go and correlate timestamps to understand.
    cancelled_because: str | None = None

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

    #: What the stream's terminal turn carried, when it carried an error (§3.7a).
    #: ``None`` covers both "no error" and "this CLI emits nothing structured
    #: enough to have a terminal turn"; the two are not distinguished here
    #: because neither is a reason to fail a run.
    provider_error: str | None = None

    #: Whether the model spoke at all. Three-valued on purpose: ``False`` is the
    #: rejected credential — events flowed, a banner, an init frame, and not one
    #: model turn — while ``None`` is a CLI that emits prose and cannot be asked.
    #: A two-valued field defaulting to ``False`` would report every run of every
    #: plain-text CLI as ``no-model-response``.
    model_turn_seen: bool | None = None

    #: What the tree says, after the runner committed whatever was left. Read
    #: with git rather than reported by the agent: the number that decides
    #: whether a pull request gets opened does not come from the process being
    #: judged. ``files_changed`` is duplicated out of ``diff`` because the
    #: classifier only ever asks that one question of it.
    files_changed: int = 0
    diff: DiffStat | None = None
    tree_hash: str | None = None

    #: Commits **the agent** made on top of the declared base, counted before the
    #: runner's own safety commit. After that commit the number always includes
    #: ours, and the question this answers stops being answerable.
    commits_ahead: int = 0

    #: Paths the agent left uncommitted, with the runner's own installed files
    #: already taken out. Non-empty with ``commits_ahead == 0`` is §3.7a's
    #: dropped commit.
    dirty_paths: tuple[str, ...] = ()

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
    # Band 1 — things we did.
    if completion.cancelled:
        return RunnerOutcome.CANCELLED
    if completion.startup_error is not None:
        return RunnerOutcome.STARTUP_FAILED
    if completion.budget_exceeded:
        return RunnerOutcome.BUDGET_EXCEEDED
    if completion.timed_out:
        return RunnerOutcome.TIMED_OUT

    # Band 2 — things that happened to it.
    if completion.egress_denied:
        return RunnerOutcome.NETWORK_DENIED
    if completion.disk_full or _looks_disk_full(completion.stderr_tail):
        return RunnerOutcome.DISK_FULL
    if completion.oom_killed or _looks_oom_killed(completion):
        return RunnerOutcome.OOM_KILLED

    # Band 3 — what its event stream said. Above the exit status, because the
    # failures this band names arrive with an exit status of zero.
    if completion.model_turn_seen is False:
        return RunnerOutcome.NO_MODEL_RESPONSE
    if completion.provider_error is not None:
        return RunnerOutcome.PROVIDER_ERROR

    # Band 4 — what the process said.
    if completion.exit_code != 0:
        return RunnerOutcome.NON_ZERO_EXIT

    # Band 5 — exited cleanly, so the question stops being "what went wrong with
    # the process" and becomes "is what it produced any good".
    if _blocked(completion):
        return RunnerOutcome.BLOCKED
    if _tests_failed(completion):
        return RunnerOutcome.TESTS_FAILED
    if _dropped_commit(completion):
        return RunnerOutcome.DROPPED_COMMIT
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


def _dropped_commit(completion: Completion) -> bool:
    """The agent worked and never claimed it.

    Both halves are required, and neither is sufficient. **Uncommitted changes
    on their own** are ordinary: an agent that committed four times and left a
    scratch file behind has not dropped anything. **No commits on their own** is
    the deliberate no-op the plan asks for a value for. It is the pair — nothing
    committed, and a tree that is not clean — that says work happened and was
    never turned into a commit, which is the characteristic weak-model failure.

    ``dirty_paths`` has already had the runner's own installed files removed. If
    it had not, this would fire on every run, because our plan and verdict are in
    that tree every time.
    """
    return completion.commits_ahead == 0 and bool(completion.dirty_paths)


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
