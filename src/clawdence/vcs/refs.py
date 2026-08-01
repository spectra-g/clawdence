"""Turning a work item into a branch name, safely and the same way every time.

Two properties, and they pull in opposite directions.

**Deterministic.** The branch is the identity of the work: ``open_pull_request``
is idempotent on the branch (``ports.vcs``), a resumed run has to find the branch
its previous incarnation pushed rather than open a second pull request beside it,
and the reaper has to be able to tell whose branch is whose. So the name is a
pure function of the work item id — not of the title, not of a counter, and not
of the time. Anything derived from the title alone would rename the branch when
somebody edits the issue, which turns one piece of work into two.

**Derived from untrusted text.** The title comes from an issue, a Slack message
or a CLI argument. A ref name is a *path* under ``.git/refs/heads`` and an
element of an argv, and both have opinions: ``git branch --delete`` is what a
title beginning with ``--delete`` becomes if it is passed positionally, ``..``
and ``@{`` are refspec syntax, and a component beginning with ``.`` is a hidden
file that git refuses to create.

The resolution is to not sanitise — to *build*. ``slugify`` keeps the characters
it wants and drops everything else, so the output alphabet is ``[a-z0-9-]`` and
the entire list of things git and getopt object to is unreachable by
construction. That is the same decision ``ScriptStage.command`` makes by being
argv rather than a shell string: remove the grammar, and nothing has to be
escaped.

``check_ref_name`` exists anyway, and is applied to the finished name including
the operator-supplied prefix. A prefix is configuration rather than issue text,
so it is trusted differently — but "trusted differently" is not "unchecked", and
the failure it catches (``branch_prefix: "-x"``) is one nobody would find by
reading the profile.
"""

from __future__ import annotations

import re
from typing import Final

from clawdence.domain.repo import DEFAULT_BRANCH_PREFIX

#: How much of the title survives. A ref is a filename, and while git itself has
#: no limit, the components of one live under ``.git/refs/heads/`` where 255
#: bytes is the usual ceiling — and a branch nobody can read in a ``git branch``
#: listing is not more useful for being complete.
SLUG_MAX: Final = 48

#: The whole name, prefix included. Well under any filesystem limit, and short
#: enough that the forge's own UI does not elide it.
NAME_MAX: Final = 120

#: The default namespace, re-exported from the domain so there is one string
#: rather than two that agree today. ``RepoProfile.branch_prefix`` is the value
#: that actually reaches a run; this is what a caller with no profile in hand
#: gets, which in practice is a test and the CLI.
DEFAULT_PREFIX: Final = DEFAULT_BRANCH_PREFIX

_KEEP = re.compile(r"[^a-z0-9]+")

#: What a finished name is allowed to look like. Slash-separated components of
#: ``[a-z0-9._-]``, each starting and ending alphanumeric. Narrower than git's
#: own rules on purpose: everything git merely discourages is also refused here,
#: because the cost of being strict is a branch called something slightly duller
#: and the cost of being permissive is a name that means something to a shell.
_VALID = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)*$")


class InvalidRefError(ValueError):
    """A ref name git would refuse, or one this project refuses first."""


def slugify(text: str, *, limit: int = SLUG_MAX) -> str:
    """Lowercase ``[a-z0-9-]``, hyphen-separated, truncated on a boundary.

    Everything outside the alphabet becomes a separator and runs collapse, so
    ``"Fix: the parser (v2) — again!"`` is ``fix-the-parser-v2-again``. Non-ASCII
    disappears rather than being transliterated: a transliteration table is a
    large amount of code whose failure mode is a branch name that is wrong in a
    language nobody on the team reads, and the work item id is carrying the
    identity anyway.

    The truncation trims back to a hyphen where there is one within reach, so a
    cut name ends at a word rather than mid-syllable. Cosmetic, and cheap.
    """
    slug = _KEEP.sub("-", text.lower()).strip("-")
    if len(slug) <= limit:
        return slug
    cut = slug[:limit]
    boundary = cut.rfind("-")
    return (cut[:boundary] if boundary > limit // 2 else cut).strip("-")


def branch_for(work_item_id: str, title: str | None = None, *, prefix: str = DEFAULT_PREFIX) -> str:
    """The branch this work item's changes go on.

    ``<prefix><work-item-id>`` when there is no usable title, and
    ``<prefix><work-item-id>-<title-slug>`` when there is. The id comes first
    because it is the part that is stable, so a truncation can only ever cost the
    decorative half, and because sorting a branch listing then groups a work
    item's branches together.

    Raises ``InvalidRefError`` when the id slugifies to nothing. That is not a
    defensive check for its own sake: an id of ``"..."`` is expressible as an
    ``Identifier`` only by way of something already having gone wrong upstream,
    and inventing a name at that point would attach real work to a branch nobody
    can trace back.
    """
    identity = slugify(work_item_id, limit=SLUG_MAX)
    if not identity:
        raise InvalidRefError(
            f"work item id {work_item_id!r} has no characters a branch name can be built from"
        )

    described = slugify(title or "", limit=SLUG_MAX)
    name = f"{prefix}{identity}-{described}" if described else f"{prefix}{identity}"
    return check_ref_name(name[:NAME_MAX].rstrip("-./"))


def check_ref_name(name: str) -> str:
    """Return ``name``, or raise ``InvalidRefError`` saying what is wrong with it.

    The message names the rule rather than restating the input, because the
    caller is an operator reading a config error and the input is already in
    front of them. ``.lock`` and ``@{`` are called out by name: both are legal
    under the character rules above and both are refused by git for reasons that
    are not guessable from the error it prints.
    """
    if not name:
        raise InvalidRefError("a branch name cannot be empty")
    if len(name) > NAME_MAX:
        raise InvalidRefError(f"branch name is {len(name)} characters; the limit is {NAME_MAX}")
    if not _VALID.match(name):
        raise InvalidRefError(
            f"{name!r} is not a usable branch name: components must be lowercase "
            f"[a-z0-9._-], separated by single slashes, each beginning and ending "
            f"with a letter or a digit"
        )
    if "@{" in name:
        raise InvalidRefError(f"{name!r} contains '@{{', which git reads as a reflog selector")
    if any(component.endswith(".lock") for component in name.split("/")):
        raise InvalidRefError(f"{name!r} ends a component with '.lock', which git reserves")
    return name


def check_prefix(prefix: str) -> str:
    """Validate a configured branch namespace. Empty is allowed; bare is not.

    An empty prefix means "put branches at the top level", which is a legitimate
    if unfriendly choice. A non-empty one must end in ``/`` — without it,
    ``clawdence`` and a work item id concatenate into ``clawdencewi-1``, which
    reads as a typo and is impossible to match with a pattern.
    """
    if not prefix:
        return prefix
    if not prefix.endswith("/"):
        raise InvalidRefError(f"branch prefix {prefix!r} must end with '/' so it names a namespace")
    check_ref_name(f"{prefix}x")
    return prefix
