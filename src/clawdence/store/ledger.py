"""The durable ``Ledger``: what the executor writes when a run is real.

Same interface as ``engine.InMemoryLedger``, so the executor cannot tell them
apart — which is what makes crash-resume a property of the store rather than a
mode the control flow has to know about. Three things happen here that cannot
happen in memory:

**A run that is opened twice is resumed, not restarted.** If the row already
exists, its ``created_at`` is kept and the prior step results are loaded, so the
executor's "skip what already succeeded" rule has something to read.

**Steps abandoned by a dead process are reconciled on the way in.** A row still
saying ``running`` when nobody is running it is v1's stale-spawn bug wearing a
different hat: every status check downstream reads it and believes it. Resume
closes those out as ``cancelled`` with an explicit reason before anything else
looks at them.

**State and audit commit together.** Each write here wraps the step row, the run
heartbeat and the audit entry in one transaction, so the log can never describe
a transition that was rolled back. This is *not* the dual-write problem — that
one is about an external side effect (a PR, a Slack post) and belongs to S4b.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from pydantic import JsonValue

from clawdence.domain import (
    Actor,
    ActorKind,
    EventKind,
    Run,
    RunStatus,
    StepError,
    StepResult,
    StepStatus,
)
from clawdence.store.schema import transaction
from clawdence.store.state import StateStore

#: Matches the engine's — the executor and its ledger read the same clock.
Clock = Callable[[], datetime]

#: Everything the engine writes is the system acting. A human approving a gate
#: (S17) or an agent producing a plan (S12) will say so themselves.
SYSTEM = Actor(kind=ActorKind.SYSTEM, id="engine")

_INTERRUPTED = StepError(
    kind="interrupted",
    message="the process executing this step did not survive to record a result",
    retryable=True,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SqliteLedger:
    """Persists one run. Satisfies ``engine.Ledger``."""

    __slots__ = ("_attempts", "_clock", "_final", "_highest", "_run_id", "_store")

    def __init__(
        self,
        store: StateStore,
        *,
        run_id: str,
        clock: Clock | None = None,
    ) -> None:
        """
        ``clock`` is consulted only for records that carry no time of their own —
        a stage skipped by a false guard never started, so it has no instant but
        still belongs in the timeline.
        """
        self._store = store
        self._run_id = run_id
        self._clock = clock if clock is not None else _utc_now
        self._attempts: list[StepResult] = []
        self._final: dict[str, StepResult] = {}
        self._highest: dict[str, int] = {}

    # The resolver the executor builds is bound to the mapping ``final``
    # returns, so every method below mutates these in place and never rebinds.

    def open_run(self, run: Run) -> Run:
        existing = self._store.get_run(run.id)
        if existing is None:
            self._store.create_run(run)
            self._audit(
                EventKind.RUN_STARTED,
                at=run.created_at,
                work_item_id=run.work_item_id,
                payload={
                    "workflow": run.workflow,
                    "workflow_version": run.workflow_version,
                    "resumed": False,
                },
            )
            return run

        at = run.updated_at
        with transaction(self._store.connection):
            self._reconcile(at=at)
            resumed = self._store.update_run(
                run.id,
                lambda current: current.model_copy(
                    update={
                        "status": RunStatus.RUNNING,
                        "updated_at": at,
                        # It is live again, so it has not finished. Leaving the
                        # old instant here would make a running run look done to
                        # every reader that checks this field first.
                        "finished_at": None,
                    }
                ),
            )
            self._audit(
                EventKind.RUN_STARTED,
                at=at,
                work_item_id=resumed.work_item_id,
                payload={
                    "workflow": resumed.workflow,
                    "workflow_version": resumed.workflow_version,
                    "resumed": True,
                },
            )
        self._load()
        return resumed

    def close_run(self, *, status: RunStatus, at: datetime) -> Run:
        with transaction(self._store.connection):
            run = self._store.update_run(
                self._run_id,
                lambda current: current.model_copy(
                    update={"status": status, "updated_at": at, "finished_at": at}
                ),
            )
            self._audit(EventKind.RUN_FINISHED, at=at, payload={"status": status.value})
        return run

    def next_attempt(self, stage_id: str) -> int:
        return self._highest.get(stage_id, 0) + 1

    def begin(self, result: StepResult) -> None:
        at = result.started_at if result.started_at is not None else self._clock()
        with transaction(self._store.connection):
            self._store.start_step(result)
            self._store.touch_run(self._run_id, at=at)
            self._audit(
                # A second attempt is a different fact from a first one, and the
                # timeline is read to find out how often steps need one.
                EventKind.STEP_RETRIED if result.attempt > 1 else EventKind.STEP_STARTED,
                at=at,
                stage_id=result.stage_id,
                payload={
                    "attempt": result.attempt,
                    "type": result.type.value,
                    "timeout_seconds": result.timeout_seconds,
                },
            )
        self._note(result)

    def record(self, result: StepResult) -> None:
        at = result.finished_at if result.finished_at is not None else self._clock()
        with transaction(self._store.connection):
            self._store.finish_step(result)
            self._store.touch_run(self._run_id, at=at)
            self._audit(
                EventKind.STEP_TIMED_OUT
                if result.status is StepStatus.TIMED_OUT
                else EventKind.STEP_FINISHED,
                at=at,
                stage_id=result.stage_id,
                payload={
                    "attempt": result.attempt,
                    "status": result.status.value,
                    # The kind, never the message. Messages carry stderr tails
                    # and this log cannot un-write a pasted key — see ``audit``.
                    "error_kind": result.error.kind if result.error is not None else None,
                },
            )
        self._note(result)
        self._attempts.append(result)
        self._final[result.stage_id] = result

    @property
    def final(self) -> Mapping[str, StepResult]:
        return self._final

    @property
    def attempts(self) -> Sequence[StepResult]:
        return self._attempts

    def _reconcile(self, *, at: datetime) -> None:
        """Close out steps whose process is gone. See the module docstring."""
        for stale in self._store.running_steps(run_id=self._run_id):
            self._store.finish_step(
                stale.model_copy(
                    update={
                        "status": StepStatus.CANCELLED,
                        "finished_at": at,
                        "error": _INTERRUPTED,
                    }
                )
            )
            self._audit(
                EventKind.STEP_FINISHED,
                at=at,
                stage_id=stale.stage_id,
                payload={
                    "attempt": stale.attempt,
                    "status": StepStatus.CANCELLED.value,
                    "error_kind": _INTERRUPTED.kind,
                },
            )

    def _load(self) -> None:
        self._attempts.clear()
        self._final.clear()
        self._highest.clear()
        for result in self._store.steps_for(self._run_id):
            self._note(result)
            if result.status is StepStatus.RUNNING:  # pragma: no cover - see below
                # Another process is executing this step right now: not settled
                # history, and not something to resolve references against.
                # Unreachable under M1, where ``_reconcile`` has just closed out
                # every running step of this run and only one run executes at a
                # time. It is here for S7, which is when a second writer becomes
                # possible and this stops being hypothetical.
                continue
            self._attempts.append(result)
            self._final[result.stage_id] = result

    def _note(self, result: StepResult) -> None:
        previous = self._highest.get(result.stage_id, 0)
        self._highest[result.stage_id] = max(previous, result.attempt)

    def _audit(
        self,
        kind: EventKind,
        *,
        at: datetime,
        stage_id: str | None = None,
        work_item_id: str | None = None,
        payload: JsonValue = None,
    ) -> None:
        self._store.audit.record(
            kind,
            at=at,
            run_id=self._run_id,
            work_item_id=work_item_id,
            stage_id=stage_id,
            actor=SYSTEM,
            payload=payload,
        )
