"""Identifier and scalar types shared across the domain.

These are constrained ``str`` aliases rather than distinct classes. JSON Schema
cannot express "this string is a run id and not a repo id" either, so the
distinction would exist only in Python and would be lost the moment a value
crossed the wire — which, for a system whose contracts are the wire format, is
the wrong place to spend the complexity.

Two shapes, and the difference matters:

``Identifier``
    Ids *we* mint. Opaque, generated, never typed by a human.

``Slug``
    Names a *human writes* in workflow YAML — a workflow name, a stage id.
    Lowercase and narrow, because these appear in condition expressions
    (``$stage.json.field``) where a permissive character set would collide with
    the grammar (ADR-0003).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

#: Generated ids: opaque, and safe in a path segment or a log line.
Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]

#: Author-written names. Constrained to what the condition grammar can parse.
Slug = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$"),
]

WorkItemId = Identifier
RunId = Identifier
StageId = Slug
StepResultId = Identifier
RepoId = Identifier
EventId = Identifier

#: A git object name. Evidence binds to one of these and is invalid for any
#: other tree (S13) — so it is a full hash, never an abbreviation, since two
#: abbreviations that differ in length can name the same commit.
TreeHash = Annotated[str, StringConstraints(pattern=r"^([0-9a-f]{40}|[0-9a-f]{64})$")]

#: A condition expression in the grammar adopted from Lobster (ADR-0003):
#: ``==`` ``!=`` ``<`` ``>`` ``&&`` ``||`` ``!``, parens, and dotted paths into
#: a prior step's result. Parsing and evaluation are S3's; here it is a string.
Condition = Annotated[str, StringConstraints(min_length=1, max_length=1024)]

#: Semantic version of a workflow definition, pinned per run.
SemVer = Annotated[
    str,
    StringConstraints(pattern=r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$"),
]
