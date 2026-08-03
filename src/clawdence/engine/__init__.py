"""The workflow engine — load a process, then execute it.

Written in Python rather than wrapping Lobster ([ADR-0003](../../../docs/adr)),
so one runtime owns control flow end to end. The layering mirrors the domain
package's: each module depends only on the ones above it, so no two parts of
the engine have to be understood together::

    errors ─ refs
      └─ conditions ─┐
      └─ interpolation ─ handlers ─┐
                          ledger ─ executor ─ report
                              loader ─┘

``refs`` is the seam that matters. ``$plan.json.confidence`` in a condition and
``${plan.json.confidence}`` in an argv element are one grammar with two
syntaxes, parsed in one place — which is what lets the loader walk every
reference in a file and prove it names an earlier stage before anything runs.

An ordered scope is the basic unit. S3b composes those scopes with runtime-sized
``for_each`` fan-out, static parallel branches, embedded sub-workflows and
bounded ``repeat`` loops. Every composition is a barrier: the stage after it
starts only after every child settles. The executor stays native async so this
changes scheduling without changing leaf handlers.
"""

from __future__ import annotations

from clawdence.engine.errors import (
    ConditionEvalError,
    ConditionSyntaxError,
    EngineError,
    InterpolationError,
    StepFailure,
    WorkflowLoadError,
)
from clawdence.engine.executor import RunReport, execute, idempotency_key
from clawdence.engine.handlers import (
    HandlerOutcome,
    HandlerRegistry,
    ScriptHandler,
    StepContext,
    StepHandler,
    StubHandler,
    UnimplementedHandler,
    default_registry,
)
from clawdence.engine.ledger import InMemoryLedger, Ledger
from clawdence.engine.loader import load_workflow, parse_workflow, validate_references
from clawdence.engine.refs import (
    MISSING,
    REQUEST,
    Facet,
    Reference,
    Resolver,
    parse_reference,
)
from clawdence.engine.report import render_json, render_text, to_dict

__all__ = [
    "MISSING",
    "REQUEST",
    "ConditionEvalError",
    "ConditionSyntaxError",
    "EngineError",
    "Facet",
    "HandlerOutcome",
    "HandlerRegistry",
    "InMemoryLedger",
    "InterpolationError",
    "Ledger",
    "Reference",
    "Resolver",
    "RunReport",
    "ScriptHandler",
    "StepContext",
    "StepFailure",
    "StepHandler",
    "StubHandler",
    "UnimplementedHandler",
    "WorkflowLoadError",
    "default_registry",
    "execute",
    "idempotency_key",
    "load_workflow",
    "parse_reference",
    "parse_workflow",
    "render_json",
    "render_text",
    "to_dict",
    "validate_references",
]
