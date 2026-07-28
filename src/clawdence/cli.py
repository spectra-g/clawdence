"""The ``clawdence`` entry point.

This is the *only* supported entry point: no supported path calls internal
modules directly, which is what keeps the packaging and distribution options
open. Subcommands (``run``, ``workflow``, ``probe``, ...) arrive with the work
that owns them.

``schema`` arrives with the domain model, because the model is the thing it
projects — and routing it through the CLI rather than a loose script is what
keeps the entry-point rule true for the build as well as for users.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from collections.abc import Sequence
from pathlib import Path

from clawdence import __version__
from clawdence.domain import jsonschema
from clawdence.engine import (
    WorkflowLoadError,
    execute,
    load_workflow,
    render_json,
    render_text,
)

DEFAULT_SCHEMA_DIR = Path("schemas")


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


def _run_command(path: Path, *, as_json: bool, work_item: str | None) -> int:
    """Load, execute, print. Exit 1 if any stage failed the run.

    The load error goes to stderr without a traceback: a typo in a workflow file
    is an ordinary thing for a user to do, and a stack trace tells them about
    our call stack rather than about their file.
    """
    try:
        workflow = load_workflow(path)
    except WorkflowLoadError as exc:
        print(exc, file=sys.stderr)
        return 2

    run_id = f"run.{secrets.token_hex(6)}"
    report = asyncio.run(
        execute(
            workflow,
            run_id=run_id,
            # Ad-hoc runs have no work item. Minted rather than left blank so
            # the record still has one identity, and marked so nothing later
            # mistakes it for something a human submitted.
            work_item_id=work_item or f"wi.adhoc.{secrets.token_hex(6)}",
        )
    )

    print(render_json(report) if as_json else render_text(report))
    return 0 if report.succeeded else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return _run_command(args.workflow, as_json=args.as_json, work_item=args.work_item)

    if args.command == "schema":
        action: str = args.action
        out: Path = args.out
        return _schema_command(action, out)

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
