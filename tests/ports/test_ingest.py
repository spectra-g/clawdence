"""The in-memory ingest adapter against the contract."""

from __future__ import annotations

import pytest

from clawdence.domain import WorkItem
from clawdence.ports import InMemoryIngest, dedupe_key
from tests.ports import factories as make
from tests.ports.contract import IngestContract
from tests.ports.factories import run


class TestInMemoryIngest(IngestContract):
    @pytest.fixture
    def ingest(self) -> InMemoryIngest:
        return InMemoryIngest()

    def arrive(self, ingest: InMemoryIngest, item: WorkItem) -> None:  # type: ignore[override]
        ingest.offer(item)


def test_offer_reports_whether_it_was_new() -> None:
    """The return value is what a webhook handler answers with: a redelivery
    gets a 200 and no new work, and it has to be able to tell the difference."""
    ingest = InMemoryIngest()
    assert ingest.offer(make.work_item("wi.1", external_id="a")) is True
    assert ingest.offer(make.work_item("wi.2", external_id="a")) is False


def test_seeded_items_are_pending() -> None:
    ingest = InMemoryIngest([make.work_item("wi.1", external_id="a")])
    assert ingest.outstanding == 1


def test_the_dedupe_key_is_the_source_id_not_ours() -> None:
    """Keying on ``WorkItem.id`` would dedupe nothing: we mint that, so a
    redelivered webhook arrives with a fresh one every time."""
    first = make.work_item("wi.1", external_id="issue-42")
    second = make.work_item("wi.2", external_id="issue-42")
    assert dedupe_key(first) == dedupe_key(second) == "cli:issue-42"
    assert first.id != second.id


def test_closing_is_observable() -> None:
    ingest = InMemoryIngest()
    assert ingest.closed is False
    run(ingest.close())
    assert ingest.closed is True


def test_acknowledging_several_at_once() -> None:
    ingest = InMemoryIngest([make.work_item(f"wi.{n}", external_id=str(n)) for n in range(3)])
    assert run(ingest.acknowledge("wi.0", "wi.1", "wi.absent")) == 2
    assert ingest.outstanding == 1
