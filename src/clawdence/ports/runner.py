"""Dispatching work across the trust boundary.

``RunnerRequest`` and ``RunnerResult`` are already domain types, because they
are the one contract that leaves the control plane (``domain.runner``). What is
here is the *port* — how a request is handed over — and the obligation that
comes with getting an answer back from a process that executed repo code.

**Dispatch is idempotent on ``idempotency_key``.** Redelivery is not
hypothetical: a step times out, the watchdog recovers it, and the run resumes
while the original container is still working. Two dispatches of one attempt
means two agents editing one worktree and, if it got as far as billing, two
charges for one story. The key is derived from ``run:stage:attempt``
(``engine.idempotency_key``), so a resumed run that reached the same attempt
number collides with the row the dead process wrote instead of duplicating it.

**Results are validated before the control plane acts on them.**
``validate_result`` is the shared version of that check. Putting it here rather
than in each adapter is the point: v1 validated runner output at the call site,
differently in each of three places, and the one that forgot to check whether a
diff existed is the one that opened empty PRs. A result that fails validation is
a ``PermanentError`` — a runner that returns a result for a different run is not
having a bad day, it is misbehaving, and retrying it is not a plan.

The default implementation refuses. A stub runner that returns
``SUCCEEDED`` would make a workflow look like it did work, which is the most
expensive way to be wrong about an orchestrator — the same reasoning as
``engine.handlers.UnimplementedHandler``, and for the same reason it is spelled
out twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from clawdence.domain import RunnerOutcome, RunnerRequest, RunnerResult
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.errors import PermanentError

#: Outcomes that mean the runner produced a tree worth talking about. Anything
#: else must not carry a ``tree_hash``, because a hash attached to a timeout is
#: a hash something will eventually try to merge.
_PRODUCED_WORK = frozenset({RunnerOutcome.SUCCEEDED, RunnerOutcome.TESTS_FAILED})


class RunnerPort(Protocol):
    """Runs a request in the data plane and returns what happened."""

    async def dispatch(self, request: RunnerRequest) -> RunnerResult:
        """Execute, and return a validated result.

        Repeating a request whose ``idempotency_key`` has already been
        dispatched returns the original result without executing again.

        Raises ``PortError`` only for failures to *dispatch* — the image is
        missing, the daemon is unreachable, the request is malformed. A run that
        started and went badly is a ``RunnerResult`` with a failing outcome, not
        an exception: the failure taxonomy in ``RunnerOutcome`` exists so the
        retry policy can tell a flaky test from an OOM kill, and collapsing
        those into an exception is exactly what v1 did.
        """
        ...

    async def cancel(self, request: RunnerRequest) -> bool:
        """Stop an in-flight dispatch. ``True`` if something was stopped.

        Cancelling something that already finished is ``False``, not an error:
        the race between a watchdog deciding a step is overdue and the step
        reporting is normal, and it must not itself produce a failure.
        """
        ...


def validate_result(request: RunnerRequest, result: RunnerResult) -> RunnerResult:
    """Check a result against the request it answers. Returns it, or raises.

    Every rule here is one that was violated in v1 at least once:

    - **Identity.** A result naming a different run or stage is not this
      attempt's answer, whatever else is true about it.
    - **A hash only where work happened.** ``tree_hash`` on a timeout or a
      startup failure is a hash something downstream will try to verify or
      merge. Conversely, succeeding without one leaves evidence unbindable.
    - **Time runs forwards.** ``finished_at`` before ``started_at`` means the
      duration used for cost attribution and stall detection is negative.
    - **Success means a diff.** ``EMPTY_DIFF`` is a distinct outcome for a
      reason; a ``SUCCEEDED`` with nothing changed is v1's ``_EmptyPRError``
      arriving one step later, as a pull request with no content.

    What this cannot check is whether the *content* is honest — the diff, the
    test evidence and the discovery notes come from a process that ran
    model-generated code. Those are re-derived from the worktree by S13, not
    trusted from this payload.
    """
    if result.run_id != request.run_id or result.stage_id != request.stage_id:
        raise PermanentError(
            "runner-result-mismatch",
            f"result is for {result.run_id}/{result.stage_id}, "
            f"but the request was {request.run_id}/{request.stage_id}",
        )

    produced = result.outcome in _PRODUCED_WORK
    if result.tree_hash is not None and not produced:
        raise PermanentError(
            "runner-result-invalid",
            f"outcome {result.outcome.value!r} carries a tree hash, but nothing was committed",
        )
    if result.outcome is RunnerOutcome.SUCCEEDED and result.tree_hash is None:
        raise PermanentError(
            "runner-result-invalid",
            "a succeeded result has no tree hash, so its evidence could not be bound to a tree",
        )

    if result.finished_at < result.started_at:
        raise PermanentError(
            "runner-result-invalid",
            f"finished_at {result.finished_at.isoformat()} precedes "
            f"started_at {result.started_at.isoformat()}",
        )

    if result.outcome is RunnerOutcome.SUCCEEDED and request.contract.require_non_empty_diff:
        changed = result.diff.files_changed if result.diff is not None else 0
        if changed == 0:
            raise PermanentError(
                "runner-result-invalid",
                "the contract requires a non-empty diff, but the result changed no files "
                "— that is an 'empty-diff' outcome, not a success",
            )

    return result


class RefusingRunner:
    """The default. Refuses, naming the step that will make it work.

    Registered wherever a ``RunnerPort`` is required and none is configured, so
    that "we forgot to wire the runner" surfaces as an error naming what to wire
    rather than as a workflow that reports success for work nobody did.
    """

    __slots__ = ()

    async def dispatch(self, request: RunnerRequest) -> RunnerResult:
        raise PermanentError(
            "no-runner",
            f"no runner is configured, so {request.stage_id!r} cannot execute — "
            f"wire clawdence.runners.HostRunner, or the container runner S7 adds",
        )

    async def cancel(self, request: RunnerRequest) -> bool:
        return False


class FakeRunner:
    """Returns canned results, keyed by stage id. The fake.

    Canned per *stage* rather than per call, because the interesting workflow
    tests are "the coding stage fails its tests, so the run retries and then
    halts" — which is a statement about a stage, and stays readable when the
    executor decides how many times to call it.

    Every request is kept in ``dispatched``, which is what the tests asserting
    the trust boundary read: that the plan text carried no credential, and that
    two attempts sent two different idempotency keys.
    """

    __slots__ = ("_by_key", "_cancelled", "_clock", "_default", "_dispatched", "_fail_with", "_of")

    def __init__(
        self,
        results: Mapping[str, RunnerResult] | None = None,
        *,
        default: RunnerResult | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._of = dict(results or {})
        self._default = default
        self._clock = clock
        self._dispatched: list[RunnerRequest] = []
        self._by_key: dict[str, RunnerResult] = {}
        self._cancelled: list[str] = []
        self._fail_with: BaseException | None = None

    def returns(self, stage_id: str, result: RunnerResult) -> None:
        self._of[stage_id] = result

    def fail_with(self, error: BaseException | None) -> None:
        """Make dispatch raise — the data plane being unreachable, not a run
        that went badly. The distinction the port's docstring insists on."""
        self._fail_with = error

    async def dispatch(self, request: RunnerRequest) -> RunnerResult:
        if self._fail_with is not None:
            raise self._fail_with

        settled = self._by_key.get(request.idempotency_key)
        if settled is not None:
            return settled

        canned = self._of.get(request.stage_id, self._default)
        if canned is None:
            raise PermanentError(
                "no-canned-result",
                f"this fake has no result for stage {request.stage_id!r}",
            )

        # The canned result is written by a test that does not know the run id,
        # so identity comes from the request. Everything else is the test's.
        result = validate_result(
            request,
            canned.model_copy(update={"run_id": request.run_id, "stage_id": request.stage_id}),
        )
        self._dispatched.append(request)
        self._by_key[request.idempotency_key] = result
        return result

    async def cancel(self, request: RunnerRequest) -> bool:
        if request.idempotency_key in self._by_key:
            return False
        self._cancelled.append(request.idempotency_key)
        return True

    @property
    def dispatched(self) -> Sequence[RunnerRequest]:
        return tuple(self._dispatched)

    @property
    def cancelled(self) -> Sequence[str]:
        return tuple(self._cancelled)
