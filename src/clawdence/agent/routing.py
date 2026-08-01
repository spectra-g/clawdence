"""Which model answers, and what happens when it can't.

``ModelSelector`` names a model, an ordered list of fallbacks, and the
capabilities the step requires. Three rules turn that into a call:

**Every candidate is validated, not just the first.** A fallback nobody checked
is a fallback that fails the first time it is used — which is, by construction,
the worst possible moment: the primary model has just run out of quota, so the run
is already degraded, and the fallback then fails for a reason that was knowable
before the run started. Validation walks the whole chain.

**Quota exhaustion moves down the chain; nothing else does.** This is the
distinction v1 got wrong, and it is worth being precise about *why* only quota:
a rate limit means this model will answer shortly, so falling through to a
different model changes the answer for no reason and pays more for it. A malformed
request fails identically everywhere. A context overflow overflows the next model
too, unless it happens to be larger — and pretending that is a fallback strategy
is how a workflow silently starts producing different output on a different model.

**The model that answered is reported.** ``Route.attempted`` lists the ones that
did not, so a run record says "the primary was out of quota and the fallback wrote
this". Without it a quota fallback is invisible, and "why did the review read
differently yesterday" has no answer in the record.

What is deliberately not here: retry with backoff. A rate limit is a
``TransientError``, and whether a step is retried is the engine's decision from
``RetryPolicy`` (``engine.executor``). A second retry loop inside the step would
be a second retry policy nobody declared, multiplying against the first one — v1
had exactly that, and its effective attempt count was the product of two numbers
in different files.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from clawdence.domain import ModelSelector
from clawdence.ports import (
    CapabilityError,
    ModelDescriptor,
    ModelPort,
    ModelRequest,
    ModelResponse,
    QuotaExhaustedError,
)


@dataclass(frozen=True, slots=True)
class Route:
    """The model that answered, and the ones that could not."""

    descriptor: ModelDescriptor
    response: ModelResponse

    #: Models tried and passed over, in order. Empty on the common path.
    attempted: tuple[str, ...] = ()

    @property
    def model(self) -> str:
        return self.descriptor.model


def candidates(selector: ModelSelector) -> tuple[str, ...]:
    """The model and its fallbacks, in the order they will be tried.

    De-duplicated, preserving order. A selector that lists its own primary as a
    fallback is a copy-paste in someone's YAML, and trying it twice would report
    two quota failures for one account and make the record read as if two models
    were out of credit.
    """
    ordered: list[str] = []
    for name in (selector.model, *selector.fallbacks):
        if name not in ordered:
            ordered.append(name)
    return tuple(ordered)


def validate(port: ModelPort, selector: ModelSelector) -> tuple[ModelDescriptor, ...]:
    """Resolve every candidate and check it against ``requires``.

    Raises ``UnknownModelError`` for a name no provider serves and
    ``CapabilityError`` for one that cannot do what the step needs. Both are
    permanent, and both are meant to be raised *before* a run starts — this is
    what "a model swap fails validation rather than failing mysteriously at run
    time" means in practice.
    """
    resolved: list[ModelDescriptor] = []
    required = frozenset(selector.requires)
    for name in candidates(selector):
        descriptor = port.describe(name)
        missing = required - frozenset(descriptor.capabilities)
        if missing:
            raise CapabilityError(name, sorted(missing, key=lambda item: item.value))
        resolved.append(descriptor)
    return tuple(resolved)


async def complete(
    port: ModelPort,
    selector: ModelSelector,
    build: Callable[[ModelDescriptor], ModelRequest],
) -> Route:
    """Ask the first candidate that has quota, and report which one answered.

    ``build`` takes the descriptor rather than the model name because the request
    depends on it: ``max_output_tokens`` is capped by what the model will emit,
    and a fallback with a smaller limit must not be sent a request built for the
    primary's.
    """
    attempted: list[str] = []

    for descriptor in validate(port, selector):
        try:
            response = await port.complete(build(descriptor))
        except QuotaExhaustedError:
            attempted.append(descriptor.model)
            continue
        return Route(descriptor=descriptor, response=response, attempted=tuple(attempted))

    # Every candidate is out of quota. Still a ``QuotaExhaustedError``, so a
    # caller distinguishing "no credit" from "the provider is unwell" still can —
    # but the message names the whole chain, because "claude-opus-5 has no quota"
    # is a misleading summary of an account that has none for anything.
    raise QuotaExhaustedError(selector.model, f"and so is every fallback: {', '.join(attempted)}")
