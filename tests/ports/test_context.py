"""The in-memory context against the contract; the null one against its own."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from clawdence.ports import InMemoryContext, KnowledgeKind, NullContext
from tests.ports import factories as make
from tests.ports.contract import Call, ContextContract, NullAdapterContract
from tests.ports.factories import run


class TestInMemoryContext(ContextContract):
    @pytest.fixture
    def context(self) -> InMemoryContext:
        return InMemoryContext()


class TestNullContext(NullAdapterContract):
    @pytest.fixture
    def calls(self) -> Sequence[Call]:
        context = NullContext()
        return (
            lambda: context.retrieve("anything"),
            lambda: context.remember(make.knowledge("k.1", "anything")),
            lambda: context.forget("k.1"),
        )


def test_the_null_context_degrades_rather_than_failing() -> None:
    """No memory configured must not fail a run: an agent with no prior
    knowledge is the first run against any repo, which has to work."""
    context = NullContext()
    run(context.remember(make.knowledge("k.1", "maven wrapper")))
    assert run(context.retrieve("maven")) == ()
    assert run(context.forget("k.1")) is False


def test_ties_break_on_a_stable_key() -> None:
    """Two items matching one word each are otherwise ordered by insertion,
    which is arrival order, which does not survive a restart."""
    context = InMemoryContext(
        [
            make.knowledge("k.zebra", "maven here"),
            make.knowledge("k.alpha", "maven there"),
        ]
    )
    assert make.texts(run(context.retrieve("maven"))) == ("k.alpha", "k.zebra")


def test_a_better_match_outranks_a_worse_one() -> None:
    context = InMemoryContext(
        [
            make.knowledge("k.weak", "maven"),
            make.knowledge("k.strong", "maven wrapper offline"),
        ]
    )
    assert make.texts(run(context.retrieve("maven wrapper offline")))[0] == "k.strong"


def test_scores_are_ordered_but_the_scale_is_the_adapter_s() -> None:
    """Only the ordering is contractual. A cosine similarity and a BM25 score
    are not comparable numbers, and a threshold over them is unjustifiable."""
    context = InMemoryContext([make.knowledge("k.1", "maven wrapper")])
    hits = run(context.retrieve("maven"))
    assert hits[0].score > 0


def test_matching_ignores_case() -> None:
    context = InMemoryContext([make.knowledge("k.1", "Maven Wrapper")])
    assert make.texts(run(context.retrieve("MAVEN"))) == ("k.1",)


def test_items_sharing_no_word_with_the_query_are_not_returned() -> None:
    """Lexical and deliberately so. A fake that simulated semantic similarity
    would invite tests asserting on *ranking*, which is the adapter's behaviour
    and not the port's contract."""
    context = InMemoryContext(
        [
            make.knowledge("k.match", "the build uses maven"),
            make.knowledge("k.miss", "unrelated observation about logging"),
        ]
    )
    assert make.texts(run(context.retrieve("maven"))) == ("k.match",)


def test_a_global_query_sees_every_repo() -> None:
    context = InMemoryContext(
        [
            make.knowledge("k.a", "maven here", repo_id="repo.a"),
            make.knowledge("k.b", "maven there", repo_id="repo.b"),
        ]
    )
    assert len(run(context.retrieve("maven"))) == 2


def test_stored_items_are_readable() -> None:
    context = InMemoryContext()
    run(context.remember(make.knowledge("k.1", "a rule", kind=KnowledgeKind.RULE)))
    assert [item.id for item in context.items] == ["k.1"]
