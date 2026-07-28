"""Budgets and cost accounting.

v1 had no cost control at all. One of the stated success criteria for v2 is a
hard cap *that actually fires*, so ``Budget.on_exceeded`` is a one-value
``Literal``: there is no configuration under which exceeding a budget is
allowed to continue. Widening it later is a deliberate schema change and a
visible one, which is the point.

Money is ``Decimal``. Token counts are large integers and per-token prices are
small; accumulating those in binary floating point drifts, and a budget that
drifts is a budget that doesn't fire when it should.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field

from clawdence.domain._base import DomainModel
from clawdence.domain.ids import RunId, StageId


class TokenUsage(DomainModel):
    """Tokens consumed by one LLM interaction.

    Cached input is counted separately rather than folded into ``input_tokens``
    because it is priced differently, and a cost ledger that cannot see the
    difference cannot explain its own numbers.
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class CostEntry(DomainModel):
    """One charge against a run's budget.

    Distinct from the audit trail: this ledger answers "what did it cost", the
    audit trail answers "what happened". v1 conflated them in
    ``llm-audit.jsonl`` and could answer neither question well.
    """

    run_id: RunId
    stage_id: StageId | None = None
    model: str | None = None
    usage: TokenUsage = TokenUsage()
    usd: Decimal = Field(default=Decimal("0"), ge=0)
    at: AwareDatetime


class Budget(DomainModel):
    """A cap on what one run, or one step, may spend.

    Every limit is optional; ``None`` means unlimited on that axis. A budget
    with every field ``None`` is legal and means "no cap" — which is a choice
    the operator has to make explicitly rather than inherit.
    """

    max_usd: Decimal | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_wall_clock_seconds: float | None = Field(default=None, gt=0)

    #: Not configurable. Exceeding a budget aborts; nothing force-proceeds.
    on_exceeded: Literal["abort"] = "abort"
