"""The domain model — typed schemas, no behaviour.

This package is the spine. Everything above it (the workflow engine, the state
store, the adapters, the runner protocol) is written against these types, and
the JSON Schema in ``schemas/`` is generated from them rather than maintained
alongside them — one source, so the two cannot drift.

The layering is strict and worth preserving, because a cycle here would mean
two parts of the spine cannot be understood independently::

    _base ─ ids
      └─ budget ─ verification ─ repo
                        └─ runner
      └─ work_item
      └─ workflow ─ run ─ events
"""

from __future__ import annotations

from clawdence.domain._base import DomainModel
from clawdence.domain.budget import Budget, CostEntry, TokenUsage
from clawdence.domain.events import EVENT_SCHEMA_VERSION, Actor, ActorKind, Event, EventKind
from clawdence.domain.ids import (
    Condition,
    EventId,
    Identifier,
    RepoId,
    RunId,
    SemVer,
    Slug,
    StageId,
    StepResultId,
    TreeHash,
    WorkItemId,
)
from clawdence.domain.repo import (
    DEFAULT_BRANCH_PREFIX,
    BuildSystem,
    CheckoutPolicy,
    E2EPolicy,
    EgressPolicy,
    IsolationTier,
    McpServer,
    MergeMethod,
    PullRequestPolicy,
    RepoProfile,
    ResourceCaps,
    RoutingSignals,
)
from clawdence.domain.run import Run, RunStatus, StepError, StepResult, StepStatus
from clawdence.domain.runner import (
    MAX_DIRTY_PATHS,
    DiffStat,
    RunnerOutcome,
    RunnerRequest,
    RunnerResult,
)
from clawdence.domain.verification import (
    ContractKind,
    FailingAssertion,
    ResumeVerb,
    TestEvidence,
    TestReporter,
    VerificationContract,
    VerificationResult,
)
from clawdence.domain.work_item import (
    IngestSource,
    SourceRef,
    Submitter,
    WorkItem,
    WorkItemType,
)
from clawdence.domain.workflow import (
    WORKFLOW_SCHEMA_VERSION,
    AgentStage,
    ApprovalStage,
    ContextOverflowPolicy,
    ModelCapability,
    ModelSelector,
    OnError,
    RetryPolicy,
    RunnerStage,
    ScriptStage,
    Stage,
    StageBase,
    StepType,
    Workflow,
)

__all__ = [
    "DEFAULT_BRANCH_PREFIX",
    "EVENT_SCHEMA_VERSION",
    "MAX_DIRTY_PATHS",
    "WORKFLOW_SCHEMA_VERSION",
    "Actor",
    "ActorKind",
    "AgentStage",
    "ApprovalStage",
    "Budget",
    "BuildSystem",
    "CheckoutPolicy",
    "Condition",
    "ContextOverflowPolicy",
    "ContractKind",
    "CostEntry",
    "DiffStat",
    "DomainModel",
    "E2EPolicy",
    "EgressPolicy",
    "Event",
    "EventId",
    "EventKind",
    "FailingAssertion",
    "Identifier",
    "IngestSource",
    "IsolationTier",
    "McpServer",
    "MergeMethod",
    "ModelCapability",
    "ModelSelector",
    "OnError",
    "PullRequestPolicy",
    "RepoId",
    "RepoProfile",
    "ResourceCaps",
    "ResumeVerb",
    "RetryPolicy",
    "RoutingSignals",
    "Run",
    "RunId",
    "RunStatus",
    "RunnerOutcome",
    "RunnerRequest",
    "RunnerResult",
    "RunnerStage",
    "ScriptStage",
    "SemVer",
    "Slug",
    "SourceRef",
    "Stage",
    "StageBase",
    "StageId",
    "StepError",
    "StepResult",
    "StepResultId",
    "StepStatus",
    "StepType",
    "Submitter",
    "TestEvidence",
    "TestReporter",
    "TokenUsage",
    "TreeHash",
    "VerificationContract",
    "VerificationResult",
    "WorkItem",
    "WorkItemId",
    "WorkItemType",
    "Workflow",
]
