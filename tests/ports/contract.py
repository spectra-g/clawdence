"""The obligations every adapter must meet, written once.

This is the deliverable S5 exists for. An interface says what methods a thing
has; it says nothing about whether calling ``ensure`` twice makes two tickets,
and that second question is the one every one of these ports gets wrong in a
different way. v1's answer was a suite per integration, which meant the GitHub
adapter and the Jira adapter were tested for different properties and only one
of them was idempotent. Here the properties are the suite, and an adapter is
correct when it passes them.

**How to use it.** Subclass the contract for your port, name the subclass
``Test…`` so pytest collects it, and override the one fixture that builds your
adapter::

    class TestMyTracker(TrackerContract):
        @pytest.fixture
        def tracker(self) -> TrackerPort:
            return MyTracker(...)

Everything else is inherited. If a future adapter needs a live service it gets
the same treatment plus a skip marker — the contract does not change to
accommodate a slower implementation, because an obligation that weakens for the
real one is an obligation the fakes are the only things meeting.

**The base classes are not collected.** They are named ``…Contract`` rather than
``Test…`` on purpose: an abstract fixture that raises would otherwise be
collected once as an error, and a suite that fails when nothing is wrong is a
suite people learn to ignore.

**Null adapters do not appear here.** ``NullTracker`` and friends deliberately
fail these — an adapter that claims to store things must return what it stored,
and a null one honestly stores nothing. They are held to ``NullAdapterContract``
instead, which is a real contract of its own: never raise, never persist, and
mark every identifier so that nothing downstream mistakes it for a real one.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from typing import Any, ClassVar

import pytest

from clawdence.domain import RunnerRequest, TokenUsage, WorkItem
from clawdence.ports import (
    ContextPort,
    ControlPort,
    IngestPort,
    KnowledgeKind,
    Message,
    MessageRole,
    ModelPort,
    ModelRequest,
    NotifyPort,
    PermanentError,
    PullRequest,
    PullRequestState,
    RunnerPort,
    SecretNotFoundError,
    SecretProvider,
    StaleMergeError,
    StopReason,
    Ticket,
    TicketState,
    TrackerPort,
    UnknownModelError,
    VcsPort,
    dedupe_key,
)
from clawdence.ports._common import NULL_PREFIX
from tests.ports import factories as make
from tests.ports.factories import run

#: A no-argument factory returning a fresh coroutine. See ``NullAdapterContract``.
type Call = Callable[[], Coroutine[Any, Any, object]]

# --------------------------------------------------------------------------- #
# SecretProvider
# --------------------------------------------------------------------------- #


class SecretProviderContract:
    """Every provider resolves by name, and no provider leaks by accident."""

    pytestmark = pytest.mark.contract

    #: Names the fixture provider is expected to hold, and their values.
    known: ClassVar[dict[str, str]] = {"TOKEN": "s3cret-value", "OTHER": "another"}

    @pytest.fixture
    def secrets(self) -> SecretProvider:
        raise NotImplementedError

    def test_resolves_a_known_name(self, secrets: SecretProvider) -> None:
        assert secrets.resolve("TOKEN").reveal() == self.known["TOKEN"]

    def test_unknown_name_raises_and_says_which(self, secrets: SecretProvider) -> None:
        with pytest.raises(SecretNotFoundError) as caught:
            secrets.resolve("ABSENT")
        assert caught.value.name == "ABSENT"
        assert "ABSENT" in str(caught.value)

    def test_unknown_name_is_permanent(self, secrets: SecretProvider) -> None:
        """An unconfigured credential is a deployment problem, not a blip.

        Retrying it burns the step's whole retry budget on a failure that will
        be identical every time, and reads in the log like a flaky service.
        """
        with pytest.raises(SecretNotFoundError) as caught:
            secrets.resolve("ABSENT")
        assert caught.value.retryable is False

    def test_find_returns_none_rather_than_raising(self, secrets: SecretProvider) -> None:
        assert secrets.find("ABSENT") is None

    def test_names_lists_what_resolves(self, secrets: SecretProvider) -> None:
        assert set(self.known) <= secrets.names()

    def test_a_secret_never_prints_its_value(self, secrets: SecretProvider) -> None:
        """The property the whole ``Secret`` wrapper exists for.

        Both spellings, because ``f"{secret}"`` calls ``__str__`` and a
        traceback calls ``__repr__``, and a type that guarded only one of them
        would leak through whichever it forgot.
        """
        secret = secrets.resolve("TOKEN")
        value = self.known["TOKEN"]
        assert value not in repr(secret)
        assert value not in str(secret)
        assert value not in f"holding {secret}"
        assert "TOKEN" in repr(secret)

    def test_reveal_is_the_only_way_out(self, secrets: SecretProvider) -> None:
        assert secrets.resolve("TOKEN").reveal() == self.known["TOKEN"]
        assert secrets.resolve("TOKEN").name == "TOKEN"


# --------------------------------------------------------------------------- #
# IngestPort
# --------------------------------------------------------------------------- #


class IngestContract:
    """At-least-once delivery, ended by acknowledgement, deduped by source id."""

    pytestmark = pytest.mark.contract

    @pytest.fixture
    def ingest(self) -> IngestPort:
        raise NotImplementedError

    def arrive(self, ingest: IngestPort, item: WorkItem) -> None:
        """Make an item arrive at this adapter.

        Not part of ``IngestPort``: arrival is an HTTP handler for the webhook
        adapter, a socket for Slack, and a method call for the fake. The
        contract needs *some* way to produce input, and this is the seam.
        """
        raise NotImplementedError

    def test_collect_returns_what_arrived(self, ingest: IngestPort) -> None:
        self.arrive(ingest, make.work_item("wi.1", external_id="a"))
        self.arrive(ingest, make.work_item("wi.2", external_id="b"))
        assert [item.id for item in run(ingest.collect())] == ["wi.1", "wi.2"]

    def test_collect_redelivers_until_acknowledged(self, ingest: IngestPort) -> None:
        """The property that makes a crash between reading and recording safe.

        At-most-once loses the request in that window, and a lost request is
        invisible: nobody knows to ask about the sprint that never started.
        """
        self.arrive(ingest, make.work_item("wi.1", external_id="a"))
        first = run(ingest.collect())
        second = run(ingest.collect())
        assert [item.id for item in first] == [item.id for item in second] == ["wi.1"]

    def test_acknowledging_ends_delivery(self, ingest: IngestPort) -> None:
        self.arrive(ingest, make.work_item("wi.1", external_id="a"))
        assert run(ingest.acknowledge("wi.1")) == 1
        assert run(ingest.collect()) == ()

    def test_acknowledging_twice_is_not_an_error(self, ingest: IngestPort) -> None:
        """A retried acknowledgement is the normal result of the crash window
        this interface exists to cover, so it cannot itself be a failure."""
        self.arrive(ingest, make.work_item("wi.1", external_id="a"))
        assert run(ingest.acknowledge("wi.1")) == 1
        assert run(ingest.acknowledge("wi.1")) == 0
        assert run(ingest.acknowledge("wi.never-seen")) == 0

    def test_redelivery_does_not_become_a_second_item(self, ingest: IngestPort) -> None:
        """Same source id, different id we minted — one work item.

        GitHub redelivers webhooks and Slack replays on reconnect. Deduping on
        our own id would dedupe nothing, because we mint a fresh one each time.
        """
        first = make.work_item("wi.1", external_id="same")
        again = make.work_item("wi.2", external_id="same")
        assert dedupe_key(first) == dedupe_key(again)

        self.arrive(ingest, first)
        self.arrive(ingest, again)
        assert [item.id for item in run(ingest.collect())] == ["wi.1"]

    def test_a_forgotten_item_does_not_come_back_as_new_work(self, ingest: IngestPort) -> None:
        """Deduplication outlives acknowledgement.

        The source redelivers on its own schedule — hours later, after a
        reconnect. If dedup state were only the pending set, that redelivery
        would start the sprint a second time.
        """
        self.arrive(ingest, make.work_item("wi.1", external_id="same"))
        run(ingest.acknowledge("wi.1"))
        self.arrive(ingest, make.work_item("wi.2", external_id="same"))
        assert run(ingest.collect()) == ()

    def test_limit_is_honoured(self, ingest: IngestPort) -> None:
        for index in range(5):
            self.arrive(ingest, make.work_item(f"wi.{index}", external_id=str(index)))
        assert len(run(ingest.collect(limit=2))) == 2

    def test_close_is_idempotent(self, ingest: IngestPort) -> None:
        run(ingest.close())
        run(ingest.close())


# --------------------------------------------------------------------------- #
# NotifyPort
# --------------------------------------------------------------------------- #


class NotifyContract:
    """Deliveries are idempotent and conversations stay together."""

    pytestmark = pytest.mark.contract

    @pytest.fixture
    def notifier(self) -> NotifyPort:
        raise NotImplementedError

    def test_delivering_returns_a_receipt(self, notifier: NotifyPort) -> None:
        receipt = run(notifier.send(make.notification("run.1:plan:1")))
        assert receipt.id
        assert receipt.duplicate is False

    def test_repeating_a_key_does_not_send_again(self, notifier: NotifyPort) -> None:
        """Without this, "retry until it works" and "post it three times" are
        the same code — and a resumed run replays its notifications."""
        first = run(notifier.send(make.notification("run.1:plan:1")))
        second = run(notifier.send(make.notification("run.1:plan:1", text="different text")))
        assert second.id == first.id
        assert second.duplicate is True

    def test_distinct_keys_are_distinct_messages(self, notifier: NotifyPort) -> None:
        first = run(notifier.send(make.notification("run.1:plan:1")))
        second = run(notifier.send(make.notification("run.1:plan:2")))
        assert second.id != first.id
        assert second.duplicate is False

    def test_a_reply_stays_in_the_thread(self, notifier: NotifyPort) -> None:
        """v1's ``slackTs``. Eight concurrent runs at top level in one channel
        is how nobody answers the clarification the BA asked for."""
        opened = run(notifier.send(make.notification("run.1:ask:1")))
        assert opened.thread is not None
        replied = run(notifier.send(make.notification("run.1:ask:2", thread=opened.thread)))
        assert replied.thread == opened.thread


# --------------------------------------------------------------------------- #
# TrackerPort
# --------------------------------------------------------------------------- #


class TrackerContract:
    """One ticket per work item, and transitions that do not surprise anyone."""

    pytestmark = pytest.mark.contract

    @pytest.fixture
    def tracker(self) -> TrackerPort:
        raise NotImplementedError

    def _ensure(
        self, tracker: TrackerPort, work_item_id: str = "wi.1", title: str = "A task"
    ) -> Ticket:
        return run(tracker.ensure(work_item_id=work_item_id, title=title, body="details"))

    def test_ensure_creates_a_ticket(self, tracker: TrackerPort) -> None:
        ticket = self._ensure(tracker)
        assert ticket.work_item_id == "wi.1"
        assert ticket.state is TicketState.OPEN

    def test_ensure_is_idempotent_on_the_work_item(self, tracker: TrackerPort) -> None:
        """``ensure`` rather than ``create`` because a caller offered ``create``
        writes check-then-act, and that races the moment two steps of one epic
        report progress together. v1 had exactly this bug."""
        first = self._ensure(tracker)
        second = self._ensure(tracker)
        assert second.id == first.id

    def test_ensure_does_not_rename(self, tracker: TrackerPort) -> None:
        """The work item is the identity, not the title. A second ``ensure``
        carrying the BA's rewritten title must not overwrite what a human may
        have edited on the ticket by hand."""
        first = self._ensure(tracker, title="Original title")
        second = self._ensure(tracker, title="Rewritten by the BA")
        assert second.title == first.title

    def test_find_before_and_after(self, tracker: TrackerPort) -> None:
        assert run(tracker.find("wi.1")) is None
        created = self._ensure(tracker)
        found = run(tracker.find("wi.1"))
        assert found is not None
        assert found.id == created.id

    def test_transition_moves_the_ticket(self, tracker: TrackerPort) -> None:
        ticket = self._ensure(tracker)
        moved = run(tracker.transition(ticket.id, TicketState.IN_PROGRESS))
        assert moved.state is TicketState.IN_PROGRESS

    def test_transition_to_the_current_state_is_a_no_op(self, tracker: TrackerPort) -> None:
        ticket = self._ensure(tracker)
        assert run(tracker.transition(ticket.id, TicketState.OPEN)).state is TicketState.OPEN

    def test_unknown_ticket_is_permanent(self, tracker: TrackerPort) -> None:
        with pytest.raises(PermanentError):
            run(tracker.transition("NOPE-1", TicketState.CLOSED))
        with pytest.raises(PermanentError):
            run(tracker.comment("NOPE-1", "hello"))

    def test_comments_accumulate(self, tracker: TrackerPort) -> None:
        """Comments are not deduped, and that is the decision.

        Two progress updates with identical text are two events; collapsing
        them would hide a retry, which is the thing a reader of the ticket most
        needs to see.
        """
        ticket = self._ensure(tracker)
        run(tracker.comment(ticket.id, "started"))
        run(tracker.comment(ticket.id, "started"))


# --------------------------------------------------------------------------- #
# VcsPort
# --------------------------------------------------------------------------- #


class VcsContract:
    """Branches, pull requests, and the merge safety property."""

    pytestmark = pytest.mark.contract

    repo_id = make.REPO_ID

    @pytest.fixture
    def vcs(self) -> VcsPort:
        raise NotImplementedError

    def seed(self, vcs: VcsPort) -> str:
        """Create the repository this contract works against; return main's head."""
        raise NotImplementedError

    def new_commit(self, vcs: VcsPort) -> str:
        """A commit id that is not on any branch — what a runner just produced."""
        raise NotImplementedError

    def advance_main(self, vcs: VcsPort) -> str:
        """Move ``main`` on, as somebody else merging would."""
        raise NotImplementedError

    def _push(self, vcs: VcsPort, branch: str, commit: str) -> None:
        run(vcs.push(self.repo_id, branch, worktree_path=make.WORKTREE, expect_commit=commit))

    def _open(self, vcs: VcsPort, *, branch: str = "feature") -> PullRequest:
        base = run(vcs.head(self.repo_id, "main"))
        run(vcs.create_branch(self.repo_id, branch, from_commit=base))
        self._push(vcs, branch, self.new_commit(vcs))
        return run(
            vcs.open_pull_request(
                self.repo_id,
                title="A change",
                body="what it does",
                head_branch=branch,
                base_branch="main",
            )
        )

    def test_head_resolves_a_ref(self, vcs: VcsPort) -> None:
        base = self.seed(vcs)
        assert run(vcs.head(self.repo_id, "main")) == base

    def test_unknown_ref_is_permanent(self, vcs: VcsPort) -> None:
        self.seed(vcs)
        with pytest.raises(PermanentError):
            run(vcs.head(self.repo_id, "no-such-branch"))

    def test_creating_the_same_branch_twice_is_a_no_op(self, vcs: VcsPort) -> None:
        base = self.seed(vcs)
        first = run(vcs.create_branch(self.repo_id, "feature", from_commit=base))
        second = run(vcs.create_branch(self.repo_id, "feature", from_commit=base))
        assert first.head == second.head == base

    def test_recreating_a_branch_elsewhere_is_refused(self, vcs: VcsPort) -> None:
        """Silently moving a branch that already exists is not something this
        should be able to do — somebody else's work is on the other end of it."""
        base = self.seed(vcs)
        run(vcs.create_branch(self.repo_id, "feature", from_commit=base))
        with pytest.raises(PermanentError):
            run(vcs.create_branch(self.repo_id, "feature", from_commit=self.new_commit(vcs)))

    def test_opening_a_pull_request_is_idempotent_on_the_branch(self, vcs: VcsPort) -> None:
        """A retried step must not open a second PR for the same work."""
        self.seed(vcs)
        first = self._open(vcs)
        again = run(
            vcs.open_pull_request(
                self.repo_id,
                title="A change",
                body="what it does",
                head_branch="feature",
                base_branch="main",
            )
        )
        assert again.number == first.number

    def test_merging_with_matching_hashes_succeeds(self, vcs: VcsPort) -> None:
        self.seed(vcs)
        pull = self._open(vcs)
        merged = run(
            vcs.merge(
                self.repo_id,
                pull.number,
                expect_head=pull.head_commit,
                expect_base=pull.base_commit,
            )
        )
        assert merged.state is PullRequestState.MERGED
        assert merged.merge_commit is not None

    def test_merging_a_moved_head_is_refused(self, vcs: VcsPort) -> None:
        """The central correctness property.

        Tests passed at commit X; a rebase or a follow-up push made the head Y.
        Merging now lands a tree nothing ever ran a test against — v1 merged on
        "checks are green" with no way to notice they were green for a
        different tree.
        """
        self.seed(vcs)
        pull = self._open(vcs)
        verified_head = pull.head_commit

        moved = self.new_commit(vcs)
        self._push(vcs, "feature", moved)

        with pytest.raises(StaleMergeError) as caught:
            run(
                vcs.merge(
                    self.repo_id,
                    pull.number,
                    expect_head=verified_head,
                    expect_base=pull.base_commit,
                )
            )
        assert caught.value.expected == verified_head
        assert caught.value.actual == moved
        assert caught.value.retryable is False

    def test_merging_onto_an_advanced_base_is_refused(self, vcs: VcsPort) -> None:
        """Somebody else merged first, so the evidence no longer covers the
        tree that would result. Re-verify, do not retry."""
        self.seed(vcs)
        pull = self._open(vcs)
        verified_base = pull.base_commit
        self.advance_main(vcs)

        with pytest.raises(StaleMergeError):
            run(
                vcs.merge(
                    self.repo_id,
                    pull.number,
                    expect_head=pull.head_commit,
                    expect_base=verified_base,
                )
            )

    def test_merging_an_already_merged_pull_request_is_not_an_error(self, vcs: VcsPort) -> None:
        """A lost response must not fail a run that actually succeeded.

        The hashes still have to match — this is idempotency, not a bypass.
        """
        self.seed(vcs)
        pull = self._open(vcs)
        first = run(
            vcs.merge(
                self.repo_id,
                pull.number,
                expect_head=pull.head_commit,
                expect_base=pull.base_commit,
            )
        )
        again = run(
            vcs.merge(
                self.repo_id,
                pull.number,
                expect_head=pull.head_commit,
                expect_base=pull.base_commit,
            )
        )
        assert again.merge_commit == first.merge_commit

    def test_get_pull_request_reports_a_base_that_moved(self, vcs: VcsPort) -> None:
        """The base advances without anything touching the PR, so it is read
        fresh — a cached base is the same bug as a stale evidence binding."""
        self.seed(vcs)
        pull = self._open(vcs)
        advanced = self.advance_main(vcs)
        refetched = run(vcs.get_pull_request(self.repo_id, pull.number))
        assert refetched is not None
        assert refetched.base_commit == advanced

    def test_unknown_pull_request_is_none(self, vcs: VcsPort) -> None:
        self.seed(vcs)
        assert run(vcs.get_pull_request(self.repo_id, 9999)) is None


# --------------------------------------------------------------------------- #
# RunnerPort
# --------------------------------------------------------------------------- #


class RunnerContract:
    """Dispatch is idempotent, and results are validated before they are used.

    Two fixtures rather than one, because a real runner needs a request pointed
    at something real. ``make_request`` defaults to the same in-memory request
    the fakes use; an adapter that executes anything overrides it with one
    naming a worktree it can actually run in. The *obligations* do not change —
    an adapter that needed the contract weakened would be an adapter the fakes
    are the only things meeting.
    """

    pytestmark = pytest.mark.contract

    @pytest.fixture
    def runner(self) -> RunnerPort:
        raise NotImplementedError

    @pytest.fixture
    def make_request(self) -> Callable[..., RunnerRequest]:
        return make.runner_request

    def test_dispatch_answers_the_request(
        self, runner: RunnerPort, make_request: Callable[..., RunnerRequest]
    ) -> None:
        request = make_request("code")
        result = run(runner.dispatch(request))
        assert (result.run_id, result.stage_id) == (request.run_id, request.stage_id)

    def test_dispatch_is_idempotent_on_the_key(
        self, runner: RunnerPort, make_request: Callable[..., RunnerRequest]
    ) -> None:
        """Two dispatches of one attempt means two agents editing one worktree,
        and two charges for one story. The watchdog recovering a step whose
        container is still alive is how that happens in practice."""
        request = make_request("code")
        first = run(runner.dispatch(request))
        second = run(runner.dispatch(request))
        assert second == first

    def test_a_second_attempt_is_a_different_dispatch(
        self, runner: RunnerPort, make_request: Callable[..., RunnerRequest]
    ) -> None:
        """``attempt`` is in the key, so a retry is genuinely new work."""
        first = make_request("code", attempt=1)
        second = make_request("code", attempt=2)
        assert first.idempotency_key != second.idempotency_key
        run(runner.dispatch(first))
        run(runner.dispatch(second))

    def test_the_request_carries_no_credential(self, runner: RunnerPort) -> None:
        """Structural, not aspirational: ``RunnerRequest`` has no field for a
        secret, and this asserts that nothing has quietly added one."""
        fields = set(RunnerRequest.model_fields)
        assert not {name for name in fields if "secret" in name or "token" in name}

    def test_cancelling_something_settled_is_false_not_an_error(
        self, runner: RunnerPort, make_request: Callable[..., RunnerRequest]
    ) -> None:
        """The watchdog deciding a step is overdue races the step reporting.
        That race is normal and must not itself produce a failure."""
        request = make_request("code")
        run(runner.dispatch(request))
        assert run(runner.cancel(request)) is False


# --------------------------------------------------------------------------- #
# ContextPort
# --------------------------------------------------------------------------- #


class ContextContract:
    """Retrieval is deterministic and scoping does not lose global rules."""

    pytestmark = pytest.mark.contract

    @pytest.fixture
    def context(self) -> ContextPort:
        raise NotImplementedError

    def _seed(self, context: ContextPort) -> None:
        run(context.remember(make.knowledge("k.1", "the build uses maven wrapper")))
        run(context.remember(make.knowledge("k.2", "maven tests run offline")))
        run(
            context.remember(
                make.knowledge(
                    "k.global",
                    "maven never publishes from a runner",
                    kind=KnowledgeKind.RULE,
                    repo_id=None,
                )
            )
        )
        run(context.remember(make.knowledge("k.other", "maven elsewhere", repo_id="repo.other")))

    def test_remembering_then_retrieving(self, context: ContextPort) -> None:
        run(context.remember(make.knowledge("k.1", "the build uses maven wrapper")))
        hits = run(context.retrieve("maven"))
        assert make.texts(hits) == ("k.1",)

    def test_retrieval_is_deterministic(self, context: ContextPort) -> None:
        """Without this, agent-step record/replay is useless — the cassette key
        would depend on retrieval order — and S21b's evals would measure index
        churn rather than prompt changes."""
        self._seed(context)
        first = make.texts(run(context.retrieve("maven build")))
        second = make.texts(run(context.retrieve("maven build")))
        assert first == second

    def test_scoping_keeps_global_items(self, context: ContextPort) -> None:
        """A retrieval that silently dropped installation-wide rules would let
        a global constraint go unenforced on every repo."""
        self._seed(context)
        hits = make.texts(run(context.retrieve("maven", repo_id=make.REPO_ID)))
        assert "k.global" in hits
        assert "k.other" not in hits

    def test_filtering_by_kind(self, context: ContextPort) -> None:
        self._seed(context)
        hits = make.texts(run(context.retrieve("maven", kinds=[KnowledgeKind.RULE])))
        assert hits == ("k.global",)

    def test_limit_is_honoured(self, context: ContextPort) -> None:
        self._seed(context)
        assert len(run(context.retrieve("maven", limit=1))) == 1

    def test_remembering_the_same_id_replaces(self, context: ContextPort) -> None:
        """Re-ingesting a rules file is routine. A memory that grows a duplicate
        on every restart returns the same fact five times and spends the
        context budget doing it."""
        run(context.remember(make.knowledge("k.1", "maven wrapper")))
        run(context.remember(make.knowledge("k.1", "maven wrapper, corrected")))
        hits = run(context.retrieve("maven"))
        assert len(hits) == 1
        assert hits[0].item.text == "maven wrapper, corrected"

    def test_forgetting(self, context: ContextPort) -> None:
        run(context.remember(make.knowledge("k.1", "maven wrapper")))
        assert run(context.forget("k.1")) is True
        assert run(context.forget("k.1")) is False
        assert run(context.retrieve("maven")) == ()

    def test_a_miss_is_empty_not_an_error(self, context: ContextPort) -> None:
        """An agent with no prior knowledge is the first run against any repo,
        which has to work."""
        assert run(context.retrieve("nothing here matches")) == ()


class ControlContract:
    """A claim is a delivery, the order is the claim rule, and nothing repeats.

    Worth a shared contract rather than two suites for the reason the module
    docstring gives, and with one specific to this port: the in-memory
    implementation is what runner tests are written against, so a fake that
    redelivers or that sorts differently would hide precisely the bugs the
    durable one exists to prevent. Both are held to the same sentences here.
    """

    pytestmark = pytest.mark.contract

    @pytest.fixture
    def control(self) -> ControlPort:
        raise NotImplementedError

    def send(self, control: ControlPort, body: str, *, priority: int = 0) -> None:
        """Queue a message. The two adapters spell this differently — one takes
        an instant, the other has a clock — so the contract asks for a verb
        rather than for a signature."""
        raise NotImplementedError

    def test_an_empty_inbox_is_an_empty_signal(self, control: ControlPort) -> None:
        signal = run(control.poll(make.RUN_ID))
        assert signal.messages == ()
        assert signal.cancel is None

    def test_a_message_comes_back_on_the_next_poll(self, control: ControlPort) -> None:
        self.send(control, "use the existing parser")
        (message,) = run(control.poll(make.RUN_ID)).messages
        assert message.body == "use the existing parser"

    def test_a_claimed_message_is_never_claimed_again(self, control: ControlPort) -> None:
        """Delivering an instruction twice is following it twice."""
        self.send(control, "revert that")
        assert len(run(control.poll(make.RUN_ID)).messages) == 1
        assert run(control.poll(make.RUN_ID)).messages == ()

    def test_priority_outranks_arrival(self, control: ControlPort) -> None:
        self.send(control, "queued")
        self.send(control, "urgent", priority=10)
        assert [m.body for m in run(control.poll(make.RUN_ID)).messages] == ["urgent", "queued"]

    def test_arrival_orders_within_a_priority_class(self, control: ControlPort) -> None:
        self.send(control, "first")
        self.send(control, "second")
        assert [m.body for m in run(control.poll(make.RUN_ID)).messages] == ["first", "second"]

    def test_ordinals_are_the_claim_order_and_start_at_one(self, control: ControlPort) -> None:
        """They name the file the agent reads, so they have to be the delivery
        order and not the arrival order."""
        self.send(control, "queued")
        self.send(control, "urgent", priority=1)
        assert [m.ordinal for m in run(control.poll(make.RUN_ID)).messages] == [1, 2]

    def test_polling_a_run_nobody_has_heard_of_is_empty(self, control: ControlPort) -> None:
        """A runner asking about an unknown run must not have its work killed
        over it."""
        assert run(control.poll("run.never-existed")).messages == ()

    def test_a_heartbeat_is_accepted(self, control: ControlPort) -> None:
        run(control.heartbeat(make.RUN_ID, at=make.at(1)))


# --------------------------------------------------------------------------- #
# ModelPort
# --------------------------------------------------------------------------- #


class ModelContract:
    """What every provider must answer about itself, and answer consistently.

    Shorter than the other contracts, and deliberately so. The obligation that
    dominates this package — every write is idempotent on a caller-derived key —
    does not apply here and must not be asserted: ``ports.model`` argues that a
    deduplicated completion breaks the one retry that matters. What is left is the
    part a caller genuinely depends on, which is that ``describe`` can be trusted
    before anything has been spent.
    """

    pytestmark = pytest.mark.contract

    #: A model the adapter under test serves.
    known: ClassVar[str] = "fake-model"

    @pytest.fixture
    def model(self) -> ModelPort:
        raise NotImplementedError

    def test_describe_answers_for_a_known_model(self, model: ModelPort) -> None:
        descriptor = model.describe(self.known)
        assert descriptor.model == self.known
        assert descriptor.context_window_tokens > 0
        assert descriptor.max_output_tokens > 0

    def test_describe_refuses_an_unknown_model(self, model: ModelPort) -> None:
        """A typo in a workflow must fail before the run, not as a 404 halfway
        through a sprint."""
        with pytest.raises(UnknownModelError):
            model.describe("no-such-model-exists")

    def test_describe_is_stable(self, model: ModelPort) -> None:
        """Called during validation and again when the budget is worked out. A
        descriptor that changed between the two would make the second check
        measure something the first did not."""
        assert model.describe(self.known) == model.describe(self.known)

    def test_prices_are_evaluable(self, model: ModelPort) -> None:
        """A dollar cap the adapter cannot evaluate is a cap that enforces
        nothing — the lesson ``TokenPrice`` carries over from the runner."""
        prices = model.describe(self.known).prices
        assert prices.usd(TokenUsage(input_tokens=1_000, output_tokens=1_000)) >= 0

    def test_a_completion_comes_back_attributed(self, model: ModelPort) -> None:
        """``ModelResponse.model`` is what answered, which is not always what was
        asked for. A response that did not say would make a quota fallback
        invisible in the run record."""
        descriptor = model.describe(self.known)
        response = run(
            model.complete(
                ModelRequest(
                    model=self.known,
                    system="you are a business analyst",
                    messages=(Message(role=MessageRole.USER, text="hello"),),
                    max_output_tokens=min(64, descriptor.max_output_tokens),
                )
            )
        )
        assert response.model
        assert isinstance(response.stop_reason, StopReason)


# --------------------------------------------------------------------------- #
# Null adapters
# --------------------------------------------------------------------------- #


class NullAdapterContract:
    """What "not configured" must behave like.

    A real contract rather than an exemption. ``NullTracker`` cannot satisfy
    ``TrackerContract`` — it stores nothing, so it cannot return what it stored
    — but it still has obligations, and they are the ones that decide whether an
    unconfigured installation runs or falls over.
    """

    pytestmark = pytest.mark.contract

    @pytest.fixture
    def calls(self) -> Sequence[Call]:
        """One coroutine factory per method of the null adapter under test.

        Factories rather than coroutines because each test awaits all of them,
        and a coroutine can only be awaited once.
        """
        raise NotImplementedError

    @pytest.fixture
    def minting(self) -> Sequence[Call]:
        """The subset of ``calls`` whose results carry an id the adapter *made*.

        A method that echoes back the caller's own object — ``NullContext``'s
        ``remember`` returns exactly what it was handed — mints nothing, and
        holding it to the marking rule would mean rewriting the caller's ids to
        satisfy a test. Default empty: an adapter that mints nothing says so by
        not overriding this.
        """
        return ()

    def test_nothing_raises(self, calls: Sequence[Call]) -> None:
        """The whole point. A null adapter that raised would make "no tracker
        configured" fail runs, which is the coupling the port removes."""
        for call in calls:
            run(call())

    def test_minted_identifiers_are_marked_as_unreal(self, minting: Sequence[Call]) -> None:
        """An id shaped like ``PROJ-14`` ends up in a notification telling
        somebody to go and read something that was never written."""
        for call in minting:
            identifier = getattr(run(call()), "id", None)
            assert isinstance(identifier, str)
            assert identifier.startswith(NULL_PREFIX)


__all__ = [
    "Call",
    "ContextContract",
    "ControlContract",
    "IngestContract",
    "ModelContract",
    "NotifyContract",
    "NullAdapterContract",
    "RunnerContract",
    "SecretProviderContract",
    "TrackerContract",
    "VcsContract",
]
