"""Runner adapters — the data plane, and the contract for talking to it.

``ports.runner`` is the *interface*: what a dispatch means and what obligations
come with answering one. This package is the other side — implementations, and
the I/O contract from the plan's §3.9 that every one of them shares. ``host`` is
the first; S7 adds ``container`` beside it and inherits everything except the
spawning.

The layering is one-directional, as elsewhere::

    verdict ─ worktree ─ stream
       └─ plan
       └─ outcome
            └─ host
                 └─ handler

The split worth explaining is between ``outcome`` and ``host``. Classification is
a pure function over a record of observations, and gathering those observations
is separate code, because the observations available depend on the tier: a
container is *told* it was OOM-killed, a host process can only infer it from a
``SIGKILL`` it did not send, and nothing observes a denied egress until S7b
exists to deny one. Splitting them means the taxonomy is completely testable
today and the tiers that arrive later fill in fields rather than reopening the
ranking.

Wiring one to a real CLI is configuration rather than code, because the runner
CLIs move faster than this system will and hardcoding somebody else's flag names
is a dependency on them not renaming anything::

    runner = HostRunner(
        AgentCommand(
            argv=("codex", "exec", "--full-auto"),
            delivery=PlanDelivery.STDIN,
            conventions_filename="AGENTS.md",
            secret_env={"OPENAI_API_KEY": "runner-llm-key"},
            prices=TokenPrice(input_usd=Decimal("3"), output_usd=Decimal("15")),
        ),
        secrets=EnvSecrets(),
        sink=write_to(sys.stderr, prefix="runner| "),
    )

The test suite deliberately does not do that. It runs a controllable stand-in
(``tests/harness/agent.py``) instead, because the questions worth asking — does a
timeout kill the child, does the budget fire, is a credential reachable — are
questions about a process, and a real CLI answers them only with a network, a
key, and a bill.

Everything here treats the worktree as **output from a process that ran
model-generated code**: paths are checked before they are opened, files are
size-capped before they are parsed, the diff is re-derived with git rather than
believed, and no control-plane credential is in the environment to be stolen.
That framing, not the isolation tier, is what makes the ``host`` tier merely
*inadvisable* rather than actively unsafe to have in the codebase.
"""

from __future__ import annotations

from clawdence.runners.handler import RETRYABLE, Dispatch, RunnerHandler
from clawdence.runners.host import (
    FORBIDDEN_ENV,
    INHERITED_ENV,
    PLAN_PATH,
    WORK_DIR,
    AgentCommand,
    HostRunner,
    PlanDelivery,
    TokenPrice,
)
from clawdence.runners.outcome import Completion, classify
from clawdence.runners.plan import build as build_plan
from clawdence.runners.stream import (
    Accumulation,
    LogLine,
    LogSink,
    Stream,
    Tail,
    TokenTally,
    write_to,
)
from clawdence.runners.verdict import (
    MAX_VERDICT_BYTES,
    VERDICT_PATH,
    RunnerVerdict,
    VerdictError,
    VerdictStatus,
)
from clawdence.runners.worktree import DEFAULT_IDENTITY, GitError, GitIdentity

__all__ = [
    "DEFAULT_IDENTITY",
    "FORBIDDEN_ENV",
    "INHERITED_ENV",
    "MAX_VERDICT_BYTES",
    "PLAN_PATH",
    "RETRYABLE",
    "VERDICT_PATH",
    "WORK_DIR",
    "Accumulation",
    "AgentCommand",
    "Completion",
    "Dispatch",
    "GitError",
    "GitIdentity",
    "HostRunner",
    "LogLine",
    "LogSink",
    "PlanDelivery",
    "RunnerHandler",
    "RunnerVerdict",
    "Stream",
    "Tail",
    "TokenPrice",
    "TokenTally",
    "VerdictError",
    "VerdictStatus",
    "build_plan",
    "classify",
    "write_to",
]
