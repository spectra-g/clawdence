"""Evidence is about one tree, and the merge gate cannot be talked out of it.

The scenario every test here is a slice of: story tests pass at commit X, a
conflict forces a rebase onto an advanced base, and the merge would land commit
Y — which nothing ever ran against. v1 had no way to notice. The fix is not a
state transition that somebody remembers to fire; it is that a result names its
tree and string equality does the rest.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clawdence.domain import ContractKind, VerificationContract, VerificationResult
from clawdence.verify import (
    StaleEvidence,
    Staleness,
    check,
    invalidated_by,
    is_fresh,
    require_fresh,
)

AT = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
BEFORE = "a" * 40
AFTER = "b" * 40


def result(*, tree_hash: str = BEFORE, passed: bool = True) -> VerificationResult:
    return VerificationResult(
        contract=VerificationContract(kind=ContractKind.TEST_AFTER),
        passed=passed,
        tree_hash=tree_hash,
        attempt=1,
        checked_at=AT,
    )


def test_evidence_is_fresh_for_the_tree_it_names() -> None:
    assert is_fresh(result(), BEFORE)
    assert check(result(), BEFORE) is None


def test_a_rebase_invalidates_evidence() -> None:
    """The whole reason the field exists.

    Nothing "marks" this stale. The rebase produced a different hash and the
    comparison does the rest, which is why it also covers the mutations nobody
    enumerated.
    """
    stale = check(result(), AFTER)

    assert stale is not None
    assert stale.reason is Staleness.TREE_MOVED
    assert stale.evidence_for == BEFORE
    assert stale.wanted == AFTER


def test_missing_evidence_is_distinguished_from_stale_evidence() -> None:
    """Different repairs: one needs a test run, the other needs a workflow that
    did not forget a step."""
    stale = check(None, AFTER)

    assert stale is not None
    assert stale.reason is Staleness.NEVER_VERIFIED


def test_evidence_that_matches_and_did_not_pass() -> None:
    """The one case where the evidence is fine and the work is not."""
    stale = check(result(passed=False), BEFORE)

    assert stale is not None
    assert stale.reason is Staleness.DID_NOT_PASS


def test_nothing_committed_is_never_satisfiable() -> None:
    """No tree means no tree for evidence to be about.

    The message says so in words rather than rendering ``None``, because a
    refusal reading "the tree is now None" sends a person looking for a bug in
    the reporting instead of at a run that committed nothing.
    """
    assert not is_fresh(result(), None)

    with pytest.raises(StaleEvidence, match=r"\(nothing committed\)"):
        require_fresh(result(), None)


def test_require_fresh_returns_the_result_when_it_is() -> None:
    assert require_fresh(result(), BEFORE).tree_hash == BEFORE


def test_require_fresh_refuses_rather_than_returning_a_flag() -> None:
    """The form a merge gate wants: there is no way to call this and carry on
    with a stale result, which a boolean invites at every site that forgets the
    ``if``."""
    with pytest.raises(StaleEvidence) as caught:
        require_fresh(result(), AFTER)

    assert caught.value.stale.reason is Staleness.TREE_MOVED


def test_the_refusal_says_which_tree_and_which_evidence() -> None:
    """A person reading this must not go looking for a broken test."""
    with pytest.raises(StaleEvidence) as caught:
        require_fresh(result(), AFTER)

    message = str(caught.value)
    assert BEFORE[:12] in message
    assert AFTER[:12] in message
    assert "never ran against the tree that would land" in message


def test_prefixes_are_never_compared() -> None:
    """``TreeHash`` is a full hash precisely because two abbreviations of
    different lengths can name the same commit."""
    assert not is_fresh(result(tree_hash="a" * 40), "a" * 39 + "c")


def test_invalidated_by_names_the_runs_that_need_re_verifying() -> None:
    """For telling a person what a base advance cost, not for causing anything
    — invalidation is not an action."""
    results = (result(tree_hash=BEFORE), result(tree_hash=AFTER))

    assert invalidated_by(results, AFTER) == (results[0],)
    assert invalidated_by(results, BEFORE) == (results[1],)
    assert len(invalidated_by(results, "c" * 40)) == 2
