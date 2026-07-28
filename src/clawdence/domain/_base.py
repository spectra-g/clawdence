"""The base every domain type derives from.

One configuration, applied everywhere, because the domain model is the spine:
if two types disagree about whether unknown fields are an error, the JSON
Schema generated from them disagrees too.

Three choices are load-bearing:

``extra="forbid"``
    An unknown field is a bug — a renamed field, a stale workflow YAML, a
    runner speaking an older protocol. Accepting it silently is how a typo in
    ``timeout_seconds`` becomes a step with no timeout. It also makes the
    generated JSON Schema closed (``additionalProperties: false``), which is
    what lets a schema consumer reject the same input we would.

``frozen=True``
    Domain values are records, not mutable state. State lives in the store
    (S4); passing a ``RepoProfile`` to a runner must not let the runner's code
    path mutate the caller's copy.

``str_strip_whitespace=False``
    Deliberately *off*. ``WorkItem.raw_text`` is preserved verbatim — v1's
    ``slackMessageRaw`` lesson was that normalising request text loses the
    repo-routing signal. Whitespace is content until proven otherwise.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Immutable, closed record with no behaviour."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        str_strip_whitespace=False,
        use_enum_values=False,
    )
