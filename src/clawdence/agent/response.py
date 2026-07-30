"""Schema-validated agent output, by name.

``AgentStage.response_schema`` is a *name*, not a schema, and this is the registry
it names into. Two consequences worth stating, because they were both decisions:

**The schemas are pydantic models, not user-supplied JSON Schema.** The domain
model is already the single source of every schema in this system (S2), the
validator ships with the runtime, and the error messages are the best available.
A JSON Schema loaded from a user's directory would need a validator in the
runtime dependencies — ``jsonschema`` is deliberately a *dev* dependency, used to
prove the generated files faithful — and it would put a second schema language
next to the one the whole spine is written in. The user-facing extensibility S12
promises is the **prompt** override, which is what people actually want to tune;
inventing a response shape is a change to what later stages can read, and that is
a code change wherever it lives.

**Structure only where something branches on it.** ``size``, ``confidence`` and
``verdict`` are typed narrowly, because a workflow's ``when`` conditions read them
and a value outside the set would make a guard silently false. Risks, findings and
steps are lists of strings, because nothing machine-reads their internals at M1 —
and every nested object is another way for a model to fail validation for a reason
that changes nothing about what the run does next.

**Validation errors never quote the value.** The message is built from the field
path and the rule that failed, and not from ``input``, which is what pydantic
would include by default. Model output routinely quotes the request, and the
request is where a pasted credential turns up (threat model T11) — so the one
place these messages go, an error message that reaches a human and a turn of
feedback that goes back to the model, is a place a value must not travel.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final, Literal

from pydantic import Field, JsonValue, ValidationError

from clawdence.domain import DomainModel


class SchemaNotFoundError(LookupError):
    """A stage named a response schema the registry does not hold."""


class ResponseInvalidError(ValueError):
    """A parsed response does not satisfy its schema.

    Carries ``explanation``, which is what a turn of feedback sends back to the
    model. Deliberately the same text a human sees: two renderings of one failure
    is how the message a person reads and the message the model is corrected with
    come to describe different problems.
    """

    def __init__(self, schema: str, explanation: str) -> None:
        super().__init__(f"the response does not satisfy the {schema!r} schema: {explanation}")
        self.schema = schema
        self.explanation = explanation


class Requirements(DomainModel):
    """What a business analyst produces.

    ``acceptance_criteria`` has no minimum length, which looks like a gap and is
    not: the role is instructed to return confidence 0 for a request that is not
    a request for software work, and a schema that demanded criteria would make
    the only honest answer to "hello" unrepresentable. Branch on ``confidence``.
    """

    summary: str
    acceptance_criteria: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()

    #: Read by ``when`` conditions, so its range is part of the contract.
    confidence: float = Field(ge=0, le=1)

    #: Set when the request text tried to redirect the analyst rather than
    #: describe work. Not a security control — it is a *report*, and the reason
    #: it is a field is so that a workflow can route it to a human instead of
    #: having it mentioned in prose nothing reads.
    unusual_request: str | None = None


class Assessment(DomainModel):
    """What a technical lead produces."""

    #: Read by ``when`` conditions; ``L`` is what S15b's split will branch on.
    size: Literal["S", "M", "L"]
    approach: str
    risks: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()

    #: Where to split, required by the prompt whenever the size is ``L``. Not
    #: enforced here: a model that says ``L`` and cannot name the seam has told
    #: us something true, and rejecting the response would replace a useful
    #: answer with a repair loop.
    split_at: str | None = None


class ImplementationPlan(DomainModel):
    """What an architect produces.

    The runner is handed this as text — ``${plan.json.result}`` renders it as
    compact JSON, and a coding agent reads JSON perfectly well. It is not a
    machine-executed script, and §1.3 is why: the plan is a proposal that enters
    the review path, so nothing here is a field the system acts on directly.
    """

    steps: tuple[str, ...] = Field(min_length=1)
    constraints: tuple[str, ...] = ()

    #: What would prove the whole thing done. S13's verification contracts are
    #: the machine-checkable version; this is the architect's statement of intent
    #: in prose, and the two are deliberately not the same field.
    evidence: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()


class Review(DomainModel):
    """What a reviewer produces."""

    #: Read by ``when`` conditions. Three values, not two: "the approach is right
    #: and something is wrong" is the common case and collapsing it into a
    #: rejection is how a fixable branch gets thrown away.
    verdict: Literal["approved", "changes_requested", "rejected"]
    summary: str
    findings: tuple[str, ...] = ()

    #: Criteria the reviewer could and could not check. Absence of evidence is a
    #: finding, and it is only a finding if there is somewhere to put it.
    verified: tuple[str, ...] = ()
    unverifiable: tuple[str, ...] = ()


#: The shipped registry. Names are what a workflow writes in ``response_schema``.
DEFAULT_SCHEMAS: Final[Mapping[str, type[DomainModel]]] = {
    "requirements": Requirements,
    "assessment": Assessment,
    "plan": ImplementationPlan,
    "review": Review,
}


class ResponseSchemas:
    """Named schemas an agent step can be held to."""

    __slots__ = ("_schemas",)

    def __init__(self, schemas: Mapping[str, type[DomainModel]] | None = None) -> None:
        self._schemas = dict(DEFAULT_SCHEMAS if schemas is None else schemas)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._schemas))

    def model_for(self, name: str) -> type[DomainModel]:
        schema = self._schemas.get(name)
        if schema is None:
            offered = ", ".join(self.names()) or "(none registered)"
            raise SchemaNotFoundError(
                f"no response schema named {name!r}; registered schemas are {offered}"
            )
        return schema

    def instruction(self, name: str) -> str:
        """The schema, rendered for a prompt.

        Appended to the request rather than described in the role prompt, so the
        shape and the validator cannot drift: a role prompt that spells out its
        own fields is one that keeps promising a field the schema dropped.
        """
        schema = self.model_for(name).model_json_schema(mode="validation")
        return (
            "Your reply must be a single JSON object satisfying this JSON Schema. "
            "Emit no other text.\n\n"
            + json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False)
        )

    def validate(self, name: str, value: JsonValue) -> JsonValue:
        """Check a parsed document, returning it normalised.

        Normalised through the model, so defaults are filled in and field order
        is the schema's. That matters for the run record: two responses that
        differ only in which optional fields the model bothered to include should
        produce the same shape for a later stage to read.
        """
        model = self.model_for(name)
        try:
            instance = model.model_validate(value)
        except ValidationError as exc:
            raise ResponseInvalidError(name, _explain(exc)) from None
        dumped: JsonValue = instance.model_dump(mode="json")
        return dumped


def _explain(exc: ValidationError) -> str:
    """Field paths and rules, never values.

    See the module docstring: ``ValidationError.errors()`` carries ``input``, and
    the input here is model output that may quote the request.
    """
    parts: list[str] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts) if parts else "it did not validate"
