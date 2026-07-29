"""The ``clawdence`` entry point.

This is the *only* supported entry point: no supported path calls internal
modules directly, which is what keeps the packaging and distribution options
open. Subcommands (``run``, ``workflow``, ``probe``, ...) arrive with the work
that owns them.

``schema`` arrives with the domain model, because the model is the thing it
projects — and routing it through the CLI rather than a loose script is what
keeps the entry-point rule true for the build as well as for users.

``runs`` arrives with the state store, and is deliberately thin: enough to find
a run, read what it did, and unstick it. The read *surface* is HQ's (S19); what
belongs here is the part an operator needs when HQ is not the thing that is
working.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from clawdence import __version__
from clawdence.domain import RunStatus, jsonschema
from clawdence.engine import (
    WorkflowLoadError,
    execute,
    load_workflow,
    render_json,
    render_text,
)
from clawdence.probe import ProbeError, probe, render_profile
from clawdence.probe import render_json as render_probe_json
from clawdence.probe import render_text as render_probe_text
from clawdence.runners import (
    DEFAULT_CACHE_RETENTION,
    DEFAULT_GRACE,
    DEFAULT_WORKTREE_RETENTION,
    WORK_ROOT,
    Cache,
    Reaper,
    Reclaimed,
)
from clawdence.store import SqliteLedger, StateStore, detect, sweep

DEFAULT_SCHEMA_DIR = Path("schemas")

#: Where runs are recorded unless told otherwise. Outside the working tree on
#: purpose: the state store is the system's record of everything it has ever
#: done, not an artefact of whichever repo happened to be the current directory.
STATE_HOME_ENV = "CLAWDENCE_HOME"
STATE_FILENAME = "state.db"


def default_state_path() -> Path:
    home = os.environ.get(STATE_HOME_ENV)
    return (Path(home) if home else Path.home() / ".clawdence") / STATE_FILENAME


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clawdence",
        description="Workflow-driven orchestration for AI coding agents.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subcommands = parser.add_subparsers(dest="command")

    run = subcommands.add_parser(
        "run",
        help="Execute a workflow file.",
    )
    run.add_argument(
        "workflow", type=Path, metavar="WORKFLOW", help="Path to a workflow YAML file."
    )
    run.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the full run report as JSON instead of a trace.",
    )
    run.add_argument(
        "--work-item",
        metavar="ID",
        help="Work item this run is for. Defaults to a generated ad-hoc id.",
    )
    _add_state_argument(run)
    run.add_argument(
        "--no-state",
        action="store_true",
        help="Execute without recording anything. Not resumable.",
    )
    run.add_argument(
        "--resume",
        metavar="RUN_ID",
        help="Continue a recorded run, re-running only what did not succeed.",
    )

    runs = subcommands.add_parser("runs", help="Inspect and recover recorded runs.")
    runs_actions = runs.add_subparsers(dest="runs_command")

    runs_list = runs_actions.add_parser("list", help="Recent runs, newest first.")
    _add_state_argument(runs_list)
    runs_list.add_argument(
        "--status",
        choices=tuple(status.value for status in RunStatus),
        help="Show only runs in this status.",
    )
    runs_list.add_argument("--limit", type=int, default=20, metavar="N")

    runs_show = runs_actions.add_parser("show", help="One run, step by step.")
    runs_show.add_argument("run_id", metavar="RUN_ID")
    _add_state_argument(runs_show)

    runs_recover = runs_actions.add_parser(
        "recover",
        help="Time out abandoned steps, halt stalled runs, replay dead letters.",
    )
    _add_state_argument(runs_recover)
    runs_recover.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what is stalled without changing anything.",
    )

    reap = subcommands.add_parser(
        "reap",
        help="Reclaim containers, worktrees and caches that no live run owns.",
    )
    _add_state_argument(reap)
    reap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be reclaimed without removing anything.",
    )
    reap.add_argument(
        "--work-root",
        type=Path,
        metavar="DIR",
        help=(
            f"Directory holding per-run worktrees (§3.3's {WORK_ROOT}). "
            f"Omitted means worktrees are not swept — there is no safe guess."
        ),
    )
    reap.add_argument(
        "--older-than",
        type=float,
        metavar="HOURS",
        help="Override every retention with one age. Applies to all three sources.",
    )
    reap.add_argument(
        "--no-caches",
        action="store_true",
        help="Leave dependency caches alone. Reclaiming one only costs a slow install.",
    )

    probe_parser = subcommands.add_parser(
        "probe",
        help="Read a repository and propose a profile for it.",
    )
    probe_parser.add_argument(
        "repo", type=Path, metavar="REPO", help="Path to a repository checkout."
    )
    probe_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the profile and the findings as JSON instead of a report.",
    )
    probe_parser.add_argument(
        "--out",
        type=Path,
        metavar="PATH",
        help="Write the profile (without the findings) here. Refuses to overwrite.",
    )
    probe_parser.add_argument(
        "--force",
        action="store_true",
        help="Allow --out to overwrite an existing file.",
    )
    probe_parser.add_argument(
        "--name", metavar="NAME", help="Override the derived repository name."
    )
    probe_parser.add_argument(
        "--id", dest="repo_id", metavar="ID", help="Override the derived repo id."
    )

    schema = subcommands.add_parser(
        "schema",
        help="Generate or verify the JSON Schema projected from the domain model.",
    )
    schema.add_argument(
        "action",
        choices=("export", "check"),
        help="export writes the schemas; check fails if what is on disk is stale.",
    )
    schema.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_SCHEMA_DIR,
        metavar="DIR",
        help=f"Directory to write to or verify (default: {DEFAULT_SCHEMA_DIR}).",
    )

    return parser


def _add_state_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state",
        type=Path,
        metavar="PATH",
        help=f"State database (default: ${STATE_HOME_ENV}/{STATE_FILENAME}).",
    )


def _schema_command(action: str, out: Path) -> int:
    if action == "export":
        changed = jsonschema.write(out)
        if changed:
            for path in changed:
                print(f"wrote {path}")
        else:
            print(f"{out} is already up to date")
        return 0

    stale = jsonschema.diff(out)
    if stale:
        print(f"{out} is out of date with the domain model:")
        for name in stale:
            print(f"  {name}")
        print("\nRun `clawdence schema export` and commit the result.")
        return 1
    print(f"{out} matches the domain model")
    return 0


def _run_command(
    path: Path,
    *,
    as_json: bool,
    work_item: str | None,
    state: Path | None,
    no_state: bool,
    resume: str | None,
) -> int:
    """Load, execute, print. Exit 1 if any stage failed the run.

    The load error goes to stderr without a traceback: a typo in a workflow file
    is an ordinary thing for a user to do, and a stack trace tells them about
    our call stack rather than about their file.
    """
    if no_state and resume is not None:
        print("--resume needs the state database that recorded the run", file=sys.stderr)
        return 2

    try:
        workflow = load_workflow(path)
    except WorkflowLoadError as exc:
        print(exc, file=sys.stderr)
        return 2

    # Ad-hoc runs have no work item. Minted rather than left blank so the record
    # still has one identity, and marked so nothing later mistakes it for
    # something a human submitted.
    work_item_id = work_item or f"wi.adhoc.{secrets.token_hex(6)}"

    if no_state:
        report = asyncio.run(
            execute(workflow, run_id=f"run.{secrets.token_hex(6)}", work_item_id=work_item_id)
        )
        print(render_json(report) if as_json else render_text(report))
        return 0 if report.succeeded else 1

    with StateStore.open(state or default_state_path()) as store:
        if resume is None:
            run_id = f"run.{secrets.token_hex(6)}"
        else:
            existing = store.get_run(resume)
            if existing is None:
                print(f"no run with id {resume!r} in this state database", file=sys.stderr)
                return 2
            if (existing.workflow, existing.workflow_version) != (workflow.name, workflow.version):
                # A run is pinned to the definition it started with, which is
                # the entire reason ``workflow_version`` is on the record. A
                # resume against a changed file would silently execute the
                # remaining stages of a different process.
                print(
                    f"run {resume} was started against "
                    f"{existing.workflow}@{existing.workflow_version}, but {path} declares "
                    f"{workflow.name}@{workflow.version}",
                    file=sys.stderr,
                )
                return 2
            run_id = resume
            work_item_id = existing.work_item_id

        report = asyncio.run(
            execute(
                workflow,
                run_id=run_id,
                work_item_id=work_item_id,
                ledger=SqliteLedger(store, run_id=run_id),
            )
        )

    print(render_json(report) if as_json else render_text(report))
    return 0 if report.succeeded else 1


def _probe_command(
    repo: Path,
    *,
    as_json: bool,
    out: Path | None,
    force: bool,
    name: str | None,
    repo_id: str | None,
) -> int:
    """Propose a profile, and say what a human still has to decide.

    The exit status is about the *proposal*, not about the repository: 1 when
    something in it still needs a person, which is the answer a script wants
    when it probes twenty repositories and needs to know which ones to look at.
    A repository that simply needs its test command written by hand is not a
    failure, but it is not done either.
    """
    try:
        result = probe(repo, name=name, repo_id=repo_id)
    except (ProbeError, ValidationError) as exc:
        print(exc, file=sys.stderr)
        return 2

    if out is not None:
        if out.exists() and not force:
            print(f"{out} exists; pass --force to overwrite it", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_profile(result) + "\n", encoding="utf-8")

    print(render_probe_json(result) if as_json else render_probe_text(result))
    if out is not None:
        print(f"\nprofile written to {out}", file=sys.stderr)
    return 1 if result.actions else 0


def _runs_list(state: Path | None, *, status: str | None, limit: int) -> int:
    with StateStore.open(state or default_state_path()) as store:
        runs = store.list_runs(status=RunStatus(status) if status else None, limit=limit)
    if not runs:
        print("no runs recorded")
        return 0
    for run in runs:
        print(
            f"{run.id}  {run.status.value:<9}  {run.workflow}@{run.workflow_version}  "
            f"{run.updated_at.isoformat(timespec='seconds')}"
        )
    return 0


def _runs_show(run_id: str, state: Path | None) -> int:
    with StateStore.open(state or default_state_path()) as store:
        run = store.get_run(run_id)
        if run is None:
            print(f"no run with id {run_id!r} in this state database", file=sys.stderr)
            return 2
        steps = store.steps_for(run_id)
        events = store.audit.read(run_id=run_id)

    print(f"run {run.id}  workflow {run.workflow}@{run.workflow_version}")
    print(f"work item {run.work_item_id}  status {run.status.value}")
    print("")
    for step in steps:
        detail = f"  {step.error.kind}: {step.error.message}" if step.error else ""
        attempt = f" (attempt {step.attempt})" if step.attempt > 1 else ""
        print(f"  {step.status.value:<10} {step.stage_id} [{step.type.value}]{attempt}{detail}")
    print("")
    print(f"{len(events)} audit records")
    return 0


def _runs_recover(state: Path | None, *, dry_run: bool) -> int:
    """The watchdog, run once, by hand.

    A long-lived supervisor is S11's; this is the same sweep an operator can run
    when a run has been sitting in ``running`` since yesterday. Dead letters are
    drained in the same pass because both answer one question — what is the
    store saying that is no longer true.
    """
    now = datetime.now(UTC)
    with StateStore.open(state or default_state_path()) as store:
        stalls = detect(store, now=now) if dry_run else sweep(store, now=now)
        parked = len(store.audit.dead_letters())
        replayed = 0
        if not dry_run and parked:
            replayed = len(store.audit.replay(at=now).replayed)

    for stall in stalls:
        print(("would recover: " if dry_run else "recovered: ") + stall.describe())
    if not stalls:
        print("nothing stalled")
    if parked:
        print(f"dead letters: {parked} parked, {replayed} replayed")
    return 0


def _reap_command(
    state: Path | None,
    *,
    dry_run: bool,
    work_root: Path | None,
    older_than: float | None,
    no_caches: bool,
) -> int:
    """Reclaim what dead runs left behind, once, by hand.

    The live set comes from the store rather than from the daemon, and that
    ordering is the safety property: a container is only removed because
    *nothing durable claims its run*, so a state database that cannot be opened
    fails the command instead of producing an empty live set — which would be an
    instruction to reap everything.

    ``--older-than`` collapses three retentions into one because that is what an
    operator with a full disk actually wants to say. It applies to the grace
    period too: overriding the retentions but not the floor under them would
    leave ``--older-than 0`` quietly meaning one hour.
    """
    override = None if older_than is None else timedelta(hours=older_than)
    with StateStore.open(state or default_state_path()) as store:
        live = tuple(run.id for run in store.list_runs(status=RunStatus.RUNNING, limit=1000))

    reaper = Reaper(
        work_root=work_root,
        cache=None if no_caches else Cache.default(),
        grace=DEFAULT_GRACE if override is None else override,
        worktree_retention=DEFAULT_WORKTREE_RETENTION if override is None else override,
        cache_retention=DEFAULT_CACHE_RETENTION if override is None else override,
    )
    reclaimed = await_sweep(reaper, live, dry_run=dry_run)

    verb = "would reclaim" if dry_run else "reclaimed"
    for name in reclaimed.containers:
        print(f"{verb} container {name}")
    for path in (*reclaimed.worktrees, *reclaimed.caches):
        print(f"{verb} {path}")
    for path in reclaimed.failed:
        print(f"could not remove {path}")
    if not reclaimed:
        print(f"nothing to reclaim ({len(live)} run(s) still live)")
    # Non-zero when something was found and could not be removed: a scheduled
    # reap that cannot free space needs to be visible to whatever scheduled it.
    return 1 if reclaimed.failed else 0


def await_sweep(reaper: Reaper, live: Sequence[str], *, dry_run: bool) -> Reclaimed:
    """``asyncio.run`` in one named place, because the reaper talks to a daemon.

    The CLI is otherwise synchronous, and scattering ``asyncio.run`` through it
    is how a second event loop eventually gets started inside the first.
    """
    return asyncio.run(reaper.sweep(live, dry_run=dry_run))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return _run_command(
            args.workflow,
            as_json=args.as_json,
            work_item=args.work_item,
            state=args.state,
            no_state=args.no_state,
            resume=args.resume,
        )

    if args.command == "runs":
        if args.runs_command == "list":
            return _runs_list(args.state, status=args.status, limit=args.limit)
        if args.runs_command == "show":
            return _runs_show(args.run_id, args.state)
        if args.runs_command == "recover":
            return _runs_recover(args.state, dry_run=args.dry_run)
        parser.parse_args(["runs", "--help"])
        return 0  # pragma: no cover - --help exits

    if args.command == "reap":
        return _reap_command(
            args.state,
            dry_run=args.dry_run,
            work_root=args.work_root,
            older_than=args.older_than,
            no_caches=args.no_caches,
        )

    if args.command == "probe":
        return _probe_command(
            args.repo,
            as_json=args.as_json,
            out=args.out,
            force=args.force,
            name=args.name,
            repo_id=args.repo_id,
        )

    if args.command == "schema":
        action: str = args.action
        out: Path = args.out
        return _schema_command(action, out)

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
