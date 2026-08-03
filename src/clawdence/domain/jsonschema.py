"""Generate JSON Schema from the domain model.

S2's requirement is one source for both the language types and the schema. The
types are the source; this module is the projection. Nothing here is
hand-maintained, and ``clawdence schema check`` fails if the committed files
stop matching the models — so a field added in Python without regenerating is
a red build rather than a silent divergence.

The output is committed rather than built on demand. Schema changes are the
expensive kind, and a diff in review is the cheapest place to notice one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from clawdence.domain.budget import Budget, CostEntry
from clawdence.domain.events import Event
from clawdence.domain.repo import RepoProfile
from clawdence.domain.run import Run, StepResult
from clawdence.domain.runner import RunnerRequest, RunnerResult
from clawdence.domain.verification import HaltRecord, VerificationContract, VerificationResult
from clawdence.domain.work_item import WorkItem
from clawdence.domain.workflow import Workflow

DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE_ID = "https://clawdence.dev/schemas"

#: The exported surface. Types reachable only as fields of these (``Stage``,
#: ``TestEvidence``, every enum) are emitted inline as ``$defs`` — one
#: self-contained file per contract, which reviews far better than a web of
#: cross-file ``$ref``s.
EXPORTED: tuple[type[BaseModel], ...] = (
    Budget,
    CostEntry,
    Event,
    # Exported because S17's operator surface is a *different reader* of this
    # record — a halt is stored, then acted on later, possibly by something
    # that is not this process. A contract that crosses that gap is one the
    # schema has to state.
    HaltRecord,
    RepoProfile,
    Run,
    RunnerRequest,
    RunnerResult,
    StepResult,
    VerificationContract,
    VerificationResult,
    WorkItem,
    Workflow,
)


def _filename(model: type[BaseModel]) -> str:
    """``RunnerRequest`` -> ``runner-request.schema.json``."""
    name = model.__name__
    parts: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            parts.append("-")
        parts.append(char.lower())
    return f"{''.join(parts)}.schema.json"


def schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON Schema for one model, with dialect and identity attached."""
    schema = model.model_json_schema(mode="validation")
    return {"$schema": DIALECT, "$id": f"{BASE_ID}/{_filename(model)}", **schema}


def render(schema: Mapping[str, Any]) -> str:
    """Serialise deterministically.

    ``sort_keys`` matters: without it the drift check would depend on pydantic's
    internal ordering, and a harmless library upgrade would show up as a
    schema change.
    """
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def generate() -> Iterator[tuple[str, str]]:
    """Yield ``(filename, contents)`` for every exported model."""
    for model in EXPORTED:
        yield _filename(model), render(schema_for(model))


def write(destination: Path) -> list[Path]:
    """Write the schemas, returning the files that changed."""
    destination.mkdir(parents=True, exist_ok=True)
    changed: list[Path] = []
    for filename, contents in generate():
        path = destination / filename
        if not path.exists() or path.read_text(encoding="utf-8") != contents:
            path.write_text(contents, encoding="utf-8")
            changed.append(path)
    return changed


def diff(destination: Path) -> list[str]:
    """Names of schemas that are missing or stale on disk.

    Also reports files in ``destination`` that no model produces, so a renamed
    or deleted contract cannot leave an orphan behind that still validates.
    """
    expected = dict(generate())
    stale = [
        filename
        for filename, contents in expected.items()
        if not (destination / filename).exists()
        or (destination / filename).read_text(encoding="utf-8") != contents
    ]
    orphaned = [
        path.name for path in sorted(destination.glob("*.schema.json")) if path.name not in expected
    ]
    return sorted(stale + orphaned)
