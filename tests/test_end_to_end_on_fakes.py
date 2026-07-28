"""S5's verification, as one test: a whole workflow on fakes.

> A full workflow runs end-to-end on fakes with **zero network calls and zero
> LLM spend**. A test run that crashes mid-way leaves no stray containers or
> worktrees.

Each clause is checked by machinery rather than by assertion where that is
possible, which is the difference between a guarantee and a hope:

- **Zero network calls** — the autouse guard in ``conftest`` makes a TCP
  connection raise. Nothing here has to remember to check.
- **Zero LLM spend** — the agent stage plays a cassette, and a cassette miss is
  an error rather than a call. Test one below removes even the possibility by
  handing the cassette a live transport and asserting it is never touched.
- **Nothing left behind** — the ``reaper`` fixture releases what a test
  registered and the fixture itself asserts nothing leaked.

The second half of the file is the part the *pipeline* will eventually do —
open a ticket, push, open a PR, merge, tell somebody. That driver is S11's and
S15's; what is checked here is that the ports compose into it, which is not
answerable from a unit test of any one of them.
"""

from __future__ import annotations

import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import JsonValue

from clawdence.domain import (
    BuildSystem,
    IsolationTier,
    RepoProfile,
    RunStatus,
    StepStatus,
    WorkItemType,
)
from clawdence.engine import execute, load_workflow
from clawdence.ports import (
    FakeRunner,
    InMemoryTracker,
    InMemoryVcs,
    Notification,
    NotificationKind,
    Outbox,
    Ports,
    PullRequestState,
    RecordingNotifier,
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
        role: senior-dev
        model:
          model: a-model
        max_turns: 2

      - id: gate
        type: approval
        prompt: Ship it?
        when: '$plan.json.confidence == "high"'

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


def _seeded_cassette(path: Path, answer: JsonValue) -> Cassette:
    """Record one agent answer, then hand back a cassette in replay mode."""
    recorder = Cassette(path, mode=Mode.RECORD)

    async def once(request: JsonValue) -> JsonValue:
        return answer

    run(
        recorder.play(
            {
                "stage": "plan",
                "role": "senior-dev",
                "model": "a-model",
                "prompt_version": None,
                "max_turns": 2,
            },
            once,
        )
    )
    recorder.save()
    return Cassette(path, mode=Mode.REPLAY)


def test_a_full_workflow_runs_on_fakes(tmp_path: Path, reaper: Reaper) -> None:
    """Every step type executes, the run is recorded, and nothing goes out."""
    workflow_path = tmp_path / "fakes.yaml"
    workflow_path.write_text(WORKFLOW, encoding="utf-8")
    workflow = load_workflow(workflow_path)

    runner = FakeRunner(
        {"code": make.runner_result("code")},
        clock=counting_clock(START),
    )
    ports = replace(Ports.fakes(), runner=runner)
    cassette = _seeded_cassette(tmp_path / "plan.json", {"confidence": "high", "steps": 3})

    store = StateStore.open(tmp_path / "state.db")
    reaper.register("state store", store.close)

    report = run(
        execute(
            workflow,
            run_id="run.e2e",
            work_item_id=make.WORK_ITEM_ID,
            registry=fake_registry(
                ports,
                cassette=cassette,
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


def test_the_agent_stage_never_reaches_a_transport(tmp_path: Path, reaper: Reaper) -> None:
    """Zero LLM spend, made structural rather than asserted after the fact.

    The cassette is handed a live transport that fails the test if called. In
    replay mode it is not called even though it is right there — which is the
    rule that stops a new prompt quietly costing money on somebody's laptop.
    """
    called: list[JsonValue] = []

    async def transport(request: JsonValue) -> JsonValue:
        called.append(request)
        raise AssertionError("replay must never reach a live transport")

    cassette = _seeded_cassette(tmp_path / "plan.json", {"confidence": "high"})
    answer = run(
        cassette.play(
            {
                "stage": "plan",
                "role": "senior-dev",
                "model": "a-model",
                "prompt_version": None,
                "max_turns": 2,
            },
            transport,
        )
    )
    assert answer == {"confidence": "high"}
    assert called == []


def test_a_guard_reading_the_agent_answer_can_stop_the_run(tmp_path: Path, reaper: Reaper) -> None:
    """The composition that matters: an agent's output decides a later branch.

    Low confidence means the gate never runs, so the runner never runs, so
    nothing is dispatched. A pipeline whose expensive step is not actually
    gated is one that spends money on work it was told not to do.
    """
    workflow_path = tmp_path / "fakes.yaml"
    workflow_path.write_text(WORKFLOW, encoding="utf-8")

    runner = FakeRunner({"code": make.runner_result("code")}, clock=counting_clock(START))
    ports = replace(Ports.fakes(), runner=runner)
    cassette = _seeded_cassette(tmp_path / "plan.json", {"confidence": "low"})

    report = run(
        execute(
            load_workflow(workflow_path),
            run_id="run.gated",
            work_item_id=make.WORK_ITEM_ID,
            registry=fake_registry(
                ports,
                cassette=cassette,
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
    ports = replace(Ports.fakes(), runner=runner)
    cassette = _seeded_cassette(tmp_path / "plan.json", {"confidence": "high"})

    report = run(
        execute(
            load_workflow(workflow_path),
            run_id="run.failing",
            work_item_id=make.WORK_ITEM_ID,
            registry=fake_registry(
                ports,
                cassette=cassette,
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
