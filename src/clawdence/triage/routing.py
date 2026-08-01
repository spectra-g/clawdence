"""Two decisions: which process runs this, and which repository it lands in.

Both are made here, both are recorded with the reason that produced them, and
both can be overridden — but only from the *envelope*, never from the body. That
distinction is the whole security posture of this module, so it is worth stating
before anything else.

``WorkItem.raw_text`` is the most attacker-controlled string in the system: a
GitHub issue on a public repository is text a stranger wrote, and ``domain
.work_item`` says it "never selects the workflow, the repo, or the isolation
tier". Repository routing reads it anyway. The two are reconciled by what the
text is allowed to *be*:

**a selector over a closed set the operator configured, never a source of new
options.** The candidate list is ``Deployment.profiles``. Text can raise one
configured repository above another; it cannot name a repository that is not
there, cannot introduce a workflow, and cannot touch an isolation tier — the tier
comes from the profile, which the operator wrote. The worst outcome available to
a hostile issue is work proposed against the wrong repository the operator had
already trusted this system with, and it arrives as a pull request that a human
reviews.

Two further controls narrow even that. Ambiguity **refuses instead of guessing**:
a winner has to score above zero and beat the runner-up outright, and anything
else is reported as unrouted for a person to resolve. And the overrides —
``workflow_override``, ``repos`` — are fields an ingestion adapter fills from the
envelope it was handed, never from the text it carries; ``clawdence submit
--workflow`` sets one, and nothing parses the body looking for instructions.

**Raw text, not the title.** v1 routed off the BA's rewritten title and the
rewrite dropped product names — the ``slackMessageRaw`` lesson, which is the
reason ``raw_text`` is stored verbatim and the reason this reads it. A title is
a paraphrase, and a paraphrase is exactly where a product name goes missing.

**Classification revises only the default.** ``ingest.normalise`` assigns ``task``
when nobody said, on the grounds that it claims least and triage would decide
properly. This is that decision. It does *not* second-guess a submitter who
chose: a request that says it is a bug is a bug, because the person who typed
``--type bug`` knows something a keyword list does not.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from clawdence.domain import RepoProfile, WorkItem, WorkItemType
from clawdence.domain.ids import RepoId
from clawdence.ingest import DEFAULT_TYPE
from clawdence.triage.config import Routing as RoutingPolicy

#: What an alias match is worth against a keyword match. An alias is a *name for
#: this repository* — "the payments service", "checkout-api" — and a keyword is a
#: subject the repository owns. A request naming the thing beats a request
#: mentioning the topic, and three-to-one is enough that one alias outranks two
#: incidental keywords without making keywords decorative.
ALIAS_WEIGHT: Final = 3
KEYWORD_WEIGHT: Final = 1

#: Bulleted or numbered lines that make a request read as several pieces of work
#: rather than one. Three, because two is an ordinary "do this and that".
_ENUMERATED_MIN: Final = 3

_ENUMERATION = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE)

#: Signals per type, most-specific first within each list. Every entry is matched
#: on a word boundary against the lowercased raw text, and the one that fired is
#: reported — a classification nobody can explain is one nobody can correct.
_SPIKE_WORDS: Final = (
    "spike",
    "investigate",
    "investigation",
    "explore",
    "research",
    "find out",
    "look into",
    "feasibility",
    "evaluate",
    "compare",
    "worth doing",
)
_SPIKE_OPENERS: Final = (
    "how",
    "why",
    "what",
    "which",
    "when",
    "where",
    "who",
    "can",
    "could",
    "should",
    "would",
    "is",
    "are",
    "does",
    "do",
    "did",
    "has",
    "have",
)
_BUG_WORDS: Final = (
    "bug",
    "broken",
    "breaks",
    "regression",
    "crash",
    "crashes",
    "crashing",
    "traceback",
    "stack trace",
    "stacktrace",
    "exception",
    "segfault",
    "hangs",
    "deadlock",
    "leaks",
    "fails",
    "failing",
    "does not work",
    "doesn't work",
    "no longer works",
    "stopped working",
    "returns 500",
    "throws",
)
_EPIC_WORDS: Final = (
    "epic",
    "milestone",
    "roadmap",
    "overhaul",
    "rewrite",
    "re-architect",
    "rearchitect",
    "redesign",
    "migrate",
    "migration",
    "end to end",
    "end-to-end",
)
_STORY_WORDS: Final = (
    "as a user",
    "as an operator",
    "i want",
    "feature",
    "user story",
    "story",
    "add",
    "support for",
    "implement",
    "introduce",
    "expose",
    "allow",
)


@dataclass(frozen=True, slots=True)
class Decision:
    """One routed choice and the sentence that justifies it.

    ``reason`` is written to be read by a person looking at
    ``clawdence triage`` months later, and it is stored on the routing event.
    S11's brief asks for both decisions to be "logged and explicitly
    overridable"; a decision without its reason is logged in name only.
    """

    value: str | None
    reason: str

    #: True when the request said so and triage deferred to it. Separate from
    #: ``reason`` because a caller wants to branch on it — an override that
    #: turned out to be wrong is a different conversation from a scoring mistake.
    overridden: bool = False

    @property
    def resolved(self) -> bool:
        return self.value is not None


@dataclass(frozen=True, slots=True)
class Candidate:
    """One repository's case for this request, and what made it."""

    repo_id: RepoId
    score: int

    #: The terms that actually matched, in the order they were tried. This is
    #: what makes a wrong routing fixable: the answer to "why did it choose the
    #: web repo" is a list of words, and the fix is an edit to a profile.
    matched: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Routed:
    """Everything triage decided about one work item."""

    work_item_id: str

    #: The type after classification, which may not be the one that arrived.
    item_type: WorkItemType
    type_reason: str

    workflow: Decision
    repo: Decision

    #: Every repository that was considered, best first. Kept even when the
    #: decision is unrouted, because a tie is only diagnosable with the scores in
    #: hand.
    candidates: tuple[Candidate, ...] = ()

    @property
    def reclassified(self) -> bool:
        """Did triage revise the type the item arrived with?"""
        return self.type_reason != _SUBMITTED

    @property
    def routed(self) -> bool:
        """Is there both a workflow and a repository to act on?"""
        return self.workflow.resolved and self.repo.resolved

    def payload(self) -> JsonValue:
        """The ``WORK_ITEM_ROUTED`` audit payload.

        Metadata only, as ``store.audit``'s policy requires: ids, names, scores
        and reasons. The request itself is never in here — ``raw_text`` is what
        was *matched against*, and putting it in the log would move the system's
        most attacker-controlled string into the one table that is append-only.
        """
        return {
            "type": self.item_type.value,
            "type_reason": self.type_reason,
            "workflow": self.workflow.value,
            "workflow_reason": self.workflow.reason,
            "workflow_overridden": self.workflow.overridden,
            "repo": self.repo.value,
            "repo_reason": self.repo.reason,
            "repo_overridden": self.repo.overridden,
            "candidates": [
                {"repo": c.repo_id, "score": c.score, "matched": list(c.matched)}
                for c in self.candidates
            ],
        }


#: The ``type_reason`` for an item whose type triage did not touch.
_SUBMITTED: Final = "the request said so"


def route(
    item: WorkItem,
    *,
    profiles: Mapping[RepoId, RepoProfile],
    policy: RoutingPolicy | None = None,
) -> Routed:
    """Classify, pick a workflow, pick a repository. Touches nothing."""
    rules = policy or RoutingPolicy()
    item_type, type_reason = classify(item) if rules.classify else (item.type, _SUBMITTED)
    candidates = score(item.raw_text, profiles)
    return Routed(
        work_item_id=item.id,
        item_type=item_type,
        type_reason=type_reason,
        workflow=_workflow(item, item_type, rules),
        repo=_repo(item, profiles, candidates),
        candidates=candidates,
    )


def classify(item: WorkItem) -> tuple[WorkItemType, str]:
    """What kind of request this is, and which signal said so.

    Only ever consulted for an item still carrying ``ingest.normalise``'s
    default; anything else is returned untouched. See the module docstring.

    **The order is a cost argument, not a confidence one.** The question shape is
    tested first because routing a question to a workflow that writes code spends
    a coding budget and opens a pull request nobody asked for, while routing a
    feature request to ``spike`` produces a report — one of those mistakes is
    recoverable by reading. Everything after that runs most-specific first, and
    the last line is the default, which is where a request that says nothing
    recognisable belongs.
    """
    if item.type is not DEFAULT_TYPE:
        return item.type, _SUBMITTED

    text = item.raw_text.lower()

    asked = _question(item.raw_text)
    if asked is not None:
        return WorkItemType.SPIKE, asked
    found = _first(text, _SPIKE_WORDS)
    if found is not None:
        return WorkItemType.SPIKE, f"the text says {found!r}"

    found = _first(text, _BUG_WORDS)
    if found is not None:
        return WorkItemType.BUG, f"the text says {found!r}"

    found = _first(text, _EPIC_WORDS)
    if found is not None:
        return WorkItemType.EPIC, f"the text says {found!r}"
    listed = len(_ENUMERATION.findall(item.raw_text))
    if listed >= _ENUMERATED_MIN:
        return (
            WorkItemType.EPIC,
            f"the request lists {listed} separate items, which is more than one piece of work",
        )

    found = _first(text, _STORY_WORDS)
    if found is not None:
        return WorkItemType.STORY, f"the text says {found!r}"

    return DEFAULT_TYPE, "nothing in the text says otherwise"


def score(raw_text: str, profiles: Mapping[RepoId, RepoProfile]) -> tuple[Candidate, ...]:
    """Every repository's score against this request, best first.

    v1's ``resolve_repo``, with the id and the name added to the alias list. A
    request naming a repository by the name it is configured under should route
    there without anybody having to remember to repeat that name in ``aliases``,
    and forgetting to is the one configuration mistake this saves.

    Ties keep the order they came in, which is why the caller compares the top
    two rather than trusting the sort: a stable sort would hand back an arbitrary
    winner with a straight face.
    """
    candidates = [
        Candidate(repo_id=repo_id, score=points, matched=matched)
        for repo_id, profile in profiles.items()
        for points, matched in (_score_one(raw_text, profile),)
    ]
    return tuple(sorted(candidates, key=lambda candidate: -candidate.score))


def _score_one(raw_text: str, profile: RepoProfile) -> tuple[int, tuple[str, ...]]:
    matched: list[str] = []
    points = 0
    for term in dict.fromkeys((profile.id, profile.name, *profile.routing.aliases)):
        if term and _mentions(raw_text, term):
            matched.append(term)
            points += ALIAS_WEIGHT
    for term in profile.routing.keywords:
        if term and term not in matched and _mentions(raw_text, term):
            matched.append(term)
            points += KEYWORD_WEIGHT
    return points, tuple(matched)


def _workflow(item: WorkItem, item_type: WorkItemType, rules: RoutingPolicy) -> Decision:
    """The workflow this runs, from the override or from the type.

    An override that names a workflow which does not exist is *not* caught here:
    it is caught when the file is opened, by the loader, with a message about a
    file. Guessing at the set of valid names would mean this module listing a
    directory, which is a second answer to "which workflows exist" and therefore
    a second thing to keep in step.
    """
    if item.workflow_override:
        return Decision(
            value=item.workflow_override,
            reason=f"the request asked for {item.workflow_override!r}",
            overridden=True,
        )
    chosen = rules.by_type.get(item_type)
    if chosen is not None:
        return Decision(value=chosen, reason=f"a {item_type.value} routes to {chosen!r}")
    return Decision(
        value=rules.default,
        reason=(
            f"nothing routes a {item_type.value}, so this is the configured "
            f"default ({rules.default!r})"
        ),
    )


def _repo(
    item: WorkItem,
    profiles: Mapping[RepoId, RepoProfile],
    candidates: Sequence[Candidate],
) -> Decision:
    """Which repository this lands in, or why nothing does.

    The unmatched cases are separate messages rather than one, because they are
    separate problems with separate fixes: nothing configured is an install that
    was never finished, a scoreless request is a profile that needs a keyword, and
    a tie is two repositories that genuinely both look right. A single "could not
    route" would send all three to the same place.
    """
    if item.repos:
        named = item.repos[0]
        if named not in profiles:
            return Decision(
                value=None,
                reason=(
                    f"the request asked for repository {named!r}, which this deployment "
                    f"does not have — it knows {', '.join(sorted(profiles)) or 'none'}"
                ),
                overridden=True,
            )
        return Decision(value=named, reason=f"the request asked for {named!r}", overridden=True)

    if not candidates:
        return Decision(
            value=None,
            reason=(
                "no repositories are configured, so there is nothing to route to; "
                "add one to the `repos` list with `clawdence probe --out`"
            ),
        )

    best = candidates[0]
    if len(candidates) == 1:
        # The walking skeleton's shape, and it is a real deployment rather than a
        # concession: one repository means routing has one answer, and demanding
        # a keyword match before using it would refuse the only choice available.
        return Decision(
            value=best.repo_id,
            reason=(
                f"{best.repo_id!r} is the only repository configured"
                if not best.matched
                else f"{best.repo_id!r} is the only repository configured, and the "
                f"request mentions {_terms(best.matched)}"
            ),
        )

    if best.score == 0:
        return Decision(
            value=None,
            reason=(
                f"the request mentions nothing that names any of the "
                f"{len(candidates)} configured repositories; say which with "
                f"`--repo`, or give the right profile an alias or a keyword"
            ),
        )

    runner_up = candidates[1]
    if best.score == runner_up.score:
        tied = [c.repo_id for c in candidates if c.score == best.score]
        return Decision(
            value=None,
            reason=(
                f"the request names {' and '.join(repr(name) for name in tied)} "
                f"equally well ({best.score} each), and picking one of them would be "
                f"a guess; say which with `--repo`"
            ),
        )

    return Decision(
        value=best.repo_id,
        reason=(
            f"the request mentions {_terms(best.matched)}, which scores {best.score} "
            f"for {best.repo_id!r} against {runner_up.score} for {runner_up.repo_id!r}"
        ),
    )


def _question(raw_text: str) -> str | None:
    """Whether this reads as a question rather than an instruction.

    Two signals and they are not the same one. A trailing ``?`` is a question
    however it opens; an interrogative first word is a question even when
    somebody left the punctuation off, which in a chat message is most of the
    time.
    """
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith("?"):
            return "the request is phrased as a question"
        opener = re.split(r"[^a-z']+", stripped.lower(), maxsplit=1)[0]
        if opener in _SPIKE_OPENERS:
            return f"the request opens with {opener!r}, which asks rather than instructs"
        return None
    return None


def _first(text: str, terms: Sequence[str]) -> str | None:
    for term in terms:
        if _mentions(text, term):
            return term
    return None


def _mentions(text: str, term: str) -> bool:
    """A whole-word, case-insensitive match.

    Lookarounds rather than ``\\b`` because the terms include ids and package
    names — ``repo.api``, ``.net`` — and ``\\b`` is defined relative to word
    characters, so it lands in the wrong place the moment a term starts or ends
    with punctuation. Whole-word matters more than it looks: without it ``api``
    matches ``rapid`` and every repository named after a three-letter word wins
    every request.
    """
    pattern = rf"(?<![0-9A-Za-z_]){re.escape(term.lower())}(?![0-9A-Za-z_])"
    return re.search(pattern, text.lower()) is not None


def _terms(matched: Sequence[str]) -> str:
    return ", ".join(repr(term) for term in matched) or "nothing in particular"
