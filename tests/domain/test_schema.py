"""The generated JSON Schema, and its agreement with the models.

Two different claims are checked here, and both matter:

*The committed files are current.* A field added in Python without
regenerating leaves ``schemas/`` describing a contract that no longer exists.

*The projection is faithful.* Every sample instance validates against the
schema generated from its own model. Without this, the schema could be
committed, current, and still wrong — describing something the Python types
would reject, or accepting something they would not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from clawdence.domain import Budget, Workflow
from clawdence.domain import jsonschema as gen
from tests.domain.samples import SAMPLES

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

CASES = sorted((model.__name__, model) for model in gen.EXPORTED)


def test_committed_schemas_match_the_models() -> None:
    """``make schema-test``'s headline check.

    Run ``clawdence schema export`` and commit the result when this fails.
    """
    assert gen.diff(SCHEMA_DIR) == []


def test_every_exported_model_has_a_sample() -> None:
    """A contract with no sample is a contract nothing below actually tests."""
    assert {model.__name__ for model in gen.EXPORTED} == set(SAMPLES)


@pytest.mark.parametrize(("name", "model"), CASES, ids=[name for name, _ in CASES])
def test_generated_schema_is_itself_valid(name: str, model: type[BaseModel]) -> None:
    Draft202012Validator.check_schema(gen.schema_for(model))


@pytest.mark.parametrize(("name", "model"), CASES, ids=[name for name, _ in CASES])
def test_sample_validates_against_its_generated_schema(name: str, model: type[BaseModel]) -> None:
    instance = json.loads(SAMPLES[name].model_dump_json())
    Draft202012Validator(gen.schema_for(model)).validate(instance)


@pytest.mark.parametrize(("name", "model"), CASES, ids=[name for name, _ in CASES])
def test_schema_rejects_unknown_fields(name: str, model: type[BaseModel]) -> None:
    """``extra="forbid"`` must reach the schema, not stop at the Python types.

    Otherwise a consumer validating against ``schemas/`` accepts input this
    codebase would refuse — which is the divergence the whole one-source rule
    exists to prevent.
    """
    validator = Draft202012Validator(gen.schema_for(model))
    instance = json.loads(SAMPLES[name].model_dump_json())
    instance["definitely_not_a_field"] = "x"
    assert not validator.is_valid(instance)


def test_stage_union_is_discriminated_by_type() -> None:
    """The workflow schema must tell a consumer *which* stage it is looking at.

    An undiscriminated ``oneOf`` would make an invalid stage report four
    unrelated errors instead of one about the stage type it actually is.
    """
    schema = gen.schema_for(Workflow)
    discriminator = schema["properties"]["stages"]["items"]["discriminator"]
    assert discriminator["propertyName"] == "type"
    assert set(discriminator["mapping"]) == {
        "script",
        "agent",
        "runner",
        "approval",
        "for_each",
        "parallel",
        "workflow",
        "repeat",
    }


def test_rendered_schema_is_deterministic() -> None:
    """Regeneration with no model change must produce no diff.

    If it did, ``schema check`` would fail on unrelated commits and people
    would learn to ignore it.
    """
    assert dict(gen.generate()) == dict(gen.generate())


def test_written_files_are_exactly_what_is_committed(tmp_path: Path) -> None:
    gen.write(tmp_path)
    written = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.schema.json")}
    committed = {p.name: p.read_text(encoding="utf-8") for p in SCHEMA_DIR.glob("*.schema.json")}
    assert written == committed


def test_write_is_idempotent(tmp_path: Path) -> None:
    """A second export reports nothing changed.

    ``write`` returns changed paths, and the CLI prints them; rewriting
    identical bytes every time would make that output meaningless.
    """
    assert gen.write(tmp_path)
    assert gen.write(tmp_path) == []


def test_diff_reports_an_orphaned_schema(tmp_path: Path) -> None:
    """A renamed or deleted contract must not leave a stale file behind.

    An orphan still validates, so nothing else would ever notice it.
    """
    gen.write(tmp_path)
    orphan = tmp_path / "retired-contract.schema.json"
    orphan.write_text("{}\n", encoding="utf-8")
    assert gen.diff(tmp_path) == ["retired-contract.schema.json"]


def test_diff_reports_a_stale_schema(tmp_path: Path) -> None:
    gen.write(tmp_path)
    (tmp_path / "budget.schema.json").write_text("{}\n", encoding="utf-8")
    assert gen.diff(tmp_path) == ["budget.schema.json"]


def test_diff_reports_a_missing_schema(tmp_path: Path) -> None:
    assert "budget.schema.json" in gen.diff(tmp_path)


def test_schema_carries_dialect_and_identity() -> None:
    schema: dict[str, Any] = gen.schema_for(Budget)
    assert schema["$schema"] == gen.DIALECT
    assert schema["$id"].endswith("/budget.schema.json")
