"""What normalisation does, and — mostly — what it refuses to do.

The failure mode of a normaliser is not an exception, it is a plausible
improvement: a tidied body, a summarised title, a guessed type. Each of those
answers a question the next reader was going to ask, and v1 paid for exactly one
of them when repository routing started reading a rewritten title.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from clawdence.domain import IngestSource, Submitter, WorkItem, WorkItemType
from clawdence.ingest import DEFAULT_TYPE, NormaliseError, normalise
from clawdence.ports.ingest import MAX_TITLE_CHARS

AT = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
WHO = Submitter(source=IngestSource.CLI, external_id="girish", trusted=True)


def build(raw_text: str, **kwargs: Any) -> WorkItem:
    return normalise(
        source=IngestSource.CLI,
        external_id="REQ-1",
        raw_text=raw_text,
        submitter=WHO,
        at=AT,
        **kwargs,
    )


class TestTheBodyIsUntouched:
    def test_it_is_stored_byte_for_byte(self) -> None:
        """The ``slackMessageRaw`` lesson: repository routing reads this field,
        not a paraphrase, and the rewrite is what dropped the product names."""
        body = "  Fix **Checkout-Pro**'s totals.\n\n\tThey round down.  \n"
        item = build(body)
        assert item.raw_text == body

    def test_leading_whitespace_is_not_ours_to_discard(self) -> None:
        """Only the title is taken from a stripped copy. Deciding that indented
        text is meaningless is a decision about somebody else's markdown."""
        item = build("    indented on purpose")
        assert item.raw_text.startswith("    ")
        assert item.title == "indented on purpose"


class TestTitles:
    def test_a_given_title_wins(self) -> None:
        item = build("some body text", title="Chosen by a human")
        assert item.title == "Chosen by a human"

    def test_it_is_taken_from_the_first_line(self) -> None:
        """A *selection* from the text, not a summary of it — which is what
        keeps it inside the rule above."""
        item = build("Fix the checkout total\n\nIt rounds down.")
        assert item.title == "Fix the checkout total"

    def test_leading_blank_lines_are_skipped(self) -> None:
        item = build("\n\n   \nFix the checkout total\nmore")
        assert item.title == "Fix the checkout total"

    def test_a_long_first_line_is_cut_visibly_and_at_a_word(self) -> None:
        """A hard cut mid-word reads as corruption; an unmarked one reads as a
        title somebody chose — and it would be the branch name and the PR title."""
        item = build(" ".join(["word"] * 200))
        title = item.title
        assert len(title) <= MAX_TITLE_CHARS
        assert title.endswith("…")
        assert not title.endswith("wor…")

    def test_a_blank_title_and_a_blank_body_are_refused_together(self) -> None:
        with pytest.raises(NormaliseError, match="no request here"):
            build("   \n\n ")


class TestDefaults:
    def test_the_type_claims_the_least(self) -> None:
        """v1 modelled Epic→Story only, so every request became an epic and went
        through full planning. A task that turns out to be an epic costs a
        reclassification; the other way costs a planning pipeline."""
        assert DEFAULT_TYPE is WorkItemType.TASK
        assert build("do a thing").type is DEFAULT_TYPE

    def test_nothing_fills_in_the_repositories(self) -> None:
        """Routing reads ``raw_text`` and is triage's. A guess made here would be
        in the record before anything had looked at a repository."""
        assert build("something about the checkout service").repos == ()

    def test_an_id_is_minted_when_none_is_given(self) -> None:
        """Safe because intake discards it on a redelivery — the stored id is the
        identity — and it is what lets an adapter build a complete item before
        knowing whether the request is new."""
        first = build("a")
        second = build("a")
        assert first.id != second.id


class TestTheEnvelope:
    def test_a_missing_source_id_is_refused(self) -> None:
        """Without one, a redelivery becomes a second work item — which is the
        single failure the whole ingestion design is shaped around."""
        with pytest.raises(NormaliseError, match="no source id"):
            normalise(
                source=IngestSource.CLI,
                external_id="  ",
                raw_text="do a thing",
                submitter=WHO,
                at=AT,
            )

    def test_the_conversation_and_url_are_carried(self) -> None:
        item = build("do a thing", conversation_id="thread-9", url="https://example.invalid/1")
        ref = item.source_ref
        assert (ref.conversation_id, ref.url) == ("thread-9", "https://example.invalid/1")
