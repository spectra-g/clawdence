"""What the system remembers about a codebase.

The memory layer proper is S14; this is the shape everything above it is written
against, so that "which embedding model" stays a decision made inside one
adapter (ADR-0008 — the provider is an interface and the dimension is data).
Nothing in this port mentions a vector, and that is deliberate: a port that took
embeddings would put the dimension in every caller's type signature and make the
provider swap a system-wide change.

**Retrieval is deterministic.** Same store, same query, same order — ties broken
on a stable key rather than left to whatever the index returns. Without that,
agent-step record/replay is useless (the cassette key would depend on retrieval
order) and S21b's evals would measure index churn instead of prompt changes.

**What comes back is data.** ``KnowledgeItem.text`` was written by a process that
read repo content, or is a discovery note from a runner, or came out of an
issue. Every one of those is a path from untrusted input into a later prompt.
The type carries ``source`` so a prompt builder can frame it as quoted material
rather than as something the system believes — see ``domain.work_item`` for the
same rule at the ingestion end.

**Remembering is idempotent on id.** Retries and re-ingestion of the same rules
file are routine; a memory that grows a duplicate on every restart is one that
eventually returns the same fact five times and spends the context budget on it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, Field

from clawdence.domain import DomainModel
from clawdence.domain.ids import Identifier, RepoId
from clawdence.ports._common import Clock, utc_now


class KnowledgeKind(StrEnum):
    """Why a piece of knowledge exists, which is what decides how it is used.

    Four, and the distinction is not cosmetic. A ``RULE`` is normative and a
    contradiction between two of them is a problem a human must resolve (the
    plan's contradiction check); a ``DISCOVERY`` is an observation that may
    simply be out of date. Storing both as "context" is how v1's prompt
    padding became indistinguishable from its constraints.
    """

    RULE = "rule"
    CONVENTION = "convention"
    DISCOVERY = "discovery"
    DECISION = "decision"


class KnowledgeItem(DomainModel):
    """One thing worth remembering."""

    id: Identifier
    kind: KnowledgeKind
    text: str

    #: Scope. ``None`` is global — true of the installation, not of a repo.
    repo_id: RepoId | None = None

    #: Where it came from, in a form a human can go and check: a file path, a
    #: PR url, ``runner:run.abc123``. Unattributed knowledge cannot be retired
    #: when it turns out to be wrong, and wrong knowledge that cannot be retired
    #: is worse than none.
    source: str

    tags: tuple[str, ...] = ()
    created_at: AwareDatetime


class Retrieval(DomainModel):
    """A hit, with how well it matched."""

    item: KnowledgeItem

    #: Higher is better. The scale is the adapter's; only the ordering is part
    #: of the contract, because a cosine similarity and a BM25 score are not
    #: comparable numbers and pretending otherwise invites a threshold nobody
    #: can justify.
    score: float = Field(ge=0)


class ContextPort(Protocol):
    """Retrieval and storage of what the system knows."""

    async def retrieve(
        self,
        query: str,
        *,
        repo_id: RepoId | None = None,
        kinds: Iterable[KnowledgeKind] | None = None,
        limit: int = 10,
    ) -> Sequence[Retrieval]:
        """Best matches first, deterministically ordered.

        ``repo_id`` scopes to that repo *plus* anything global; passing ``None``
        searches everything. A retrieval that silently excluded global rules
        would let an installation-wide constraint go unenforced on every repo.
        """
        ...

    async def remember(self, item: KnowledgeItem) -> KnowledgeItem:
        """Store, or replace an item with the same id. Returns what is stored."""
        ...

    async def forget(self, item_id: str) -> bool:
        """Remove an item. ``False`` if it was not there."""
        ...


class NullContext:
    """Remembers nothing. The default until S14.

    Retrieval returns empty rather than raising, because "no memory configured"
    should degrade an agent's context, not fail the run — an agent with no prior
    knowledge is the first run against any repo, which has to work.
    """

    __slots__ = ()

    async def retrieve(
        self,
        query: str,
        *,
        repo_id: RepoId | None = None,
        kinds: Iterable[KnowledgeKind] | None = None,
        limit: int = 10,
    ) -> Sequence[Retrieval]:
        return ()

    async def remember(self, item: KnowledgeItem) -> KnowledgeItem:
        return item

    async def forget(self, item_id: str) -> bool:
        return False


def _tokens(text: str) -> frozenset[str]:
    return frozenset(word for word in text.casefold().split() if word)


class InMemoryContext:
    """Scores by word overlap. The fake.

    Not an approximation of a vector index, and it does not try to be: it is
    lexical, deterministic, offline and free. A fake that called an embedding
    API would defeat the point of the whole harness, and a fake that simulated
    semantic similarity would encourage tests that assert on *ranking*, which is
    the adapter's behaviour and not the port's contract.
    """

    __slots__ = ("_clock", "_items")

    def __init__(self, items: Iterable[KnowledgeItem] = (), *, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._items: dict[str, KnowledgeItem] = {item.id: item for item in items}

    async def retrieve(
        self,
        query: str,
        *,
        repo_id: RepoId | None = None,
        kinds: Iterable[KnowledgeKind] | None = None,
        limit: int = 10,
    ) -> Sequence[Retrieval]:
        wanted = None if kinds is None else frozenset(kinds)
        needles = _tokens(query)

        hits: list[Retrieval] = []
        for item in self._items.values():
            if repo_id is not None and item.repo_id is not None and item.repo_id != repo_id:
                continue
            if wanted is not None and item.kind not in wanted:
                continue
            overlap = needles & _tokens(item.text)
            if not overlap:
                continue
            hits.append(Retrieval(item=item, score=len(overlap) / len(needles)))

        # Descending score, then ascending id. The second key is what makes this
        # deterministic — two items matching one word each are otherwise ordered
        # by dict insertion, which is arrival order, which is not reproducible
        # across a restart.
        hits.sort(key=lambda hit: (-hit.score, hit.item.id))
        return tuple(hits[:limit])

    async def remember(self, item: KnowledgeItem) -> KnowledgeItem:
        self._items[item.id] = item
        return item

    async def forget(self, item_id: str) -> bool:
        return self._items.pop(item_id, None) is not None

    @property
    def items(self) -> Sequence[KnowledgeItem]:
        return tuple(self._items.values())
