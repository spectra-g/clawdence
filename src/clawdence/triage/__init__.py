"""Triage: which process runs this request, which repository it lands in, and
the composition root that carries it from one to the other.

This is the step the walking skeleton was missing. Every other part of the path
existed and was tested — ingestion put a request in a durable inbox (S10), the
engine ran a declared process (S3), agent steps called a model (S12), the runner
executed repo code in a container (S6, S7), and ``vcs`` handed out worktrees and
opened pull requests (S15) — and nothing joined them, because the join needs two
answers nobody had yet: *which workflow* and *which repository*.

The layering is one-directional, as everywhere else::

    config
     ├─ routing
     └─ wiring
         └─ pipeline
             └─ report

``config`` is the file, ``routing`` is the two decisions, ``wiring`` turns
configuration into adapters, ``pipeline`` is the order they are used in, and
``report`` renders any of it for a person. ``routing`` depends on ``config`` for
one type — the policy — and on nothing else; it never sees a repository on disk,
a forge or a run, which is what makes the interesting rules testable with a
mapping and a string.

**This package holds the only code that decides anything on a request's behalf,**
which is why the two modules that do it are pure functions over data with the
reasons attached. A routing decision that cannot be explained is one nobody can
correct, and correcting one is an edit to a repository profile rather than to
code — see ``routing`` on how a stranger's issue text is allowed to select among
repositories the operator configured without ever being able to add one.
"""

from __future__ import annotations

from clawdence.triage.config import (
    CONFIG_FILENAME,
    CONFIG_SCHEMA_VERSION,
    Config,
    ConfigError,
    Deployment,
    Paths,
    Routing,
    RunnerConfig,
    default_config_path,
    load,
    parse,
)
from clawdence.triage.pipeline import (
    DEFAULT_CONTRACT,
    DEFAULT_PLAN_TEMPLATE,
    Outcome,
    Pipeline,
    acknowledge,
    workflow_names,
)
from clawdence.triage.report import (
    render_deployment,
    render_outcome,
    render_repo,
    render_routing,
    render_routing_json,
)
from clawdence.triage.routing import (
    ALIAS_WEIGHT,
    KEYWORD_WEIGHT,
    Candidate,
    Decision,
    Routed,
    classify,
    route,
    score,
)
from clawdence.triage.wiring import (
    MODEL_KEY_ENV,
    repo_store,
    runner,
    secret_names,
    secrets_for,
    vcs,
    worktrees,
)

__all__ = [
    "ALIAS_WEIGHT",
    "CONFIG_FILENAME",
    "CONFIG_SCHEMA_VERSION",
    "DEFAULT_CONTRACT",
    "DEFAULT_PLAN_TEMPLATE",
    "KEYWORD_WEIGHT",
    "MODEL_KEY_ENV",
    "Candidate",
    "Config",
    "ConfigError",
    "Decision",
    "Deployment",
    "Outcome",
    "Paths",
    "Pipeline",
    "Routed",
    "Routing",
    "RunnerConfig",
    "acknowledge",
    "classify",
    "default_config_path",
    "load",
    "parse",
    "render_deployment",
    "render_outcome",
    "render_repo",
    "render_routing",
    "render_routing_json",
    "repo_store",
    "route",
    "runner",
    "score",
    "secret_names",
    "secrets_for",
    "vcs",
    "workflow_names",
    "worktrees",
]
