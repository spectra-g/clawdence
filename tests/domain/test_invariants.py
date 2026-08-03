"""The properties the domain model exists to enforce.

Each test here corresponds to something that was a real defect in v1 or a
decision recorded in an ADR. They are not tests of pydantic; they are tests
that the *choices* survive — that someone refactoring these types later finds
out immediately if they drop one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from clawdence.domain import (
    AgentStage,
    ApprovalStage,
    Budget,
    ContractKind,
    ForEachStage,
    IngestSource,
    McpServer,
    ModelSelector,
    ParallelStage,
    RepeatStage,
    RepoProfile,
    Run,
    RunnerRequest,
    RunnerStage,
    ScriptStage,
    StepType,
    Submitter,
    SubWorkflowStage,
    VerificationContract,
    VerificationResult,
    Workflow,
)
from tests.domain.samples import (
    BUDGET,
    EVENT,
    RUN,
    RUNNER_REQUEST,
    WORK_ITEM,
    WORKFLOW,
    WORKFLOW_STAGES,
)


def test_unknown_fields_are_rejected() -> None:
    """A typo is an error, not a silently ignored field.

    ``timeout_second`` must not produce a stage with no timeout.
    """
    with pytest.raises(ValidationError):
        Budget(max_usd=Decimal("1"), max_dollars=Decimal("2"))  # type: ignore[call-arg]


def test_domain_values_are_immutable() -> None:
    """Records, not mutable state. State lives in the store (S4)."""
    with pytest.raises(ValidationError):
        BUDGET.max_usd = Decimal("999")  # type: ignore[misc]


def test_verification_evidence_requires_a_tree_hash() -> None:
    """Evidence with no tree is not evidence.

    This is the structural half of the fix for the rebase hole: tests pass at
    commit X, a rebase moves the work onto an advanced base, and the merge
    lands a tree nothing verified. A result that cannot exist without naming
    its tree makes that impossible to express.
    """
    with pytest.raises(ValidationError):
        VerificationResult(
            contract=VerificationContract(kind=ContractKind.NONE),
            passed=True,
            attempt=1,
            checked_at=datetime.now(tz=UTC),
        )  # type: ignore[call-arg]


def test_tree_hash_must_be_a_full_hash() -> None:
    """Abbreviations are refused.

    Two abbreviations of different lengths can name the same commit, so
    comparing them for "is this the tree the evidence was produced against"
    is not a reliable equality test.
    """
    with pytest.raises(ValidationError):
        VerificationResult(
            contract=VerificationContract(kind=ContractKind.NONE),
            passed=True,
            tree_hash="9f4c1b2",
            attempt=1,
            checked_at=datetime.now(tz=UTC),
        )


def test_exhausted_retries_can_only_halt() -> None:
    """v1's rule, made unrepresentable rather than merely followed.

    There is no value of ``on_exhausted`` that force-proceeds or
    force-approves. Widening this is a schema change, and a visible one.
    """
    with pytest.raises(ValidationError):
        VerificationContract(kind=ContractKind.TEST_AFTER, on_exhausted="proceed")


def test_exceeding_a_budget_can_only_abort() -> None:
    with pytest.raises(ValidationError):
        Budget(max_usd=Decimal("1"), on_exceeded="warn")


def test_script_commands_are_argv_not_shell_strings() -> None:
    """ADR-0003's divergence from Lobster, enforced by the type.

    Lobster substitutes ``${arg}`` into command *text*, which is command
    injection once untrusted issue text is an argument. A list has no string
    for an argument to break out of — and an interpolated value stays one
    element however many spaces or semicolons it contains.
    """
    stage = ScriptStage(id="run-tests", command=("pytest", "-k", "not slow; rm -rf /"))
    assert stage.command[2] == "not slow; rm -rf /"
    assert len(stage.command) == 3


def test_script_command_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        ScriptStage(id="noop", command=())


def test_duplicate_stage_ids_are_rejected_at_load_time() -> None:
    """Caught before the run spends money, not when execution reaches the
    second stage — Lobster detects cycles at run time and this is the same
    class of mistake."""
    duplicated = (WORKFLOW_STAGES[0], WORKFLOW_STAGES[0])
    with pytest.raises(ValidationError, match="duplicate stage id"):
        Workflow(name="broken", version="1.0.0", stages=duplicated)


def test_a_workflow_needs_at_least_one_stage() -> None:
    with pytest.raises(ValidationError):
        Workflow(name="empty", version="1.0.0", stages=())


def test_stage_ids_are_slugs_the_condition_grammar_can_parse() -> None:
    """``$stage.json.field`` references have to survive tokenising.

    A dot or a space in a stage id would collide with the path separator.
    """
    with pytest.raises(ValidationError):
        ScriptStage(id="run tests", command=("pytest",))
    with pytest.raises(ValidationError):
        ScriptStage(id="run.tests", command=("pytest",))


def test_stage_type_discriminates_the_union() -> None:
    """Parsing a workflow yields the concrete stage type, not the base."""
    workflow = Workflow.model_validate_json(WORKFLOW.model_dump_json())
    assert [type(stage) for stage in workflow.stages] == [
        ScriptStage,
        AgentStage,
        RunnerStage,
        ApprovalStage,
        ForEachStage,
        ParallelStage,
        RepeatStage,
        SubWorkflowStage,
    ]
    assert workflow.stages[1].type is StepType.AGENT


def test_agent_turn_budget_is_declared_and_bounded() -> None:
    """v1 discovered turn budgets by trial; here they are declared.

    Zero turns is not a step, and an unbounded loop is the failure mode the
    declaration exists to prevent.
    """
    with pytest.raises(ValidationError):
        AgentStage(id="ba", role="ba", task="t", model=ModelSelector(model="m"), max_turns=0)
    with pytest.raises(ValidationError):
        AgentStage(id="ba", role="ba", task="t", model=ModelSelector(model="m"), max_turns=99)


def test_agent_context_overflow_fails_loudly_by_default() -> None:
    """Never silently drop context — that was v1's whole Kimi failure class."""
    stage = AgentStage(id="ba", role="ba", task="t", model=ModelSelector(model="m"))
    assert stage.on_context_overflow.value == "fail"


def test_egress_denies_the_git_remote_by_default() -> None:
    """The control plane pushes, not the runner."""
    profile = RepoProfile(id="r", name="acme/r", remote_url="https://example.invalid/r.git")
    assert profile.egress.allow_git_remote is False
    assert profile.egress.unrestricted is False


def test_default_isolation_tier_is_a_container_without_a_docker_socket() -> None:
    """Socket mode is never a default. It is host root with extra steps."""
    profile = RepoProfile(id="r", name="acme/r", remote_url="https://example.invalid/r.git")
    assert profile.isolation_tier.value == "container"


def test_socket_mode_cannot_be_selected_without_acknowledging_it() -> None:
    """§3.2's "opt-in per repo, loudly documented" as a rule the model enforces.

    Loud is the hard part. A warning in a README is one nobody read, and the
    person choosing this tier is usually the person least placed to know that a
    mounted daemon socket is host root by another spelling. So the profile does
    not validate without a second field saying so, and the refusal lands on
    whoever writes the profile rather than on whoever is watching the run.
    """
    fields = {
        "id": "r",
        "name": "acme/r",
        "remote_url": "https://example.invalid/r.git",
        "isolation_tier": "container+docker:socket",
    }
    with pytest.raises(ValidationError, match="docker_socket_acknowledged"):
        RepoProfile.model_validate(fields)
    acknowledged = RepoProfile.model_validate({**fields, "docker_socket_acknowledged": True})
    assert acknowledged.isolation_tier.value == "container+docker:socket"


def test_needing_docker_does_not_by_itself_grant_it() -> None:
    """Two facts, and the probe (S9) only establishes one of them: what the
    repository's tests want is not what the operator agreed to hand over."""
    profile = RepoProfile(
        id="r", name="acme/r", remote_url="https://example.invalid/r.git", needs_docker=True
    )
    assert profile.isolation_tier.value == "container"
    assert profile.docker_socket_acknowledged is False


def test_work_is_untrusted_by_the_time_it_reaches_a_runner() -> None:
    """``Submitter.trusted`` is deny-by-default and so is what it becomes.

    §3.3 gates Docker capability on the provenance of the work rather than on
    the repository alone, which only holds if the provenance survives the trip:
    a field that defaulted to trusted here would quietly re-grant the capability
    to every source that routes to an opted-in repository.
    """
    assert RUNNER_REQUEST.trusted_provenance is False


def test_mcp_config_names_an_env_var_and_never_holds_a_token() -> None:
    """Profiles are written to disk and printed by ``clawdence probe``."""
    assert "mcp_servers" in RepoProfile.model_fields
    assert set(McpServer.model_fields) == {"name", "url", "bearer_token_env_var"}


def test_runner_request_carries_no_control_plane_secrets() -> None:
    """The request crosses the trust boundary, so its shape is the boundary.

    A field named for a credential here would be a hole that no amount of
    careful calling could close.
    """
    forbidden = ("token", "secret", "password", "credential", "api_key")
    for field in RunnerRequest.model_fields:
        assert not any(word in field.lower() for word in forbidden), field


def test_runner_worktree_path_must_be_absolute() -> None:
    """Path identity between host and container is not cosmetic: testcontainers
    hands host paths to the daemon when mounting sibling volumes, so a relative
    or differing path breaks those mounts silently."""
    payload = RUNNER_REQUEST.model_dump(mode="json")
    payload["worktree_path"] = "work/run-1"
    with pytest.raises(ValidationError):
        RunnerRequest.model_validate(payload)


def test_raw_request_text_is_preserved_verbatim() -> None:
    """v1's ``slackMessageRaw`` lesson.

    Repo routing reads this field. Stripping it — even just whitespace — is
    the first step towards routing off a paraphrase, which is what broke
    product-name matching in v1.
    """
    item = WORK_ITEM
    assert item.raw_text.startswith("  ")
    assert item.raw_text.endswith("  ")
    restored = type(item).model_validate_json(item.model_dump_json())
    assert restored.raw_text == item.raw_text


def test_submitters_are_untrusted_by_default() -> None:
    """Deny-by-default for public sources. A GitHub issue is a stranger's text
    flowing into an agent prompt and then a runner with write access."""
    assert Submitter(source=IngestSource.GITHUB, external_id="octocat").trusted is False


def test_naive_datetimes_are_rejected() -> None:
    """Watchdogs compare timestamps written by different processes."""
    payload = RUN.model_dump(mode="json")
    payload["created_at"] = "2026-07-28T09:30:00"
    with pytest.raises(ValidationError):
        Run.model_validate(payload)


def test_work_item_targets_repos_one_to_many() -> None:
    """Plan open question 10, closed in the schema.

    v2.0 populates exactly one, but widening this later would touch every
    consumer and it costs nothing now.
    """
    item = WORK_ITEM
    assert isinstance(item.repos, tuple)
    assert item.model_copy(update={"repos": ("a", "b")}).repos == ("a", "b")


def test_audit_records_whether_redaction_ran() -> None:
    """You cannot delete from an append-only store, so the screening pass has
    to be observable — a record written by a path that skipped it is a bug
    worth being able to find."""
    assert EVENT.redacted is True
