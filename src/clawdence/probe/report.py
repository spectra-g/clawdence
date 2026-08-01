"""Two renderings of a proposal, for the two readers.

The text form is for the person deciding whether to commit this. It leads with
what a human must do, because a report whose first screen is thirty lines of
things that went fine is a report where the one line asking for a decision gets
scrolled past — and for this command that line is usually the Docker one.

The JSON form is the machine's, and it is what the snapshot tests assert on: the
profile *and* the reasoning, so a change to either shows up as a diff in review
rather than as behaviour nobody attributed to a commit.
"""

from __future__ import annotations

import json

from clawdence.probe.assemble import ProbeResult
from clawdence.probe.findings import Finding, Level

_MARKS = {Level.ACTION: "!", Level.DECIDED: "·", Level.NOTE: "-"}


def render_text(result: ProbeResult) -> str:
    profile = result.profile
    lines = [
        f"{profile.name}  ({profile.build_system.value}, {profile.isolation_tier.value})",
        f"  {profile.remote_url or '(no remote)'} @ {profile.default_branch}",
        "",
        f"  install  {_argv(profile.install_command)}",
        f"  build    {_argv(profile.build_command)}",
        f"  test     {_argv(profile.test_command)}",
        "",
    ]

    actions = result.actions
    if actions:
        lines.append(f"{len(actions)} thing(s) need you:")
        lines.extend(_finding(finding) for finding in actions)
        lines.append("")

    rest = [finding for finding in result.findings if finding.level is not Level.ACTION]
    if rest:
        lines.append("what the probe read:")
        lines.extend(_finding(finding) for finding in rest)
        lines.append("")

    lines.append("This profile is a proposal. Nothing has been written or applied.")
    return "\n".join(lines)


def render_json(result: ProbeResult) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)


def render_profile(result: ProbeResult) -> str:
    """The profile alone — the part that gets committed."""
    return json.dumps(result.profile.model_dump(mode="json"), indent=2, sort_keys=True)


def _argv(command: tuple[str, ...]) -> str:
    return " ".join(command) if command else "(none proposed)"


def _finding(finding: Finding) -> str:
    where = f"  [{', '.join(finding.evidence)}]" if finding.evidence else ""
    field = f"{finding.profile_field}: " if finding.profile_field else ""
    return _wrap(f"  {_MARKS[finding.level]} {field}{finding.message}{where}")


def _wrap(text: str, width: int = 96) -> str:
    """Hand-rolled and deliberate: ``textwrap`` would also reflow the evidence
    paths, and a path broken across two lines is one nobody can copy."""
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = "    " + word
        else:
            current = f"{current} {word}" if current else word
    lines.append(current)
    return "\n".join(lines)
