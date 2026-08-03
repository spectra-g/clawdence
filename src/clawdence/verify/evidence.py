"""Evidence is about a tree, and about no other tree.

This is the smallest module in the package and the one the others exist to
protect. The property: a ``VerificationResult`` is evidence for exactly the
commit named in its ``tree_hash``, and asking whether it is evidence for a
different commit has one answer.

**The failure it makes structurally impossible.** Story tests pass at commit X.
Two stories touch the same file, so the merge conflicts, and S15b rebases onto
an advanced base — producing commit Y. Nothing ran against Y. Merging on the
strength of the run against X ships a tree that was never verified, and does it
while every dashboard reads green, because a result that passed is still sitting
there and nothing about it says which tree it was about. That is not a rare
race; with concurrency it is the normal path for stories 2..N.

The tempting fix is a state transition: notice the rebase, mark the story
"needs re-verification". v1 would have written it that way, and it fails the
same way v1's twelve halt sites failed — it works for the mutations somebody
remembered (rebase) and not for the ones they did not (force-push, an amended
commit, a base that advanced under a still-open PR, a squash that rewrote the
tree). Binding the evidence to the hash inverts that: **every** mutation
invalidates, including the ones nobody enumerated, because the check is
"does this hash match" rather than "did one of these events happen".

So there is no ``mark_stale``. Nothing has to remember to call anything. The
merge gate asks ``require_fresh(result, head)`` and the answer falls out of
string equality — which is the entire trick, and the reason this module is
forty lines rather than a state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from clawdence.domain import VerificationResult


class Staleness(StrEnum):
    """Why a result is not evidence for the tree in hand.

    Three values rather than a boolean, because they are shown to different
    people. ``TREE_MOVED`` is routine under concurrency and is repaired by
    re-running the tests. ``NEVER_VERIFIED`` means nothing ever produced
    evidence, which is a workflow that forgot a step. ``DID_NOT_PASS`` is a
    result that exists, matches the tree, and says the work is not done — the
    one case where the *evidence* is fine and the *work* is not.
    """

    NEVER_VERIFIED = "never-verified"
    TREE_MOVED = "tree-moved"
    DID_NOT_PASS = "did-not-pass"  # noqa: S105 - a staleness reason, not a credential


@dataclass(frozen=True, slots=True)
class Stale:
    """A refusal, with enough in it to say why out loud."""

    reason: Staleness
    #: The tree the caller wants evidence about.
    wanted: str | None
    #: The tree the evidence is actually about. ``None`` when there is none.
    evidence_for: str | None = None

    def __str__(self) -> str:
        if self.reason is Staleness.NEVER_VERIFIED:
            return f"no verification evidence exists for {_short(self.wanted)}"
        if self.reason is Staleness.TREE_MOVED:
            return (
                f"evidence was produced against {_short(self.evidence_for)} and the tree is now "
                f"{_short(self.wanted)}; the tests never ran against the tree that would land"
            )
        return f"verification of {_short(self.evidence_for)} did not pass"


class StaleEvidence(Exception):
    """Raised by ``require_fresh``. Carries the ``Stale`` that explains it."""

    def __init__(self, stale: Stale) -> None:
        super().__init__(str(stale))
        self.stale = stale


def check(result: VerificationResult | None, tree_hash: str | None) -> Stale | None:
    """``None`` when ``result`` is passing evidence for ``tree_hash``.

    A ``None`` tree is never satisfiable: it means nothing was committed, so
    there is no tree for evidence to be about and no honest way to say the
    evidence covers it.
    """
    if result is None:
        return Stale(reason=Staleness.NEVER_VERIFIED, wanted=tree_hash)
    if tree_hash is None or result.tree_hash != tree_hash:
        return Stale(
            reason=Staleness.TREE_MOVED,
            wanted=tree_hash,
            evidence_for=result.tree_hash,
        )
    if not result.passed:
        return Stale(
            reason=Staleness.DID_NOT_PASS,
            wanted=tree_hash,
            evidence_for=result.tree_hash,
        )
    return None


def is_fresh(result: VerificationResult | None, tree_hash: str | None) -> bool:
    """Whether the tree may be merged on the strength of this result."""
    return check(result, tree_hash) is None


def require_fresh(result: VerificationResult | None, tree_hash: str | None) -> VerificationResult:
    """The result, or ``StaleEvidence``.

    The form a merge gate wants: there is no way to call this and carry on with
    a stale result, which a boolean check invites at every call site that
    forgets the ``if``.
    """
    stale = check(result, tree_hash)
    if stale is not None:
        raise StaleEvidence(stale)
    if result is None:  # pragma: no cover - `check` returns Stale for a missing result
        # Unreachable, and written out rather than asserted. `python -O` strips
        # an assert, and the one thing this function must never do under any
        # interpreter flag is return something a merge gate reads as evidence.
        raise StaleEvidence(Stale(reason=Staleness.NEVER_VERIFIED, wanted=tree_hash))
    return result


def invalidated_by(
    results: tuple[VerificationResult, ...], tree_hash: str | None
) -> tuple[VerificationResult, ...]:
    """Which of these results the current tree has invalidated.

    For reporting a rebase honestly: after the base advances, this names the
    evidence that used to justify a merge and no longer does. Nothing calls it
    to *cause* invalidation — invalidation is not an action — it is for telling
    a person which runs need re-verifying.
    """
    return tuple(result for result in results if result.tree_hash != tree_hash)


def _short(tree_hash: str | None) -> str:
    """Abbreviate for a message only.

    Never for comparison: ``TreeHash`` is a full hash precisely because two
    abbreviations of different lengths can name the same commit, and comparing
    prefixes is how "the tree did not move" becomes true of a tree that moved.
    """
    if tree_hash is None:
        return "(nothing committed)"
    return tree_hash[:12]
