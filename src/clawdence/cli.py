"""The ``clawdence`` entry point.

This is the *only* supported entry point: no supported path calls internal
modules directly, which is what keeps the packaging and distribution options
open. Subcommands (``run``, ``workflow``, ``probe``, ...) arrive with the work
that owns them.

``schema`` arrives with the domain model, because the model is the thing it
projects — and routing it through the CLI rather than a loose script is what
keeps the entry-point rule true for the build as well as for users.

``runs`` arrives with the state store, and is deliberately thin: enough to find
a run, read what it did, unstick it, and (``replay``) rebuild it from the audit
log. The read *surface* is HQ's (S19); what belongs here is the part an
operator needs when HQ is not the thing that is working.

``submit`` and ``inbox`` arrive with ingestion (S10), and they are not a
convenience wrapper over one: the command line *is* an ``IngestPort`` source,
the first one, and the only one whose arrival and whose consumption are
guaranteed to be different processes. Everything Slack and GitHub will need —
deduplication that survives a restart, edits, withdrawal, conversation
threading — has to work here before there is a socket to hide it behind.

``reset``, ``replay`` and ``audit`` arrive with the dev loop (S20). They are the
commands that make every *other* step's verification quick, which is why the
plan kept recommending they be pulled forward — and they are the reason the
entry-point rule above holds under pressure: the alternative to a subcommand is
a shell script in the repository root that opens the database itself.

``repos``, ``triage`` and ``work`` arrive with S11, and they are the three
different questions a composition root creates. ``repos`` is *what is
configured* — including the ``check`` verb S15 could not write, because "fail at
configuration time" needed a file saying which repositories exist. ``triage`` is
*what would happen*, and it acts on nothing: routing is the one decision this
system makes on a request's behalf, so being able to interrogate it without
spending anything is not a convenience. ``work`` is the one that does it, and it
is the command M1's goal is written in — a request goes in at ``submit`` and a
pull request comes out here.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import JsonValue, ValidationError

from clawdence import __version__, triage
from clawdence.agent import AgentHandler, AnthropicModels, PromptRegistry
from clawdence.devloop import (
    ResetRefused,
    render_dead_letters,
    render_events,
    render_events_json,
    render_replay,
    render_replay_json,
    render_reset,
    render_run,
    render_run_json,
    replay,
    reset,
)
from clawdence.devloop import refusal as reset_refusal
from clawdence.devloop.report import effect_dict
from clawdence.domain import EventKind, RunStatus, StepType, WorkItemType, jsonschema
from clawdence.engine import (
    HandlerRegistry,
    StepHandler,
    UnimplementedHandler,
    WorkflowLoadError,
    default_registry,
    dry_run,
    execute,
    load_workflow,
    render_dry_run,
    render_dry_run_json,
    render_json,
    render_mermaid,
    render_text,
    render_tree,
)
from clawdence.ingest import DEFAULT_TYPE, NormaliseError
from clawdence.ingest import cli as ingest_cli
from clawdence.ingest import report as ingest_report
from clawdence.ports import EnvSecrets
from clawdence.ports.errors import PortError
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
    write_to,
)
from clawdence.store import (
    Admission,
    ArrivalState,
    EffectState,
    ExternalEffects,
    Intake,
    SqliteLedger,
    StateStore,
    StoreError,
    backup,
    detect,
    restore,
    sweep,
    tombstone_and_rewrite,
)
from clawdence.triage import CONFIG_FILENAME

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
    runs_show.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the run and every step as JSON instead of a trace.",
    )

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

    runs_replay = runs_actions.add_parser(
        "replay",
        help="Rebuild a run from its audit log and compare it with what is stored.",
    )
    runs_replay.add_argument("run_id", metavar="RUN_ID")
    _add_state_argument(runs_replay)
    runs_replay.add_argument(
        "--through",
        type=int,
        metavar="N",
        help=(
            "Fold only this run's first N events — what did it look like before "
            "the stage that went wrong. Skips the comparison; see `replay`."
        ),
    )
    runs_replay.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the reconstruction and the divergences as JSON.",
    )

    effects = subcommands.add_parser(
        "effects", help="Inspect and retry durable external deliveries."
    )
    effects_actions = effects.add_subparsers(dest="effects_command")

    effects_list = effects_actions.add_parser("list", help="List external effects.")
    _add_state_argument(effects_list)
    effects_list.add_argument(
        "--status",
        choices=tuple(state.value for state in EffectState),
        help="Show only effects in this delivery state.",
    )
    effects_list.add_argument("--run", dest="run_id", metavar="RUN_ID")
    effects_list.add_argument("--limit", type=int, default=20, metavar="N")
    effects_list.add_argument("--json", action="store_true", dest="as_json")

    effects_show = effects_actions.add_parser("show", help="Show one external effect.")
    effects_show.add_argument("effect_id", metavar="EFFECT_ID")
    _add_state_argument(effects_show)
    effects_show.add_argument("--json", action="store_true", dest="as_json")

    effects_retry = effects_actions.add_parser(
        "retry", help="Return one parked effect to the delivery queue."
    )
    effects_retry.add_argument("effect_id", metavar="EFFECT_ID")
    _add_state_argument(effects_retry)

    state_parser = subcommands.add_parser(
        "state", help="Back up, restore or repair the state system of record."
    )
    state_actions = state_parser.add_subparsers(dest="state_command")

    state_backup = state_actions.add_parser(
        "backup", help="Take a consistent, schema-checked online backup."
    )
    state_backup.add_argument("destination", type=Path, metavar="BACKUP")
    _add_state_argument(state_backup)

    state_restore = state_actions.add_parser(
        "restore", help="Restore a checked backup into a clean destination."
    )
    state_restore.add_argument("backup", type=Path, metavar="BACKUP")
    _add_state_argument(state_restore)

    state_redact = state_actions.add_parser(
        "redact", help="Replace one missed secret and append an audit tombstone."
    )
    state_redact.add_argument(
        "--secret-file",
        type=Path,
        required=True,
        metavar="PATH",
        help="Read the exact secret from a file, keeping it out of argv and shell history.",
    )
    state_redact.add_argument(
        "--reason", required=True, metavar="TEXT", help="Operator reason recorded in audit."
    )
    state_redact.add_argument(
        "--as", dest="operator", metavar="WHO", help="Operator identity recorded in audit."
    )
    _add_state_argument(state_redact)

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

    reset_parser = subcommands.add_parser(
        "reset",
        help="Empty the state database and remove what runs left on this machine.",
    )
    _add_state_argument(reset_parser)
    reset_parser.add_argument(
        "--work-root",
        type=Path,
        metavar="DIR",
        help=(
            f"Directory holding per-run worktrees (§3.3's {WORK_ROOT}). "
            f"Omitted means worktrees are left alone — there is no safe guess."
        ),
    )
    reset_parser.add_argument(
        "--caches",
        action="store_true",
        help="Clear dependency caches too. Costs a slow install and makes nothing cleaner.",
    )
    reset_parser.add_argument(
        "--keep-inbox",
        action="store_true",
        help="Keep submitted requests. Anything already picked up goes back in the queue.",
    )
    reset_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would go without removing anything.",
    )
    reset_parser.add_argument(
        "--force",
        action="store_true",
        help="Reset even while runs are still running, abandoning them.",
    )
    reset_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation. Required when nothing is on a terminal to ask.",
    )

    audit = subcommands.add_parser("audit", help="Read the audit log.")
    _add_state_argument(audit)
    audit.add_argument(
        "run_id",
        metavar="RUN_ID",
        nargs="?",
        help="Only records for this run. Omitted means every run — narrow with --work-item/--kind.",
    )
    audit.add_argument("--work-item", metavar="ID", help="Only records for this work item.")
    audit.add_argument(
        "--kind",
        action="append",
        default=[],
        dest="kinds",
        choices=tuple(kind.value for kind in EventKind),
        metavar="KIND",
        help="Only records of this kind. Repeatable.",
    )
    audit.add_argument(
        "--since",
        metavar="WHEN",
        help="Only records at or after this ISO-8601 instant.",
    )
    audit.add_argument("--limit", type=int, default=50, metavar="N")
    audit.add_argument(
        "--all",
        action="store_true",
        dest="oldest_first",
        help="Take the oldest N rather than the newest N.",
    )
    audit.add_argument(
        "--dead-letters",
        action="store_true",
        help="Show records that could not join the log instead of the log itself.",
    )
    audit.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the records as JSON.",
    )

    submit = subcommands.add_parser(
        "submit",
        help="Submit a request. Reads the text from --text, a file, or stdin.",
    )
    submit.add_argument(
        "--text",
        metavar="TEXT",
        help="The request itself. Omit to read it from --file, or from stdin.",
    )
    submit.add_argument(
        "--file",
        type=Path,
        metavar="PATH",
        help="Read the request from this file.",
    )
    submit.add_argument(
        "--title",
        metavar="TITLE",
        help="Title. Defaults to the first line of the request, cut to length.",
    )
    submit.add_argument(
        "--type",
        dest="item_type",
        choices=tuple(kind.value for kind in WorkItemType),
        default=DEFAULT_TYPE.value,
        help="What kind of request this is. Triage may reclassify it.",
    )
    submit.add_argument(
        "--ref",
        metavar="REF",
        help=(
            "Idempotency key for this request. Submitting the same REF twice is "
            "one request said twice. Omitted means a fresh one per invocation."
        ),
    )
    submit.add_argument(
        "--conversation",
        metavar="ID",
        help="Conversation this belongs to, so replies can be threaded onto it.",
    )
    submit.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="LABEL",
        dest="labels",
        help="Attach a label. Repeatable.",
    )
    submit.add_argument(
        "--workflow",
        metavar="NAME",
        help="Force a workflow instead of letting triage choose one.",
    )
    submit.add_argument(
        "--as",
        dest="submitter",
        metavar="WHO",
        help="Submit on behalf of this identity. Defaults to the current user.",
    )
    submit.add_argument(
        "--amend",
        action="store_true",
        help="Replace the content of an existing --ref. Fails if there is none.",
    )
    submit.add_argument(
        "--withdraw",
        metavar="REF",
        help="Take back a request. Needs no text.",
    )
    submit.add_argument(
        "--reply",
        metavar="CONVERSATION",
        help="Add a follow-up to a conversation. Never creates a work item.",
    )
    submit.add_argument(
        "--reason",
        metavar="TEXT",
        default="withdrawn from the command line",
        help="Why, for --withdraw.",
    )
    submit.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the admission as JSON instead of a line.",
    )
    _add_state_argument(submit)

    inbox = subcommands.add_parser("inbox", help="Inspect submitted requests.")
    inbox_actions = inbox.add_subparsers(dest="inbox_command")

    inbox_list = inbox_actions.add_parser("list", help="Requests, newest first.")
    _add_state_argument(inbox_list)
    # ``--status`` rather than ``--state``: ``--state`` is the database path on
    # every other subcommand, and one flag meaning two things is how somebody
    # eventually points a filter at a file.
    inbox_list.add_argument(
        "--status",
        dest="state_filter",
        choices=tuple(state.value for state in ArrivalState),
        help="Show only requests in this state.",
    )
    inbox_list.add_argument("--limit", type=int, default=20, metavar="N")

    inbox_show = inbox_actions.add_parser(
        "show", help="One request, verbatim, with its conversation."
    )
    inbox_show.add_argument("ref", metavar="REF", help="The reference it was submitted under.")
    _add_state_argument(inbox_show)

    repos = subcommands.add_parser("repos", help="Inspect the configured repositories.")
    repos_actions = repos.add_subparsers(dest="repos_command")

    repos_list = repos_actions.add_parser("list", help="What this deployment is wired to.")
    _add_config_argument(repos_list)

    repos_show = repos_actions.add_parser("show", help="One repository, with its routing signals.")
    repos_show.add_argument("repo_id", metavar="REPO_ID")
    _add_config_argument(repos_show)

    repos_check = repos_actions.add_parser(
        "check",
        help="Ask each forge whether its repository can be worked on as configured.",
    )
    _add_config_argument(repos_check)
    repos_check.add_argument(
        "repo_id",
        metavar="REPO_ID",
        nargs="?",
        help="Check only this one. Omitted means all of them.",
    )

    triage_parser = subcommands.add_parser(
        "triage",
        help="Say which workflow and repository a request would route to. Runs nothing.",
    )
    triage_parser.add_argument(
        "ref",
        metavar="REF",
        nargs="?",
        help="A submitted request. Omitted means every pending one.",
    )
    _add_config_argument(triage_parser)
    _add_state_argument(triage_parser)
    triage_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the decisions as JSON.",
    )

    work = subcommands.add_parser(
        "work",
        help="Take pending requests and run them: route, execute, open a pull request.",
    )
    work.add_argument(
        "ref",
        metavar="REF",
        nargs="?",
        help="Work this one request. Omitted means every pending one, oldest first.",
    )
    _add_config_argument(work)
    _add_state_argument(work)
    work.add_argument(
        "--limit",
        type=int,
        default=1,
        metavar="N",
        help=(
            "How many pending requests to take. Defaults to 1: each one spends "
            "money, and a queue drained by accident is an expensive surprise."
        ),
    )
    work.add_argument(
        "--dry-run",
        action="store_true",
        help="Route and report without running anything. Same as `triage`.",
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

    workflow_parser = subcommands.add_parser(
        "workflow",
        help="Check, draw and rehearse a workflow file. Runs nothing real.",
    )
    workflow_actions = workflow_parser.add_subparsers(dest="workflow_command")

    workflow_validate = workflow_actions.add_parser(
        "validate",
        help="Check one or more workflow files and say where the problems are.",
    )
    workflow_validate.add_argument(
        "files", type=Path, nargs="+", metavar="WORKFLOW", help="Workflow YAML files."
    )
    workflow_validate.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one record per file instead of a report.",
    )

    workflow_graph = workflow_actions.add_parser(
        "graph",
        help="Draw the process the file declares, without running it.",
    )
    workflow_graph.add_argument("file", type=Path, metavar="WORKFLOW")
    workflow_graph.add_argument(
        "--format",
        dest="graph_format",
        choices=("text", "mermaid"),
        default="text",
        help="text is an outline for the terminal; mermaid renders in GitHub.",
    )

    workflow_test = workflow_actions.add_parser(
        "test",
        help="Rehearse a workflow against invented results. No model, no repo, no state.",
    )
    workflow_test.add_argument("file", type=Path, metavar="WORKFLOW")
    workflow_test.add_argument(
        "--request",
        type=Path,
        metavar="PATH",
        help="JSON or YAML file standing in for the work item, read as ${request.json.…}.",
    )
    workflow_test.add_argument(
        "--request-text",
        metavar="TEXT",
        help="Shorthand for a request whose 'text' is this. Overrides --request's text.",
    )
    workflow_test.add_argument(
        "--output",
        action="append",
        default=[],
        metavar="STAGE=JSON",
        help=(
            "Override what a stage is pretended to have produced, to walk another "
            "branch. JSON, or @path to a file. Repeatable."
        ),
    )
    workflow_test.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the rehearsal, the plan and the invented values as JSON.",
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


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help=f"Deployment configuration (default: ${STATE_HOME_ENV}/{CONFIG_FILENAME}).",
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


def _state_backup(state: Path | None, destination: Path) -> int:
    try:
        with StateStore.open(state or default_state_path()) as store:
            report = backup(store, destination)
    except (OSError, StoreError, sqlite3.DatabaseError) as exc:
        print(f"backup refused: {exc}", file=sys.stderr)
        return 2
    print(
        f"backed up schema {report.schema_version} to {report.destination} ({report.bytes} bytes)"
    )
    return 0


def _state_restore(state: Path | None, backup_path: Path) -> int:
    try:
        report = restore(backup_path, state or default_state_path())
    except (OSError, StoreError, sqlite3.DatabaseError) as exc:
        print(f"restore refused: {exc}", file=sys.stderr)
        return 2
    print(f"restored schema {report.schema_version} to {report.destination} ({report.bytes} bytes)")
    return 0


def _state_redact(
    state: Path | None,
    secret_file: Path,
    *,
    reason: str,
    operator: str | None,
) -> int:
    try:
        secret = secret_file.read_text(encoding="utf-8").rstrip("\r\n")
        with StateStore.open(state or default_state_path()) as store:
            report = tombstone_and_rewrite(
                store,
                secret,
                reason=reason,
                requested_by=operator or getpass.getuser(),
            )
    except (OSError, StoreError, sqlite3.DatabaseError) as exc:
        print(f"redaction rewrite refused: {exc}", file=sys.stderr)
        return 2
    tables = ", ".join(f"{name}={count}" for name, count in report.rows.items()) or "none"
    print(
        f"rewrote {report.occurrences} occurrence(s) in {report.changed_rows} row(s); "
        f"tables: {tables}"
    )
    return 0


#: The environment variable an API key is read from. Uppercase and conventional,
#: because it is the name every provider's own documentation uses and a user who
#: has one exported already should not have to learn ours.
API_KEY_ENV = "ANTHROPIC_API_KEY"


def _registry(environ: Mapping[str, str] | None = None) -> HandlerRegistry:
    """The handlers a CLI run gets: script always, agent if a key is present.

    Wired here rather than in ``default_registry`` because the engine must not
    know what a provider is, and read from the environment rather than from a
    config file because configuration is S22's and a key in a file is a key in a
    backup. The allowlist is one name, so a workflow cannot make this resolve a
    different variable.

    **No key means the refusal stands**, and the refusal names what to wire. That
    is the whole reason this returns a registry rather than raising: a workflow
    with no agent steps runs perfectly well on a machine with no credentials, and
    ``clawdence run examples/toy.yaml`` must not start demanding one.
    """
    env = os.environ if environ is None else environ
    if not env.get(API_KEY_ENV):
        return default_registry(env)
    return default_registry(
        env,
        agent=AgentHandler(
            model=AnthropicModels(EnvSecrets(env, allowed={API_KEY_ENV}), secret_name=API_KEY_ENV),
            prompts=PromptRegistry.from_env(env),
        ),
    )


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
            execute(
                workflow,
                run_id=f"run.{secrets.token_hex(6)}",
                work_item_id=work_item_id,
                registry=_registry(),
            )
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
                registry=_registry(),
                ledger=SqliteLedger(store, run_id=run_id),
            )
        )

    print(render_json(report) if as_json else render_text(report))
    return 0 if report.succeeded else 1


def _workflow_validate(paths: Sequence[Path], *, as_json: bool) -> int:
    """Check every file named, and exit non-zero if any of them will not load.

    Every file, not up to the first failure: someone editing three workflows
    wants all three verdicts from one run, and a checker that stops early makes
    fixing them a serial process.
    """
    records: list[dict[str, JsonValue]] = []
    failed = False
    for path in paths:
        try:
            workflow = load_workflow(path)
        except WorkflowLoadError as exc:
            failed = True
            records.append(
                {
                    "file": str(path),
                    "ok": False,
                    "error": exc.message,
                    "line": exc.line,
                    "stage": exc.stage_id,
                    "hint": exc.hint,
                }
            )
            if not as_json:
                print(exc, file=sys.stderr)
            continue
        embedded: list[JsonValue] = [*sorted(workflow.sub_workflows)]
        records.append(
            {
                "file": str(path),
                "ok": True,
                "name": workflow.name,
                "version": workflow.version,
                "schema_version": workflow.schema_version,
                "stages": len(workflow.stages),
                "sub_workflows": embedded,
            }
        )
        if not as_json:
            count = len(workflow.stages)
            print(
                f"ok  {path}  {workflow.name}@{workflow.version}  "
                f"({count} stage{'' if count == 1 else 's'}, schema {workflow.schema_version})"
            )
    if as_json:
        print(json.dumps(records, indent=2, sort_keys=True, ensure_ascii=False))
    return 1 if failed else 0


def _workflow_graph(path: Path, *, graph_format: str) -> int:
    try:
        workflow = load_workflow(path)
    except WorkflowLoadError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(render_mermaid(workflow) if graph_format == "mermaid" else render_tree(workflow))
    return 0


def _workflow_test(
    path: Path,
    *,
    request: Path | None,
    request_text: str | None,
    overrides: Sequence[str],
    as_json: bool,
) -> int:
    """Rehearse the file. Exit 1 if the rehearsal did not reach the end.

    Exit 1 rather than zero-with-a-warning because this is meant to run in CI
    over a directory of workflows, and the point of a dry run is to be the thing
    that fails before a real one does.
    """
    try:
        workflow = load_workflow(path)
    except WorkflowLoadError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        seeded = _dry_run_request(request, request_text)
        outputs = _dry_run_outputs(overrides)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    report = asyncio.run(dry_run(workflow, request=seeded, outputs=outputs))
    print(render_dry_run_json(report) if as_json else render_dry_run(report))
    return 0 if report.succeeded else 1


def _dry_run_request(path: Path | None, text: str | None) -> JsonValue | None:
    """The stand-in work item: a file, a string, both, or neither."""
    seeded: JsonValue | None = None
    if path is not None:
        seeded = _read_json_document(path, "--request")
    if text is not None:
        merged = dict(seeded) if isinstance(seeded, dict) else {}
        merged["text"] = text
        seeded = merged
    return seeded


def _dry_run_outputs(overrides: Sequence[str]) -> dict[str, JsonValue]:
    outputs: dict[str, JsonValue] = {}
    for override in overrides:
        stage_id, separator, raw = override.partition("=")
        if not separator or not stage_id:
            raise ValueError(f"--output takes STAGE=JSON, not {override!r}")
        if raw.startswith("@"):
            outputs[stage_id] = _read_json_document(Path(raw[1:]), f"--output {stage_id}")
            continue
        try:
            outputs[stage_id] = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"--output {stage_id}: {exc}") from None
    return outputs


def _read_json_document(path: Path, what: str) -> JsonValue:
    """Read JSON or YAML. ``safe_load`` only — see ``engine.loader`` on why."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{what}: cannot read {path}: {exc.strerror or exc}") from None
    try:
        loaded: JsonValue = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{what}: {path} is not valid JSON or YAML: {exc}") from None
    return loaded


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


def _read_request(text: str | None, file: Path | None) -> str:
    """The request body, from whichever of the three ways it was given.

    stdin is the fallback rather than a flag because ``gh issue view … |
    clawdence submit`` is the shape this gets used in, and a pipe with no
    terminal behind it is unambiguous. A *terminal* with nothing piped into it
    is not: waiting there looks like the command has hung, so it refuses and
    says what it wanted.
    """
    if text is not None and file is not None:
        raise NormaliseError("--text and --file both give the request; pass one")
    if text is not None:
        return text
    if file is not None:
        try:
            return file.read_text(encoding="utf-8")
        except OSError as exc:
            raise NormaliseError(f"cannot read {file}: {exc}") from exc
    if sys.stdin.isatty():
        raise NormaliseError("no request given — pass --text, --file, or pipe it in")
    return sys.stdin.read()


def _submit_command(
    *,
    text: str | None,
    file: Path | None,
    title: str | None,
    item_type: str,
    ref: str | None,
    conversation: str | None,
    labels: Sequence[str],
    workflow: str | None,
    submitter: str | None,
    amend: bool,
    withdraw: str | None,
    reply: str | None,
    reason: str,
    as_json: bool,
    state: Path | None,
) -> int:
    """One arrival at the CLI ingestion adapter.

    Exit status is about *what happened to the request*, not about whether the
    command worked: 0 when the intake now holds what the caller asked it to,
    and 3 when it does but something already acted on the older version — the
    case a script re-submitting into a running pipeline has to be able to
    branch on, and the one a person needs to read twice.
    """
    now = datetime.now(UTC)
    verbs = [name for name, given in (("--withdraw", withdraw), ("--reply", reply)) if given]
    if amend:
        verbs.append("--amend")
    if len(verbs) > 1:
        print(f"{' and '.join(verbs)} ask for different things; pass one", file=sys.stderr)
        return 2

    try:
        with StateStore.open(state or default_state_path()) as store:
            intake = Intake(store)
            turn = None
            if withdraw is not None:
                admission = ingest_cli.withdraw(intake, withdraw, at=now, reason=reason)
            elif reply is not None:
                admission, turn = ingest_cli.reply(
                    intake,
                    reply,
                    body=_read_request(text, file),
                    at=now,
                    author=submitter,
                )
            else:
                admission = ingest_cli.submit(
                    intake,
                    text=_read_request(text, file),
                    at=now,
                    ref=ref,
                    title=title,
                    item_type=WorkItemType(item_type),
                    conversation_id=conversation,
                    labels=tuple(labels),
                    workflow_override=workflow,
                    submitter=ingest_cli.cli_submitter(submitter) if submitter else None,
                    amend=amend,
                )
    except (NormaliseError, StoreError, ValidationError) as exc:
        print(exc, file=sys.stderr)
        return 2

    print(
        ingest_report.render_json(admission, turn=turn)
        if as_json
        else ingest_report.render_text(admission)
    )
    return 3 if admission.requeued else 0


def _inbox_list(state: Path | None, *, arrival_state: str | None, limit: int) -> int:
    with StateStore.open(state or default_state_path()) as store:
        admissions = Intake(store).list(
            state=ArrivalState(arrival_state) if arrival_state else None, limit=limit
        )
    print(ingest_report.render_listing(admissions))
    return 0


def _inbox_show(ref: str, state: Path | None) -> int:
    with StateStore.open(state or default_state_path()) as store:
        intake = Intake(store)
        key = ingest_cli.key(ref)
        admission = intake.get(key)
        if admission is None:
            print(f"nothing has been submitted under {ref!r}", file=sys.stderr)
            return 2
        turns = intake.turns(key)
    print(ingest_report.render_detail(admission, turns))
    return 0


def _deployment(config: Path | None) -> triage.Deployment:
    """Read the configuration, or say what is missing. Raises ``ConfigError``."""
    return triage.load(config or triage.default_config_path())


def _repos_list(config: Path | None) -> int:
    try:
        deployment = _deployment(config)
    except triage.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(triage.render_deployment(deployment))
    return 0


def _repos_show(repo_id: str, config: Path | None) -> int:
    try:
        deployment = _deployment(config)
        profile = deployment.profile(repo_id)
    except triage.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(triage.render_repo(profile))
    return 0


def _repos_check(repo_id: str | None, config: Path | None) -> int:
    """The verb S15 wrote ``check_policy`` for and could not expose.

    Exit status is about whether the repositories are *usable*, not about whether
    the command worked: 1 when any of them refuses, which is the answer a
    deployment script wants before it starts accepting requests. Advisories are
    printed and do not fail — a repository asking for two approvals is working as
    intended, and refusing over it would block adoption on every well-governed
    project.
    """
    try:
        deployment = _deployment(config)
        profiles = (
            (deployment.profile(repo_id),)
            if repo_id
            else tuple(sorted(deployment.profiles.values(), key=lambda p: p.id))
        )
        secrets_provider = triage.secrets_for(deployment)
        store = triage.repo_store(deployment, secrets_provider)
        vcs = triage.vcs(deployment, store, secrets_provider)
    except triage.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    if not profiles:
        print("no repositories configured — `clawdence probe --out` writes one")
        return 0

    blocked = 0
    for profile in profiles:
        try:
            violations = asyncio.run(vcs.check_policy(profile))
        except PortError as exc:
            print(f"{profile.id}  could not be checked: {exc.message}")
            blocked += 1
            continue
        if not violations:
            print(f"{profile.id}  ok")
            continue
        print(f"{profile.id}")
        for violation in violations:
            print(f"  {violation.describe()}")
        blocked += any(violation.blocking for violation in violations)
    return 1 if blocked else 0


def _pending(intake: Intake, ref: str | None, limit: int) -> tuple[Admission, ...]:
    """The requests a triage or work command is about.

    A named reference is taken whatever state it is in — somebody asking about a
    specific request means that one, including one already acknowledged. Without
    one, only ``pending`` rows, oldest first, which is ``Intake.collect``'s order
    and the order a queue is meant to be worked in.
    """
    if ref is not None:
        admission = intake.get(ingest_cli.key(ref))
        return () if admission is None else (admission,)
    items = intake.collect(limit=limit)
    found = [intake.for_work_item(item.id) for item in items]
    return tuple(admission for admission in found if admission is not None)


def _triage_command(
    ref: str | None,
    *,
    config: Path | None,
    state: Path | None,
    as_json: bool,
) -> int:
    """Decide, print, change nothing.

    Exit 1 when something did not route, because that is the finding: a request
    nobody can place is one a person has to look at, and a script checking a
    queue before draining it needs to know.
    """
    try:
        deployment = _deployment(config)
    except triage.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    with StateStore.open(state or default_state_path()) as store:
        admissions = _pending(Intake(store), ref, limit=50)

    if not admissions:
        print(f"nothing submitted under {ref!r}" if ref else "nothing pending")
        return 2 if ref else 0

    decisions = [
        triage.route(
            admission.item,
            profiles=deployment.profiles,
            policy=deployment.config.routing,
        )
        for admission in admissions
    ]
    if as_json:
        print(json.dumps([routed.payload() for routed in decisions], indent=2, sort_keys=True))
    else:
        print(
            "\n\n".join(
                triage.render_routing(
                    routed,
                    title=admission.item.title,
                    ref=admission.item.source_ref.external_id,
                )
                for routed, admission in zip(decisions, admissions, strict=True)
            )
        )
    return 1 if any(not routed.routed for routed in decisions) else 0


def _work_command(
    ref: str | None,
    *,
    config: Path | None,
    state: Path | None,
    limit: int,
    dry_run: bool,
) -> int:
    """Submission to pull request, which is the whole of M1's goal in one verb.

    Requests are acknowledged only when a run actually started for them (see
    ``triage.acknowledge``): one that could not be routed stays in the queue,
    because taking it out would leave a person waiting on work that will never
    begin.

    The exit status is the *worst* outcome, not the last one. A command that
    worked three requests and failed on the second has failed, and a loop that
    reported the third one's success would hide it.
    """
    if dry_run:
        return _triage_command(ref, config=config, state=state, as_json=False)

    try:
        deployment = _deployment(config)
        secrets_provider = triage.secrets_for(deployment)
        repo_store = triage.repo_store(deployment, secrets_provider)
        runner = (
            triage.runner(
                deployment.config.runner,
                secrets_provider,
                sink=write_to(sys.stdout, prefix="  │ "),
                repo_store=deployment.repo_store,
            )
            if deployment.config.runner is not None
            else None
        )
    except triage.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    status = 0
    with StateStore.open(state or default_state_path()) as store:
        intake = Intake(store)
        pipeline = triage.Pipeline(
            deployment=deployment,
            store=store,
            repos=repo_store,
            worktrees=triage.worktrees(deployment, repo_store),
            vcs=triage.vcs(deployment, repo_store, secrets_provider),
            runner=runner,
            handlers=_agent_handler(),
        )

        # Drain due durable effects before fresh work. A process that died after
        # the agent committed resumes cheap, idempotent remote operations instead
        # of dispatching the agent again.
        resumed = asyncio.run(pipeline.resume_publications(ref=ref, limit=limit))
        resumed_items = {outcome.item_id for outcome in resumed}
        for outcome in resumed:
            triage.acknowledge(intake, outcome)
            admission = intake.for_work_item(outcome.item_id)
            outcome_ref = admission.item.source_ref.external_id if admission is not None else ref
            print(triage.render_outcome(outcome, ref=outcome_ref))
            if outcome.refusal is not None:
                status = max(status, 2)
            elif not outcome.succeeded:
                status = max(status, 1)

        remaining = max(0, limit - len(resumed))
        admissions = () if remaining == 0 else _pending(intake, ref, limit=remaining)
        admissions = tuple(
            admission for admission in admissions if admission.item.id not in resumed_items
        )
        if not admissions:
            if resumed:
                return status
            print(f"nothing submitted under {ref!r}" if ref else "nothing pending")
            return 2 if ref else 0

        for admission in admissions:
            # Printed before the run starts, not just in the outcome at the
            # end, because the run is the expensive part: a request that
            # routed by elimination rather than by content — the "only
            # repository configured" case — is a mistake worth seeing before
            # minutes are spent on it, not after.
            routed = triage.route(
                admission.item,
                profiles=deployment.profiles,
                policy=deployment.config.routing,
            )
            print(triage.render_routing(routed, title=admission.item.title, ref=None))
            print()

            # Minted only once routing has somewhere to send it — an unrouted
            # item opens no run (see ``Pipeline.start``), and printing "starting
            # run.xxx" right under "Nothing was started" would say both at once.
            run_id = f"run.{secrets.token_hex(6)}" if routed.routed else None
            if run_id is not None:
                # Minted before the potentially long-running call below, so
                # there is something to watch it with immediately rather than
                # only after it finishes — a runner step can run for minutes,
                # and `work` prints nothing else in that time.
                print(
                    f"{admission.item.id}  starting {run_id}\n"
                    f"  clawdence runs show {run_id}   (in another terminal, to watch progress)"
                )
            outcome = asyncio.run(pipeline.start(admission.item, run_id=run_id))
            triage.acknowledge(intake, outcome)
            print(triage.render_outcome(outcome, ref=admission.item.source_ref.external_id))
            if outcome.refusal is not None:
                status = max(status, 2)
            elif not outcome.succeeded:
                status = max(status, 1)
    return status


def _agent_handler() -> dict[StepType, StepHandler]:
    """The agent handler, if there is a key for it. Otherwise nothing.

    Same rule as ``_registry``: no key means the refusal stands and names what to
    wire. A pipeline is not a reason to start demanding credentials a workflow
    may not need — ``quick-fix`` has no agent step at all.
    """
    registry = _registry()
    handler = registry.for_type(StepType.AGENT)
    return {} if isinstance(handler, UnimplementedHandler) else {StepType.AGENT: handler}


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


def _runs_show(run_id: str, state: Path | None, *, as_json: bool = False) -> int:
    with StateStore.open(state or default_state_path()) as store:
        run = store.get_run(run_id)
        if run is None:
            print(f"no run with id {run_id!r} in this state database", file=sys.stderr)
            return 2
        steps = store.steps_for(run_id)
        events = len(store.audit.read(run_id=run_id))
        effects = ExternalEffects(store).list(run_id=run_id)

    print(
        render_run_json(run, steps, events=events, effects=effects)
        if as_json
        else render_run(run, steps, events=events, effects=effects)
    )
    return 0


def _effects_list(
    state: Path | None,
    *,
    status: str | None,
    run_id: str | None,
    limit: int,
    as_json: bool,
) -> int:
    with StateStore.open(state or default_state_path()) as store:
        effects = ExternalEffects(store).list(
            state=EffectState(status) if status else None,
            run_id=run_id,
            limit=limit,
        )
    if as_json:
        print(json.dumps([effect_dict(effect, command=False) for effect in effects], indent=2))
        return 0
    if not effects:
        print("no external effects")
        return 0
    for effect in effects:
        print(
            f"{effect.id}  {effect.state.value:<10}  {effect.kind}  {effect.run_id}  "
            f"{effect.attempts}/{effect.max_attempts} attempt(s)"
        )
    return 0


def _effects_show(effect_id: str, state: Path | None, *, as_json: bool) -> int:
    with StateStore.open(state or default_state_path()) as store:
        effect = ExternalEffects(store).get(effect_id)
    if effect is None:
        print(f"no external effect with id {effect_id!r}", file=sys.stderr)
        return 2
    payload = effect_dict(effect, command=True)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    print(f"effect {effect.id}  {effect.kind}")
    print(f"run {effect.run_id}  delivery {effect.state.value}")
    print(f"attempts {effect.attempts}/{effect.max_attempts}")
    if effect.error_kind is not None:
        print(f"error {effect.error_kind}: {effect.error_detail}")
    print("\ncommand:")
    print(json.dumps(effect.command, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _effects_retry(effect_id: str, state: Path | None) -> int:
    with StateStore.open(state or default_state_path()) as store:
        effects = ExternalEffects(store)
        if effects.get(effect_id) is None:
            print(f"no external effect with id {effect_id!r}", file=sys.stderr)
            return 2
        try:
            retried = effects.retry(effect_id)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
    print(f"{retried.id}  pending — it will be attempted by the next `clawdence work`")
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


def _reset_command(
    state: Path | None,
    *,
    work_root: Path | None,
    caches: bool,
    keep_inbox: bool,
    dry_run: bool,
    force: bool,
    yes: bool,
) -> int:
    """Back to clean, with the list shown before anything goes.

    The plan is computed with a dry sweep, printed, and only then confirmed —
    so what is being agreed to is the actual list rather than a sentence about
    it. It is deliberately computed twice: the second sweep is what removes
    things, and having it re-decide means a container that started between the
    question and the answer is not removed on the strength of a stale list.

    A non-terminal stdin refuses instead of prompting. The command is destructive
    and unattended is exactly where it should not be guessing, so a CI job or a
    pipe has to say ``--yes`` — which is a thing somebody typed once, on purpose.
    """
    now = datetime.now(UTC)
    path = state or default_state_path()
    cache = Cache.default() if caches else None

    with StateStore.open(path) as store:
        plan = asyncio.run(
            reset(
                store,
                at=now,
                work_root=work_root,
                cache=cache,
                keep_inbox=keep_inbox,
                force=True,
                dry_run=True,
            )
        )
        if plan.abandoned and not force and not dry_run:
            print(reset_refusal(plan.abandoned), file=sys.stderr)
            return 2
        if dry_run:
            print(render_reset(plan))
            return 0

        if not yes:
            print(render_reset(plan))
            if not sys.stdin.isatty():
                print(
                    "\nreset removes all of the above and cannot be undone; "
                    "pass --yes to confirm without a terminal",
                    file=sys.stderr,
                )
                return 2
            answer = input(f"\nremove all of this from {path}? [y/N] ")
            if answer.strip().lower() not in {"y", "yes"}:
                print("nothing was removed")
                return 2

        try:
            done = asyncio.run(
                reset(
                    store,
                    at=now,
                    work_root=work_root,
                    cache=cache,
                    keep_inbox=keep_inbox,
                    force=force,
                )
            )
        except ResetRefused as exc:
            print(exc, file=sys.stderr)
            return 2

    print(render_reset(done))
    return 1 if done.debris.failed else 0


def _replay_command(
    run_id: str,
    state: Path | None,
    *,
    through: int | None,
    as_json: bool,
) -> int:
    """Fold the log, diff it, and exit on whether the two agree.

    Non-zero for a divergence, because that is the finding: something changed
    state without recording it, or recorded something it did not do. A truncated
    replay exits 0 — it made no claim, and failing there would train whoever
    scripted it to ignore the status.
    """
    with StateStore.open(state or default_state_path()) as store:
        if store.get_run(run_id) is None and not store.audit.read(run_id=run_id):
            print(
                f"no run with id {run_id!r} in this state database, and nothing "
                f"in the log for it either",
                file=sys.stderr,
            )
            return 2
        result = replay(store, run_id, through=through)

    print(render_replay_json(result) if as_json else render_replay(result))
    return 1 if result.divergences else 0


def _audit_command(
    state: Path | None,
    *,
    run_id: str | None,
    work_item_id: str | None,
    kinds: Sequence[str],
    since: str | None,
    limit: int,
    oldest_first: bool,
    dead_letters: bool,
    as_json: bool,
) -> int:
    moment: datetime | None = None
    if since is not None:
        try:
            moment = datetime.fromisoformat(since)
        except ValueError:
            print(f"{since!r} is not an ISO-8601 instant", file=sys.stderr)
            return 2
        if moment.tzinfo is None:
            # Naive means "in my timezone" to a person and "in UTC" to the
            # database, and picking one silently makes the filter wrong by
            # however many hours the reader happens to be from Greenwich.
            print(
                f"{since!r} has no timezone — say +00:00, or Z, or your own offset",
                file=sys.stderr,
            )
            return 2

    with StateStore.open(state or default_state_path()) as store:
        if dead_letters:
            print(render_dead_letters(store.audit.dead_letters()))
            return 0
        events = store.audit.read(
            run_id=run_id,
            work_item_id=work_item_id,
            kinds=[EventKind(kind) for kind in kinds] if kinds else None,
            since=moment,
            limit=limit,
            tail=not oldest_first,
        )

    print(render_events_json(events) if as_json else render_events(events, tail=not oldest_first))
    return 0


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
            return _runs_show(args.run_id, args.state, as_json=args.as_json)
        if args.runs_command == "recover":
            return _runs_recover(args.state, dry_run=args.dry_run)
        if args.runs_command == "replay":
            return _replay_command(
                args.run_id,
                args.state,
                through=args.through,
                as_json=args.as_json,
            )
        parser.parse_args(["runs", "--help"])
        return 0  # pragma: no cover - --help exits

    if args.command == "effects":
        if args.effects_command == "list":
            return _effects_list(
                args.state,
                status=args.status,
                run_id=args.run_id,
                limit=args.limit,
                as_json=args.as_json,
            )
        if args.effects_command == "show":
            return _effects_show(args.effect_id, args.state, as_json=args.as_json)
        if args.effects_command == "retry":
            return _effects_retry(args.effect_id, args.state)
        parser.parse_args(["effects", "--help"])
        return 0  # pragma: no cover - --help exits

    if args.command == "state":
        if args.state_command == "backup":
            return _state_backup(args.state, args.destination)
        if args.state_command == "restore":
            return _state_restore(args.state, args.backup)
        if args.state_command == "redact":
            return _state_redact(
                args.state,
                args.secret_file,
                reason=args.reason,
                operator=args.operator,
            )
        parser.parse_args(["state", "--help"])
        return 0  # pragma: no cover - --help exits

    if args.command == "reap":
        return _reap_command(
            args.state,
            dry_run=args.dry_run,
            work_root=args.work_root,
            older_than=args.older_than,
            no_caches=args.no_caches,
        )

    if args.command == "reset":
        return _reset_command(
            args.state,
            work_root=args.work_root,
            caches=args.caches,
            keep_inbox=args.keep_inbox,
            dry_run=args.dry_run,
            force=args.force,
            yes=args.yes,
        )

    if args.command == "audit":
        return _audit_command(
            args.state,
            run_id=args.run_id,
            work_item_id=args.work_item,
            kinds=args.kinds,
            since=args.since,
            limit=args.limit,
            oldest_first=args.oldest_first,
            dead_letters=args.dead_letters,
            as_json=args.as_json,
        )

    if args.command == "submit":
        return _submit_command(
            text=args.text,
            file=args.file,
            title=args.title,
            item_type=args.item_type,
            ref=args.ref,
            conversation=args.conversation,
            labels=args.labels,
            workflow=args.workflow,
            submitter=args.submitter,
            amend=args.amend,
            withdraw=args.withdraw,
            reply=args.reply,
            reason=args.reason,
            as_json=args.as_json,
            state=args.state,
        )

    if args.command == "inbox":
        if args.inbox_command == "list":
            return _inbox_list(args.state, arrival_state=args.state_filter, limit=args.limit)
        if args.inbox_command == "show":
            return _inbox_show(args.ref, args.state)
        parser.parse_args(["inbox", "--help"])
        return 0  # pragma: no cover - --help exits

    if args.command == "repos":
        if args.repos_command == "list":
            return _repos_list(args.config)
        if args.repos_command == "show":
            return _repos_show(args.repo_id, args.config)
        if args.repos_command == "check":
            return _repos_check(args.repo_id, args.config)
        parser.parse_args(["repos", "--help"])
        return 0  # pragma: no cover - --help exits

    if args.command == "triage":
        return _triage_command(
            args.ref,
            config=args.config,
            state=args.state,
            as_json=args.as_json,
        )

    if args.command == "work":
        return _work_command(
            args.ref,
            config=args.config,
            state=args.state,
            limit=args.limit,
            dry_run=args.dry_run,
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

    if args.command == "workflow":
        if args.workflow_command == "validate":
            return _workflow_validate(args.files, as_json=args.as_json)
        if args.workflow_command == "graph":
            return _workflow_graph(args.file, graph_format=args.graph_format)
        if args.workflow_command == "test":
            return _workflow_test(
                args.file,
                request=args.request,
                request_text=args.request_text,
                overrides=args.output,
                as_json=args.as_json,
            )
        parser.parse_args(["workflow", "--help"])
        return 0  # pragma: no cover - --help exits

    if args.command == "schema":
        action: str = args.action
        out: Path = args.out
        return _schema_command(action, out)

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
