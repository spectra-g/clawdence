"""M1's goal, as a test: a submitted request goes in and a pull request comes out.

Nothing here is stubbed except the model and the forge's HTTP API, both for the
reasons the suite has always given: an LLM costs money and needs a network, and
GitHub needs an account. Everything else is the product. The request goes through
``clawdence.ingest`` into a real SQLite intake; triage reads it; the worktree is a
real ``git worktree`` off a real mirror of a real bare repository; the runner is
the real ``HostRunner`` driving a controllable subprocess; and the branch that
gets pushed is checked by reading it back out of the remote.

Read ``tests/vcs/test_pipeline`` beside this. That file wrote the same sequence
by hand because nothing in the product performed it, and every line of it was a
call a human made. Here the only calls are ``submit`` and ``start``.

The workflow used throughout is ``examples/quick-fix.yaml`` — the shipped one,
not a fixture. A pipeline test against a workflow written in the test file would
prove the pipeline works on workflows the test author wrote.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from clawdence.domain import IsolationTier, RepoProfile, RunStatus, WorkItem
from clawdence.ingest import cli as ingest_cli
from clawdence.runners import HostRunner
from clawdence.store import Intake, StateStore
from clawdence.triage import Deployment, Pipeline, acknowledge, load
from clawdence.vcs import GhVcs, RepoStore, WorktreeManager
from clawdence.vcs.git import git
from tests.harness.agent import FakeAgent
from tests.harness.forge import Forge
from tests.ports.factories import run
from tests.triage.conftest import WIDGET, ConfigWriter

AT = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

PipelineFactory = Callable[[FakeAgent], Pipeline]

#: A test report the verdict reader accepts. The same shape ``tests/runners``
#: uses: a `passed` count on its own is not evidence, because the contract asks
#: which reporter produced it and how many tests there were in total.
TESTS_PASSED = {"reporter": "pytest-json-report", "total": 4, "passed": 4}


@pytest.fixture
def host_widget(forge: Forge) -> RepoProfile:
    """The widget, running on the host tier.

    Spelled out rather than defaulted, as ``tests/runners/conftest`` does: the
    host runner refuses anything else, so a test that uses it has to have said
    so. The container tier is exercised against a real daemon in
    ``make docker-tests``; what this file is about is the sequence, and the
    sequence is identical either side of that choice.
    """
    return RepoProfile.model_validate(
        {
            "id": WIDGET,
            "name": "widget",
            "remote_url": forge.url,
            "default_branch": "main",
            "isolation_tier": IsolationTier.HOST,
            "aliases": ("widget", "widget-api"),
            "keywords": ("adder", "arithmetic"),
        }
    )


@pytest.fixture
def host_deployment(write_config: ConfigWriter, host_widget: RepoProfile) -> Deployment:
    """One repository, so routing has one answer and this file is about the rest."""
    return load(write_config(host_widget))


@pytest.fixture
def pipeline_for(host_deployment: Deployment, state: StateStore, forge: Forge) -> PipelineFactory:
    """Builds a pipeline around a given agent program.

    A factory rather than a fixture taking ``agent``, and the reason is a real
    trap: ``FakeAgent.command()`` serialises the program into *argv*, so it is
    frozen at the moment the runner is built. A test that took a ready-made
    pipeline and then edited the agent would silently run the original program
    and assert against it — passing, or failing for a reason that has nothing to
    do with the change. Making the agent an argument means it cannot be changed
    after the fact.
    """

    def build(agent: FakeAgent) -> Pipeline:
        store = RepoStore(root=host_deployment.repo_store, token_name=None)
        return Pipeline(
            deployment=host_deployment,
            store=state,
            repos=store,
            worktrees=WorktreeManager(
                store=store, work_root=host_deployment.work_root, min_free_mb=0
            ),
            vcs=GhVcs(
                store=store,
                profiles=host_deployment.profiles,
                gh_path=forge.gh,
                token_name=None,
                environ={"PATH": _path(), "HOME": str(forge.root)},
            ),
            runner=HostRunner(agent.command()),
        )

    return build


@pytest.fixture
def pipeline(pipeline_for: PipelineFactory) -> Pipeline:
    """The happy path, wired. Most tests want this one."""
    return pipeline_for(working())


def working() -> FakeAgent:
    """An agent that does the work and says the tests passed.

    ``commit`` is called explicitly, because not calling it is §3.7a's dropped
    commit and there is a test below for that. ``verdict`` is what satisfies the
    ``TEST_AFTER`` contract the pipeline dispatches with — a run that only edited
    files reports ``TESTS_FAILED``, which is the contract doing its job.
    """
    return (
        FakeAgent()
        .say("reading the request")
        .write("app.py", "def add(a, b):\n    return float(a) + float(b)\n")
        .commit("handle floats")
        .verdict(status="passed", tests=TESTS_PASSED)
    )


def _path() -> str:
    return os.environ.get("PATH", "")


def submit(state: StateStore, text: str, **kwargs: object) -> str:
    """One request through the real ingestion path. Returns the work item id."""
    admission = ingest_cli.submit(Intake(state), text=text, at=AT, **kwargs)  # type: ignore[arg-type]
    return admission.item.id


def item_for(state: StateStore, item_id: str) -> WorkItem:
    admission = Intake(state).for_work_item(item_id)
    assert admission is not None
    return admission.item


# ------------------------------------------------------------------ the goal


def test_a_submitted_request_becomes_a_pull_request(
    pipeline: Pipeline, state: StateStore, forge: Forge
) -> None:
    """M1's stated goal: CLI in → one repo, one workflow, runner → PR out.

    Every assertion below is about something that left the process: a ref on the
    remote, a pull request in the forge's records, and the file contents git
    reports for the branch that was pushed. A test that asserted on the
    ``Outcome`` alone would pass if the publishing half were a no-op.
    """
    item_id = submit(state, "The widget adder should accept floating point numbers.")
    outcome = run(pipeline.start(item_for(state, item_id)))

    assert outcome.refusal is None, outcome.refusal
    assert outcome.succeeded is True
    assert outcome.published is True

    pull = outcome.pull_request
    assert pull is not None
    assert pull.base_branch == "main"
    assert pull.head_branch.startswith("clawdence/")
    assert pull.work_item_id == item_id

    published = run(git(forge.remote, "show", f"refs/heads/{pull.head_branch}:app.py"))
    assert "float(a)" in published


def test_the_workflow_it_chose_is_the_one_the_run_records(
    pipeline: Pipeline, state: StateStore
) -> None:
    """Triage's decision and the run's record are the same decision.

    The routed workflow is the file that was loaded, and the run row names it.
    Nothing re-derives it, which is what stops the two from disagreeing.
    """
    item_id = submit(state, "The widget adder should accept floating point numbers.")
    outcome = run(pipeline.start(item_for(state, item_id)))

    assert outcome.routed.workflow.value == "quick-fix"
    stored = state.get_run(outcome.run_id or "")
    assert stored is not None
    assert stored.workflow == "quick-fix"
    assert stored.repo_id == WIDGET
    assert stored.status is RunStatus.DONE


def test_the_agent_is_given_the_request_and_not_a_placeholder(
    pipeline_for: PipelineFactory, state: StateStore, forge: Forge
) -> None:
    """``${request.json.text}`` is why the ``intake`` script stages are gone.

    The agent saves what arrived on stdin, and the assertion is that the request
    text is in it verbatim, on the branch that got pushed. Before S11 a workflow
    could only get a request into a plan by having a script stage echo a
    hardcoded string, which is what the examples did and what their comments
    admitted to.
    """
    agent = (
        FakeAgent()
        .read_stdin("plan.txt")
        .commit("record the plan")
        .verdict(status="passed", tests=TESTS_PASSED)
    )
    text = "The widget adder should accept floating point numbers, e.g. 0.5 + 0.25."
    item_id = submit(state, text)
    outcome = run(pipeline_for(agent).start(item_for(state, item_id)))

    assert outcome.published is True
    pull = outcome.pull_request
    assert pull is not None
    delivered = run(git(forge.remote, "show", f"refs/heads/{pull.head_branch}:plan.txt"))
    assert text in delivered


# ------------------------------------------------------------- what refuses


def test_a_run_that_commits_nothing_opens_no_pull_request(
    pipeline_for: PipelineFactory, state: StateStore
) -> None:
    """ "There was nothing to do" is an outcome, not a failure to publish.

    The agent edits nothing and commits nothing, so the branch never moves off
    the base. An empty pull request would be worse than none: it asks a human to
    review a diff that does not exist.
    """
    agent = FakeAgent().say("nothing to change here").verdict(status="passed", tests=TESTS_PASSED)
    item_id = submit(state, "The widget adder should accept floating point numbers.")
    outcome = run(pipeline_for(agent).start(item_for(state, item_id)))

    assert outcome.published is False
    assert outcome.refusal is None


def test_an_unreviewable_diff_is_refused_before_anything_is_published(
    pipeline_for: PipelineFactory, state: StateStore, forge: Forge
) -> None:
    """The one defence layer S15 ported from v1's four, now with a caller.

    S15 built ``hygiene.audit`` and said the caller refuses on its behalf. This
    is that caller. The remote is checked afterwards because the property is not
    "an error was returned" — it is that nothing left the machine.
    """
    agent = (
        FakeAgent()
        .write("node_modules/left-pad/index.js", "module.exports = 1\n")
        .write("app.py", "def add(a, b):\n    return a + b  # touched\n")
        .commit("vendored the world")
        .verdict(status="passed", tests=TESTS_PASSED)
    )
    item_id = submit(state, "The widget adder should accept floating point numbers.")
    outcome = run(pipeline_for(agent).start(item_for(state, item_id)))

    assert outcome.published is False
    assert outcome.refusal is not None
    assert "not reviewable" in outcome.refusal
    assert outcome.findings

    refs = run(git(forge.remote, "for-each-ref", "--format=%(refname)", "refs/heads/"))
    assert refs.splitlines() == ["refs/heads/main"]


def test_an_unroutable_request_stays_in_the_queue(pipeline: Pipeline, state: StateStore) -> None:
    """Acknowledging a request nobody could route would strand it.

    That is v1's ``sessions.json`` bug in different clothing, and it is why
    ``triage.acknowledge`` looks at whether a run started rather than at whether
    the command finished.
    """
    deployment = pipeline.deployment
    object.__setattr__(deployment, "_profiles", {})  # nothing configured

    item_id = submit(state, "Please make the thing faster.")
    intake = Intake(state)
    outcome = run(pipeline.start(item_for(state, item_id)))
    acknowledge(intake, outcome)

    assert outcome.run_id is None
    assert outcome.refusal is not None
    assert [item.id for item in intake.collect()] == [item_id]


def test_a_request_that_was_worked_on_leaves_the_queue(
    pipeline: Pipeline, state: StateStore
) -> None:
    item_id = submit(state, "The widget adder should accept floating point numbers.")
    intake = Intake(state)
    outcome = run(pipeline.start(item_for(state, item_id)))
    acknowledge(intake, outcome)

    assert outcome.run_id is not None
    assert intake.collect() == ()


# ---------------------------------------------------------------- clean-up


def test_nothing_is_left_on_disk_when_the_run_is_over(
    pipeline: Pipeline, state: StateStore, host_deployment: Deployment
) -> None:
    """Acquire in a ``try``, release in the ``finally`` — S15's third criterion,
    met by the thing that actually acquires rather than by a test doing it."""
    item_id = submit(state, "The widget adder should accept floating point numbers.")
    outcome = run(pipeline.start(item_for(state, item_id)))

    assert outcome.run_id is not None
    assert not (host_deployment.work_root / outcome.run_id).exists()


def test_the_routing_decision_is_in_the_audit_log(pipeline: Pipeline, state: StateStore) -> None:
    """``WORK_ITEM_ROUTED`` has existed since S2 with nothing writing it."""
    item_id = submit(state, "The widget adder should accept floating point numbers.")
    run(pipeline.start(item_for(state, item_id)))

    events = state.audit.read(work_item_id=item_id)
    routed = [event for event in events if event.kind.value == "work_item.routed"]
    assert len(routed) == 1
    assert isinstance(routed[0].payload, dict)
    assert routed[0].payload["repo"] == WIDGET


def test_a_spike_never_touches_the_data_plane(
    pipeline: Pipeline, state: StateStore, host_deployment: Deployment
) -> None:
    """ "explore → report, no PR" is enforced by the shape of the workflow.

    ``spike.yaml`` has no runner step, so the pipeline gives it no checkout —
    there is no branch to push and no pull request to open, whatever the run
    does. The agent stages fail here because no model is wired, which is the
    right refusal and not what this test is about.
    """
    item_id = submit(state, "Can the widget arithmetic overflow on 64-bit inputs?")
    outcome = run(pipeline.start(item_for(state, item_id)))

    assert outcome.routed.workflow.value == "spike"
    assert outcome.published is False
    assert not list(host_deployment.work_root.glob("run.*"))
