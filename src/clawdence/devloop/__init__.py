"""The dev loop — reset, replay, and the two views onto what happened.

Small step, high leverage, and the plan has been saying so since S4: without it,
every other step's manual verification is slow. Four things, and each answers a
question the build had been answering by hand:

``reset``
    Get back to a clean environment in one command. The interesting half is what
    it refuses and what it *keeps* — see ``reset``.

``replay``
    Fold a run's audit log and diff it against the stored state. Not a restore
    path, deliberately (ADR-0005), and what it actually catches is a writer that
    changes state without recording it — see ``replay``.

``audit``
    Read the log with the filters a person debugging actually has: this run,
    this work item, this kind, since this instant, and the last N rather than
    the first N.

``runs show``
    The run-inspection view, with durations and error messages rather than a
    count of records.

Nothing here is on a hot path and nothing here is imported by the engine, the
runner or the store. That is deliberate: a dev tool that the system depends on
is no longer a dev tool, and this package is free to grow a viewer for whatever
the next step adds without anything having to care.
"""

from __future__ import annotations

from clawdence.devloop.errors import DevLoopError, ResetRefused
from clawdence.devloop.replay import (
    MODELLED,
    UNOBSERVABLE,
    Divergence,
    Replay,
    RunProjection,
    StepProjection,
    compare,
    fold,
    replay,
)
from clawdence.devloop.report import (
    render_dead_letters,
    render_events,
    render_events_json,
    render_replay,
    render_replay_json,
    render_reset,
    render_run,
    render_run_json,
)
from clawdence.devloop.reset import (
    INBOX_TABLES,
    TABLES,
    Reset,
    live_runs,
    refusal,
    reset,
)

__all__ = [
    "INBOX_TABLES",
    "MODELLED",
    "TABLES",
    "UNOBSERVABLE",
    "DevLoopError",
    "Divergence",
    "Replay",
    "Reset",
    "ResetRefused",
    "RunProjection",
    "StepProjection",
    "compare",
    "fold",
    "live_runs",
    "refusal",
    "render_dead_letters",
    "render_events",
    "render_events_json",
    "render_replay",
    "render_replay_json",
    "render_reset",
    "render_run",
    "render_run_json",
    "replay",
    "reset",
]
