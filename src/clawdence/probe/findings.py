"""What the probe says about what it did.

A proposed profile on its own is not reviewable. ``build_system: maven`` is a
claim, and the only question a reviewer has is *how do you know* — which file
said so, and what the probe looked at and rejected. So every field the probe
sets carries the evidence that set it, and every field it declines to set says
what was missing.

Three levels, and the distinction is about **who has to do something**:

``DECIDED``
    The probe set a field and can name the file that made it. Read it, or don't.

``ACTION``
    The profile is incomplete or unsafe until a human edits it. Two kinds:
    something the repository does not declare (no test script, no git remote),
    and something the probe is *forbidden* to decide for you — the docker socket
    grant is the whole example, and it is the reason this level exists rather
    than a warning printed somewhere.

``NOTE``
    A signal the probe saw and deliberately did not act on. These matter more
    than they look: "there is a Dockerfile, and it is not why ``needs_docker``
    is false" is the difference between a probe that is wrong and a probe whose
    reasoning you can check.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Level(StrEnum):
    DECIDED = "decided"
    ACTION = "action"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing the probe concluded, with the file that made it conclude it."""

    level: Level
    message: str
    #: The profile field this is about, when it is about one. Dotted for nested
    #: fields, matching the JSON the reviewer is looking at.
    profile_field: str | None = None
    #: Repo-relative paths, never absolute: a report that leaks the probing
    #: machine's directory layout is one nobody can paste into an issue.
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "message": self.message,
            "field": self.profile_field,
            "evidence": list(self.evidence),
        }


class FindingLog:
    """The one mutable thing in the probe.

    Passed down rather than returned up, because the place that knows *why* a
    file was skipped is the reader that skipped it, three levels below the
    function assembling the profile. A refusal plumbed back through three return
    types is a refusal that eventually gets dropped to keep a signature tidy.
    """

    def __init__(self) -> None:
        self.entries: list[Finding] = []

    def decided(self, message: str, *evidence: str, field: str | None = None) -> None:
        self._add(Level.DECIDED, message, evidence, field)

    def action(self, message: str, *evidence: str, field: str | None = None) -> None:
        self._add(Level.ACTION, message, evidence, field)

    def note(self, message: str, *evidence: str, field: str | None = None) -> None:
        self._add(Level.NOTE, message, evidence, field)

    def at(self, level: Level) -> tuple[Finding, ...]:
        return tuple(entry for entry in self.entries if entry.level is level)

    def _add(
        self,
        level: Level,
        message: str,
        evidence: tuple[str, ...],
        field: str | None,
    ) -> None:
        self.entries.append(
            Finding(level=level, message=message, profile_field=field, evidence=evidence)
        )
