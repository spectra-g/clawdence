"""The edges of the system — every interface that talks to something else.

Eight ports, and the rule they exist to enforce is one sentence: **nothing above
this package knows what service it is talking to.** The pipeline opens a pull
request; whether that is GitHub, a local git remote or a dictionary is decided
once, at startup, in ``Ports``. v1 had GitHub's API shape, Slack's message
format and Jira's transition ids spread through a 5,107-line orchestrator, which
is why it could not be tested without credentials and could not be run without
all three services.

``control`` (S6c) is the one that points inwards rather than outwards: the thing
on the other side of it is the control plane's own store, and the caller is the
runner. It is a port for the same reason as the rest — the runner must not know
how a steering message is persisted — and it is the only one whose absence has a
default that quietly does nothing, because a run nobody can steer is a real
configuration rather than a broken one.

The layering is one-directional, as in ``domain``, ``engine`` and ``store``::

    errors ─ _common
      └─ secrets
      └─ ingest · notify · tracker · vcs · runner · context · control
                       └─ outbox
                            └─ __init__ (the ``Ports`` bundle)

Four properties are stated in the ports rather than left to each adapter,
because each of them was a defect in v1 that came from stating it per call site:

**Retryability travels with the failure.** ``TransientError`` and
``PermanentError``; the caller never inspects a message to guess.

**Every write is idempotent on a key the caller derives.** Notifications,
tickets, pull requests and runner dispatches all key on something stable
(``run:stage:attempt``, the work item, the branch), so redelivery collides
instead of duplicating. The contract suite checks each one.

**Non-fatal is a wrapper, not a convention.** ``Outbox`` is the single
implementation of "the tracker being down does not fail the run".

**Fakes are part of the package, not the tests.** Every port ships an in-memory
implementation next to its interface, for three reasons: S3c's ``workflow test``
needs them at run time and not just under pytest; an implementation living
beside its interface stays honest as the interface changes; and it means the
contract suite tests something the product actually contains. They are never a
*default* — ``Ports.fakes()`` has to be asked for by name, the same rule
``engine.StubHandler`` follows, because a fake reachable by accident is a system
that reports success for work nobody did.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from clawdence.ports.context import (
    ContextPort,
    InMemoryContext,
    KnowledgeItem,
    KnowledgeKind,
    NullContext,
    Retrieval,
)
from clawdence.ports.control import (
    DEFAULT_POLL_SECONDS,
    MAX_STEERING_CHARS,
    Cancellation,
    ControlPort,
    InMemoryControl,
    NoControl,
    Signal,
    Steer,
)
from clawdence.ports.errors import (
    OutboxFullError,
    PermanentError,
    PortError,
    SecretNotFoundError,
    TransientError,
)
from clawdence.ports.ingest import IngestPort, InMemoryIngest, dedupe_key
from clawdence.ports.notify import (
    Notification,
    NotificationKind,
    NotifyPort,
    NullNotifier,
    Receipt,
    RecordingNotifier,
)
from clawdence.ports.outbox import FlushReport, Outbox, Undelivered
from clawdence.ports.runner import FakeRunner, RefusingRunner, RunnerPort, validate_result
from clawdence.ports.secrets import (
    REDACTED,
    EnvSecrets,
    NullSecrets,
    Secret,
    SecretProvider,
    StaticSecrets,
)
from clawdence.ports.tracker import (
    InMemoryTracker,
    NullTracker,
    Ticket,
    TicketState,
    TrackerPort,
)
from clawdence.ports.vcs import (
    Branch,
    InMemoryVcs,
    MergeMethod,
    PullRequest,
    PullRequestState,
    StaleMergeError,
    VcsPort,
)


@dataclass(frozen=True, slots=True)
class Ports:
    """Every adapter the control plane holds, wired once at startup.

    A bundle rather than a service locator: the fields are typed, so a component
    that needs the tracker takes ``TrackerPort`` and not this. What this exists
    for is the *composition root* — the single place that decides GitHub-or-fake
    — and for making "what does this system talk to" answerable by reading one
    class.

    The defaults are the ones that do nothing and say so: no secrets, no
    notifications, no tracker, no memory, and a runner that refuses while naming
    what to wire. A control plane assembled with no configuration runs script
    workflows and fails clearly on everything else, which is exactly what it can
    honestly do today.

    ``ingest`` and ``vcs`` have no null default, because there is no honest one.
    A system with no source of work and no version control is not a degraded
    installation; it is a misconfiguration, and it should fail at startup rather
    than at the first pull request.
    """

    ingest: IngestPort
    vcs: VcsPort
    runner: RunnerPort = field(default_factory=RefusingRunner)
    notify: NotifyPort = field(default_factory=NullNotifier)
    tracker: TrackerPort = field(default_factory=NullTracker)
    context: ContextPort = field(default_factory=NullContext)
    secrets: SecretProvider = field(default_factory=NullSecrets)
    control: ControlPort = field(default_factory=NoControl)

    @classmethod
    def fakes(cls) -> Ports:
        """Everything in memory. Must be asked for by name; never a default.

        This is what makes the harness's promise checkable: a full workflow run
        against these touches no socket, spends nothing, and leaves nothing
        behind.

        Swap one out with ``dataclasses.replace(Ports.fakes(), runner=...)``.
        There is no keyword-override parameter here because a test that cares
        about a particular fake almost always wants to hold a reference to it
        and assert on what it recorded, so it constructs it anyway.
        """
        return cls(
            ingest=InMemoryIngest(),
            vcs=InMemoryVcs(),
            runner=FakeRunner(),
            notify=RecordingNotifier(),
            tracker=InMemoryTracker(),
            context=InMemoryContext(),
            secrets=StaticSecrets(),
            control=InMemoryControl(),
        )


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "MAX_STEERING_CHARS",
    "REDACTED",
    "Branch",
    "Cancellation",
    "ContextPort",
    "ControlPort",
    "EnvSecrets",
    "FakeRunner",
    "FlushReport",
    "InMemoryContext",
    "InMemoryControl",
    "InMemoryIngest",
    "InMemoryTracker",
    "InMemoryVcs",
    "IngestPort",
    "KnowledgeItem",
    "KnowledgeKind",
    "MergeMethod",
    "NoControl",
    "Notification",
    "NotificationKind",
    "NotifyPort",
    "NullContext",
    "NullNotifier",
    "NullSecrets",
    "NullTracker",
    "Outbox",
    "OutboxFullError",
    "PermanentError",
    "PortError",
    "Ports",
    "PullRequest",
    "PullRequestState",
    "Receipt",
    "RecordingNotifier",
    "RefusingRunner",
    "Retrieval",
    "RunnerPort",
    "Secret",
    "SecretNotFoundError",
    "SecretProvider",
    "Signal",
    "StaleMergeError",
    "StaticSecrets",
    "Steer",
    "Ticket",
    "TicketState",
    "TrackerPort",
    "TransientError",
    "Undelivered",
    "VcsPort",
    "dedupe_key",
    "validate_result",
]
