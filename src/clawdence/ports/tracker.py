"""Tickets — GitHub Issues, Jira, or nothing at all.

"Nothing at all" is a first-class option, which is why this is a port rather
than a feature. v1 assumed a tracker existed and put ticket ids in the middle of
its state model; a user with no Jira instance had to fake one. ``NullTracker``
is a supported configuration, and the pipeline is written so that losing the
tracker degrades traceability and nothing else.

**One ticket per work item, enforced structurally.** ``ensure`` takes a work
item id and is idempotent on it: called twice, the second call returns the
ticket the first one created. Not ``create`` — a method named ``create``
invites a caller to check first and then create, and that check-then-act is a
race the moment two steps of one epic report progress at the same time. v1 had
exactly this bug, guarded by hand in each of its handlers.

**Transitions are declared, not free-form.** ``TicketState`` is four values, and
the mapping onto a real tracker's workflow is the adapter's problem. A port that
took an arbitrary status string would push every tracker's vocabulary into the
control plane, which is how v1 ended up with Jira transition ids in its
orchestrator.

Tracker failures are non-fatal (ARCHITECTURE §Failure domains) — but this port
still raises, and ``outbox`` is what makes them non-fatal. See ``notify`` for
why that split is deliberate.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime

from clawdence.domain import DomainModel
from clawdence.domain.ids import RepoId, RunId, WorkItemId
from clawdence.ports._common import NULL_PREFIX, Clock, utc_now
from clawdence.ports.errors import PermanentError


class TicketState(StrEnum):
    """The states the control plane knows how to mean.

    Four, not the seven a Jira board has. Anything richer is the adapter's
    translation, because the pipeline only ever needs to say "started",
    "waiting on a person", "finished" or "abandoned".
    """

    OPEN = "open"
    IN_PROGRESS = "in-progress"
    BLOCKED = "blocked"
    CLOSED = "closed"


class Ticket(DomainModel):
    """A ticket as the control plane sees it."""

    #: The tracker's own id — ``PROJ-14``, ``#212``. Opaque.
    id: str

    work_item_id: WorkItemId
    repo_id: RepoId | None = None

    title: str
    state: TicketState = TicketState.OPEN
    url: str | None = None
    labels: tuple[str, ...] = ()

    created_at: AwareDatetime
    updated_at: AwareDatetime


class TrackerPort(Protocol):
    """Records what the system is doing, where humans already look."""

    async def ensure(
        self,
        *,
        work_item_id: WorkItemId,
        title: str,
        body: str,
        repo_id: RepoId | None = None,
        labels: Sequence[str] = (),
    ) -> Ticket:
        """The ticket for this work item, creating it if there isn't one.

        Idempotent on ``work_item_id``. A second call with a different title
        does *not* rename: the work item is the identity, and a rename would
        make the BA's rewrite overwrite what a human may have edited by hand.
        """
        ...

    async def transition(self, ticket_id: str, state: TicketState) -> Ticket:
        """Move a ticket. Transitioning to its current state is a no-op."""
        ...

    async def comment(self, ticket_id: str, text: str, *, run_id: RunId | None = None) -> None:
        """Add a comment. Not idempotent, and deliberately not.

        A comment is a running narrative — two progress updates that happen to
        have the same text are two events, and collapsing them would hide a
        retry. Callers that must not repeat themselves route through
        ``Outbox``, which keys on the caller's own idempotency key.
        """
        ...

    async def find(self, work_item_id: WorkItemId) -> Ticket | None:
        """The ticket for a work item, or ``None``."""
        ...


class NullTracker:
    """No tracker configured. A supported deployment, not a stub.

    Every operation succeeds and nothing is recorded, so a pipeline written
    against ``TrackerPort`` runs unchanged for a user who has no Jira. It
    deliberately does **not** satisfy the tracker contract suite, and the
    difference is worth naming: an adapter that claims to store things has to
    return what it stored, and this one is honest about storing nothing —
    ``find`` returns ``None`` even for a work item ``ensure`` was just called
    with.

    Nothing here raises. A ``NullTracker`` that raised on ``transition`` would
    make "no tracker configured" fail runs, which is exactly the coupling the
    port exists to remove.
    """

    __slots__ = ("_clock",)

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock

    def _ticket(
        self,
        *,
        ticket_id: str,
        work_item_id: WorkItemId,
        title: str,
        state: TicketState,
        repo_id: RepoId | None = None,
        labels: Sequence[str] = (),
    ) -> Ticket:
        now = self._clock()
        return Ticket(
            id=ticket_id,
            work_item_id=work_item_id,
            repo_id=repo_id,
            title=title,
            state=state,
            labels=tuple(labels),
            created_at=now,
            updated_at=now,
        )

    async def ensure(
        self,
        *,
        work_item_id: WorkItemId,
        title: str,
        body: str,
        repo_id: RepoId | None = None,
        labels: Sequence[str] = (),
    ) -> Ticket:
        return self._ticket(
            ticket_id=f"{NULL_PREFIX}{work_item_id}",
            work_item_id=work_item_id,
            title=title,
            state=TicketState.OPEN,
            repo_id=repo_id,
            labels=labels,
        )

    async def transition(self, ticket_id: str, state: TicketState) -> Ticket:
        return self._ticket(
            ticket_id=ticket_id,
            work_item_id=ticket_id.removeprefix(NULL_PREFIX),
            title="",
            state=state,
        )

    async def comment(self, ticket_id: str, text: str, *, run_id: RunId | None = None) -> None:
        return None

    async def find(self, work_item_id: WorkItemId) -> Ticket | None:
        return None


class InMemoryTracker:
    """A dict of tickets. The fake."""

    __slots__ = ("_by_work_item", "_clock", "_comments", "_fail_with", "_next", "_tickets")

    def __init__(self, clock: Clock = utc_now) -> None:
        self._clock = clock
        self._tickets: dict[str, Ticket] = {}
        self._by_work_item: dict[str, str] = {}
        self._comments: list[tuple[str, str]] = []
        self._next = 0
        self._fail_with: BaseException | None = None

    def fail_with(self, error: BaseException | None) -> None:
        self._fail_with = error

    def _check(self) -> None:
        if self._fail_with is not None:
            raise self._fail_with

    async def ensure(
        self,
        *,
        work_item_id: WorkItemId,
        title: str,
        body: str,
        repo_id: RepoId | None = None,
        labels: Sequence[str] = (),
    ) -> Ticket:
        self._check()
        existing = self._by_work_item.get(work_item_id)
        if existing is not None:
            return self._tickets[existing]

        self._next += 1
        now = self._clock()
        ticket = Ticket(
            id=f"TICKET-{self._next}",
            work_item_id=work_item_id,
            repo_id=repo_id,
            title=title,
            labels=tuple(labels),
            url=f"https://tracker.invalid/TICKET-{self._next}",
            created_at=now,
            updated_at=now,
        )
        self._tickets[ticket.id] = ticket
        self._by_work_item[work_item_id] = ticket.id
        return ticket

    async def transition(self, ticket_id: str, state: TicketState) -> Ticket:
        self._check()
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise PermanentError("unknown-ticket", f"no ticket with id {ticket_id!r}")
        if ticket.state is state:
            return ticket
        moved = ticket.model_copy(update={"state": state, "updated_at": self._clock()})
        self._tickets[ticket_id] = moved
        return moved

    async def comment(self, ticket_id: str, text: str, *, run_id: RunId | None = None) -> None:
        self._check()
        if ticket_id not in self._tickets:
            raise PermanentError("unknown-ticket", f"no ticket with id {ticket_id!r}")
        self._comments.append((ticket_id, text))

    async def find(self, work_item_id: WorkItemId) -> Ticket | None:
        self._check()
        ticket_id = self._by_work_item.get(work_item_id)
        return None if ticket_id is None else self._tickets[ticket_id]

    @property
    def tickets(self) -> Sequence[Ticket]:
        return tuple(self._tickets.values())

    def comments_on(self, ticket_id: str) -> Sequence[str]:
        return tuple(text for target, text in self._comments if target == ticket_id)
