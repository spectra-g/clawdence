"""Where work comes from — CLI, chat, issues, webhooks.

The port is read-only, and that is the design decision worth stating. How an
item *arrives* has no common shape: the webhook adapter is an HTTP handler, the
Slack adapter holds a socket, the CLI adapter is one process invocation. Trying
to unify those into a ``submit(payload)`` method would mean a payload type that
is `Any` in practice, which types nothing and tests nothing. What every source
does have in common is the pair below: hand over normalised work items, and be
told which ones were dealt with.

**Delivery is at-least-once, and acknowledgement is what ends it.** Every source
this system will speak to redelivers — GitHub retries webhooks, Slack replays on
reconnect, a CLI invocation that dies before the run is recorded left the request
unhandled. So ``collect`` returns what has not been acknowledged, and returns it
again until it has been. The alternative, at-most-once, drops a request whenever
the control plane dies in the window between reading and recording, and a
dropped request is invisible: nobody knows to ask about the sprint that never
started.

**Redelivery must not become a second work item.** ``dedupe_key`` is the one
rule, expressed once. v1 wrote duplicate guards per handler and they drifted;
here every adapter keys on the same thing, and the contract suite checks it.

``WorkItem.raw_text`` arrives from outside the trust boundary — a public issue is
text a stranger wrote (see ``domain.work_item``). Ingestion normalises the
envelope and never the content: no summarising, no rewriting, no stripping.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol

from clawdence.domain import WorkItem


def dedupe_key(item: WorkItem) -> str:
    """``source:external_id`` — the identity of a request, not of a delivery.

    Deliberately *not* ``WorkItem.id``: we mint that, so a redelivered webhook
    would get a fresh one and dedupe against nothing. The source's own id is the
    only field both deliveries agree on.
    """
    return f"{item.source_ref.source.value}:{item.source_ref.external_id}"


class IngestPort(Protocol):
    """A source of work items."""

    async def collect(self, *, limit: int = 50) -> Sequence[WorkItem]:
        """Unacknowledged items, oldest first.

        Returns the same items on a second call. Bounded by ``limit`` because a
        source that has been unreachable for a day comes back with a backlog,
        and the control plane should start working through it rather than load
        all of it first.
        """
        ...

    async def acknowledge(self, *item_ids: str) -> int:
        """Mark items as dealt with. Returns how many were still outstanding.

        Called *after* the work item is durably recorded, never before —
        acknowledging first turns a crash into a lost request, which is the
        failure this whole interface is shaped to avoid.

        Acknowledging something already acknowledged, or never seen, is not an
        error: a retried acknowledgement is the normal consequence of the crash
        window this exists to cover.
        """
        ...

    async def close(self) -> None:
        """Release whatever the adapter is holding. Idempotent."""
        ...


class InMemoryIngest:
    """A queue you can push into. The fake, and the CLI adapter's core.

    ``offer`` is not part of ``IngestPort`` on purpose — it is this
    implementation's arrival mechanism, and every real adapter has a different
    one. The contract suite takes an arrival hook for exactly this reason.
    """

    __slots__ = ("_closed", "_pending", "_seen")

    def __init__(self, items: Iterable[WorkItem] = ()) -> None:
        self._pending: dict[str, WorkItem] = {}
        self._seen: set[str] = set()
        self._closed = False
        for item in items:
            self.offer(item)

    def offer(self, item: WorkItem) -> bool:
        """Accept an arrival. ``False`` if it is a redelivery already known.

        Known means *ever* seen, not *currently pending*: an item that was
        collected, acknowledged and forgotten must not come back as new work
        when the source redelivers it an hour later.
        """
        key = dedupe_key(item)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._pending[item.id] = item
        return True

    async def collect(self, *, limit: int = 50) -> Sequence[WorkItem]:
        # dicts preserve insertion order, which is arrival order, which is the
        # order a backlog should be worked through.
        return tuple(self._pending.values())[:limit]

    async def acknowledge(self, *item_ids: str) -> int:
        return sum(1 for item_id in item_ids if self._pending.pop(item_id, None) is not None)

    async def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def outstanding(self) -> int:
        return len(self._pending)
