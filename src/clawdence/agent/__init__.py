"""Agent steps — a role, a task, a model, and a bounded conversation.

``ports.model`` is the *interface*: what a completion means, what failures it can
have, and what can be asked about a model before anything is spent. This package
is everything above it — the step type, the prompt registry, the repair path, the
routing, and one real provider.

The layering is one-directional, as in ``domain``, ``engine``, ``ports`` and
``runners``::

    prompts · repair · response · tools
        └─ routing
            └─ handler
        └─ anthropic

Four of those five are separate modules for the same reason the runner's
``outcome`` is separate from its tiers: each is a pure function over data, and each
is completely testable without a model. ``repair`` never sees a request,
``response`` never sees a provider, ``prompts`` never sees a run, and ``routing``
is exercised by tests where every candidate model is a dictionary entry. What is
left in ``handler`` is the part that genuinely needs all of them at once — the
execution model — and it is small enough to read in one sitting as a result.

**What v1 did ad hoc, and where each of those lives now.** Prompts were
``prompts/*.txt``, unversioned and unoverridable → ``prompts``. Model choice was
per-agent in ``openclaw.json`` → ``ModelSelector`` on the stage, resolved by
``routing``. ``_repair_json`` was called at three call sites with three behaviours
→ ``repair``, once, with every repair named. Turn budgets, context growth and
session resets were worked out by watching failures → declared fields on
``AgentStage``, enforced in ``handler``. Eleven ``skills/`` directories with no
model of what any agent could reach → ``tools``, which is empty and says why.

**Nothing here writes anything.** An agent step's product is a structured result
that enters the normal review path (§1.3). ``AgentHandler`` is constructed with a
model port, a prompt registry, a schema registry and a tool surface; it is given no
store, no workflow loader and no VCS, so there is no path by which its output
arrives already applied. That is what makes the rule enforceable rather than
aspirational.

Wiring is configuration, not code::

    handler = AgentHandler(
        model=AnthropicModels(secrets),
        prompts=PromptRegistry.from_env(),
    )
    registry = default_registry(agent=handler)
"""

from __future__ import annotations

from clawdence.agent.anthropic import (
    API_VERSION,
    CATALOGUE,
    DEFAULT_BASE_URL,
    DEFAULT_SECRET_NAME,
    DEFAULT_TIMEOUT_SECONDS,
    AnthropicModels,
    ProviderHttpError,
    from_payload,
    to_payload,
)
from clawdence.agent.handler import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    ELIDED,
    AgentHandler,
    ContextReport,
    validate_stage,
)
from clawdence.agent.prompts import (
    BUILTIN_ROOT,
    FENCE,
    OVERRIDE_PATH_ENV,
    Prompt,
    PromptNotFoundError,
    PromptOrigin,
    PromptRegistry,
    frame,
)
from clawdence.agent.repair import Repaired, RepairFailed, extract_json
from clawdence.agent.response import (
    DEFAULT_SCHEMAS,
    Assessment,
    ImplementationPlan,
    Requirements,
    ResponseInvalidError,
    ResponseSchemas,
    Review,
    SchemaNotFoundError,
)
from clawdence.agent.routing import Route, candidates
from clawdence.agent.tools import ToolSurface, UnknownToolError

__all__ = [
    "API_VERSION",
    "BUILTIN_ROOT",
    "CATALOGUE",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_SCHEMAS",
    "DEFAULT_SECRET_NAME",
    "DEFAULT_TIMEOUT_SECONDS",
    "ELIDED",
    "FENCE",
    "OVERRIDE_PATH_ENV",
    "AgentHandler",
    "AnthropicModels",
    "Assessment",
    "ContextReport",
    "ImplementationPlan",
    "Prompt",
    "PromptNotFoundError",
    "PromptOrigin",
    "PromptRegistry",
    "ProviderHttpError",
    "RepairFailed",
    "Repaired",
    "Requirements",
    "ResponseInvalidError",
    "ResponseSchemas",
    "Review",
    "Route",
    "SchemaNotFoundError",
    "ToolSurface",
    "UnknownToolError",
    "candidates",
    "extract_json",
    "frame",
    "from_payload",
    "to_payload",
    "validate_stage",
]
