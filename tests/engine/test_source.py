"""S3c source positions: a value in the document, a line in the file."""

from __future__ import annotations

from textwrap import dedent

from clawdence.engine.source import SourceMap

DOCUMENT = dedent(
    """\
    schema_version: 1
    name: demo
    stages:
      - id: build
        type: script
        command:
          - make
          - build
      - id: check
        type: script
        when: '$build.succeeded'
        command: [make, check]
    """
)


class TestLines:
    def test_a_top_level_key_is_its_own_line(self) -> None:
        source = SourceMap.from_text(DOCUMENT)
        assert source.line(("name",)) == 2

    def test_a_sequence_item_is_the_line_it_starts_on(self) -> None:
        source = SourceMap.from_text(DOCUMENT)
        assert source.line(("stages", 0)) == 4
        assert source.line(("stages", 1)) == 9

    def test_a_field_reports_its_key_not_its_value(self) -> None:
        # `command:` is on line 6; the list under it starts on 7. The author
        # looking for "the command is wrong" looks at 6.
        source = SourceMap.from_text(DOCUMENT)
        assert source.line(("stages", 0, "command")) == 6

    def test_an_element_inside_a_field_has_its_own_line(self) -> None:
        source = SourceMap.from_text(DOCUMENT)
        assert source.line(("stages", 0, "command", 1)) == 8

    def test_a_flow_sequence_collapses_onto_one_line(self) -> None:
        source = SourceMap.from_text(DOCUMENT)
        assert source.line(("stages", 1, "command", 0)) == 12


class TestPartialPaths:
    def test_a_segment_that_is_not_in_the_document_is_stepped_over(self) -> None:
        """This is what pydantic hands us for a tagged union.

        ``stages.0.script.command`` has an ``script`` in it that was never a key
        — it is the discriminator. Stopping there would report the stage's line
        for an error about one field.
        """
        source = SourceMap.from_text(DOCUMENT)
        assert source.line(("stages", 0, "script", "command")) == 6

    def test_a_path_past_a_leaf_answers_with_the_leaf(self) -> None:
        source = SourceMap.from_text(DOCUMENT)
        assert source.line(("name", "nonsense", "deeper")) == 2

    def test_an_unknown_path_answers_with_the_document(self) -> None:
        source = SourceMap.from_text(DOCUMENT)
        assert source.line(("absent",)) == 1

    def test_the_empty_path_is_the_document(self) -> None:
        assert SourceMap.from_text(DOCUMENT).line(()) == 1


class TestDegenerateInput:
    def test_text_that_does_not_compose_has_no_positions(self) -> None:
        """The parse beside this one reports the syntax error, and better.

        Positions are a courtesy on top of an error; they never decide whether
        there is one.
        """
        source = SourceMap.from_text("name: demo\n  version: oops\n")
        assert source.lines == {}
        assert source.line(("name",)) is None

    def test_an_empty_document_has_no_positions(self) -> None:
        assert SourceMap.from_text("").lines == {}

    def test_a_non_scalar_key_is_ignored_rather_than_crashing(self) -> None:
        # YAML permits `? [a, b]` as a key. Nothing addresses it by path, and it
        # must not stop the rest of the document being mapped.
        source = SourceMap.from_text("? [a, b]\n: 1\nname: demo\n")
        assert source.line(("name",)) == 3
