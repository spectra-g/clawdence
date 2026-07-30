"""Repository settings against a profile, with no network in sight."""

from __future__ import annotations

import pytest

from clawdence.domain import MergeMethod, PullRequestPolicy, RepoProfile
from clawdence.vcs import (
    BranchProtection,
    ForgeCapabilities,
    PolicyRefused,
    Rule,
    evaluate,
    refuse_if_blocking,
)


def a_profile(**overrides: object) -> RepoProfile:
    fields: dict[str, object] = {
        "id": "repo.widget",
        "name": "widget",
        "remote_url": "https://forge.invalid/acme/widget",
    }
    fields.update(overrides)
    return RepoProfile.model_validate(fields)


def rules(violations: tuple[object, ...]) -> set[Rule]:
    return {violation.rule for violation in violations}  # type: ignore[attr-defined]


def test_an_unprotected_repository_with_push_access_is_fine() -> None:
    assert evaluate(a_profile(), BranchProtection(branch="main")) == ()


def test_signed_commits_are_refused_and_the_refusal_explains_itself() -> None:
    """Not a gap to be closed by adding a key. A signing key in the control plane
    produces commits marked *verified* — the strongest attestation a repository
    has — from the one process every model's output passes through."""
    found = evaluate(a_profile(), BranchProtection(branch="main", require_signed_commits=True))
    assert rules(found) == {Rule.SIGNED_COMMITS}
    assert found[0].blocking is True
    assert "--no-gpg-sign" in found[0].message


def test_a_default_branch_the_repository_does_not_use_is_refused() -> None:
    found = evaluate(
        a_profile(default_branch="master"),
        BranchProtection(branch="master"),
        ForgeCapabilities(default_branch="main"),
    )
    assert rules(found) == {Rule.DEFAULT_BRANCH}


def test_no_push_access_is_refused() -> None:
    """Every run would produce a branch that exists locally and goes nowhere."""
    found = evaluate(
        a_profile(), BranchProtection(branch="main"), ForgeCapabilities(can_push=False)
    )
    assert rules(found) == {Rule.PUSH_ACCESS}


def test_a_merge_method_the_repository_has_disabled_is_refused() -> None:
    found = evaluate(
        a_profile(pull_request=PullRequestPolicy(merge_method=MergeMethod.REBASE)),
        BranchProtection(branch="main"),
        ForgeCapabilities(merge_methods=frozenset({MergeMethod.SQUASH})),
    )
    assert rules(found) == {Rule.MERGE_METHOD}
    assert "squash" in found[0].message


def test_an_unknown_merge_method_set_checks_nothing() -> None:
    """Empty means the repository could not be read, and refusing on a failure to
    read would refuse every repository whose token lacks a scope."""
    assert evaluate(a_profile(), BranchProtection(branch="main"), ForgeCapabilities()) == ()


def test_a_push_restriction_that_excludes_us_is_refused() -> None:
    found = evaluate(
        a_profile(),
        BranchProtection(branch="main", restricts_pushes=True, push_allowances=("someone",)),
        ForgeCapabilities(login="clawbot"),
    )
    assert rules(found) == {Rule.PUSH_RESTRICTED}


def test_a_push_restriction_that_includes_us_is_not() -> None:
    assert (
        evaluate(
            a_profile(),
            BranchProtection(branch="main", restricts_pushes=True, push_allowances=("clawbot",)),
            ForgeCapabilities(login="clawbot"),
        )
        == ()
    )


def test_an_unknown_identity_suppresses_the_push_restriction_rule() -> None:
    """An identity we could not read is not evidence of a missing permission."""
    assert (
        evaluate(
            a_profile(),
            BranchProtection(branch="main", restricts_pushes=True, push_allowances=("someone",)),
            ForgeCapabilities(login=None),
        )
        == ()
    )


def test_required_reviews_and_checks_are_advisory() -> None:
    """That is the repository working as intended. Refusing over it would block
    adoption on every well-governed project."""
    found = evaluate(
        a_profile(),
        BranchProtection(
            branch="main",
            required_approving_reviews=2,
            required_status_checks=("ci/build", "ci/test"),
        ),
    )
    assert rules(found) == {Rule.REQUIRED_REVIEWS, Rule.REQUIRED_CHECKS}
    assert not any(violation.blocking for violation in found)


def test_blocking_violations_sort_first() -> None:
    """An operator reading a list of six wants the one that stops the run at the
    top, not in the middle."""
    found = evaluate(
        a_profile(),
        BranchProtection(branch="main", require_signed_commits=True, required_approving_reviews=1),
    )
    assert [violation.blocking for violation in found] == [True, False]


def test_refuse_if_blocking_raises_on_the_blocking_ones_only() -> None:
    profile = a_profile()
    advisory = evaluate(a_profile(), BranchProtection(branch="main", required_approving_reviews=1))
    refuse_if_blocking(profile, advisory)  # does not raise

    blocking = evaluate(profile, BranchProtection(branch="main", require_signed_commits=True))
    with pytest.raises(PolicyRefused) as caught:
        refuse_if_blocking(profile, blocking)
    assert caught.value.retryable is False
    assert "repo.widget" in caught.value.message
    assert caught.value.violations == blocking


def test_evaluate_and_refuse_are_separate_so_advisories_stay_reachable() -> None:
    """A single function that raised would make the warnings unreachable, which
    is how they stop being written."""
    found = evaluate(
        a_profile(),
        BranchProtection(
            branch="main", require_signed_commits=True, required_status_checks=("ci",)
        ),
    )
    assert len(found) == 2
    with pytest.raises(PolicyRefused) as caught:
        refuse_if_blocking(a_profile(), found)
    assert len(caught.value.violations) == 1


def test_a_violation_describes_itself_for_a_human() -> None:
    found = evaluate(a_profile(), BranchProtection(branch="main", require_signed_commits=True))
    assert found[0].describe().startswith("refuses: ")
    advisory = evaluate(a_profile(), BranchProtection(branch="main", required_approving_reviews=1))
    assert advisory[0].describe().startswith("warns: ")
