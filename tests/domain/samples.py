"""One fully-populated instance of every exported contract.

Fully populated on purpose. A sample built from defaults exercises almost
nothing: it would round-trip, validate, and tell you only that the required
fields work. These fill in the optional fields too, so a serialisation bug in
``Decimal``, a tz-aware ``datetime``, a nested discriminated union, or a tuple
field shows up as a failing test rather than as a surprise at S4.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from clawdence.domain import (
    Actor,
    ActorKind,
    AgentStage,
    ApprovalStage,
    Budget,
    BuildSystem,
    ContextOverflowPolicy,
    ContractKind,
    CostEntry,
    DiffStat,
    E2EPolicy,
    EgressPolicy,
    Event,
    EventKind,
    FailingAssertion,
    IngestSource,
    IsolationTier,
    McpServer,
    ModelCapability,
    ModelSelector,
    OnError,
    RepoProfile,
    ResourceCaps,
    ResumeVerb,
    RetryPolicy,
    Run,
    RunnerOutcome,
    RunnerRequest,
    RunnerResult,
    RunnerStage,
    RunStatus,
    ScriptStage,
    SourceRef,
    StepError,
    StepResult,
    StepStatus,
    StepType,
    Submitter,
    TestEvidence,
    TestReporter,
    TokenUsage,
    VerificationContract,
    VerificationResult,
    Workflow,
    WorkItem,
    WorkItemType,
)

NOW = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)
LATER = datetime(2026, 7, 28, 9, 47, 12, tzinfo=UTC)

TREE = "9f4c1b2ad3e5f60718293a4b5c6d7e8f90a1b2c3"
BASE = "0123456789abcdef0123456789abcdef01234567"

BUDGET = Budget(
    max_usd=Decimal("4.50"),
    max_tokens=400_000,
    max_wall_clock_seconds=1800.0,
)

USAGE = TokenUsage(
    input_tokens=18_320,
    output_tokens=2_910,
    cached_input_tokens=12_000,
    reasoning_tokens=640,
)

COST_ENTRY = CostEntry(
    run_id="run-01J8ZQ",
    stage_id="implement",
    model="claude-opus-5",
    usage=USAGE,
    usd=Decimal("0.3412"),
    at=NOW,
)

TEST_EVIDENCE = TestEvidence(
    reporter=TestReporter.PYTEST_JSON_REPORT,
    total=214,
    passed=212,
    failed=1,
    skipped=1,
    duration_seconds=41.7,
    failures=(
        FailingAssertion(
            test_id="tests/test_billing.py::test_prorates_mid_cycle",
            file="tests/test_billing.py",
            line=88,
            message="AssertionError: assert Decimal('12.50') == Decimal('12.49')",
            frames=("billing/proration.py:41 in _daily_rate",),
        ),
    ),
)

CONTRACT = VerificationContract(
    kind=ContractKind.OUTSIDE_IN_TDD,
    max_attempts=3,
    pre_verify=("cp", "-n", ".env.example", ".env"),
    require_non_empty_diff=True,
    require_full_test_suite=True,
    allowed_resume_verbs=(ResumeVerb.RETRY, ResumeVerb.SKIP),
)

VERIFICATION_RESULT = VerificationResult(
    contract=CONTRACT,
    passed=False,
    tree_hash=TREE,
    attempt=2,
    evidence=TEST_EVIDENCE,
    checked_at=LATER,
    detail="1 failing assertion in the proration path",
)

REPO_PROFILE = RepoProfile(
    id="acme-billing",
    name="acme/billing",
    remote_url="https://github.com/acme/billing.git",
    default_branch="main",
    build_system=BuildSystem.UV,
    toolchain={"python": "3.13", "node": "24.5"},
    exec_prefix=("mise", "exec", "python@3.13", "--"),
    install_command=("uv", "sync", "--frozen"),
    build_command=("uv", "build"),
    test_command=("uv", "run", "pytest", "--json-report"),
    needs_docker=True,
    isolation_tier=IsolationTier.CONTAINER_DOCKER_DIND_ROOTLESS,
    test_reporter=TestReporter.PYTEST_JSON_REPORT,
    e2e_runner=E2EPolicy.CI_ONLY,
    require_full_test_suite=True,
    agents_md_path="AGENTS.md",
    egress=EgressPolicy(
        allow_llm_api=True,
        allow_package_registries=True,
        allow_mcp_servers=True,
        allow_git_remote=False,
        extra_allowed_hosts=("artifacts.internal.acme.example",),
        unrestricted=False,
    ),
    caps=ResourceCaps(
        cpu_count=4.0,
        memory_mb=8192,
        disk_mb=20480,
        pid_limit=512,
        wall_clock_seconds=2400.0,
    ),
    mcp_servers=(
        McpServer(
            name="acme-docs",
            url="https://mcp.acme.example/docs",
            # The env var's *name*. If a real token could go here the field
            # would be wrong, which is the point of the test that asserts
            # McpServer has no field one could be assigned to.
            bearer_token_env_var="ACME_DOCS_MCP_TOKEN",  # noqa: S106
        ),
    ),
    aliases=("billing", "invoicing"),
    keywords=("invoice", "proration", "subscription"),
)

WORK_ITEM = WorkItem(
    id="wi-01J8ZQ4T",
    type=WorkItemType.BUG,
    title="Proration is off by a cent mid-cycle",
    raw_text="  Billing shows 12.49 instead of 12.50 when a plan changes mid-cycle.  ",
    submitter=Submitter(
        source=IngestSource.SLACK,
        external_id="U024BE7LH",
        display_name="Priya",
        trusted=True,
    ),
    source_ref=SourceRef(
        source=IngestSource.SLACK,
        external_id="1753689000.004200",
        conversation_id="1753688000.001100",
        url="https://acme.slack.com/archives/C01/p1753689000004200",
    ),
    repos=("acme-billing",),
    parent_id="wi-01J8ZQ00",
    labels=("billing", "regression"),
    workflow_override=None,
    created_at=NOW,
    size_estimate="S",
)

WORKFLOW_STAGES = (
    ScriptStage(
        id="checkout",
        name="Prepare the worktree",
        command=("git", "checkout", "-b", "fix/proration"),
        env={"GIT_AUTHOR_NAME": "clawdence"},
        cwd="/clawdence/work/run-01J8ZQ",
        stdin=None,
        on_error=OnError.FAIL,
        retry=RetryPolicy(max_attempts=2, backoff_seconds=1.5),
        timeout_seconds=60.0,
    ),
    AgentStage(
        id="plan",
        name="Senior dev plans the change",
        when='$checkout.json.status == "ok"',
        role="senior-dev",
        prompt_version="3",
        task="Plan the change described in ${intake.json.text}",
        model=ModelSelector(
            model="claude-opus-5",
            fallbacks=("claude-sonnet-5",),
            requires=(ModelCapability.TOOL_CALLING, ModelCapability.STRUCTURED_OUTPUT),
            temperature=0.0,
            seed=17,
        ),
        max_turns=2,
        context_budget_tokens=120_000,
        on_context_overflow=ContextOverflowPolicy.COMPACT,
        response_schema="plan.v1",
        tools=("read", "grep"),
        salvage_partial_output=True,
        budget=Budget(max_usd=Decimal("1.00")),
        timeout_seconds=600.0,
    ),
    RunnerStage(
        id="implement",
        when='$plan.json.confidence != "low"',
        isolation_tier_override=None,
        budget=BUDGET,
        timeout_seconds=2400.0,
        on_error=OnError.SKIP_REST,
    ),
    ApprovalStage(
        id="review",
        prompt="Approve the fix, or reject with feedback.",
        required_approver="priya",
        require_different_approver=True,
        response_schema="review-decision.v1",
        timeout_seconds_override=86_400.0,
    ),
)

WORKFLOW = Workflow(
    schema_version=1,
    name="quick-fix",
    version="1.2.0",
    description="Code, verify, PR. No planning agents.",
    stages=WORKFLOW_STAGES,
    default_budget=BUDGET,
)

RUN = Run(
    id="run-01J8ZQ",
    work_item_id="wi-01J8ZQ4T",
    workflow="quick-fix",
    workflow_version="1.2.0",
    status=RunStatus.VERIFYING,
    repo_id="acme-billing",
    budget=BUDGET,
    created_at=NOW,
    updated_at=LATER,
    finished_at=None,
)

STEP_RESULT = StepResult(
    id="sr-01J8ZQ4T-implement-1",
    run_id="run-01J8ZQ",
    stage_id="implement",
    type=StepType.RUNNER,
    status=StepStatus.FAILED,
    attempt=2,
    idempotency_key="run-01J8ZQ:implement:2",
    started_at=NOW,
    finished_at=LATER,
    output={"diff_files": 3, "tests": {"failed": 1}},
    response=None,
    error=StepError(
        kind="tests-failed",
        message="1 failing assertion in the proration path",
        retryable=True,
    ),
)

RUNNER_REQUEST = RunnerRequest(
    run_id="run-01J8ZQ",
    stage_id="implement",
    work_item_id="wi-01J8ZQ4T",
    worktree_path="/clawdence/work/run-01J8ZQ",
    branch="fix/proration",
    base_commit=BASE,
    profile=REPO_PROFILE,
    contract=CONTRACT,
    budget=BUDGET,
    plan="Fix the daily-rate rounding in billing/proration.py.",
    carried_stubs=("billing/proration.py:_leap_year_rate",),
    idempotency_key="run-01J8ZQ:implement:2",
    created_at=NOW,
)

RUNNER_RESULT = RunnerResult(
    run_id="run-01J8ZQ",
    stage_id="implement",
    outcome=RunnerOutcome.TESTS_FAILED,
    tree_hash=TREE,
    exit_code=1,
    diff=DiffStat(files_changed=3, insertions=41, deletions=12),
    test_evidence=TEST_EVIDENCE,
    usage=USAGE,
    cost=COST_ENTRY,
    discovery_notes=("Proration logic is duplicated in two modules.",),
    unresolved_stubs=("billing/proration.py:_leap_year_rate",),
    started_at=NOW,
    finished_at=LATER,
    message="Tests failed on attempt 2.",
)

EVENT = Event(
    id="ev-01J8ZQ5",
    schema_version=1,
    kind=EventKind.STEP_FINISHED,
    at=LATER,
    run_id="run-01J8ZQ",
    work_item_id="wi-01J8ZQ4T",
    stage_id="implement",
    actor=Actor(kind=ActorKind.RUNNER, id="runner-7", display_name="container runner"),
    payload={"outcome": "tests-failed", "attempt": 2},
    redacted=True,
)

#: Keyed by the exported model's name, so the schema tests can pair each
#: contract with an instance without a second list to keep in sync.
SAMPLES = {
    "Budget": BUDGET,
    "CostEntry": COST_ENTRY,
    "Event": EVENT,
    "RepoProfile": REPO_PROFILE,
    "Run": RUN,
    "RunnerRequest": RUNNER_REQUEST,
    "RunnerResult": RUNNER_RESULT,
    "StepResult": STEP_RESULT,
    "VerificationContract": CONTRACT,
    "VerificationResult": VERIFICATION_RESULT,
    "WorkItem": WORK_ITEM,
    "Workflow": WORKFLOW,
}
