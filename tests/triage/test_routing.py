"""S11's two decisions, and the verify it was written against.

The step's verification is two sentences and both are here:

  *Three inputs — a feature request, a one-line bug, a research question — route
  to three different workflows.*

  *A request naming a product routes to the right repo; the same request
  paraphrased still routes correctly because raw text is retained.*

The second one is the ``slackMessageRaw`` lesson, and testing it needs a
paraphrase that is *worse* than the original in exactly the way v1's was: a title
that drops the product name. So the paraphrase test builds an item whose title
says nothing useful and whose ``raw_text`` still names the repository, and
asserts the routing is unchanged — which can only be true if the scorer never
looked at the title.

Everything else in here is about the decisions this module refuses to make.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clawdence.domain import (
    IngestSource,
    RepoProfile,
    SourceRef,
    Submitter,
    WorkItem,
    WorkItemType,
)
from clawdence.ingest import DEFAULT_TYPE
from clawdence.triage import Routing, classify, route, score
from tests.triage.conftest import PORTAL, WIDGET

AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def item(
    text: str,
    *,
    title: str | None = None,
    item_type: WorkItemType = WorkItemType.TASK,
    workflow_override: str | None = None,
    repos: tuple[str, ...] = (),
    trusted: bool = True,
) -> WorkItem:
    """A work item as ``ingest.normalise`` would have produced it.

    ``type`` defaults to ``TASK`` because that is what ingestion assigns when
    nobody said, and it is the only value triage is allowed to revise — a test
    that defaulted to something else would be testing the path where
    classification does nothing.
    """
    return WorkItem(
        id="wi.test",
        type=item_type,
        title=title or text.splitlines()[0][:80],
        raw_text=text,
        submitter=Submitter(source=IngestSource.CLI, external_id="someone", trusted=trusted),
        source_ref=SourceRef(source=IngestSource.CLI, external_id="ref.1"),
        workflow_override=workflow_override,
        repos=repos,
        created_at=AT,
    )


# ---------------------------------------------------------------- the verify


def test_three_kinds_of_request_route_to_three_workflows(
    widget: RepoProfile, portal: RepoProfile
) -> None:
    """S11's first verify criterion, as one assertion.

    All three go through the same function with the same configuration; nothing
    about the engine, the pipeline or the workflow files distinguishes them. The
    only input that differs is what somebody typed.
    """
    profiles = {widget.id: widget, portal.id: portal}
    feature = route(item("As a user I want the widget adder to accept floats."), profiles=profiles)
    bug = route(
        item("The portal login is broken — it throws on an empty password."),
        profiles=profiles,
    )
    question = route(
        item("Can the widget arithmetic overflow on 64-bit inputs?"), profiles=profiles
    )

    assert (feature.workflow.value, bug.workflow.value, question.workflow.value) == (
        "sprint",
        "quick-fix",
        "spike",
    )
    assert (feature.item_type, bug.item_type, question.item_type) == (
        WorkItemType.STORY,
        WorkItemType.BUG,
        WorkItemType.SPIKE,
    )


def test_a_paraphrased_request_still_finds_its_repository(
    widget: RepoProfile, portal: RepoProfile
) -> None:
    """S11's second criterion, and v1's ``slackMessageRaw`` bug written down.

    The title here is what a business analyst's rewrite produced: accurate,
    readable, and missing the one word that says where the work goes. Routing
    reads ``raw_text``, so the answer does not change — and if anything ever
    starts reading the title, this is the test that fails.
    """
    profiles = {widget.id: widget, portal.id: portal}
    original = "The widget adder should accept floating point numbers."
    named = route(item(original), profiles=profiles)
    paraphrased = route(
        item(original, title="Improve numeric handling in the service"),
        profiles=profiles,
    )

    assert named.repo.value == WIDGET
    assert paraphrased.repo.value == named.repo.value
    assert paraphrased.repo.reason == named.repo.reason


# ------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Why does the reaper delete live worktrees?", WorkItemType.SPIKE),
        ("Should we move the mirrors off the work root", WorkItemType.SPIKE),
        ("Investigate the flaky push test", WorkItemType.SPIKE),
        ("The merge check is broken on rebased branches", WorkItemType.BUG),
        ("push fails with a 403 after the token rotated", WorkItemType.BUG),
        ("Rewrite the scheduler to use a priority queue", WorkItemType.EPIC),
        ("As a user I want a dry run flag", WorkItemType.STORY),
        ("Add a --json flag to the reap command", WorkItemType.STORY),
        ("Bump the pinned ruff version", WorkItemType.TASK),
    ],
)
def test_the_classifier_reads_the_signal_it_says_it_read(text: str, expected: WorkItemType) -> None:
    kind, reason = classify(item(text))
    assert kind is expected
    assert reason, "every classification carries the signal that produced it"


def test_a_submitted_type_is_never_second_guessed() -> None:
    """``--type`` is a person saying something a keyword list does not know.

    The text here reads unmistakably like a question, and the submitter said it
    is a bug. Reclassifying would be the system overruling somebody who was
    looking at the same words and knew more.
    """
    kind, reason = classify(item("Why does login fail?", item_type=WorkItemType.BUG))
    assert kind is WorkItemType.BUG
    assert reason == "the request said so"


def test_a_list_of_deliverables_is_more_than_one_piece_of_work() -> None:
    kind, reason = classify(
        item("Please do the following:\n- rotate the token\n- fix the reaper\n- ship it")
    )
    assert kind is WorkItemType.EPIC
    assert "3 separate items" in reason


def test_classification_can_be_turned_off(widget: RepoProfile) -> None:
    """A deployment whose sources all set the type properly wants its word taken."""
    routed = route(
        item("Why does the reaper delete live worktrees?"),
        profiles={widget.id: widget},
        policy=Routing(classify=False),
    )
    assert routed.item_type is WorkItemType.TASK
    assert routed.reclassified is False


# ------------------------------------------------------------- repo scoring


def test_an_alias_outweighs_a_keyword(widget: RepoProfile, portal: RepoProfile) -> None:
    """A request naming the thing beats a request mentioning the topic.

    ``login`` is one of the portal's keywords and appears here; ``widget`` is one
    of the widget's aliases and appears once. Three-to-one is what makes the
    named repository win a sentence that is mostly about the other one's subject.
    """
    routed = route(
        item("The widget should not print login tokens in its debug output."),
        profiles={widget.id: widget, portal.id: portal},
    )
    assert routed.repo.value == WIDGET


def test_a_repository_wins_on_its_own_id_and_name_without_repeating_them(
    forge_url_profile: RepoProfile, portal: RepoProfile
) -> None:
    """The id and the name are implicit aliases.

    Forgetting to repeat a repository's own name in ``aliases`` is the one
    configuration mistake worth saving somebody from, because the resulting
    failure — "it does not route and I have no idea why" — looks like a bug in
    the scorer.
    """
    profiles = {forge_url_profile.id: forge_url_profile, portal.id: portal}
    assert (
        route(item("please fix ledger-service"), profiles=profiles).repo.value
        == forge_url_profile.id
    )


def test_a_substring_is_not_a_mention(portal: RepoProfile) -> None:
    """Whole words only, and this is why.

    ``sum`` is one of the widget's keywords. Without a word boundary it matches
    ``consumer``, ``assume`` and ``summary``, and a repository named after a
    short word would win every request in the queue.
    """
    narrow = RepoProfile.model_validate(
        {
            "id": WIDGET,
            "name": "widget",
            "remote_url": portal.remote_url,
            "keywords": ("sum",),
        }
    )
    profiles = {narrow.id: narrow, portal.id: portal}
    routed = route(
        item("Write a consumer summary and assume the portal defaults."),
        profiles=profiles,
    )
    assert routed.repo.value == PORTAL


def test_the_scores_are_reported_whether_or_not_anything_won(
    widget: RepoProfile, portal: RepoProfile
) -> None:
    scored = score("the widget adder and the portal login", {widget.id: widget, portal.id: portal})
    assert [candidate.repo_id for candidate in scored] == [WIDGET, PORTAL] or [
        candidate.repo_id for candidate in scored
    ] == [PORTAL, WIDGET]
    assert all(candidate.matched for candidate in scored)


# --------------------------------------------------------------- refusals


def test_a_tie_refuses_rather_than_picking_one(widget: RepoProfile, portal: RepoProfile) -> None:
    """Ambiguity is a decision, and this module does not make decisions.

    One alias each is a genuine tie: the request names both repositories exactly
    as well. Picking the first would be a guess with a straight face, and the
    person who wrote the request is the one who can resolve it in a second.
    """
    routed = route(
        item("Make the widget and the portal agree about timestamps."),
        profiles={widget.id: widget, portal.id: portal},
    )
    assert routed.repo.value is None
    assert routed.routed is False
    assert "--repo" in routed.repo.reason


def test_a_request_that_names_nothing_refuses_and_says_what_would_fix_it(
    widget: RepoProfile, portal: RepoProfile
) -> None:
    routed = route(
        item("Please make the thing faster."),
        profiles={widget.id: widget, portal.id: portal},
    )
    assert routed.repo.value is None
    assert "alias" in routed.repo.reason


def test_the_only_configured_repository_wins_without_being_named(
    widget: RepoProfile,
) -> None:
    """The walking skeleton's shape, and it is a real deployment.

    One repository means routing has one answer. Demanding a keyword match first
    would refuse the only choice available, which is the behaviour of a system
    that has confused caution with correctness.
    """
    routed = route(item("Please make the thing faster."), profiles={widget.id: widget})
    assert routed.repo.value == WIDGET
    assert "only repository" in routed.repo.reason


def test_no_repositories_configured_is_its_own_message() -> None:
    routed = route(item("anything at all"), profiles={})
    assert routed.repo.value is None
    assert "probe" in routed.repo.reason


# --------------------------------------------------------------- overrides


def test_the_envelope_can_name_the_repository(widget: RepoProfile, portal: RepoProfile) -> None:
    """``clawdence submit --repo`` wins over anything the text says.

    The text here names the widget three times and the override says portal. The
    override is a field an ingestion adapter filled from what it was *handed*,
    not from what it was *told* — which is the whole distinction the module
    docstring is about.
    """
    routed = route(
        item("widget widget-api adder", repos=(PORTAL,)),
        profiles={widget.id: widget, portal.id: portal},
    )
    assert routed.repo.value == PORTAL
    assert routed.repo.overridden is True


def test_an_override_naming_an_unconfigured_repository_refuses(
    widget: RepoProfile,
) -> None:
    """The closed set holds even for an override.

    This is the security property stated as a test: nothing outside
    ``Deployment.profiles`` is reachable, however the request asks. A hostile
    issue that names ``repo.production-secrets`` gets this message, not a run.
    """
    routed = route(
        item("widget", repos=("repo.production-secrets",)),
        profiles={widget.id: widget},
    )
    assert routed.repo.value is None
    assert routed.repo.overridden is True
    assert "does not have" in routed.repo.reason


def test_the_envelope_can_name_the_workflow(widget: RepoProfile) -> None:
    routed = route(
        item("Why does this happen?", workflow_override="quick-fix"),
        profiles={widget.id: widget},
    )
    assert routed.item_type is WorkItemType.SPIKE
    assert routed.workflow.value == "quick-fix"
    assert routed.workflow.overridden is True


def test_a_type_with_no_route_falls_back_to_the_configured_default(
    widget: RepoProfile,
) -> None:
    routed = route(
        item("anything"),
        profiles={widget.id: widget},
        policy=Routing(by_type={}, default="sprint"),
    )
    assert routed.workflow.value == "sprint"
    assert "default" in routed.workflow.reason


# ------------------------------------------------------------------ the log


def test_the_payload_carries_the_reasoning_and_not_the_request(
    widget: RepoProfile, portal: RepoProfile
) -> None:
    """``WORK_ITEM_ROUTED`` is metadata, per ``store.audit``'s policy.

    ``raw_text`` is the most attacker-controlled string in the system and the
    audit log is append-only. What is matched against never goes in it; what was
    decided does.
    """
    text = "The widget adder mishandles floats"
    payload = route(item(text), profiles={widget.id: widget, portal.id: portal}).payload()

    assert isinstance(payload, dict)
    assert payload["repo"] == WIDGET
    assert payload["candidates"]
    assert text not in str(payload)


@pytest.fixture
def forge_url_profile(portal: RepoProfile) -> RepoProfile:
    """A repository with no aliases and no keywords at all."""
    return RepoProfile.model_validate(
        {
            "id": "ledger-service",
            "name": "ledger-service",
            "remote_url": portal.remote_url,
        }
    )


def test_a_blank_request_is_not_a_question(widget: RepoProfile) -> None:
    """The interrogative check reads the *first non-empty* line, so leading blank
    lines — which is what a pasted issue body looks like — must not stop it."""
    kind, reason = classify(item("\n\n   \nWhy does the reaper delete live worktrees?"))
    assert kind is WorkItemType.SPIKE
    assert "question" in reason


def test_whitespace_only_text_falls_through_to_the_default() -> None:
    """``normalise`` refuses an empty request, so this is unreachable through
    ingestion — it is here because ``_question`` has to terminate on it rather
    than index into an empty list."""
    kind, _ = classify(item("   \n\n  ", title="a title"))
    assert kind is DEFAULT_TYPE
