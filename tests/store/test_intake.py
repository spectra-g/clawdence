"""The four verbs, and the lifecycle they move a request through.

The tests worth reading twice are the ones about *telling two arrivals apart*.
Everything a source sends looks the same on the wire — a webhook redelivery and
an edit are the same POST — so the whole design rests on comparing content
against what is stored, and on which fields are excluded from that comparison.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clawdence.domain import (
    EventKind,
    IngestSource,
    SourceRef,
    Submitter,
    WorkItem,
    WorkItemType,
)
from clawdence.ports.ingest import MAX_REQUEST_CHARS, MAX_TITLE_CHARS, SELF_ID
from clawdence.store import (
    ArrivalState,
    Disposition,
    Intake,
    StateStore,
    StoreIngest,
    SubmissionRejectedError,
    UnknownConversationError,
    UnknownSubmissionError,
)
from tests.ports.factories import run

START = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def item(
    item_id: str = "wi.1",
    *,
    external_id: str = "REQ-1",
    title: str = "Fix the checkout total",
    raw_text: str = "Fix the checkout total — it rounds the wrong way.",
    source: IngestSource = IngestSource.CLI,
    conversation_id: str | None = None,
    submitter: str = "girish",
    url: str | None = None,
    labels: tuple[str, ...] = (),
    item_type: WorkItemType = WorkItemType.TASK,
) -> WorkItem:
    return WorkItem(
        id=item_id,
        type=item_type,
        title=title,
        raw_text=raw_text,
        submitter=Submitter(source=source, external_id=submitter, trusted=True),
        source_ref=SourceRef(
            source=source,
            external_id=external_id,
            conversation_id=conversation_id,
            url=url,
        ),
        labels=labels,
        created_at=at(0),
    )


@pytest.fixture
def intake(state: StateStore) -> Intake:
    return Intake(state)


class TestFirstArrival:
    def test_a_new_request_is_created_and_pending(self, intake: Intake) -> None:
        admission = intake.submit(item(), at=at(0))
        assert admission.disposition is Disposition.CREATED
        assert admission.state is ArrivalState.PENDING
        assert admission.revision == 1
        assert admission.item.id == "wi.1"

    def test_it_is_immediately_collectable(self, intake: Intake) -> None:
        intake.submit(item(), at=at(0))
        assert [collected.id for collected in intake.collect()] == ["wi.1"]

    def test_arrival_order_is_collection_order(self, intake: Intake) -> None:
        """A backlog is worked through oldest first, and ``seq`` is what says so
        — a wall clock would make two arrivals in the same microsecond
        ambiguous exactly when somebody is asking which came first."""
        for index in range(3):
            intake.submit(item(f"wi.{index}", external_id=f"REQ-{index}"), at=at(0))
        assert [collected.id for collected in intake.collect()] == ["wi.0", "wi.1", "wi.2"]

    def test_the_audit_record_carries_the_envelope_and_not_the_body(
        self, intake: Intake, state: StateStore
    ) -> None:
        """``audit``'s policy is metadata only (S4), and ``raw_text`` is the most
        attacker-controlled string in the system. An append-only log cannot
        un-write it."""
        body = "the request text, which must not reach the log"
        intake.submit(item(raw_text=body), at=at(0))

        events = state.audit.read()
        assert [event.kind for event in events] == [EventKind.WORK_ITEM_RECEIVED]
        payload = events[0].payload
        assert isinstance(payload, dict)
        assert payload["disposition"] == "created"
        assert payload["dedupe_key"] == "cli:REQ-1"
        assert body not in str(payload)


class TestRedelivery:
    def test_the_same_request_twice_is_one_work_item(self, intake: Intake) -> None:
        """GitHub redelivers webhooks; Slack replays on reconnect. Our own
        minted id is fresh each time, which is why the source's is the key."""
        first = intake.submit(item("wi.1"), at=at(0))
        second = intake.submit(item("wi.2"), at=at(10))

        assert second.disposition is Disposition.DUPLICATE
        assert second.item.id == first.item.id == "wi.1"
        assert len(intake.collect()) == 1

    def test_a_redelivery_does_not_move_the_timestamp(
        self, intake: Intake, state: StateStore
    ) -> None:
        """Otherwise "when did this last change" quietly means "when did GitHub
        last retry", which is the wrong answer to the only question the column
        is read for."""
        intake.submit(item(), at=at(0))
        before = _updated_at(state)
        intake.submit(item("wi.2"), at=at(600))
        assert _updated_at(state) == before

    def test_a_redelivery_writes_no_second_audit_record(
        self, intake: Intake, state: StateStore
    ) -> None:
        intake.submit(item(), at=at(0))
        intake.submit(item("wi.2"), at=at(10))
        assert len(state.audit.read()) == 1


class TestAmendment:
    def test_changed_text_under_the_same_key_is_an_amendment(self, intake: Intake) -> None:
        intake.submit(item(), at=at(0))
        admission = intake.submit(
            item("wi.2", raw_text="Actually, it drops the tax line."), at=at(5)
        )

        assert admission.disposition is Disposition.AMENDED
        assert admission.revision == 2
        assert admission.item.raw_text == "Actually, it drops the tax line."

    def test_the_work_item_id_survives_an_amendment(self, intake: Intake) -> None:
        """Everything downstream refers to a request by this. An edit that
        renamed it would strand every reference."""
        first = intake.submit(item("wi.1"), at=at(0))
        amended = intake.submit(item("wi.2", raw_text="different"), at=at(5))
        assert amended.item.id == first.item.id == "wi.1"

    def test_the_submitter_is_not_transferred_by_an_edit(self, intake: Intake) -> None:
        """A request belongs to whoever made it. An arrival under the same key
        from somebody else is either a source doing something odd or an attempt
        to inherit one, and neither should silently rewrite the record."""
        intake.submit(item(submitter="girish"), at=at(0))
        amended = intake.submit(
            item("wi.2", raw_text="new text", submitter="somebody-else"), at=at(5)
        )
        assert amended.item.submitter.external_id == "girish"

    def test_a_moved_url_is_taken_from_the_arrival(self, intake: Intake) -> None:
        """The one part of ``source_ref`` a source may legitimately change."""
        intake.submit(item(url="https://example.invalid/1"), at=at(0))
        amended = intake.submit(
            item("wi.2", raw_text="new text", url="https://example.invalid/moved"), at=at(5)
        )
        assert amended.item.source_ref.url == "https://example.invalid/moved"
        assert amended.item.source_ref.external_id == "REQ-1"

    def test_amending_something_never_submitted_is_refused(self, intake: Intake) -> None:
        """``submit`` would create it. ``amend`` is the surface that *knows* it
        is editing, so a reference with a typo in it is a typo and not new work."""
        with pytest.raises(UnknownSubmissionError, match="REQ-9"):
            intake.amend(item(external_id="REQ-9"), at=at(0))

    def test_amending_with_the_same_text_is_a_no_op(self, intake: Intake) -> None:
        intake.submit(item(), at=at(0))
        admission = intake.amend(item("wi.2"), at=at(5))
        assert admission.disposition is Disposition.DUPLICATE
        assert admission.revision == 1

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("title", "A different title"),
            ("labels", ("urgent",)),
            ("item_type", WorkItemType.BUG),
        ],
    )
    def test_any_content_field_counts_as_a_change(
        self, intake: Intake, field: str, value: object
    ) -> None:
        """Not just the body. Somebody who relabels an issue or reclassifies it
        has changed the request, and a comparison that only read ``raw_text``
        would report a redelivery."""
        intake.submit(item(), at=at(0))
        admission = intake.submit(item("wi.2", **{field: value}), at=at(5))  # type: ignore[arg-type]
        assert admission.disposition is Disposition.AMENDED


class TestAcknowledgement:
    def test_acknowledging_ends_delivery(self, intake: Intake) -> None:
        intake.submit(item(), at=at(0))
        assert intake.acknowledge("wi.1", at=at(1)) == 1
        assert intake.collect() == ()

    def test_acknowledging_twice_is_not_an_error(self, intake: Intake) -> None:
        intake.submit(item(), at=at(0))
        assert intake.acknowledge("wi.1", at=at(1)) == 1
        assert intake.acknowledge("wi.1", at=at(2)) == 0
        assert intake.acknowledge("wi.never-seen", at=at(3)) == 0

    def test_it_records_which_revision_was_handed_on(self, intake: Intake) -> None:
        intake.submit(item(), at=at(0))
        intake.submit(item("wi.2", raw_text="v2"), at=at(1))
        intake.acknowledge("wi.1", at=at(2))

        admission = intake.get("cli:REQ-1")
        assert admission is not None
        assert (admission.revision, admission.acknowledged_revision) == (2, 2)

    def test_an_edit_after_acknowledgement_requeues_the_request(self, intake: Intake) -> None:
        """The alternative is that a correction sent thirty seconds too late is
        silently ignored — the worst option available, because the person who
        sent it has no way to find out."""
        intake.submit(item(), at=at(0))
        intake.acknowledge("wi.1", at=at(1))

        admission = intake.submit(item("wi.2", raw_text="on second thoughts"), at=at(2))
        assert admission.requeued is True
        assert admission.state is ArrivalState.PENDING
        assert [collected.id for collected in intake.collect()] == ["wi.1"]

    def test_the_requeue_keeps_what_was_handed_on(self, intake: Intake) -> None:
        """ "We ran revision 1 and they are now on revision 2" is the whole
        content of the warning the CLI prints."""
        intake.submit(item(), at=at(0))
        intake.acknowledge("wi.1", at=at(1))
        admission = intake.submit(item("wi.2", raw_text="on second thoughts"), at=at(2))
        assert (admission.revision, admission.acknowledged_revision) == (2, 1)

    def test_a_redelivery_after_acknowledgement_stays_quiet(self, intake: Intake) -> None:
        """The source redelivers on its own schedule, hours later, after a
        reconnect. If that restarted the work, nothing would ever finish."""
        intake.submit(item(), at=at(0))
        intake.acknowledge("wi.1", at=at(1))
        admission = intake.submit(item("wi.2"), at=at(3600))

        assert admission.disposition is Disposition.DUPLICATE
        assert admission.requeued is False
        assert intake.collect() == ()


class TestWithdrawal:
    def test_a_withdrawn_request_leaves_the_queue(self, intake: Intake) -> None:
        intake.submit(item(), at=at(0))
        admission = intake.withdraw("cli:REQ-1", reason="never mind", at=at(1))

        assert admission.disposition is Disposition.WITHDRAWN
        assert admission.state is ArrivalState.WITHDRAWN
        assert intake.collect() == ()

    def test_the_row_survives_the_withdrawal(self, intake: Intake) -> None:
        """ "We never received it" and "you took it back" are different answers,
        and a deleted row cannot tell them apart."""
        intake.submit(item(), at=at(0))
        intake.withdraw("cli:REQ-1", reason="never mind", at=at(1))
        assert intake.get("cli:REQ-1") is not None

    def test_withdrawing_after_acknowledgement_says_so(self, intake: Intake) -> None:
        """It still records — the person did ask — but ``acknowledged_revision``
        being set is how the caller knows to say "this had already been picked
        up" rather than "done". Stopping the work is a cancel, not this."""
        intake.submit(item(), at=at(0))
        intake.acknowledge("wi.1", at=at(1))
        admission = intake.withdraw("cli:REQ-1", reason="never mind", at=at(2))
        assert admission.acknowledged_revision == 1

    def test_withdrawing_something_never_submitted_is_refused(self, intake: Intake) -> None:
        with pytest.raises(UnknownSubmissionError):
            intake.withdraw("cli:REQ-9", reason="never mind", at=at(0))

    def test_resubmitting_a_withdrawn_request_reopens_it(self, intake: Intake) -> None:
        """An issue can be closed and reopened, and the reopened one is the same
        issue. A second work item here would be two epics for one request."""
        intake.submit(item(), at=at(0))
        intake.withdraw("cli:REQ-1", reason="never mind", at=at(1))
        admission = intake.submit(item("wi.2"), at=at(2))

        assert admission.disposition is Disposition.REOPENED
        assert admission.item.id == "wi.1"
        assert [collected.id for collected in intake.collect()] == ["wi.1"]

    def test_reopening_unchanged_text_does_not_bump_the_revision(self, intake: Intake) -> None:
        """Re-opening is a state change, not a new version of the text, and a
        revision that counted both would mean two things at once."""
        intake.submit(item(), at=at(0))
        intake.withdraw("cli:REQ-1", reason="never mind", at=at(1))
        assert intake.submit(item("wi.2"), at=at(2)).revision == 1

    def test_reopening_with_new_text_does_bump_it(self, intake: Intake) -> None:
        intake.submit(item(), at=at(0))
        intake.withdraw("cli:REQ-1", reason="never mind", at=at(1))
        assert intake.submit(item("wi.2", raw_text="rewritten"), at=at(2)).revision == 2


class TestConversations:
    def test_a_reply_attaches_to_the_request_that_owns_the_conversation(
        self, intake: Intake
    ) -> None:
        intake.submit(item(conversation_id="thread-9"), at=at(0))
        admission, turn = intake.reply(
            source=IngestSource.CLI,
            conversation_id="thread-9",
            body="Only on CI, never locally.",
            author="girish",
            at=at(5),
        )
        assert admission.item.id == "wi.1"
        assert turn.body == "Only on CI, never locally."

    def test_a_reply_does_not_create_a_work_item(self, intake: Intake) -> None:
        """The bug v1 had: a clarification answer arriving as a fresh request,
        producing two epics that both went through planning."""
        intake.submit(item(conversation_id="thread-9"), at=at(0))
        intake.reply(
            source=IngestSource.CLI,
            conversation_id="thread-9",
            body="Only on CI.",
            author="girish",
            at=at(5),
        )
        assert len(intake.list()) == 1
        assert [collected.id for collected in intake.collect()] == ["wi.1"]

    def test_a_reply_does_not_amend_the_request(self, intake: Intake) -> None:
        """A follow-up is not a correction. Folding it into ``raw_text`` would
        both destroy the verbatim body and re-queue work nobody asked to redo."""
        intake.submit(item(conversation_id="thread-9"), at=at(0))
        intake.acknowledge("wi.1", at=at(1))
        intake.reply(
            source=IngestSource.CLI,
            conversation_id="thread-9",
            body="Only on CI.",
            author="girish",
            at=at(5),
        )
        admission = intake.get("cli:REQ-1")
        assert admission is not None
        assert admission.revision == 1
        assert admission.state is ArrivalState.ACKNOWLEDGED

    def test_turns_come_back_in_order(self, intake: Intake) -> None:
        intake.submit(item(conversation_id="thread-9"), at=at(0))
        for index in range(3):
            intake.reply(
                source=IngestSource.CLI,
                conversation_id="thread-9",
                body=f"turn {index}",
                author="girish",
                at=at(index),
            )
        assert [turn.body for turn in intake.turns("cli:REQ-1")] == ["turn 0", "turn 1", "turn 2"]

    def test_a_reply_to_nothing_is_refused(self, intake: Intake) -> None:
        """A follow-up whose parent is missing is a routing bug or a stray
        message, and inventing a request out of half a conversation is worse
        than saying so."""
        with pytest.raises(UnknownConversationError, match="thread-9"):
            intake.reply(
                source=IngestSource.CLI,
                conversation_id="thread-9",
                body="anyone?",
                author="girish",
                at=at(0),
            )

    def test_a_conversation_resolves_to_its_most_recent_request(self, intake: Intake) -> None:
        """The same thread used twice. A reply belongs to the one being talked
        about now, not the one from three weeks ago."""
        intake.submit(item("wi.1", external_id="REQ-1", conversation_id="thread-9"), at=at(0))
        intake.submit(item("wi.2", external_id="REQ-2", conversation_id="thread-9"), at=at(10))
        admission, _ = intake.reply(
            source=IngestSource.CLI,
            conversation_id="thread-9",
            body="this one",
            author="girish",
            at=at(20),
        )
        assert admission.item.id == "wi.2"

    def test_an_empty_reply_is_refused(self, intake: Intake) -> None:
        intake.submit(item(conversation_id="thread-9"), at=at(0))
        with pytest.raises(SubmissionRejectedError, match="nothing in it"):
            intake.reply(
                source=IngestSource.CLI,
                conversation_id="thread-9",
                body="   \n ",
                author="girish",
                at=at(1),
            )


class TestRefusals:
    def test_the_system_will_not_ingest_its_own_output(self, intake: Intake) -> None:
        """Bot-loop prevention. The system posts to the channels it reads from:
        without this, its summary becomes work, which produces another summary."""
        with pytest.raises(SubmissionRejectedError, match="loop"):
            intake.submit(item(submitter=SELF_ID), at=at(0))

    def test_an_empty_request_is_refused(self, intake: Intake) -> None:
        with pytest.raises(SubmissionRejectedError, match="asks for nothing"):
            intake.submit(item(raw_text="   \n\t "), at=at(0))

    def test_an_oversized_request_is_refused_not_truncated(self, intake: Intake) -> None:
        """Half a request can ask for the opposite of the whole one."""
        with pytest.raises(SubmissionRejectedError, match="refused rather than truncated"):
            intake.submit(item(raw_text="x" * (MAX_REQUEST_CHARS + 1)), at=at(0))

    def test_an_oversized_title_is_refused(self, intake: Intake) -> None:
        with pytest.raises(SubmissionRejectedError, match="title"):
            intake.submit(item(title="t" * (MAX_TITLE_CHARS + 1)), at=at(0))

    def test_a_refused_arrival_leaves_nothing_behind(self, intake: Intake) -> None:
        with pytest.raises(SubmissionRejectedError):
            intake.submit(item(submitter=SELF_ID), at=at(0))
        assert intake.list() == ()


class TestDurability:
    def test_dedupe_survives_the_process_that_submitted(
        self, tmp_path: object, stores: object
    ) -> None:
        """The property the CLI adapter exists on. ``clawdence submit`` is one
        process and whatever acts on the request is another, so an in-memory
        guard would guard nothing — this is why intake is a table."""
        path = f"{tmp_path}/state.db"
        first: StateStore = stores(path)  # type: ignore[operator]
        Intake(first).submit(item(), at=at(0))
        first.close()

        second: StateStore = stores(path)  # type: ignore[operator]
        admission = Intake(second).submit(item("wi.2"), at=at(10))
        assert admission.disposition is Disposition.DUPLICATE
        assert admission.item.id == "wi.1"

    def test_the_unique_constraint_is_the_guard_not_a_python_check(
        self, intake: Intake, state: StateStore
    ) -> None:
        """Two processes racing the same first arrival both see no row. What
        stops the second is the database, which is why the key is UNIQUE."""
        intake.submit(item(), at=at(0))
        columns = state.connection.execute("PRAGMA index_list(intake)").fetchall()
        assert any(row["unique"] for row in columns)


class TestStoreIngestPort:
    """The port surface. The contract suite proper is in ``tests/ports``."""

    def test_close_does_not_close_the_store(self, state: StateStore) -> None:
        """The store outlives the adapter — it is holding runs and steps, and an
        ingest adapter shutting it down would take the record with it."""
        adapter = StoreIngest(state)
        run(adapter.close())
        assert adapter.closed is True
        assert state.list_runs() == ()

    def test_collect_and_acknowledge_go_through_to_the_table(self, state: StateStore) -> None:
        adapter = StoreIngest(state)
        adapter.intake.submit(item(), at=at(0))
        assert [collected.id for collected in run(adapter.collect())] == ["wi.1"]
        assert run(adapter.acknowledge("wi.1")) == 1
        assert run(adapter.collect()) == ()


def _updated_at(state: StateStore) -> str:
    value: str = state.connection.execute("SELECT updated_at FROM intake").fetchone()[0]
    return value
