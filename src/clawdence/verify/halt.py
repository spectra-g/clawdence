"""When a run stops for a person, and what that person may then do.

v1 had twelve ``_halt_story_for_human`` call sites and no enumeration of what
they meant. The consequence was not the halting — halting was right — it was the
*vocabulary*: with no list of states, every call site invented the resumption
that made sense locally, and the verb set grew to ``restart`` / ``retry`` /
``retry-coding`` / ``retry-consensus`` / ``approve`` / ``skip`` / ``pause`` /
``unpause`` / ``resume``, several of which overlapped and none of which was
defined against the others.

**DECIDED 2026-08-03 — this module owns the states; S17 owns the surface.** Here:
which conditions halt, what a halted run records, which resumptions each state
admits, and the invariant below. There: how a person acts on one — the verb set
derived from this table, who may use it, what they are shown, and the audit
trail of who decided what. Deliberately no operator UI in this file. A verb set
derived from a state machine that already exists is the thing v1 did not have.

**The invariant, and why it is a test rather than a docstring.** No exhausted
retry ever force-proceeds or force-approves. In v1 that was a rule held across
twelve sites by convention; here ``RESUMPTIONS`` is one table and
``test_no_state_admits_approval`` iterates the whole ``HaltState`` enum, so a
state added later fails the suite until somebody decides what it admits. The
domain half of the same rule is ``on_exhausted``'s one-value ``Literal``: there
is no value that says "give up and merge anyway", and there is no state that
admits the verb that would.

That invariant is also why ``VerificationContract.allowed_resume_verbs`` is
gone. It defaulted to all four verbs including ``approve``, which made the rule
above configurable — a contract could declare that its exhausted retries admit
"approve it anyway", and the ``Literal`` guarding the same thing one field above
would be decoration. Which resumptions a halt admits is a property of *why it
halted*, and belongs with the states.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from clawdence.domain import (
    HaltRecord,
    HaltState,
    ResumeVerb,
    RunnerOutcome,
    Shortfall,
    VerificationContract,
    VerificationResult,
)
from clawdence.verify.evidence import Stale, Staleness

#: Which resumptions each halt state admits. **The one place this is decided.**
#:
#: Read it as "what would have to be different for this verb to mean anything":
#:
#: - ``RETRY`` re-runs the failed work with the same budget and the same
#:   surroundings. It is admitted where a human may plausibly have changed
#:   something the run depends on but not the run itself — a fixture, a
#:   dependency, a flaky service — and withheld from ``BUDGET_EXCEEDED``, where
#:   the identical attempt would spend the identical money to hit the identical
#:   cap.
#: - ``RESTART`` starts over, and can carry new inputs: a new budget, a
#:   corrected plan. Admitted everywhere, because there is no halt state that
#:   starting again cannot in principle address.
#: - ``SKIP`` abandons the work item. Nothing merges — see ``ResumeVerb`` — so
#:   it is not the force-proceed this module forbids. Withheld from
#:   ``EVIDENCE_STALE`` alone, because there the work is finished and correct
#:   and the only thing missing is a test run; abandoning it would throw away
#:   completed work over a bookkeeping gap.
#: - ``APPROVE`` is admitted by nothing here. It belongs to S17's approval
#:   gates, where a human is deciding a question that was *asked*; a
#:   verification halt is a question that was answered, and the answer was no.
RESUMPTIONS: Final[Mapping[HaltState, tuple[ResumeVerb, ...]]] = {
    HaltState.RETRIES_EXHAUSTED: (ResumeVerb.RETRY, ResumeVerb.RESTART, ResumeVerb.SKIP),
    HaltState.BLOCKED: (ResumeVerb.RETRY, ResumeVerb.RESTART, ResumeVerb.SKIP),
    HaltState.BUDGET_EXCEEDED: (ResumeVerb.RESTART, ResumeVerb.SKIP),
    HaltState.EVIDENCE_STALE: (ResumeVerb.RETRY, ResumeVerb.RESTART),
    HaltState.VERIFICATION_ERROR: (ResumeVerb.RETRY, ResumeVerb.RESTART, ResumeVerb.SKIP),
}

#: Runner outcomes that halt as ``BLOCKED`` rather than counting an attempt.
#: Each names something a second identical attempt re-discovers at full price,
#: which is v1's budget being spent to learn that the fixture is still missing.
#: The source of truth for "is this worth retrying" stays
#: ``runners.handler.RETRYABLE``; this is the narrower question of which
#: non-retryable outcomes are a *person's* problem rather than a failed contract.
BLOCKING: Final[frozenset[RunnerOutcome]] = frozenset(
    {
        RunnerOutcome.BLOCKED,
        RunnerOutcome.EMPTY_DIFF,
        RunnerOutcome.OOM_KILLED,
        RunnerOutcome.DISK_FULL,
        RunnerOutcome.PROVIDER_ERROR,
        RunnerOutcome.NO_MODEL_RESPONSE,
    }
)


@dataclass(frozen=True, slots=True)
class Proceed:
    """The contract is met. The result is the evidence, bound to its tree."""

    result: VerificationResult


@dataclass(frozen=True, slots=True)
class Retry:
    """Try again. ``attempt`` is the number the next one will carry."""

    attempt: int
    result: VerificationResult

    #: Why the last one fell short, so the next attempt's prompt can say. This
    #: is the difference between a retry that is worth its money and v1's,
    #: which re-ran the same instructions and hoped.
    shortfalls: tuple[Shortfall, ...] = ()


@dataclass(frozen=True, slots=True)
class Halt:
    """Stop and hand it to a person. ``record`` is what they get."""

    record: HaltRecord


#: What ``decide`` returns. A union rather than a status field, so a caller that
#: forgets a case fails type checking instead of falling through to the merge.
Decision = Proceed | Retry | Halt


def admits(state: HaltState, verb: ResumeVerb) -> bool:
    """Whether ``verb`` is a legitimate resumption of ``state``."""
    return verb in RESUMPTIONS[state]


def decide(
    result: VerificationResult,
    *,
    run_id: str,
    attempts_made: int,
    outcome: RunnerOutcome = RunnerOutcome.SUCCEEDED,
    budget_exceeded: bool = False,
    verification_error: str | None = None,
    work_item_id: str | None = None,
    stage_id: str | None = None,
    now: datetime | None = None,
) -> Decision:
    """Proceed, retry, or halt — the whole of the loop's control flow.

    The ordering is the interesting part, and it is the same discipline as
    ``outcome.classify``: **the reasons that make another attempt pointless are
    checked before the count is.** A run that is out of money, or blocked on a
    missing dependency, halts on attempt one rather than spending two more to
    arrive at the same place with a smaller budget. That is the specific v1
    behaviour this ordering exists to prevent.

    A passing result short-circuits everything, including a budget that was
    exceeded on the way: the work is done and the evidence is bound to the tree,
    and throwing that away to halt on a cap that has already been paid would be
    spending the money and discarding the result.
    """
    at = now or datetime.now(UTC)

    if result.passed:
        return Proceed(result=result)

    if verification_error is not None:
        return Halt(
            record=_record(
                HaltState.VERIFICATION_ERROR,
                result=result,
                run_id=run_id,
                work_item_id=work_item_id,
                stage_id=stage_id,
                attempts=attempts_made,
                at=at,
                summary=(
                    f"the contract could not be evaluated: {verification_error}. Nothing here "
                    f"says the work is wrong — it says we cannot tell"
                ),
            )
        )

    if budget_exceeded:
        return Halt(
            record=_record(
                HaltState.BUDGET_EXCEEDED,
                result=result,
                run_id=run_id,
                work_item_id=work_item_id,
                stage_id=stage_id,
                attempts=attempts_made,
                at=at,
                summary=(
                    "the run reached its budget cap before meeting the contract; retrying spends "
                    "the same budget to reach the same cap, so this needs a new one or nothing"
                ),
            )
        )

    if outcome in BLOCKING:
        return Halt(
            record=_record(
                HaltState.BLOCKED,
                result=result,
                run_id=run_id,
                work_item_id=work_item_id,
                stage_id=stage_id,
                attempts=attempts_made,
                at=at,
                summary=(
                    f"the attempt stopped on {outcome.value}, which a second identical attempt "
                    f"re-discovers at full price; the repair is outside this run"
                ),
            )
        )

    if attempts_made < result.contract.max_attempts:
        return Retry(
            attempt=attempts_made + 1,
            result=result,
            shortfalls=result.shortfalls,
        )

    return Halt(
        record=_record(
            HaltState.RETRIES_EXHAUSTED,
            result=result,
            run_id=run_id,
            work_item_id=work_item_id,
            stage_id=stage_id,
            attempts=attempts_made,
            at=at,
            summary=(
                f"the {result.contract.kind.value} contract was not met in "
                f"{result.contract.max_attempts} attempts"
            ),
        )
    )


def stale(
    stale_evidence: Stale,
    contract: VerificationContract,
    *,
    run_id: str,
    tree_hash: str | None,
    last_result: VerificationResult | None = None,
    work_item_id: str | None = None,
    stage_id: str | None = None,
    now: datetime | None = None,
) -> Halt:
    """The halt a merge gate raises when the tree moved under its evidence.

    Separate from ``decide`` because it happens somewhere else entirely: not in
    the verification loop but at the merge, possibly days later, after S15b's
    auto-rebase produced a commit nothing has run against. The record says so in
    those words, because "verification failed" would be wrong — nothing failed,
    and telling a person their tests broke when their base advanced sends them
    looking in the wrong place.
    """
    reason = (
        "the branch was rebased or its base advanced, so the tests that passed ran against a "
        "tree that will not be merged; re-verification against the current tree is required "
        "before this can land"
        if stale_evidence.reason is Staleness.TREE_MOVED
        else str(stale_evidence)
    )
    return Halt(
        record=_record(
            HaltState.EVIDENCE_STALE,
            result=last_result,
            contract=contract,
            run_id=run_id,
            work_item_id=work_item_id,
            stage_id=stage_id,
            attempts=last_result.attempt if last_result else 0,
            at=now or datetime.now(UTC),
            summary=reason,
            tree_hash=tree_hash,
        )
    )


def _record(
    state: HaltState,
    *,
    result: VerificationResult | None,
    run_id: str,
    work_item_id: str | None,
    stage_id: str | None,
    attempts: int,
    at: datetime,
    summary: str,
    contract: VerificationContract | None = None,
    tree_hash: str | None = None,
) -> HaltRecord:
    """Build the record, filling ``admits`` from the table.

    The single writer of ``HaltRecord.admits``, which is what makes the
    denormalisation on that field safe: it is copied from ``RESUMPTIONS`` at the
    moment the halt is created, so a stored record and the table agree by
    construction rather than by anyone remembering.
    """
    settled = contract if contract is not None else (result.contract if result else None)
    if settled is None:  # pragma: no cover - every caller supplies one of the two
        raise ValueError("a halt record needs a contract, from the result or directly")

    # The result's tree wins where there is one: it is the tree the evidence is
    # about, which is the tree a person needs to look at. The argument is for
    # `stale`, where the whole point is that the two differ.
    settled_tree = tree_hash if tree_hash is not None else (result.tree_hash if result else None)

    return HaltRecord(
        state=state,
        run_id=run_id,
        work_item_id=work_item_id,
        stage_id=stage_id,
        at=at,
        attempts=attempts,
        max_attempts=settled.max_attempts,
        contract=settled.kind,
        tree_hash=settled_tree,
        last_result=result,
        admits=RESUMPTIONS[state],
        summary=summary,
    )
