"""Turning signals into a proposed profile.

The probe **proposes**. It writes nothing into the system, and nothing loads a
profile from disk yet; the output is JSON a human reads, edits and commits. That
is the whole of §3.5's v2.0 scope, and it is what makes the one refusal below
coherent rather than theatrical.

**The tier is decided here, except for the part that is not the probe's.** §3.5
says the valuable half of the probe is that it decides the isolation tier rather
than the user guessing it, and S8 says the socket grant belongs to a person. The
two are only in tension if you read "decides the tier" as "grants the daemon".
So: the probe decides ``container`` versus nothing else, and where the tests
need a daemon it says so, names the evidence, and stops — because
``docker_socket_acknowledged`` is the operator agreeing to hand over something
equivalent to host root, and a probe that filled it in would be defeating the
gate it was inferring for. The inference raises the question; the human answers
it. That leaves the probe's output *unable* to express the socket tier, which
is a property worth having: nothing this command emits can be committed straight
into a run with a daemon in it.

``host`` is never proposed either, for a duller reason: it is the tier with no
containment, and choosing it is a statement about the machine rather than about
the repository.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from clawdence.domain import IsolationTier, RepoProfile
from clawdence.probe.findings import Finding, FindingLog, Level
from clawdence.probe.scan import ProbeError, Tree, git_facts
from clawdence.probe.signals import conventions, layout, needs_docker, toolchain
from clawdence.probe.stacks import detect

#: ``RepoId`` is an ``Identifier``: it must start alphanumeric and hold only
#: ``[A-Za-z0-9._:-]``. A repository directory can be called anything.
_ILLEGAL_IN_ID: Final = re.compile(r"[^A-Za-z0-9._:-]+")

#: Used when a name sanitises down to nothing at all.
_FALLBACK_ID: Final = "repo"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """A profile nobody has agreed to yet, and why it looks like that."""

    profile: RepoProfile
    findings: tuple[Finding, ...]

    @property
    def actions(self) -> tuple[Finding, ...]:
        """What a human must do before this profile is usable."""
        return tuple(finding for finding in self.findings if finding.level is Level.ACTION)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.model_dump(mode="json"),
            "findings": [finding.to_dict() for finding in self.findings],
        }


def probe(path: Path, *, name: str | None = None, repo_id: str | None = None) -> ProbeResult:
    """Read a repository and propose a ``RepoProfile`` for it.

    Raises ``ProbeError`` only for a path that cannot be probed at all. Every
    other failure — no build system, no lockfile, no remote — produces a profile
    with the field left empty and a finding saying what is missing, because a
    partial proposal a human completes is more use than an exception.
    """
    root = path.expanduser()
    if not root.is_dir():
        raise ProbeError(f"{path} is not a directory")

    log = FindingLog()
    tree = Tree(root, log)

    facts = git_facts(root, log)
    resolved_name = name or _name_from(facts.remote_url) or root.resolve().name
    stack = detect(tree, log)
    members = layout(tree, log, stack)
    docker = needs_docker(tree, log, members.members)

    if docker:
        log.action(
            "these tests need a Docker daemon, and this profile does not grant one. The "
            "probe proposes `container`, which has no daemon in it, because the tier that "
            "does — `container+docker:socket` — mounts the host's socket and is equivalent "
            "to giving the agent host root. Granting it is two edits, both yours: set "
            "isolation_tier to `container+docker:socket` and docker_socket_acknowledged to "
            "true. The profile will not validate with one and not the other",
            field="isolation_tier",
        )

    profile = RepoProfile(
        id=repo_id or _id_from(resolved_name, log),
        name=resolved_name,
        remote_url=facts.remote_url or "",
        default_branch=facts.default_branch or "main",
        build_system=stack.build_system,
        toolchain=toolchain(tree, log),
        install_command=stack.install,
        build_command=stack.build,
        test_command=stack.test,
        needs_docker=docker,
        isolation_tier=IsolationTier.CONTAINER,
        test_reporter=stack.reporter,
        agents_md_path=conventions(tree, log),
    )
    return ProbeResult(profile=profile, findings=tuple(log.entries))


def _name_from(remote_url: str | None) -> str | None:
    """The repository's name as its remote spells it.

    Preferred over the directory name because a checkout can be called anything
    — ``work``, ``tmp``, ``clawdence-2`` — and the name is what a human will
    match a work item against later (S11).
    """
    if not remote_url:
        return None
    trimmed = remote_url.rstrip("/").removesuffix(".git")
    tail = re.split(r"[/:]", trimmed)[-1]
    return tail or None


def _id_from(name: str, log: FindingLog) -> str:
    """A ``RepoId`` derived from the name, or one that at least validates."""
    candidate = _ILLEGAL_IN_ID.sub("-", name).lstrip("-.:")[:128]
    if not candidate:
        log.action(
            f"{name!r} has no characters an id may contain, so the id is {_FALLBACK_ID!r}. "
            f"Set it to something you will recognise in a run listing",
            field="id",
        )
        return _FALLBACK_ID
    if candidate != name:
        log.note(
            f"id is {candidate!r}: an id may only hold letters, digits and `.:-`, and "
            f"{name!r} does not",
            field="id",
        )
    return candidate
