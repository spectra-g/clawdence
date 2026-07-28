"""Work items — what someone asked for, before anything has been decided.

v1 modelled Epic→Story only, so every request became an epic and went through
the full planning pipeline whether it needed it or not. Five types here, and
the type is what triage routes on.

``raw_text`` is the field to be careful with, in both directions:

*It is preserved verbatim.* v1 routed repos off the BA's rewritten title, and
the rewrite dropped product names — the ``slackMessageRaw`` lesson. Repo
routing reads this field, not a paraphrase.

*It is attacker-controlled.* A GitHub issue on a public repo is text a stranger
wrote, flowing into an agent prompt and then a runner with repo write access.
It is data, never instructions, and it never selects the workflow, the repo, or
the isolation tier.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from clawdence.domain._base import DomainModel
from clawdence.domain.ids import RepoId, WorkItemId


class WorkItemType(StrEnum):
    EPIC = "epic"
    STORY = "story"
    TASK = "task"
    BUG = "bug"
    SPIKE = "spike"


class IngestSource(StrEnum):
    CLI = "cli"
    SLACK = "slack"
    GITHUB = "github"
    WEBHOOK = "webhook"


class Submitter(DomainModel):
    """Who asked.

    ``trusted`` is deny-by-default and gates more than politeness: work from an
    untrusted submitter may not use ``container+docker:socket``, because socket
    access defeats both the egress allowlist and the plane split.
    """

    source: IngestSource
    external_id: str
    display_name: str | None = None
    trusted: bool = False


class SourceRef(DomainModel):
    """Where the item came from — and therefore where replies go.

    A request from GitHub is answered on GitHub. ``conversation_id``
    generalises v1's ``slackTs``: the BA asking a question and getting an
    answer is one conversation, not two unrelated events.
    """

    source: IngestSource

    #: Source-stable key. Ingestion is idempotent on this, because GitHub
    #: redelivers webhooks and Slack messages get edited.
    external_id: str

    conversation_id: str | None = None
    url: str | None = None


class WorkItem(DomainModel):
    """A request, normalised. Produced by every ingestion adapter."""

    id: WorkItemId
    type: WorkItemType
    title: str
    raw_text: str
    submitter: Submitter
    source_ref: SourceRef

    #: Target repositories. 1:N from the start (plan open question 10): v2.0
    #: only ever populates one, but widening this later would touch every
    #: consumer, and it is free now. Empty until triage resolves it.
    repos: tuple[RepoId, ...] = ()

    #: Epic→story, story→split-story. A tree, not a graph.
    parent_id: WorkItemId | None = None

    labels: tuple[str, ...] = ()

    #: Explicit overrides from the request. Triage logs both the routing
    #: decision and whether an override took precedence.
    workflow_override: str | None = None

    created_at: AwareDatetime
    size_estimate: str | None = Field(default=None, pattern=r"^(XS|S|M|L|XL)$")
