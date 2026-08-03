"""Where a run's record goes while it executes.

The executor owns control flow and nothing else. Everything it *learns* — the
run row, and one row per step attempt — leaves through this interface, which is
what let S4 put a database behind it without changing a line of the control flow
above it. Two implementations, differing only in durability: ``InMemoryLedger``
here, and ``store.SqliteLedger``, which is the source of truth per ADR-0005.

``begin`` is the method that exists for S4 rather than S3. A step result written
only when a step *finishes* cannot describe a step that never finished — which
is precisely the state a killed process leaves behind, and precisely v1's
stale-spawn bug. The running row, carrying ``started_at`` and the timeout the
attempt was started under, is what the watchdog reads and what resume finds
abandoned.

``next_attempt`` exists for the same reason. Attempt numbers are global to a
``(run, concrete execution node)`` rather than local to one process. At root
the node id is the authored stage id; composition derives a stable id from its
fan-out/branch/iteration scope. A resumed run that restarted its counter at 1
would collide with the row the previous incarnation already wrote.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from clawdence.domain import Run, RunStatus, StepResult


class Ledger(Protocol):
    """The executor's whole view of persistence."""

    def open_run(self, run: Run) -> Run:
        """Begin, or re-join, a run.

        Takes the record the executor would write and returns the authoritative
        one. Those differ on resume: the stored run keeps its original
        ``created_at``, and returning it rather than the candidate is what stops
        a resumed run from claiming it started when it was restarted.
        """
        ...

    def close_run(self, *, status: RunStatus, at: datetime) -> Run:
        """Record a terminal status, returning the finished record."""
        ...

    def next_attempt(self, stage_id: str) -> int:
        """The attempt number a fresh execution of this stage should carry."""
        ...

    def begin(self, result: StepResult) -> None:
        """Record an attempt as started, before it can report anything."""
        ...

    def record(self, result: StepResult) -> None:
        """Record how an attempt ended. Supersedes the row ``begin`` wrote."""
        ...

    @property
    def final(self) -> Mapping[str, StepResult]:
        """The latest result per stage, including any from a previous run."""
        ...

    @property
    def attempts(self) -> Sequence[StepResult]:
        """Every finished attempt, oldest first, superseded ones included."""
        ...


@dataclass(slots=True)
class InMemoryLedger:
    """A run that leaves no trace.

    What S3 had, kept because it is the honest default for ``clawdence run
    --no-state`` and because every control-flow test wants it: an executor test
    should fail over control flow, not over SQL.
    """

    _run: Run | None = None
    _attempts: list[StepResult] = field(default_factory=list)
    _final: dict[str, StepResult] = field(default_factory=dict)
    _highest: dict[str, int] = field(default_factory=dict)

    def open_run(self, run: Run) -> Run:
        self._run = run
        return run

    def close_run(self, *, status: RunStatus, at: datetime) -> Run:
        if self._run is None:  # pragma: no cover - execute always opens first
            raise RuntimeError("close_run before open_run")
        self._run = self._run.model_copy(
            update={"status": status, "updated_at": at, "finished_at": at}
        )
        return self._run

    def next_attempt(self, stage_id: str) -> int:
        return self._highest.get(stage_id, 0) + 1

    def begin(self, result: StepResult) -> None:
        self._note(result)

    def record(self, result: StepResult) -> None:
        self._note(result)
        self._attempts.append(result)
        self._final[result.stage_id] = result

    def _note(self, result: StepResult) -> None:
        previous = self._highest.get(result.stage_id, 0)
        self._highest[result.stage_id] = max(previous, result.attempt)

    @property
    def final(self) -> Mapping[str, StepResult]:
        return self._final

    @property
    def attempts(self) -> Sequence[StepResult]:
        return self._attempts
