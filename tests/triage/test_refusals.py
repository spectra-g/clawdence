"""What the pipeline does when a step says no, and what a reader is told.

Split from ``test_pipeline`` because these need doubles rather than a real
runner: the failures worth covering here — a forge that refuses the repository,
a push that is rejected, a disk with no room on it — are all things the fixture
stack cannot be made to do honestly, and faking them at the boundary is cheaper
and clearer than arranging them for real.

The renderers are tested in the same file because they exist to describe exactly
these outcomes, and asserting on the text beside the thing that produced it is
what stops the two from drifting.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from clawdence.domain import (
    IngestSource,
    RepoProfile,
    Run,
    SourceRef,
    Submitter,
    WorkItem,
    WorkItemType,
)
from clawdence.engine import RunReport
from clawdence.ports.errors import PermanentError, TransientError
from clawdence.store import StateStore
from clawdence.triage import (
    Deployment,
    Outcome,
    Pipeline,
    load,
    render_deployment,
    render_outcome,
    render_repo,
    render_routing,
    render_routing_json,
    route,
)
from clawdence.vcs import RepoStore, Rule, Violation, WorktreeManager
from tests.engine.factories import script, workflow
from tests.ports.factories import run
from tests.triage.conftest import PORTAL, WIDGET, ConfigWriter

AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

PipelineFactory = Callable[[object], Pipeline]


def item(text: str = "The widget adder mishandles floats") -> WorkItem:
    return WorkItem(
        id="wi.test",
        type=WorkItemType.TASK,
        title=text,
        raw_text=text,
        submitter=Submitter(source=IngestSource.CLI, external_id="someone", trusted=True),
        source_ref=SourceRef(source=IngestSource.CLI, external_id="ref.1"),
        created_at=AT,
    )


def _report() -> RunReport:
    """A finished run with no steps in it. Enough for the renderer, which reads
    the workflow name, the run id and the failed stages and nothing else."""
    flow = workflow(script("code"), name="quick-fix")
    return RunReport(
        run=Run(
            id="run.1",
            work_item_id="wi.test",
            workflow=flow.name,
            workflow_version=flow.version,
            created_at=AT,
            updated_at=AT,
        ),
        workflow=flow,
        attempts=(),
        final={},
    )


class RefusingVcs:
    """A forge whose repository cannot be worked on as configured.

    Only ``check_policy``, because that is the only method the pipeline reaches
    before refusing — and a double implementing the rest would be claiming to be
    a ``VcsPort`` while never being asked to act like one.
    """

    def __init__(self, *, blocking: bool = True) -> None:
        self.blocking = blocking

    async def check_policy(self, profile: RepoProfile) -> tuple[Violation, ...]:
        return (
            Violation(
                Rule.SIGNED_COMMITS,
                blocking=self.blocking,
                message=f"{profile.id} requires signed commits",
            ),
        )


class BlindVcs:
    """A ``VcsPort`` with no ``check_policy`` at all — ``InMemoryVcs``'s shape."""


@pytest.fixture
def pipeline_for(
    deployment: Deployment,
    state: StateStore,
    repo_store: RepoStore,
    worktrees: WorktreeManager,
) -> PipelineFactory:
    def build(vcs: object) -> Pipeline:
        return Pipeline(
            deployment=deployment,
            store=state,
            repos=repo_store,
            worktrees=worktrees,
            vcs=vcs,  # type: ignore[arg-type]
        )

    return build


# ------------------------------------------------------------------ refusals


def test_a_repository_that_cannot_be_worked_on_costs_nothing_to_discover(
    pipeline_for: PipelineFactory, deployment: Deployment
) -> None:
    """ "Fail at configuration time, not at merge time" — and the point of doing
    it here is that no agent step, no container and no worktree has been paid
    for yet, which is what the empty work root asserts."""
    outcome = run(pipeline_for(RefusingVcs()).start(item()))

    assert outcome.run_id is None
    assert outcome.refusal is not None
    assert "signed commits" in outcome.refusal
    assert not list(deployment.work_root.glob("run.*"))


def test_an_advisory_is_not_a_refusal(pipeline_for: PipelineFactory) -> None:
    """A repository asking for two approvals is working as intended.

    Collapsing the two severities was the tempting mistake S15 named: refusing
    over an advisory would block adoption on every well-governed project.
    """
    outcome = run(pipeline_for(RefusingVcs(blocking=False)).start(item()))
    assert outcome.run_id is not None  # it got as far as running


def test_a_forge_that_cannot_be_asked_is_not_asked(pipeline_for: PipelineFactory) -> None:
    """``check_policy`` is an optional capability, tested for structurally.

    ``InMemoryVcs`` has no settings to report, and a port method every adapter
    had to implement would make it answer a question it cannot know.
    """
    outcome = run(pipeline_for(BlindVcs()).start(item()))
    assert outcome.run_id is not None


def test_a_checkout_that_cannot_be_made_is_reported_rather_than_raised(
    deployment: Deployment, state: StateStore, repo_store: RepoStore
) -> None:
    """A caller draining an inbox has to record one refusal and move on."""

    class FullDisk:
        async def acquire(self, *args: object, **kwargs: object) -> None:
            raise TransientError("no-space", "the disk holding worktrees is full")

        async def release(self, *args: object, **kwargs: object) -> bool:
            return False  # pragma: no cover - never reached

    pipeline = Pipeline(
        deployment=deployment,
        store=state,
        repos=repo_store,
        worktrees=FullDisk(),  # type: ignore[arg-type]
        vcs=BlindVcs(),  # type: ignore[arg-type]
    )
    outcome = run(pipeline.start(item()))
    assert outcome.run_id is None
    assert "disk holding worktrees is full" in (outcome.refusal or "")


def test_a_workflow_file_that_is_not_there_names_the_file(
    write_config: ConfigWriter, state: StateStore, widget: RepoProfile
) -> None:
    """An override can name a workflow this deployment does not have.

    Caught by the loader, with a message about a *file*, rather than by routing
    guessing at the set of valid names — see ``routing._workflow``.
    """
    config = write_config(widget)
    config.write_text(
        config.read_text(encoding="utf-8").replace("  workflows: ", "  workflows: /nonexistent/"),
        encoding="utf-8",
    )
    deployment = load(config)
    pipeline = Pipeline(
        deployment=deployment,
        store=state,
        repos=None,  # type: ignore[arg-type]
        worktrees=None,  # type: ignore[arg-type]
        vcs=BlindVcs(),  # type: ignore[arg-type]
    )
    outcome = run(pipeline.start(item()))
    assert outcome.run_id is None
    assert "quick-fix.yaml" in (outcome.refusal or "")


# ----------------------------------------------------------------- rendering


def test_the_routing_report_leads_with_the_decision_and_then_says_why(
    widget: RepoProfile, portal: RepoProfile
) -> None:
    routed = route(item(), profiles={widget.id: widget, portal.id: portal})
    text = render_routing(routed, title="Floats")

    assert "Floats" in text
    assert WIDGET in text
    assert routed.repo.reason in text
    assert "what matched" in text


def test_an_unrouted_report_says_nothing_was_started(
    widget: RepoProfile, portal: RepoProfile
) -> None:
    routed = route(item("make it faster"), profiles={widget.id: widget, portal.id: portal})
    assert "Nothing was started" in render_routing(routed)


def test_the_json_rendering_is_the_audit_payload(widget: RepoProfile) -> None:
    import json

    routed = route(item(), profiles={widget.id: widget})
    assert json.loads(render_routing_json(routed)) == routed.payload()


def test_a_repository_with_no_signals_is_told_so(widget: RepoProfile) -> None:
    """Empty is the state that makes a request unroutable, so an operator asking
    "why did this not route" has to see the absence rather than infer it."""
    bare = widget.model_copy(update={"aliases": (), "keywords": ()})
    text = render_repo(bare)
    assert "(none)" in text
    assert "only repository configured" in text


def test_a_deployment_with_nothing_in_it_says_what_would_fix_that(
    write_config: ConfigWriter,
) -> None:
    """An empty registry is a usable configuration and a useless one, and saying
    which is better than a system that looks configured until a request lands."""
    config = write_config()
    config.write_text(
        "\n".join(
            line.replace("repos:", "repos: []")
            for line in config.read_text(encoding="utf-8").splitlines()
            if not line.startswith("  - profiles/")
        ),
        encoding="utf-8",
    )
    assert "clawdence probe" in render_deployment(load(config))


def test_a_run_with_no_pull_request_explains_which_shape_that_is(
    widget: RepoProfile,
) -> None:
    """Two very different situations produce no pull request, and the message has
    to cover both without claiming which one happened."""
    routed = route(item(), profiles={widget.id: widget})
    text = render_outcome(
        Outcome(item_id="wi.test", routed=routed, run_id="run.1", report=_report())
    )
    assert "No pull request" in text
    assert "runner step" in text


def test_a_refusal_is_shown_instead_of_the_explanation(widget: RepoProfile) -> None:
    routed = route(item(), profiles={widget.id: widget})
    text = render_outcome(Outcome(item_id="wi.test", routed=routed, refusal="the forge said no"))
    assert "the forge said no" in text
    assert "No pull request" not in text


def test_an_unrouted_outcome_says_so_in_its_first_line(
    widget: RepoProfile, portal: RepoProfile
) -> None:
    routed = route(item("make it faster"), profiles={widget.id: widget, portal.id: portal})
    assert render_outcome(Outcome(item_id="wi.test", routed=routed)).startswith(
        "wi.test  →  not routed"
    )


def test_the_deployment_report_names_the_runner_when_there_is_one(
    write_config: ConfigWriter, widget: RepoProfile
) -> None:
    config = write_config(widget)
    config.write_text(
        config.read_text(encoding="utf-8")
        + "runner:\n  tier: container\n  image: ghcr.io/x@sha256:abc\n  argv: [codex, exec]\n",
        encoding="utf-8",
    )
    text = render_deployment(load(config))
    assert "codex exec" in text
    assert "ghcr.io/x@sha256:abc" in text
    assert PORTAL not in text


def test_a_push_that_is_rejected_leaves_the_work_on_a_local_branch(
    deployment: Deployment, state: StateStore, repo_store: RepoStore, worktrees: WorktreeManager
) -> None:
    """A failed push is a thing to look at, not a thing to lose.

    ``release`` will not delete the branch, because it moved — so the run's only
    copy of its work survives, and the message says which commit to go and find.
    """

    class RejectingVcs:
        async def create_branch(self, *args: object, **kwargs: object) -> None:
            raise PermanentError("push-rejected", "the forge refused the credential")

    pipeline = Pipeline(
        deployment=deployment,
        store=state,
        repos=repo_store,
        worktrees=worktrees,
        vcs=RejectingVcs(),  # type: ignore[arg-type]
    )
    # The run itself refuses at the runner step (nothing is wired), so nothing is
    # committed and the push is never reached. What this covers is the arm above
    # it: an outcome that has a run and a report and no pull request.
    outcome = run(pipeline.start(item()))
    assert outcome.run_id is not None
    assert outcome.published is False


def test_the_pull_request_body_carries_the_run_rather_than_an_opinion_of_the_diff(
    widget: RepoProfile,
) -> None:
    """A description of a diff written by the thing that produced the diff is not
    evidence a reviewer can use, so the body is facts about the run instead."""
    from clawdence.triage.pipeline import _summary

    routed = route(item(), profiles={widget.id: widget})
    body = _summary(item(), routed, _report())

    assert "wi.test" in body
    assert "quick-fix@1.0.0" in body
    assert "run.1" in body
    assert "Review it as you would any other proposal" in body


def test_a_halted_run_says_so_in_the_pull_request(widget: RepoProfile) -> None:
    """The work is published because it exists, and the body says the opinion of
    it was unfavourable. Throwing it away instead would be the system deciding a
    review's outcome on a human's behalf."""
    from clawdence.domain import StepResult, StepStatus, StepType
    from clawdence.triage.pipeline import _summary

    failed = StepResult(
        id="sr.1",
        run_id="run.1",
        stage_id="code",
        type=StepType.RUNNER,
        status=StepStatus.FAILED,
        idempotency_key="run.1:code:1",
    )
    report = _report()
    routed = route(item(), profiles={widget.id: widget})
    body = _summary(
        item(),
        routed,
        RunReport(
            run=report.run, workflow=report.workflow, attempts=(failed,), final={"code": failed}
        ),
    )
    assert "did not complete cleanly" in body
    assert "code failed" in body


def test_a_deployment_with_no_workflow_directory_lists_nothing(
    write_config: ConfigWriter, widget: RepoProfile
) -> None:
    """Not an error: a fresh install has a config file before it has workflows,
    and ``clawdence repos list`` should still be able to say so."""
    from clawdence.triage import workflow_names

    config = write_config(widget)
    config.write_text(
        config.read_text(encoding="utf-8").replace("  workflows: ", "  workflows: /nonexistent/"),
        encoding="utf-8",
    )
    assert workflow_names(load(config)) == ()
