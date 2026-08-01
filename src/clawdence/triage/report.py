"""Renderings of a routing decision, a deployment and a finished run.

The through-line: **every one of these leads with the decision and then says
why.** A routing report that showed only the answer would be unfalsifiable — the
reader could see that the request went to the web repository and would have no
way to tell whether that was right. The reasons are the whole point of the
command, and they are the reason ``routing`` carries them on the decision rather
than composing a message at the edge.

Text is for a person; JSON is for the machine and for the tests, which assert on
it so that a change to a decision shows up as a diff in review.
"""

from __future__ import annotations

import json

from clawdence.domain import RepoProfile
from clawdence.triage.config import Deployment
from clawdence.triage.pipeline import Outcome, workflow_names
from clawdence.triage.routing import Routed

#: Marks the two decisions apart from the reasoning under them, and it is the
#: same glyph the probe uses for a settled fact.
_BULLET = "·"


def render_routing(routed: Routed, *, title: str | None = None, ref: str | None = None) -> str:
    """One work item's routing, decision first."""
    lines = [f"{routed.work_item_id}  {title}" if title else routed.work_item_id, ""]

    lines.append(f"  type      {routed.item_type.value}")
    lines.append(f"            {_BULLET} {routed.type_reason}")
    lines.append(f"  workflow  {routed.workflow.value or '(unrouted)'}")
    lines.append(f"            {_BULLET} {routed.workflow.reason}")
    lines.append(f"  repo      {routed.repo.value or '(unrouted)'}")
    lines.append(f"            {_BULLET} {routed.repo.reason}")

    scored = [candidate for candidate in routed.candidates if candidate.score]
    if scored:
        lines += ["", "  what matched:"]
        lines += [
            f"    {candidate.score:>3}  {candidate.repo_id}  ({', '.join(candidate.matched)})"
            for candidate in scored
        ]

    if not routed.routed:
        lines += ["", "  Nothing was started. Nothing has been written."]
    elif ref is not None:
        lines += ["", f"  next      clawdence work {ref}"]
    return "\n".join(lines)


def render_routing_json(routed: Routed) -> str:
    return json.dumps(routed.payload(), indent=2, sort_keys=True, ensure_ascii=False)


def render_deployment(deployment: Deployment) -> str:
    """What this configuration says the system may do.

    The runner line is the one worth reading twice, and it is why the tier is
    printed rather than assumed: a deployment with no ``runner:`` section runs
    every workflow up to its first runner step and then refuses, which looks like
    a bug until you have seen this line say so.
    """
    config = deployment.config
    lines = [
        str(deployment.origin),
        "",
        f"  mirrors    {config.paths.repo_store}",
        f"  worktrees  {config.paths.work_root}",
        f"  workflows  {config.paths.workflows}"
        + (f"  ({', '.join(workflow_names(deployment))})" if workflow_names(deployment) else ""),
        f"  forge      ${config.forge_token_env}"
        if config.forge_token_env
        else "  forge      (no credential configured — ssh remotes and public https only)",
        "",
    ]

    if config.runner is None:
        lines += [
            "  runner     (none configured — `runner` steps refuse, so any workflow",
            "             with one stops before the data plane)",
            "",
        ]
    else:
        lines += [
            f"  runner     {' '.join(config.runner.argv)}",
            f"             {config.runner.tier.value}"
            + (f", {config.runner.image}" if config.runner.image else ""),
            "",
        ]

    if not deployment.profiles:
        lines.append("no repositories configured — `clawdence probe --out` writes one")
        return "\n".join(lines)

    lines.append(
        f"{len(deployment.profiles)} repositor"
        + ("y:" if len(deployment.profiles) == 1 else "ies:")
    )
    lines += [
        f"  {profile.id:<24} {profile.name}  ({profile.isolation_tier.value})"
        for profile in sorted(deployment.profiles.values(), key=lambda p: p.id)
    ]
    return "\n".join(lines)


def render_repo(profile: RepoProfile) -> str:
    """One repository, with the fields routing reads called out.

    ``aliases`` and ``keywords`` are printed even when empty, because empty is the
    state that makes a request unroutable and an operator staring at "why did this
    not route" needs to see the absence rather than infer it.
    """
    lines = [
        f"{profile.id}  {profile.name}",
        f"  {profile.remote_url} @ {profile.default_branch}",
        "",
        f"  build      {profile.build_system.value}",
        f"  isolation  {profile.isolation_tier.value}",
        f"  branches   {profile.branch_prefix or '(no namespace)'}",
        f"  merge      {profile.pull_request.merge_method.value}",
        f"  parallel   {profile.max_concurrent_runs} run(s) at a time",
        "",
        "  routing signals — what a request has to say to land here:",
        f"    aliases   {', '.join(profile.routing.aliases) or '(none)'}",
        f"    keywords  {', '.join(profile.routing.keywords) or '(none)'}",
    ]
    if not profile.routing.aliases and not profile.routing.keywords:
        lines.append("    with neither, this only wins when it is the only repository configured")
    return "\n".join(lines)


def render_outcome(outcome: Outcome, *, ref: str | None = None) -> str:
    """What happened to one request, in the order a reader wants it.

    The pull request first when there is one, because that is the answer; the
    refusal first when there is not, because that is the answer instead. A
    reader who hits a failure should not have to already know that ``runs
    show`` exists, or that a run which started at all is acknowledged and
    will not be retried by running ``work`` again — both are said here,
    at the point they become true.
    """
    routed = outcome.routed
    head = (
        f"{outcome.item_id}  →  {routed.workflow.value}  →  {routed.repo.value}"
        if routed.routed
        else f"{outcome.item_id}  →  not routed"
    )
    lines = [head]

    if outcome.pull_request is not None:
        pull = outcome.pull_request
        lines.append(f"  pull request  {pull.url or f'#{pull.number}'}")
    if outcome.run_id is not None:
        lines.append(f"  run           {outcome.run_id}")
    if outcome.report is not None and outcome.report.failed_stages:
        lines.append(f"  failed        {', '.join(outcome.report.failed_stages)}")
        for stage_id in outcome.report.failed_stages:
            result = outcome.report.final.get(stage_id)
            if result is not None and result.error is not None:
                lines.append(f"                {result.error.kind}: {result.error.message}")
    if outcome.refusal is not None:
        lines += ["", f"  {outcome.refusal}"]
    elif outcome.report is not None and outcome.pull_request is None:
        lines += [
            "",
            "  No pull request: this run committed nothing. For a workflow with no",
            "  runner step that is the expected shape; for one with a runner step it",
            "  means the agent concluded there was nothing to change.",
        ]

    next_lines: list[str] = []
    if outcome.run_id is not None and not outcome.succeeded:
        next_lines = [
            f"  next          clawdence runs show {outcome.run_id}",
            "                this request is already acknowledged — a run started, so "
            "`clawdence work` will not pick it up again even after you fix the cause "
            "above; submit a new request to retry",
        ]
    elif outcome.refusal is not None and outcome.run_id is None and ref is not None:
        next_lines = [f"  next          fix the above, then `clawdence work {ref}` again"]
    if next_lines:
        lines += ["", *next_lines]
    return "\n".join(lines)
