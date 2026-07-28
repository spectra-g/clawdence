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
from collections.abc import Sequence
from pathlib import Path

from clawdence import __version__
from clawdence.domain import jsonschema

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "schema":
        action: str = args.action
        out: Path = args.out
        return _schema_command(action, out)

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
