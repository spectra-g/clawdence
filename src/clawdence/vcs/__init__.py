"""Version control: the branch work happens on, and the pull request it becomes.

``ports.vcs`` is the *interface* — what a branch is, what a pull request is, and
the one property the whole thing exists to protect, which is that a merge states
what was verified. This package is everything under it: the git plumbing, the
local mirrors, the worktree lifecycle, the diff audit, the policy check, and one
real adapter.

The layering is one-directional, as in ``domain``, ``engine``, ``ports``,
``runners`` and ``agent``::

    git
     ├─ refs · hygiene
     └─ store
         └─ worktrees
         └─ gh ─ policy

``git`` sits at the bottom and is shared with the runner: ``runners.worktree``
imports its invocation rather than keeping a second copy, because the hardening
in it (a repository can make ``git status`` execute a program) is a security
control, and a security control with two implementations has one that is out of
date. Four of the six modules are pure functions over data — ``refs`` never sees
a repository, ``hygiene`` never sees a forge, ``policy`` never sees a network —
which is what makes the interesting rules testable without either.

**What S15 could not do before and can now.** ``runners.Dispatch`` has always
taken a repository, a worktree path, a branch and a base commit as *data*,
because S6 declined to invent them. ``WorktreeManager.acquire`` is where they
come from. The one thing still missing between here and a runner step that runs
unattended is choosing *which* repository a work item belongs to, which is S11's.

``git``, ``head`` and ``exclude`` are deliberately *not* re-exported here even
though everything else is. A package attribute named ``git`` would shadow the
submodule of the same name, so ``from clawdence.vcs import git`` would hand back
a function to anyone expecting the module — including ``clawdence.vcs.git`` in a
traceback, which would then be pointing at the wrong thing. Import the plumbing
from where it lives.

**Where the boundaries are.** Nothing here decides whether to merge — that is a
verification contract's (S13) and a human's (S17). Nothing here rebases or
aggregates an epic (S15b). ``hygiene`` reports and does not refuse, ``policy``
evaluates and only ``refuse_if_blocking`` raises, and ``WorktreeManager.release``
deletes a branch only when it can prove nothing was committed on it. The pattern
is the same in all three: this package establishes facts, and the caller acts on
them.
"""

from __future__ import annotations

from clawdence.vcs.gh import (
    INHERITED_ENV,
    GhUnavailableError,
    GhVcs,
    read_template,
    render_body,
    repo_slug,
)
from clawdence.vcs.git import (
    BASE_ENV,
    DEFAULT_IDENTITY,
    HARDENING,
    GitError,
    GitIdentity,
    authenticated,
    origin_of,
)
from clawdence.vcs.hygiene import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_VENDORED,
    Finding,
    Problem,
    audit,
)
from clawdence.vcs.policy import (
    CAN_SIGN_COMMITS,
    UNKNOWN_FORGE,
    BranchProtection,
    ForgeCapabilities,
    PolicyRefused,
    Rule,
    Violation,
    evaluate,
    refuse_if_blocking,
)
from clawdence.vcs.refs import (
    DEFAULT_PREFIX,
    NAME_MAX,
    SLUG_MAX,
    InvalidRefError,
    branch_for,
    check_prefix,
    check_ref_name,
    slugify,
)
from clawdence.vcs.store import (
    DEFAULT_LOCK_TIMEOUT,
    DEFAULT_TOKEN_NAME,
    LockTimeout,
    RepoStore,
    mirror_name,
)
from clawdence.vcs.worktrees import (
    DEFAULT_MIN_FREE_MB,
    NoSpaceError,
    Worktree,
    WorktreeManager,
)

__all__ = [
    "BASE_ENV",
    "CAN_SIGN_COMMITS",
    "DEFAULT_IDENTITY",
    "DEFAULT_LOCK_TIMEOUT",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MIN_FREE_MB",
    "DEFAULT_PREFIX",
    "DEFAULT_TOKEN_NAME",
    "DEFAULT_VENDORED",
    "HARDENING",
    "INHERITED_ENV",
    "NAME_MAX",
    "SLUG_MAX",
    "UNKNOWN_FORGE",
    "BranchProtection",
    "Finding",
    "ForgeCapabilities",
    "GhUnavailableError",
    "GhVcs",
    "GitError",
    "GitIdentity",
    "InvalidRefError",
    "LockTimeout",
    "NoSpaceError",
    "PolicyRefused",
    "Problem",
    "RepoStore",
    "Rule",
    "Violation",
    "Worktree",
    "WorktreeManager",
    "audit",
    "authenticated",
    "branch_for",
    "check_prefix",
    "check_ref_name",
    "evaluate",
    "mirror_name",
    "origin_of",
    "read_template",
    "refuse_if_blocking",
    "render_body",
    "repo_slug",
    "slugify",
]
