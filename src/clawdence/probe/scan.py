"""Reading a repository the probe does not trust.

A repository is not input the probe chose. It may be a fork, a pull request
branch, or — once S10b's public ingestion exists — a repo named by a stranger.
It is also, routinely, enormous. So every read here is bounded, and the bounds
are visible in the report rather than silent:

**A budget, not a walk.** The probe reads a fixed set of manifest paths plus at
most :data:`MAX_MEMBERS` workspace members, and stops after
:data:`MAX_FILES_READ` files. There is no ``rglob`` over the tree — the one
place a pattern from the repository is expanded is bounded twice, by the skip
list and by a count.

**Vendored trees are not evidence.** ``node_modules``, ``vendor``, ``target``
and their friends hold the *transitive closure* of somebody else's
dependencies. A ``testcontainers`` manifest down there belongs to a dependency
of a dependency, and treating it as this repository's declaration would flip the
isolation tier of any repo that installed before being probed. Same reason
lockfiles are never scanned for dependency names: a lockfile is the closure, and
the closure is not what the repository asked for.

**Nothing outside the root is readable.** Every path is resolved and checked
against the resolved root before it is opened, so a symlink named
``pyproject.toml`` pointing at ``~/.aws/credentials`` reads nothing. Cheap, and
the alternative is a probe that can be aimed at a file by the thing it is
probing.

**Parse the parseable, read the rest.** JSON, TOML and YAML are parsed, because
the probe needs fields out of them — a test script, a dependency table — and all
three have bounded stdlib parsers (YAML via ``safe_load``, which constructs no
objects). ``pom.xml`` and the Gradle DSL are *not* parsed. XML from an untrusted
repository is an entity-expansion bomb waiting for a parser, and the Gradle DSL
is a programming language; what the probe needs from both is whether a string
appears, and a substring search over a bounded file cannot be made to allocate.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from clawdence.probe.findings import FindingLog

#: Per file. Manifests are small; something claiming to be one and holding a
#: megabyte is not a manifest, and reading it is the probe's problem, not the
#: repository's.
MAX_FILE_BYTES: Final = 1 << 20

#: Total files opened for one probe. Reached only by a repository with far more
#: workspace members than the probe will look at anyway.
MAX_FILES_READ: Final = 400

#: Workspace members expanded from the globs a repository declares. A monorepo
#: with more than this many packages exists; probing all of them to decide one
#: boolean does not pay for itself.
MAX_MEMBERS: Final = 64

#: Never descended into. Dependency trees, build outputs, and the git database.
SKIP_DIRS: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".gradle",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
        "venv",
    }
)

#: How long a git query may take before the probe gives up on it. A repository
#: on a stalled network mount must not hang the command.
GIT_TIMEOUT_SECONDS: Final = 10.0


class ProbeError(RuntimeError):
    """The path cannot be probed at all — not a directory, or unreadable."""


class Tree:
    """Bounded, symlink-safe access to one repository's files."""

    def __init__(self, root: Path, log: FindingLog) -> None:
        self.root = root
        self._resolved_root = root.resolve()
        self._log = log
        self._reads = 0
        self._budget_reported = False

    # -- existence -------------------------------------------------------

    def has(self, rel: str) -> bool:
        path = self._safe(rel)
        return path is not None and path.is_file()

    def first(self, *rels: str) -> str | None:
        """The first of these paths that exists, in the order given.

        Order is the precedence rule, stated by the caller at the call site,
        which is where a reader of the detection table is already looking.
        """
        return next((rel for rel in rels if self.has(rel)), None)

    def is_executable(self, rel: str) -> bool:
        """git records the executable bit, so this is a property of the commit
        and not of the machine — which is what makes it worth checking."""
        path = self._safe(rel)
        return path is not None and path.is_file() and os.access(path, os.X_OK)

    # -- reading ---------------------------------------------------------

    def text(self, rel: str) -> str | None:
        path = self._safe(rel)
        if path is None or not path.is_file():
            return None
        if not self._spend(rel):
            return None
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > MAX_FILE_BYTES:
            self._log.note(
                f"{rel} is {size // 1024}KiB, over the {MAX_FILE_BYTES // 1024}KiB read cap, "
                f"and was not read — anything it would have told the probe is missing here",
                rel,
            )
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def mapping(self, rel: str) -> Mapping[str, Any] | None:
        """A JSON object, or nothing. A malformed manifest is a finding.

        Malformed is reported rather than swallowed because the alternative —
        a probe that quietly treats a broken ``package.json`` as an absent one —
        produces a profile with no test command and no explanation for it.
        """
        raw = self.text(rel)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._log.action(f"{rel} is not valid JSON ({exc.msg}); nothing was read from it", rel)
            return None
        return parsed if isinstance(parsed, dict) else None

    def toml(self, rel: str) -> Mapping[str, Any] | None:
        raw = self.text(rel)
        if raw is None:
            return None
        try:
            return tomllib.loads(raw)
        except tomllib.TOMLDecodeError as exc:
            self._log.action(f"{rel} is not valid TOML ({exc}); nothing was read from it", rel)
            return None

    def yaml(self, rel: str) -> Mapping[str, Any] | None:
        """``safe_load`` only — the full loader constructs arbitrary objects,
        and this file came out of a repository."""
        raw = self.text(rel)
        if raw is None:
            return None
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            first_line = str(exc).splitlines()[0]
            self._log.action(f"{rel} is not valid YAML ({first_line}); nothing was read", rel)
            return None
        return parsed if isinstance(parsed, dict) else None

    # -- workspace members -----------------------------------------------

    def members(self, patterns: Sequence[str]) -> tuple[str, ...]:
        """Directories matching the workspace globs a manifest declared.

        The globs come from the repository, so they are treated as hostile in
        the two ways a glob can be: a pattern that escapes the root (handled by
        resolving every hit) and a pattern that matches the world (handled by
        stopping at :data:`MAX_MEMBERS`). Negations — pnpm's ``!packages/x`` —
        are dropped rather than implemented, since over-including a member only
        means one more manifest is read.
        """
        found: list[str] = []
        for pattern in patterns:
            if pattern.startswith(("!", "/")) or ".." in pattern:
                continue
            for path in self._bounded_glob(pattern):
                rel = path.relative_to(self.root).as_posix()
                if rel not in found:
                    found.append(rel)
                if len(found) >= MAX_MEMBERS:
                    self._log.note(
                        f"stopped at {MAX_MEMBERS} workspace members; later ones were not "
                        f"read, so a dependency declared only in one of them is not seen"
                    )
                    return tuple(found)
        return tuple(found)

    def _bounded_glob(self, pattern: str) -> Iterator[Path]:
        # `Path.glob` on a pattern from the repository: bounded by the skip list
        # (so it cannot descend a dependency tree) and by the member cap above.
        try:
            # Sorted, because `glob` yields in directory order and this list
            # reaches a snapshot test and a report a human reads. Neither should
            # differ between two machines probing the same commit.
            candidates = sorted(self.root.glob(pattern))
        except (ValueError, IndexError):  # pragma: no cover - malformed pattern
            return
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            parts = candidate.relative_to(self.root).parts
            if any(part in SKIP_DIRS for part in parts):
                continue
            if self._safe(candidate.relative_to(self.root).as_posix()) is None:
                continue
            yield candidate

    # -- internals -------------------------------------------------------

    def _safe(self, rel: str) -> Path | None:
        """Resolve ``rel`` inside the root, or refuse.

        The check is on the *resolved* path, so a symlink is followed and then
        judged by where it landed. A file that resolves outside the repository
        is not this repository's evidence, whatever it is named.
        """
        candidate = self.root / rel
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - resolve on a broken mount
            return None
        if not resolved.is_relative_to(self._resolved_root):
            self._log.note(
                f"{rel} resolves outside the repository and was not read — a symlink pointing "
                f"out of a repo is not that repo's declaration",
                rel,
            )
            return None
        return candidate

    def _spend(self, rel: str) -> bool:
        if self._reads >= MAX_FILES_READ:
            if not self._budget_reported:
                self._log.note(
                    f"stopped after reading {MAX_FILES_READ} files; {rel} and anything after "
                    f"it were not read"
                )
                self._budget_reported = True
            return False
        self._reads += 1
        return True


@dataclass(frozen=True, slots=True)
class GitFacts:
    """What git knows that no file in the tree does.

    ``remote_url`` and ``default_branch`` are not derivable from the working
    tree, and both are required by the profile. Reading them from the repository
    is the difference between a proposal a human confirms and a form a human
    fills in.
    """

    remote_url: str | None
    default_branch: str | None
    is_repo: bool


def git_facts(root: Path, log: FindingLog) -> GitFacts:
    """Ask git about the checkout, tolerating every way it can be absent.

    Deliberately *not* hermetic, unlike the test fixtures: this is the user's
    own repository, and the remote lives in their ``.git/config``. What is
    suppressed is only interactivity — a probe that stops to prompt for a
    credential is a probe that hangs in CI.
    """
    if shutil.which("git") is None:
        log.action(
            "git is not on PATH, so remote_url and default_branch could not be read; "
            "fill both in by hand"
        )
        return GitFacts(remote_url=None, default_branch=None, is_repo=False)

    if _git(root, "rev-parse", "--git-dir") is None:
        log.action(
            f"{root.name} is not a git repository, so there is no remote to record; a profile "
            f"with no remote_url names nothing the VCS adapter can clone",
            field="remote_url",
        )
        return GitFacts(remote_url=None, default_branch=None, is_repo=False)

    remote = _git(root, "remote", "get-url", "origin")
    if remote is None:
        log.action(
            "this checkout has no remote named origin, so remote_url is empty. Nothing can "
            "clone a repository the profile does not name",
            field="remote_url",
        )
    else:
        log.decided(f"origin is {remote}", field="remote_url")

    # `origin/HEAD` is what the remote calls its default branch, and it only
    # exists if somebody cloned or ran `git remote set-head`. Falling back to
    # the *current* branch is a guess, so it is reported as one.
    head = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    branch = head.removeprefix("origin/") if head else None
    if branch is None:
        branch = _git(root, "branch", "--show-current")
        if branch:
            log.note(
                f"origin/HEAD is not set, so default_branch is the branch currently checked "
                f"out ({branch}) rather than the remote's default",
                field="default_branch",
            )
    return GitFacts(remote_url=remote or None, default_branch=branch or None, is_repo=True)


def _git(cwd: Path, *args: str) -> str | None:
    """Run one git query. ``None`` for every failure, which is the same answer.

    argv, never a shell. ``GIT_TERMINAL_PROMPT=0`` because a credential prompt
    inside a probe is a hang rather than an error, and the environment is
    otherwise the user's own — this is their repository and their remote.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(cwd), *args],  # noqa: S607 - PATH lookup is checked by the caller
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None
