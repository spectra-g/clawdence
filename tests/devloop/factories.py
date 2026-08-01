"""One real run, driven the way the executor drives one.

Every replay test needs a run whose log and whose state were written by the
*same* code path the product uses. Hand-built rows would let a reconstruction
agree with a fixture rather than with the ledger, which is the one thing these
tests are for.
"""

from __future__ import annotations

from typing import Any

from clawdence.domain import StepType, Workflow
from clawdence.engine import HandlerRegistry, RunReport, execute
from clawdence.store import SqliteLedger, StateStore
from tests.engine.factories import run as drive
from tests.engine.factories import ticking_clock
from tests.store.factories import WORK_ITEM_ID

RUN_ID = "run.devloop"


def registry(handler: Any) -> HandlerRegistry:
    return HandlerRegistry(dict.fromkeys(StepType, handler))


def go(
    store: StateStore,
    wf: Workflow,
    handler: Any,
    *,
    run_id: str = RUN_ID,
    clock: Any = None,
) -> RunReport:
    """Execute a workflow against a real store. Returns the report."""
    tick = clock or ticking_clock()
    return drive(
        execute(
            wf,
            run_id=run_id,
            work_item_id=WORK_ITEM_ID,
            registry=registry(handler),
            ledger=SqliteLedger(store, run_id=run_id, clock=tick),
            clock=tick,
        )
    )
