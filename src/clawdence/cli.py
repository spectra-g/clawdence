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
from datetime import UTC, datetime
from pathlib import Path

from clawdence import __version__
from clawdence.domain import RunStatus, jsonschema
from clawdence.engine import (
    WorkflowLoadError,
    execute,
    load_workflow,
    render_json,
    render_text,
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

    if args.command == "schema":
        action: str = args.action
        out: Path = args.out
        return _schema_command(action, out)

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
