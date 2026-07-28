"""Finding the runs nobody is running any more.

This replaces v1's ``check_stale_spawns``, whose bugs all came from the same
place: hand-written status checks that drifted from what was actually true, so
the daemon would report a story as ``PLANNING`` while nothing had been planning
it for an hour. The fix is not a better check, it is a better *input* — a step
row written before the work starts, carrying the timeout it was started under.
Overdue is then arithmetic rather than inference.

Two shapes of stall, because a process can die in two places:

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

**What recovery does, and deliberately does not do.** It marks the step timed
out and halts the run, then stops. It does not retry, because retrying is the
workflow's declared policy and applying it needs an executor — what the watchdog
owns is making the *state* stop lying. Restarting the work is
``clawdence run --resume``, which re-enters the run with its retry policy
intact and its finished steps left alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from clawdence.domain import EventKind, RunStatus, StepError, StepResult, StepStatus
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


class StallKind(StrEnum):
    STEP = "step"
    RUN = "run"


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
        return f"run {self.run_id} has not progressed in {overdue}"


def detect(
    store: StateStore,
    *,
    now: datetime,
    default_step_seconds: float = DEFAULT_STEP_SECONDS,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
) -> tuple[Stall, ...]:
    """Everything overdue, without changing anything.

    Separate from ``recover`` so that looking is free: an operator asking what
    is stuck should not have to halt it to find out, and ``clawdence runs
    recover --dry-run`` is the same code path as the real thing.
    """
    stalls: list[Stall] = []
    stalled_runs: set[str] = set()

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
    """Make the state match reality: time the step out, halt the run."""
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
) -> tuple[Stall, ...]:
    """Detect, recover, and report what was recovered."""
    stalls = detect(
        store,
        now=now,
        default_step_seconds=default_step_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )
    for stall in stalls:
        recover(store, stall, now=now)
    return stalls


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
