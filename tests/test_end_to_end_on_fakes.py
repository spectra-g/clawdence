"""S5's verification, as one test: a whole workflow on fakes.

> A full workflow runs end-to-end on fakes with **zero network calls and zero
> LLM spend**. A test run that crashes mid-way leaves no stray containers or
> worktrees.

Each clause is checked by machinery rather than by assertion where that is
possible, which is the difference between a guarantee and a hope:

- **Zero network calls** — the autouse guard in ``conftest`` makes a TCP
  connection raise. Nothing here has to remember to check.
- **Zero LLM spend** — the agent stage runs the *real* handler (S12) against a
  scripted model port, and the cassette test below runs it against the real
  provider adapter with a recording in front of it, handing the cassette a live
  transport and asserting it is never touched.
- **Nothing left behind** — the ``reaper`` fixture releases what a test
  registered and the fixture itself asserts nothing leaked.

The second half of the file is the part the *pipeline* will eventually do —
open a ticket, push, open a PR, merge, tell somebody. That driver is S11's and
S15's; what is checked here is that the ports compose into it, which is not
answerable from a unit test of any one of them.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import JsonValue

from clawdence.agent import CATALOGUE, DEFAULT_SECRET_NAME, AnthropicModels
from clawdence.domain import (
    BuildSystem,
    IsolationTier,
    RepoProfile,
    RunStatus,
    StepStatus,
    WorkItemType,
)
from clawdence.engine import RunReport, execute, load_workflow
from clawdence.ports import (
    FAKE_MODEL,
    FakeRunner,
    InMemoryTracker,
    InMemoryVcs,
    ModelDescriptor,
    Notification,
    NotificationKind,
    Outbox,
    Ports,
    PullRequestState,
    RecordingNotifier,
    ScriptedModel,
    StaticSecrets,
    TicketState,
    TransientError,
    dedupe_key,
)
from clawdence.ports._common import counting_clock
from clawdence.store import SqliteLedger, StateStore
from tests.harness.cassette import Cassette, Mode
from tests.harness.cleanup import Reaper
from tests.harness.wiring import fake_registry
from tests.ports import factories as make
from tests.ports.factories import START, run

WORKFLOW = textwrap.dedent(
    """
    schema_version: 1
    name: fakes
    version: 1.0.0
    description: Every step type, executed against fakes.

    stages:
      - id: classify
        type: script
        command:
          - python3
          - -c
          - 'import json; print(json.dumps({"size": "S"}))'
        timeout_seconds: 30

      - id: plan
        type: agent
        role: business-analyst
        task: 'Work out what is being asked: ${classify.json.parsed.size}'
        response_schema: requirements
        model:
          model: fake-model
        max_turns: 2

      - id: gate
        type: approval
        prompt: Ship it?
        when: '$plan.json.result.confidence >= 0.5'

      - id: code
        type: runner
        when: '$gate.response.decision == "approved"'
        retry:
          max_attempts: 2

      - id: report
        type: script
        command: [python3, -c, 'print("done")']
    """
)


def _profile() -> RepoProfile:
    return RepoProfile(
        id=make.REPO_ID,
        name="test-repo",
        remote_url="https://forge.invalid/test-repo",
        build_system=BuildSystem.UV,
        isolation_tier=IsolationTier.CONTAINER,
    )


def replace_model(descriptor: ModelDescriptor, name: str) -> ModelDescriptor:
    """The same model under a different name, so the shipped catalogue does not
    have to be edited for a test to use it."""
    return descriptor.model_copy(update={"model": name})


def _answer(confidence: float) -> str:
    """What the scripted model replies. A document, because the stage declares a
    schema and the handler validates against it."""
    return json.dumps(
        {
            "summary": "make the thing do the thing",
            "acceptance_criteria": ["the thing does the thing"],
            "confidence": confidence,
        }
    )


def _model(confidence: float = 0.9) -> ScriptedModel:
    """A model port that answers the business analyst and nothing else.

    Keyed on the role prompt, so a stage that used a different role would get no
    answer rather than this one — the property ``ScriptedModel`` exists for.
    """
    return ScriptedModel(
        {"business analyst": _answer(confidence)},
        catalogue={FAKE_MODEL.model: FAKE_MODEL},
    )


def test_a_full_workflow_runs_on_fakes(tmp_path: Path, reaper: Reaper) -> None:
    """Every step type executes, the run is recorded, and nothing goes out."""
    workflow_path = tmp_path / "fakes.yaml"
    workflow_path.write_text(WORKFLOW, encoding="utf-8")
    workflow = load_workflow(workflow_path)

    runner = FakeRunner(
        {"code": make.runner_result("code")},
        clock=counting_clock(START),
    )
    ports = replace(Ports.fakes(), runner=runner, model=_model())

    store = StateStore.open(tmp_path / "state.db")
    reaper.register("state store", store.close)

    report = run(
        execute(
            workflow,
            run_id="run.e2e",
            work_item_id=make.WORK_ITEM_ID,
            registry=fake_registry(
                ports,
                profile=_profile(),
                work_item_id=make.WORK_ITEM_ID,
                branch="clawdence/wi-test",
                base_commit=make.commit(1),
                worktree_path=make.WORKTREE,
                decisions={"gate": {"decision": "approved", "approver": "a-human"}},
            ),
            ledger=SqliteLedger(store, run_id="run.e2e"),
        )
    )

    assert report.succeeded
    assert report.run.status is RunStatus.DONE
    assert {stage: result.status for stage, result in report.final.items()} == {
        "classify": StepStatus.SUCCEEDED,
        "plan": StepStatus.SUCCEEDED,
        "gate": StepStatus.SUCCEEDED,
        "code": StepStatus.SUCCEEDED,
        "report": StepStatus.SUCCEEDED,
    }

    # The runner was dispatched once, with an idempotency key derived the same
    # way the ledger derives it.
    assert [request.idempotency_key for request in runner.dispatched] == ["run.e2e:code:1"]

    # And the whole thing survived the process: the store holds it.
    assert store.get_run("run.e2e") is not None
    assert len(store.steps_for("run.e2e")) == 5


def _provider_reply(confidence: float) -> JsonValue:
    """A Messages API reply, as the provider would send it."""
    return {
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": _answer(confidence)}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 900, "output_tokens": 60},
    }


def test_the_real_provider_replays_and_never_reaches_a_transport(tmp_path: Path) -> None:
    """Zero LLM spend, made structural rather than asserted after the fact.

    This is the seam ``tests/harness/cassette`` was written for — "S12 plugs its
    real transport into ``play``" — and it is the whole agent step, not a stand-in:
    ``AnthropicModels`` builds the payload it would really post, the cassette keys
    on that payload, and the recording answers it.

    Two runs, and the second is the point. Recording once and replaying once proves
    the payload digest is *stable*: a request built from a prompt file, a schema
    projection and a framed task has a great many chances to vary between two
    identical runs, and a digest that varied would make every cassette in the
    project a single-use fixture. The replay run is handed a live transport that
    fails the test if it is called, so a miss cannot fall through to a socket — it
    fails here, offline and free, which is the rule that stops a new prompt quietly
    costing money on somebody's laptop.
    """
    workflow_path = tmp_path / "fakes.yaml"
    workflow_path.write_text(WORKFLOW, encoding="utf-8")
    workflow = load_workflow(workflow_path)
    tape = tmp_path / "business-analyst.json"

    async def canned(request: JsonValue) -> JsonValue:
        return _provider_reply(0.9)

    async def forbidden(request: JsonValue) -> JsonValue:
        raise AssertionError("replay must never reach a live transport")

    def go(cassette: Cassette, live: object, run_id: str) -> object:
        transport = canned if live is canned else forbidden
        provider = AnthropicModels(
            StaticSecrets({DEFAULT_SECRET_NAME: "not-a-real-key"}),
            catalogue={"fake-model": replace_model(CATALOGUE["claude-sonnet-5"], "fake-model")},
            transport=lambda payload: cassette.play(payload, transport),
        )
        return run(
            execute(
                workflow,
                run_id=run_id,
                work_item_id=make.WORK_ITEM_ID,
                registry=fake_registry(
                    replace(Ports.fakes(), model=provider, runner=FakeRunner()),
                    profile=_profile(),
                    work_item_id=make.WORK_ITEM_ID,
                    branch="clawdence/wi-test",
                    base_commit=make.commit(1),
                    worktree_path=make.WORKTREE,
                    decisions={"gate": {"decision": "rejected"}},
                ),
            )
        )

    recorder = Cassette(tape, mode=Mode.RECORD)
    recorded = go(recorder, canned, "run.record")
    assert recorder.save() is True

    player = Cassette(tape, mode=Mode.REPLAY)
    replayed = go(player, forbidden, "run.replay")

    assert player.unused == frozenset()
    for report in (recorded, replayed):
        assert isinstance(report, RunReport)
        assert report.final["plan"].status is StepStatus.SUCCEEDED
        output = report.final["plan"].output
        assert isinstance(output, dict)
        assert output["model"] == "claude-sonnet-5"
        assert output["prompt_origin"] == "builtin"
        assert output["result"] == {
            "summary": "make the thing do the thing",
            "acceptance_criteria": ["the thing does the thing"],
            "out_of_scope": [],
            "open_questions": [],
            "confidence": 0.9,
            "unusual_request": None,
        }


def test_a_guard_reading_the_agent_answer_can_stop_the_run(tmp_path: Path, reaper: Reaper) -> None:
    """The composition that matters: an agent's output decides a later branch.

    Low confidence means the gate never runs, so the runner never runs, so
    nothing is dispatched. A pipeline whose expensive step is not actually
    gated is one that spends money on work it was told not to do.
    """
    workflow_path = tmp_path / "fakes.yaml"
    workflow_path.write_text(WORKFLOW, encoding="utf-8")

    runner = FakeRunner({"code": make.runner_result("code")}, clock=counting_clock(START))
    ports = replace(Ports.fakes(), runner=runner, model=_model(confidence=0.1))

    report = run(
        execute(
            load_workflow(workflow_path),
            run_id="run.gated",
            work_item_id=make.WORK_ITEM_ID,
            registry=fake_registry(
                ports,
                profile=_profile(),
                work_item_id=make.WORK_ITEM_ID,
                branch="clawdence/wi-test",
                base_commit=make.commit(1),
                worktree_path=make.WORKTREE,
                decisions={"gate": {"decision": "approved"}},
            ),
        )
    )

    assert report.succeeded
    assert report.final["gate"].status is StepStatus.SKIPPED
    assert report.final["code"].status is StepStatus.SKIPPED
    assert runner.dispatched == ()


def test_a_failing_runner_retries_and_then_halts(tmp_path: Path) -> None:
    """The failure taxonomy is honoured rather than collapsed.

    ``TESTS_FAILED`` is retryable, so the declared policy gets its second
    attempt; two distinct idempotency keys prove the attempts were distinct
    dispatches and not one dispatch counted twice.
    """
    workflow_path = tmp_path / "fakes.yaml"
    workflow_path.write_text(WORKFLOW, encoding="utf-8")

    from clawdence.domain import RunnerOutcome

    runner = FakeRunner(
        {"code": make.runner_result("code", outcome=RunnerOutcome.TESTS_FAILED)},
        clock=counting_clock(START),
    )
    ports = replace(Ports.fakes(), runner=runner, model=_model())

    report = run(
        execute(
            load_workflow(workflow_path),
            run_id="run.failing",
            work_item_id=make.WORK_ITEM_ID,
            registry=fake_registry(
                ports,
                profile=_profile(),
                work_item_id=make.WORK_ITEM_ID,
                branch="clawdence/wi-test",
                base_commit=make.commit(1),
                worktree_path=make.WORKTREE,
                decisions={"gate": {"decision": "approved"}},
            ),
        )
    )

    assert not report.succeeded
    assert report.run.status is RunStatus.HALTED
    assert report.final["code"].status is StepStatus.FAILED
    assert report.final["code"].error is not None
    assert report.final["code"].error.kind == "runner-tests-failed"
    assert [request.idempotency_key for request in runner.dispatched] == [
        "run.failing:code:1",
        "run.failing:code:2",
    ]
    assert report.final["report"].status is StepStatus.SKIPPED


def test_the_ports_compose_into_a_pipeline(tmp_path: Path) -> None:
    """What S11 and S15 will drive: ingest, ticket, push, PR, merge, tell.

    Written here because "do these seven interfaces fit together" is not
    answerable from a unit test of any one of them, and because the join
    between them — the tree hash the runner produced is the tree hash the merge
    is checked against — is the property the whole design turns on.
    """
    clock = counting_clock(START)
    ports = replace(
        Ports.fakes(),
        vcs=InMemoryVcs(clock=clock),
        tracker=InMemoryTracker(clock=clock),
        notify=RecordingNotifier(clock=clock),
    )
    vcs, tracker, notifier = ports.vcs, ports.tracker, ports.notify
    assert isinstance(vcs, InMemoryVcs)
    assert isinstance(tracker, InMemoryTracker)
    assert isinstance(notifier, RecordingNotifier)

    # 1. Work arrives, once, however many times the source redelivers it.
    item = make.work_item("wi.compose", external_id="issue-7")
    ingest = ports.ingest
    assert hasattr(ingest, "offer")
    ingest.offer(item)
    ingest.offer(make.work_item("wi.duplicate", external_id="issue-7"))
    collected = run(ingest.collect())
    assert [collected_item.id for collected_item in collected] == ["wi.compose"]
    assert dedupe_key(collected[0]) == "cli:issue-7"

    # 2. A ticket, idempotently.
    ticket = run(tracker.ensure(work_item_id=item.id, title=item.title, body=item.raw_text))
    assert run(tracker.ensure(work_item_id=item.id, title="renamed", body="")).id == ticket.id
    run(tracker.transition(ticket.id, TicketState.IN_PROGRESS))

    # 3. A branch off main, and the tree the runner produced pushed onto it.
    base = vcs.seed(make.REPO_ID)
    run(vcs.create_branch(make.REPO_ID, "clawdence/wi-compose", from_commit=base))
    produced = vcs.commit()
    run(
        vcs.push(
            make.REPO_ID,
            "clawdence/wi-compose",
            worktree_path=make.WORKTREE,
            expect_commit=produced,
        )
    )

    # 4. A pull request, idempotently.
    pull = run(
        vcs.open_pull_request(
            make.REPO_ID,
            title=item.title,
            body="what it does",
            head_branch="clawdence/wi-compose",
            base_branch="main",
            work_item_id=item.id,
        )
    )
    assert pull.head_commit == produced

    # 5. Merge, stating exactly what the evidence covered.
    merged = run(
        vcs.merge(
            make.REPO_ID,
            pull.number,
            expect_head=produced,
            expect_base=pull.base_commit,
        )
    )
    assert merged.state is PullRequestState.MERGED

    # 6. Close the ticket and say so, through an outbox so a chat outage cannot
    #    fail a run that has already merged.
    run(tracker.transition(ticket.id, TicketState.CLOSED))
    outbox: Outbox[Notification] = Outbox(notifier.send, clock=clock)
    delivered = run(
        outbox.send(
            Notification(
                kind=NotificationKind.SUMMARY,
                channel="#builds",
                text=f"merged {merged.merge_commit}",
                work_item_id=item.id,
                idempotency_key="run.compose:summary:1",
            ),
            key="run.compose:summary:1",
        )
    )

    assert delivered is True
    assert [sent.kind for sent in notifier.sent] == [NotificationKind.SUMMARY]
    assert item.type is WorkItemType.TASK
    run(ingest.acknowledge(item.id))
    assert run(ingest.collect()) == ()


def test_a_chat_outage_does_not_fail_the_pipeline() -> None:
    """The failure domain table says notify is non-fatal. This is that, run.

    A system that halts because it could not announce that it was working is
    worse than one that works quietly — and the retry, when the channel comes
    back, does not post the message twice.
    """
    notifier = RecordingNotifier(clock=counting_clock(START))
    outbox: Outbox[Notification] = Outbox(notifier.send, clock=counting_clock(START))
    message = Notification(
        kind=NotificationKind.PROGRESS,
        channel="#builds",
        text="still going",
        idempotency_key="run.x:code:1",
    )

    notifier.fail_with(TransientError("unavailable", "503"))
    while_down = run(outbox.send(message, key="run.x:code:1"))
    assert while_down is False
    assert len(notifier.sent) == 0

    notifier.fail_with(None)
    assert run(outbox.flush()).ok
    assert len(notifier.sent) == 1

    # And a resumed run replaying the same notification does not repeat it.
    replayed = run(outbox.send(message, key="run.x:code:1"))
    assert replayed is True
    assert len(notifier.sent) == 1


def test_a_crash_mid_test_still_releases_what_was_registered(tmp_path: Path) -> None:
    """ "A test run that crashes mid-way leaves no stray containers or
    worktrees" — the reaper releases on the way out of a failure, not only on
    the way out of a pass.
    """
    released: list[str] = []
    keeper = Reaper()
    keeper.register("worktree", lambda: released.append("worktree"))

    with pytest.raises(RuntimeError):
        try:
            raise RuntimeError("the test blew up")
        finally:
            keeper.release_all()

    assert released == ["worktree"]
    assert keeper.outstanding == ()
