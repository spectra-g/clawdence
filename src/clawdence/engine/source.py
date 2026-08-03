"""Where in the file a value was written.

``yaml.safe_load`` returns data with no memory of the document it came from, so
a load error can name the stage but not the line — and "stage ``review`` refers
to a stage that does not exist" in a 150-line file is an instruction to go and
count. The fix is a second pass: ``yaml.compose`` produces the same document as
a node tree, and every node carries its start mark.

The map is keyed by **document path** — ``("stages", 3, "command", 1)`` — for
one reason: pydantic's ``ValidationError`` already reports errors by exactly
that path, so every schema error gets a line without the loader knowing anything
about where it came from. The loader's own checks then use the same keys.

Lookup is by longest known prefix, so a path that runs past a leaf still answers
with the nearest enclosing line rather than nothing. Two cases need it and both
are ordinary: pydantic inserts the discriminator tag into the path for a tagged
union (``("stages", 0, "script", "command")``), and a reference inside an
interpolated string has no node of its own.

Composing is a second parse of the same text, and that is deliberate: the
alternative — parsing once and constructing data from the node tree ourselves —
would put a hand-written YAML resolver on the path every workflow file takes,
so the file the engine executes could differ from the file ``yaml.safe_load``
describes. Positions are for error messages; nothing is executed from them.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

import yaml

#: One path segment: a mapping key or a sequence index.
type Segment = str | int
#: A path *into a document*, never a path in a filesystem — which is why it is
#: not called ``Path`` in a module that its callers import beside ``pathlib``.
type DocumentPath = tuple[Segment, ...]

#: Composing a hostile document is bounded by the same rule the loader uses for
#: everything else — cost nothing before the run starts. A file too deep to walk
#: simply has no positions; it will still be rejected on its merits.
_MAX_DEPTH: Final = 100


@dataclass(frozen=True, slots=True)
class SourceMap:
    """1-based line numbers for the values in one YAML document."""

    lines: dict[DocumentPath, int]

    @classmethod
    def from_text(cls, text: str) -> SourceMap:
        """Build a map, or an empty one if the text does not compose.

        Empty rather than raising: this runs beside a parse that has its own
        error handling, and a document that fails here fails there too — with a
        better message. Positions are a courtesy on top of an error, never the
        thing that decides whether there is one.
        """
        try:
            root = yaml.compose(text)
        except yaml.YAMLError:
            return cls(lines={})
        if root is None:
            return cls(lines={})
        return cls(lines=dict(_walk(root, path=(), depth=0)))

    def line(self, path: Sequence[Segment]) -> int | None:
        """The line of the value at ``path``, or the deepest part of it that is real.

        A segment with no node of its own is stepped over rather than ending the
        walk, because the segment that is not in the document is usually in the
        middle: pydantic writes the discriminator into the path of a tagged
        union, so ``stages.1.agent.max_turns`` has to reach ``max_turns`` past an
        ``agent`` that was never a key. Answering with the enclosing stage would
        be true and useless — the author is being told about one field.
        """
        best = self.lines.get(())
        current: DocumentPath = ()
        for segment in path:
            candidate = (*current, segment)
            found = self.lines.get(candidate)
            if found is None:
                continue
            current, best = candidate, found
        return best


def _walk(node: yaml.Node, *, path: DocumentPath, depth: int) -> Iterator[tuple[DocumentPath, int]]:
    yield path, node.start_mark.line + 1
    if depth >= _MAX_DEPTH:  # pragma: no cover - depth-limited documents are rejected anyway
        return
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode):
                # The *key's* mark, not the value's. `command:` on line 12 with
                # its list starting on line 13 should report 12: that is the
                # line an author looks for when told the field is wrong.
                yield from _walk(value, path=(*path, key.value), depth=depth + 1)
                yield (*path, key.value), key.start_mark.line + 1
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            yield from _walk(item, path=(*path, index), depth=depth + 1)
