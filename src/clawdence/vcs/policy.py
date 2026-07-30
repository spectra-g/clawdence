"""What the repository will refuse, discovered before a run rather than at merge.

A run that reaches the merge and is turned away has already cost an agent step,
a container, a test suite and somebody's attention. Every rule here is knowable
from the repository's own settings before any of that is spent, and the plan asks
for exactly that: *if a repo demands signed commits and the runner can't sign,
fail at config time with a clear message, not at merge time.*

The split is deliberate. ``evaluate`` is a pure function of three inputs — the
profile, what the forge says about the branch, and what this installation can
actually do — so every rule is testable without a network, and ``GhVcs`` is the
only thing that has to know what GitHub's protection API looks like.

**Signing is a refusal, not a gap.** ``runners.worktree.commit_all`` passes
``--no-gpg-sign``, and that is not an oversight to be fixed by adding a key.
A signing key in the control plane is a credential that produces commits marked
*verified* — the strongest attestation a repository has — from the one process
every model output passes through. §1.3 says an agent's product is a proposal
that enters the normal review path; a verified signature is the review path
being told the proposal already passed. So the answer to "this repository
requires signed commits" is that this system cannot work on it, said plainly,
before anyone waits for it to try.

**Two severities, because two different people act on them.** A blocking
violation means the merge cannot succeed and the configuration has to change. An
advisory one means it will not succeed *unattended* — required reviews and
required status checks are the repository asking for a human and for its own CI,
which is a reasonable thing for a repository to ask and not something to refuse
over. Collapsing the two would either block adoption on every well-governed repo
or let a genuinely broken configuration through, and there is no third option
that does not distinguish them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from clawdence.domain import MergeMethod, RepoProfile
from clawdence.ports.errors import PermanentError

#: Whether this system can produce signed commits. A constant rather than a
#: capability flag, because the answer is a design position (see the module
#: docstring) rather than a property of a deployment. If it ever becomes true it
#: will be because a decision was recorded, and this is where it is written down.
CAN_SIGN_COMMITS = False


class Rule(StrEnum):
    DEFAULT_BRANCH = "default-branch"
    PUSH_ACCESS = "push-access"
    SIGNED_COMMITS = "signed-commits"
    MERGE_METHOD = "merge-method"
    PUSH_RESTRICTED = "push-restricted"
    REQUIRED_REVIEWS = "required-reviews"
    REQUIRED_CHECKS = "required-checks"


@dataclass(frozen=True, slots=True)
class BranchProtection:
    """What the forge enforces on one branch.

    Absent protection is this with every field at its default, which is also what
    an unprotected branch means — so a repository the token cannot read
    protection for is treated as unprotected, and the merge attempt is where that
    is discovered. Guessing the other way would refuse to work on every
    repository whose token lacks the admin scope, which is most of them.
    """

    branch: str
    required_status_checks: tuple[str, ...] = ()
    required_approving_reviews: int = 0
    require_signed_commits: bool = False

    #: Whether the branch limits *who* may push, and to whom.
    restricts_pushes: bool = False
    push_allowances: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ForgeCapabilities:
    """What this installation can do at this repository, as the forge sees it."""

    #: The login the token authenticates as. ``None`` when it could not be read,
    #: which suppresses the push-restriction rule rather than failing it: an
    #: unknown identity is not evidence of a missing permission.
    login: str | None = None

    can_push: bool = True
    default_branch: str | None = None

    #: Merge buttons the repository has enabled. Empty means unknown, and an
    #: unknown set checks nothing — the same reasoning as ``login``.
    merge_methods: frozenset[MergeMethod] = field(default_factory=frozenset)


#: Everything unknown, which checks nothing. A module-level singleton rather than
#: a call in a default argument, and the distinction is not only the linter's:
#: this value is the *meaning* of "we could not read the forge", and giving it a
#: name makes that readable at every call site that passes nothing.
UNKNOWN_FORGE: Final = ForgeCapabilities()


@dataclass(frozen=True, slots=True)
class Violation:
    """One reason this repository and this profile do not fit together."""

    rule: Rule
    blocking: bool
    message: str

    def describe(self) -> str:
        return f"{'refuses' if self.blocking else 'warns'}: {self.message}"


class PolicyRefused(PermanentError):
    """The repository's settings make this profile unusable.

    Permanent by construction: every rule that produces one is a setting a human
    changes, and no number of retries changes a branch protection rule.
    """

    def __init__(self, repo_id: str, violations: tuple[Violation, ...]) -> None:
        super().__init__(
            "repo-policy",
            f"{repo_id} cannot be worked on as configured:\n  "
            + "\n  ".join(violation.message for violation in violations),
        )
        self.violations = violations


def evaluate(
    profile: RepoProfile,
    protection: BranchProtection,
    capabilities: ForgeCapabilities = UNKNOWN_FORGE,
) -> tuple[Violation, ...]:
    """Every way this profile and this repository disagree, worst first."""
    found: list[Violation] = []

    if capabilities.default_branch and capabilities.default_branch != profile.default_branch:
        found.append(
            Violation(
                Rule.DEFAULT_BRANCH,
                blocking=True,
                message=(
                    f"the profile branches from {profile.default_branch!r} and the repository's "
                    f"default branch is {capabilities.default_branch!r}; work would be cut from "
                    f"a ref the project does not treat as its trunk"
                ),
            )
        )

    if not capabilities.can_push:
        found.append(
            Violation(
                Rule.PUSH_ACCESS,
                blocking=True,
                message=(
                    "the credential has no push access, so a branch can be created locally and "
                    "never published — every run would produce work that goes nowhere"
                ),
            )
        )

    if protection.require_signed_commits and not CAN_SIGN_COMMITS:
        found.append(
            Violation(
                Rule.SIGNED_COMMITS,
                blocking=True,
                message=(
                    f"{protection.branch!r} requires signed commits and this system commits with "
                    f"--no-gpg-sign deliberately: a signing key here would let the process that "
                    f"handles every model's output mark commits as verified. Allow unsigned "
                    f"commits from this account, or do not point it at this repository"
                ),
            )
        )

    method = profile.pull_request.merge_method
    if capabilities.merge_methods and method not in capabilities.merge_methods:
        enabled = ", ".join(sorted(value.value for value in capabilities.merge_methods)) or "none"
        found.append(
            Violation(
                Rule.MERGE_METHOD,
                blocking=True,
                message=(
                    f"the profile merges with {method.value!r} and the repository has only "
                    f"{enabled} enabled"
                ),
            )
        )

    if (
        protection.restricts_pushes
        and capabilities.login is not None
        and capabilities.login not in protection.push_allowances
    ):
        found.append(
            Violation(
                Rule.PUSH_RESTRICTED,
                blocking=True,
                message=(
                    f"{protection.branch!r} restricts who may push and {capabilities.login!r} is "
                    f"not on the list; the pull request would open and never be mergeable"
                ),
            )
        )

    if protection.required_approving_reviews > 0:
        found.append(
            Violation(
                Rule.REQUIRED_REVIEWS,
                blocking=False,
                message=(
                    f"{protection.branch!r} requires {protection.required_approving_reviews} "
                    f"approving review(s), so merges wait for a human. That is the repository "
                    f"working as intended; unattended merge is what it rules out"
                ),
            )
        )

    if protection.required_status_checks:
        found.append(
            Violation(
                Rule.REQUIRED_CHECKS,
                blocking=False,
                message=(
                    f"{protection.branch!r} requires the forge's own checks "
                    f"({', '.join(protection.required_status_checks)}); a merge waits for them "
                    f"regardless of what the verification contract concluded locally"
                ),
            )
        )

    return tuple(sorted(found, key=lambda violation: not violation.blocking))


def refuse_if_blocking(profile: RepoProfile, violations: tuple[Violation, ...]) -> None:
    """Raise ``PolicyRefused`` if anything blocking is in ``violations``.

    Separate from ``evaluate`` so that a caller reporting a configuration — the
    CLI, a startup check — can show the advisories too. A single function that
    raised would make the warnings unreachable, which is how they stop being
    written.
    """
    blocking = tuple(violation for violation in violations if violation.blocking)
    if blocking:
        raise PolicyRefused(profile.id, blocking)
