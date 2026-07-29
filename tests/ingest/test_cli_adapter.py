"""The CLI adapter: identity, references, and the four verbs through it."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from clawdence.domain import IngestSource
from clawdence.ingest import cli as adapter
from clawdence.store import (
    IN_MEMORY,
    Disposition,
    Intake,
    StateStore,
    UnknownSubmissionError,
)

AT = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


@pytest.fixture
def intake() -> Iterator[Intake]:
    with StateStore.open(IN_MEMORY) as store:
        yield Intake(store)


class TestIdentity:
    def test_a_cli_submitter_is_trusted(self) -> None:
        """A statement about the CLI, not about the person. Whoever runs this
        already has the state database and the runner's credentials; a flag is
        not what stands between them and the system, and setting it otherwise
        would make it mean "we do not know" in the one case where we do."""
        who = adapter.cli_submitter("girish")
        assert (who.source, who.external_id, who.trusted) == (IngestSource.CLI, "girish", True)

    def test_no_identity_given_means_the_current_user(self) -> None:
        assert adapter.cli_submitter().external_id == adapter.whoami()

    def test_a_blank_identity_is_recorded_as_unknown(self) -> None:
        """Not as the current user. Somebody passed ``--as`` and gave nothing,
        and "who asked for this" answered wrongly is worse than not answered."""
        assert adapter.cli_submitter("   ").external_id == adapter.UNKNOWN_USER


class TestReferences:
    def test_a_minted_reference_is_fresh_per_invocation(self) -> None:
        """Typing the command twice means two requests. The tempting
        alternative — hashing the text — silently refuses two people who asked
        for the same thing on the same day."""
        assert adapter.mint_ref() != adapter.mint_ref()

    def test_minted_references_are_marked(self) -> None:
        """A listing should show at a glance which requests named themselves."""
        assert adapter.mint_ref().startswith(adapter.REF_PREFIX)

    def test_the_key_matches_the_port_rule(self) -> None:
        assert adapter.key("REQ-1") == "cli:REQ-1"


class TestVerbs:
    def test_the_same_ref_twice_is_one_request(self, intake: Intake) -> None:
        first = adapter.submit(intake, text="Fix the total", at=AT, ref="REQ-1")
        second = adapter.submit(intake, text="Fix the total", at=AT, ref="REQ-1")

        assert second.disposition is Disposition.DUPLICATE
        assert second.item.id == first.item.id

    def test_no_ref_means_a_new_request_each_time(self, intake: Intake) -> None:
        first = adapter.submit(intake, text="Fix the total", at=AT)
        second = adapter.submit(intake, text="Fix the total", at=AT)
        assert first.item.id != second.item.id

    def test_amend_replaces_the_content(self, intake: Intake) -> None:
        adapter.submit(intake, text="Fix the total", at=AT, ref="REQ-1")
        admission = adapter.submit(intake, text="Fix the tax line", at=AT, ref="REQ-1", amend=True)
        assert admission.disposition is Disposition.AMENDED
        assert admission.item.raw_text == "Fix the tax line"

    def test_amending_an_unknown_ref_does_not_create_one(self, intake: Intake) -> None:
        """The reason ``--amend`` exists as a verb rather than being inferred:
        here the person knows they are editing, so a mistyped reference is a
        typo and not a new piece of work."""
        with pytest.raises(UnknownSubmissionError):
            adapter.submit(intake, text="Fix the tax line", at=AT, ref="REQ-9", amend=True)

    def test_withdraw_takes_it_out_of_the_queue(self, intake: Intake) -> None:
        adapter.submit(intake, text="Fix the total", at=AT, ref="REQ-1")
        admission = adapter.withdraw(intake, "REQ-1", at=AT, reason="never mind")
        assert admission.disposition is Disposition.WITHDRAWN
        assert intake.collect() == ()

    def test_reply_threads_onto_the_conversation(self, intake: Intake) -> None:
        submitted = adapter.submit(
            intake, text="Flaky checkout test", at=AT, ref="REQ-1", conversation_id="thread-9"
        )
        admission, turn = adapter.reply(
            intake, "thread-9", body="Only on CI.", at=AT, author="girish"
        )
        assert admission.item.id == submitted.item.id
        assert intake.turns("cli:REQ-1") == (turn,)
