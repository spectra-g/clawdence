"""Finding the runs nobody is running any more.

This replaces v1's ``check_stale_spawns``, whose bugs all came from the same
place: hand-written status checks that drifted from what was actually true, so
the daemon would report a story as ``PLANNING`` while nothing had been planning
it for an hour. The fix is not a better check, it is a better *input* — a step
row written before the work starts, carrying the timeout it was started under.
Overdue is then arithmetic rather than inference.

Three shapes of stall. The first two are S4's, and they are about a process that
died in one of two places:

**A step that is overdue.** Its row still says ``running`` and its declared
timeout has passed. The executor's own ``asyncio.wait_for`` handles this while
the executor is alive; this handles the case where it is not.

**A run with no step in flight and no heartbeat.** The process died *between*
steps, so there is nothing overdue to find — only a run claiming to be running
that has not touched its row since. Without this the structural case is
invisible, which is precisely how v1 accumulated work items that were forever
almost-started.

Steps that declared no timeout still get one, from ``DEFAULT_STEP_SECONDS``. A
step with no limit is a step that can hang forever, and "the author did not say"
is not a reason to let it.

**A run that is alive, inside its timeout, and has said nothing** — S6c's, and
§3.11 asks for it as a *different detector* rather than a shorter budget for the
two above, because neither of them can see it. The process is running, so
nothing is orphaned; the step is inside the limit its author declared, so
nothing is overdue. The motivating incident was a linter accumulating 57 CPU-minutes
behind a stuck tool call with the run reporting healthy throughout, and both S4
detectors would have reported it healthy too. The signal is the timestamp of the
newest thing the run *said*, which the runner writes through
``ControlPort.heartbeat`` as it reads the agent's output.

Why this matters more than it looks: under M1 a hung run is annoying and
somebody notices, because runs are serial and manual. Under S7's bounded
N-in-flight queue it holds a slot forever, so one hang permanently degrades
throughput and N hangs deadlock the queue.

**What recovery does, and deliberately does not do.** For the first two it marks
the step timed out and halts the run, then stops. It does not retry, because
retrying is the workflow's declared policy and applying it needs an executor —
what the watchdog owns is making the *state* stop lying. Restarting the work is
``clawdence run --resume``, which re-enters the run with its retry policy intact
and its finished steps left alone.

For a silent run it does something different and deliberately gentler: it
**asks the run to stop** through the cancellation latch, and changes no state of
its own. §3.11 requires that a silent run fail through the normal collection
path so whatever the agent committed before it hung is preserved rather than
abandoned, and the only thing that can walk that path is the process holding the
worktree. Halting the row from out here would leave that process running and the
work uncollected — the state would stop lying by starting to lie the other way.
If nobody is attending the run, nothing acknowledges the request and the step's
own timeout catches it as an ordinary overdue step later. That is the backstop,
and it is why this detector can afford to be polite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from clawdence.domain import ActorKind, EventKind, RunStatus, StepError, StepResult, StepStatus
from clawdence.store.control import Cancellations
from clawdence.store.ledger import SYSTEM
from clawdence.store.schema import transaction
from clawdence.store.state import StateStore

#: The limit for a step whose stage declared none. An hour is long enough not to
#: cut off honest work and short enough that a wedged run is noticed the same
#: day. Stages that care declare their own.
DEFAULT_STEP_SECONDS: Final = 3600.0

#: How long a run may claim to be running, with nothing in flight, before it is
#: treated as abandoned. Longer than any gap the executor puts between steps.
DEFAULT_HEARTBEAT_SECONDS: Final = 900.0

#: How long a run with a step in flight may say **nothing** before the silence
#: is treated as a hang (§3.11). Forty-five minutes, and the generosity is the
#: design rather than caution: a false positive here kills real work, and the
#: things that legitimately go quiet — a cold dependency install, a full test
#: suite on a large repository, a container image pull — are minutes, not most
#: of an hour. The motivating incident ran for well over this with the run reporting
#: healthy, so a budget that would have caught it has plenty of room underneath.
#:
#: **On by default, and that is the load-bearing part.** A reclaim safety net
#: that depends on an operator remembering a setting is not a safety net; it is
#: a setting. Tunable through every entry point that reaches ``detect``.
DEFAULT_SILENCE_SECONDS: Final = 2700.0

#: The watchdog asking for a stop is not a person asking for one, and a timeline
#: that could not tell them apart would make "who killed my run" unanswerable.
WATCHDOG = "watchdog"


class StallKind(StrEnum):
    STEP = "step"
    RUN = "run"
    SILENT = "silent"


@dataclass(frozen=True, slots=True)
class Stall:
    """One thing that stopped moving, and by how much."""

    kind: StallKind
    run_id: str
    stage_id: str | None
    limit_seconds: float
    overdue_by_seconds: float

    def describe(self) -> str:
        overdue = f"{self.overdue_by_seconds:.0f}s past its {self.limit_seconds:.0f}s limit"
        if self.kind is StallKind.STEP:
            return f"run {self.run_id} step {self.stage_id} is {overdue}"
        if self.kind is StallKind.SILENT:
            return (
                f"run {self.run_id} step {self.stage_id} is still running and has said "
                f"nothing for {overdue}"
            )
        return f"run {self.run_id} has not progressed in {overdue}"


def detect(
    store: StateStore,
    *,
    now: datetime,
    default_step_seconds: float = DEFAULT_STEP_SECONDS,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    silence_seconds: float = DEFAULT_SILENCE_SECONDS,
) -> tuple[Stall, ...]:
    """Everything overdue, without changing anything.

    Separate from ``recover`` so that looking is free: an operator asking what
    is stuck should not have to halt it to find out, and ``clawdence runs
    recover --dry-run`` is the same code path as the real thing.
    """
    stalls: list[Stall] = []
    stalled_runs: set[str] = set()
    cancellations = Cancellations(store)

    for step in store.running_steps():
        if step.started_at is None:  # pragma: no cover - begin always sets it
            continue
        limit = step.timeout_seconds if step.timeout_seconds is not None else default_step_seconds
        overdue = (now - step.started_at).total_seconds() - limit
        if overdue > 0:
            stalled_runs.add(step.run_id)
            stalls.append(
                Stall(
                    kind=StallKind.STEP,
                    run_id=step.run_id,
                    stage_id=step.stage_id,
                    limit_seconds=limit,
                    overdue_by_seconds=overdue,
                )
            )
            continue

        # Inside its declared timeout, so neither S4 detector will ever look at
        # it again. The question left is whether anything has been heard.
        silent = _silence(store, step, now=now, budget=silence_seconds)
        if silent is not None and cancellations.pending(step.run_id) is None:
            # Already asked to stop is not a new stall. Re-reporting it every
            # sweep would make a run that is shutting down look like a run that
            # is accumulating problems, and the sweep runs on a timer.
            stalls.append(silent)

    for run in store.list_runs(status=RunStatus.RUNNING):
        if run.id in stalled_runs or store.running_steps(run_id=run.id):
            # Either already caught above, or genuinely working. A run whose
            # step is in flight and inside its timeout is not stalled however
            # long it has been going.
            continue
        overdue = (now - run.updated_at).total_seconds() - heartbeat_seconds
        if overdue > 0:
            stalls.append(
                Stall(
                    kind=StallKind.RUN,
                    run_id=run.id,
                    stage_id=None,
                    limit_seconds=heartbeat_seconds,
                    overdue_by_seconds=overdue,
                )
            )
    return tuple(stalls)


def recover(store: StateStore, stall: Stall, *, now: datetime) -> None:
    """Make the state match reality: time the step out, halt the run.

    Except for a silent run, where the state is not lying yet — the run really
    is running — and what is wrong is that it has stopped doing anything. See
    ``_ask_to_stop``.
    """
    if stall.kind is StallKind.SILENT:
        _ask_to_stop(store, stall, now=now)
        return
    with transaction(store.connection):
        if stall.kind is StallKind.STEP:
            for step in store.running_steps(run_id=stall.run_id):
                if step.stage_id == stall.stage_id:
                    _time_out(store, step, stall=stall, now=now)
        _halt(store, stall, now=now)


def sweep(
    store: StateStore,
    *,
    now: datetime,
    default_step_seconds: float = DEFAULT_STEP_SECONDS,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    silence_seconds: float = DEFAULT_SILENCE_SECONDS,
) -> tuple[Stall, ...]:
    """Detect, recover, and report what was recovered."""
    stalls = detect(
        store,
        now=now,
        default_step_seconds=default_step_seconds,
        heartbeat_seconds=heartbeat_seconds,
        silence_seconds=silence_seconds,
    )
    for stall in stalls:
        recover(store, stall, now=now)
    return stalls


def _silence(
    store: StateStore,
    step: StepResult,
    *,
    now: datetime,
    budget: float,
) -> Stall | None:
    """Whether a step in flight has stopped saying anything (§3.11).

    ``updated_at`` is the column, and reusing S4's heartbeat rather than adding
    a second one is deliberate: it already means "the newest instant anything
    knew about this run", it is already monotone under ``MAX`` so two writers
    cannot move it backwards, and the runner's ``ControlPort.heartbeat`` writes
    to it through the same ``touch_run``. A second column would be a second
    thing to remember to write, and the failure mode of forgetting is a detector
    that kills healthy runs.

    The step's own ``started_at`` is taken as a floor. Without it a run that sat
    idle past the budget and *then* started a step would be reported silent
    immediately, because the ledger's ``begin`` writes the heartbeat at the
    instant the step starts and nothing has had a chance to speak yet — a
    detector that fires before the work has had time to produce a line is one
    nobody would leave switched on.
    """
    run = store.get_run(step.run_id)
    if run is None or run.status is not RunStatus.RUNNING:  # pragma: no cover - FK guarantees it
        return None
    last_heard = max(run.updated_at, step.started_at) if step.started_at else run.updated_at
    quiet = (now - last_heard).total_seconds() - budget
    if quiet <= 0:
        return None
    return Stall(
        kind=StallKind.SILENT,
        run_id=step.run_id,
        stage_id=step.stage_id,
        limit_seconds=budget,
        overdue_by_seconds=quiet,
    )


def _ask_to_stop(store: StateStore, stall: Stall, *, now: datetime) -> None:
    """Request a cancel, and change nothing else.

    The whole recovery, and the restraint is the point. The process holding the
    worktree is the only thing that can collect what the agent committed before
    it hung, and it collects it on its way out of a cancellation — the same path
    an operator's stop takes. So this hands the live runner a reason and lets it
    walk that path. Marking the step timed out from here instead would abandon
    the work and leave a process running against a row that says it is not.
    """
    Cancellations(store).request(
        stall.run_id,
        at=now,
        reason=stall.describe(),
        requested_by=WATCHDOG,
        actor_kind=ActorKind.SYSTEM,
    )


def _time_out(store: StateStore, step: StepResult, *, stall: Stall, now: datetime) -> None:
    store.finish_step(
        step.model_copy(
            update={
                "status": StepStatus.TIMED_OUT,
                "finished_at": now,
                "error": StepError(
                    kind="watchdog-timeout",
                    message=(
                        f"no result after {stall.limit_seconds:.0f}s; "
                        "the process running this step is gone"
                    ),
                    retryable=True,
                ),
            }
        )
    )
    store.audit.record(
        EventKind.STEP_TIMED_OUT,
        at=now,
        run_id=step.run_id,
        stage_id=step.stage_id,
        actor=SYSTEM,
        payload={
            # Which of the two found it matters: the executor timing a step out
            # means the engine was alive and in control, the watchdog finding it
            # means the engine was not.
            "detected_by": "watchdog",
            "attempt": step.attempt,
            "limit_seconds": stall.limit_seconds,
            "overdue_by_seconds": round(stall.overdue_by_seconds, 3),
        },
    )


def _halt(store: StateStore, stall: Stall, *, now: datetime) -> None:
    store.update_run(
        stall.run_id,
        lambda current: current.model_copy(
            update={"status": RunStatus.HALTED, "updated_at": now, "finished_at": now}
        ),
    )
    store.audit.record(
        EventKind.HALTED_FOR_HUMAN,
        at=now,
        run_id=stall.run_id,
        stage_id=stall.stage_id,
        actor=SYSTEM,
        payload={
            "reason": stall.kind.value,
            "detail": stall.describe(),
            "resume_with": "clawdence run --resume",
        },
    )
