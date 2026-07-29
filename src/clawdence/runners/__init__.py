"""Runner adapters — the data plane, and the contract for talking to it.

``ports.runner`` is the *interface*: what a dispatch means and what obligations
come with answering one. This package is the other side — implementations, and
the I/O contract from the plan's §3.9 that every one of them shares.

The layering is one-directional, as elsewhere::

    verdict ─ worktree ─ stream ─ process ─ turns ─ installed ─ steering ─ cache
       └─ plan
       └─ outcome
            └─ agent
                 ├─ host
                 └─ container ─ engine
                      ├─ dockerd
                      ├─ handler
                      ├─ scheduler
                      └─ reaper

``agent`` is the runner, and ``host``, ``container`` and ``dockerd`` are tiers of
it. That is the S7 shape and it is deliberate: they differ in what they spawn,
what the agent's environment starts from, what the tier can say afterwards, and
what has to be given back — four hooks — and in nothing else. A second runner
class "for containers" is how two tiers acquire two different bugs in idempotent
dispatch, which is the same mistake v1 made one layer up when each integration
got its own test suite and only one of them turned out to be idempotent.

S8's ``dockerd`` takes that one step further and subclasses ``container``
outright, because the tier that mounts the host daemon's socket differs from the
default one by a mount, a group, a hosts entry and four environment variables —
and agrees with it about everything else, including every control the socket
then defeats. It pays for the capability in refusals rather than in code: the
repository has to acknowledge the tier in its profile, the work has to have come
from a trusted submitter, and testcontainers' own reaper has to be left on.

The other split worth explaining is between ``outcome`` and the tiers.
Classification is a pure function over a record of observations, and gathering
those observations is separate code, because the observations available depend on
the tier: a container is *told* it was OOM-killed, a host process can only infer
it from a ``SIGKILL`` it did not send, and nothing observes a denied egress until
S7b exists to deny one. Splitting them meant the taxonomy was completely testable
in S6, and S7 filled in ``oom_killed`` without reopening the ranking.

S6b added the two modules that answer questions the exit status cannot. ``turns``
reads the agent's own event stream, because a provider failure arrives as an exit
status of **zero** and reporting that as success is a false success — the one
class of bug everything downstream is defenceless against. ``installed`` records
the bytes the runner writes into the worktree, because "the tree is dirty" only
means the agent left work behind if the dirt is not our own plan and conventions
file, and because a repository that tracks a file at the path we install to gets
our copy swept into its pull request otherwise (§3.9).

The rest of S7 added the three modules that are about a *fleet* of runs rather
than one, and none of them is inside ``agent`` for the same reason: how many runs
may happen at once (``scheduler``), what a dead control plane left behind
(``reaper``) and what survives between runs (``cache``) are all questions a
single dispatch cannot answer about itself. ``scheduler`` decorates any
``RunnerPort``, so the queue is exercised by tests that never start a process,
and ``reaper`` is the only thing here that deletes something no run asked it to.

S6c added ``steering``, which is the channel *into* a run (§3.11). It is a
directory of files under the one the runner already owns, and it is a directory
of files because the container tier's isolation claim is that the worktree bind
mount is the only thing the agent can see — a second transport would be a second
hole in the boundary S7 spent a step closing. What polls for those messages,
what carries the run's liveness back out, and what stops a run somebody
cancelled from outside are all one loop in ``agent``, because all three are
periodic and none of them is triggered by the agent saying anything.

Wiring one to a real CLI is configuration rather than code, because the runner
CLIs move faster than this system will and hardcoding somebody else's flag names
is a dependency on them not renaming anything::

    runner = ContainerRunner(
        AgentCommand(
            argv=("codex", "exec", "--full-auto"),
            delivery=PlanDelivery.STDIN,
            conventions_filename="AGENTS.md",
            secret_env={"OPENAI_API_KEY": "runner-llm-key"},
            prices=TokenPrice(input_usd=Decimal("3"), output_usd=Decimal("15")),
        ),
        image="ghcr.io/example/runner@sha256:…",
        secrets=EnvSecrets(),
        sink=write_to(sys.stderr, prefix="runner| "),
    )

The test suite deliberately does not do that. It runs a controllable stand-in
(``tests/harness/agent.py``) instead, because the questions worth asking — does a
timeout kill the child, does the budget fire, is a credential reachable — are
questions about a process, and a real CLI answers them only with a network, a
key, and a bill. The container tier is the same story twice over: the argv is
proven against a scripted engine (``tests/harness/engine.py``) so the suite stays
hermetic, and the claims that are only meaningful from *inside* a container —
that no control-plane credential is in the environment, that no other repository
is on the filesystem — are asserted against a real daemon in
``tests/runners/test_container_live.py``, which ``make docker-tests`` runs.

Everything here treats the worktree as **output from a process that ran
model-generated code**: paths are checked before they are opened, files are
size-capped before they are parsed, the diff is re-derived with git rather than
believed, and no control-plane credential is in the environment to be stolen.
That framing, not the isolation tier, is what makes the ``host`` tier merely
*inadvisable* rather than actively unsafe to have in the codebase.
"""

from __future__ import annotations

from clawdence.runners.agent import (
    FORBIDDEN_ENV,
    AgentCommand,
    AgentRunner,
    Environment,
    Launch,
    Phase,
    PlanDelivery,
    TokenPrice,
)
from clawdence.runners.cache import CACHE_HOME_ENV, Cache, CachePlan, cache_home
from clawdence.runners.container import (
    LABEL_NAMESPACE,
    RUN_ID_LABEL,
    WORK_ROOT,
    ContainerRunner,
    container_name,
)
from clawdence.runners.dockerd import (
    DOCKER_SOCKET,
    HOST_ALIAS,
    HOST_OVERRIDE_ENV,
    RYUK_DISABLED_ENV,
    SESSION_LABEL,
    DockerSocketRunner,
)
from clawdence.runners.engine import (
    CLIENT_ENV,
    ContainerEngine,
    ContainerSpec,
    ContainerState,
    EngineError,
    Mount,
)
from clawdence.runners.handler import RETRYABLE, Dispatch, RunnerHandler
from clawdence.runners.host import INHERITED_ENV, HostRunner
from clawdence.runners.installed import HOME_DIR, PLAN_PATH, WORK_DIR, Installed
from clawdence.runners.outcome import Completion, classify
from clawdence.runners.plan import build as build_plan
from clawdence.runners.reaper import (
    DEFAULT_CACHE_RETENTION,
    DEFAULT_GRACE,
    DEFAULT_WORKTREE_RETENTION,
    Reaper,
    Reclaimed,
)
from clawdence.runners.scheduler import DEFAULT_LIMIT, Scheduler
from clawdence.runners.steering import STEERING_DIR
from clawdence.runners.stream import (
    Accumulation,
    LogLine,
    LogSink,
    Stream,
    Tail,
    TokenTally,
    write_to,
)
from clawdence.runners.turns import MAX_ERROR_CHARS, TurnTracker
from clawdence.runners.verdict import (
    MAX_VERDICT_BYTES,
    VERDICT_PATH,
    RunnerVerdict,
    VerdictError,
    VerdictStatus,
)
from clawdence.runners.worktree import DEFAULT_IDENTITY, GitError, GitIdentity

__all__ = [
    "CACHE_HOME_ENV",
    "CLIENT_ENV",
    "DEFAULT_CACHE_RETENTION",
    "DEFAULT_GRACE",
    "DEFAULT_IDENTITY",
    "DEFAULT_LIMIT",
    "DEFAULT_WORKTREE_RETENTION",
    "DOCKER_SOCKET",
    "FORBIDDEN_ENV",
    "HOME_DIR",
    "HOST_ALIAS",
    "HOST_OVERRIDE_ENV",
    "INHERITED_ENV",
    "LABEL_NAMESPACE",
    "MAX_ERROR_CHARS",
    "MAX_VERDICT_BYTES",
    "PLAN_PATH",
    "RETRYABLE",
    "RUN_ID_LABEL",
    "RYUK_DISABLED_ENV",
    "SESSION_LABEL",
    "STEERING_DIR",
    "VERDICT_PATH",
    "WORK_DIR",
    "WORK_ROOT",
    "Accumulation",
    "AgentCommand",
    "AgentRunner",
    "Cache",
    "CachePlan",
    "Completion",
    "ContainerEngine",
    "ContainerRunner",
    "ContainerSpec",
    "ContainerState",
    "Dispatch",
    "DockerSocketRunner",
    "EngineError",
    "Environment",
    "GitError",
    "GitIdentity",
    "HostRunner",
    "Installed",
    "Launch",
    "LogLine",
    "LogSink",
    "Mount",
    "Phase",
    "PlanDelivery",
    "Reaper",
    "Reclaimed",
    "RunnerHandler",
    "RunnerVerdict",
    "Scheduler",
    "Stream",
    "Tail",
    "TokenPrice",
    "TokenTally",
    "TurnTracker",
    "VerdictError",
    "VerdictStatus",
    "build_plan",
    "cache_home",
    "classify",
    "container_name",
    "write_to",
]
