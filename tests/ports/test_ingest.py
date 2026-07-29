"""The ingest adapters against the contract.

Two of them, and running the same suite over both is the point: the fake dedupes
in a dictionary and the store adapter dedupes with a unique constraint across
processes, and the pipeline above them must not be able to tell which it has.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from clawdence.domain import WorkItem
from clawdence.ports import InMemoryIngest, dedupe_key
from clawdence.ports.ingest import SELF_ID, is_self
from clawdence.store import IN_MEMORY, StateStore, StoreIngest
from tests.ports import factories as make
from tests.ports.contract import IngestContract
from tests.ports.factories import run


class TestInMemoryIngest(IngestContract):
    @pytest.fixture
    def ingest(self) -> InMemoryIngest:
        return InMemoryIngest()

    def arrive(self, ingest: InMemoryIngest, item: WorkItem) -> None:  # type: ignore[override]
        ingest.offer(item)


class TestStoreIngest(IngestContract):
    """The durable adapter (S10). Arrival is ``intake.submit`` rather than
    ``offer`` — the same split, for the same reason: arrival has no common
    shape, so the contract takes a hook and the port stays read-only."""

    @pytest.fixture
    def ingest(self) -> Iterator[StoreIngest]:
        with StateStore.open(IN_MEMORY) as store:
            yield StoreIngest(store)

    def arrive(self, ingest: StoreIngest, item: WorkItem) -> None:  # type: ignore[override]
        ingest.intake.submit(item, at=item.created_at)


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


def test_the_system_recognises_its_own_voice() -> None:
    """Bot-loop prevention is one reserved name, checked once at intake. An
    adapter's whole obligation is to map its bot identity onto it — stated as a
    function here rather than as a string comparison in each adapter, which is
    how v1's per-handler guards drifted apart."""
    item = make.work_item("wi.1")
    assert is_self(item.submitter) is False
    assert is_self(item.submitter.model_copy(update={"external_id": SELF_ID})) is True
