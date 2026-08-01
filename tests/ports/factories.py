"""Builders for port tests — data only.

Same rule as ``tests/store/factories``: nothing here opens anything, every
instant comes from ``at()``, and the point is that a test reads as the thing it
is asserting rather than as six lines of constructor boilerplate.

``commit()`` mints hashes the same way ``InMemoryVcs`` does, because
``TreeHash`` is a validated pattern and a test that wants "some other commit"
should not have to remember that it is forty hex digits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from clawdence.domain import (
    Budget,
    ContractKind,
    DiffStat,
    IngestSource,
    RepoProfile,
    RunnerOutcome,
    RunnerRequest,
    RunnerResult,
    SourceRef,
    Submitter,
    VerificationContract,
    WorkItem,
    WorkItemType,
)
from clawdence.ports import KnowledgeItem, KnowledgeKind, Notification, NotificationKind

RUN_ID = "run.test"
REPO_ID = "repo.test"
WORK_ITEM_ID = "wi.test"
START = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

#: An absolute path that is never created. ``RunnerRequest.worktree_path`` is
#: validated as absolute, and deliberately not under a temp directory: nothing
#: in these tests touches a filesystem, and a path that looks like a real
#: scratch directory invites a future test to start writing to it.
WORKTREE = "/clawdence/worktree"


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """One ``asyncio.run`` per test.

    The engine tests take the same line (``tests/engine/factories``): a
    ``pytest-asyncio`` dependency would buy a decorator, and every dependency
    here is pinned exactly and is therefore a standing maintenance obligation.
    """
    return asyncio.run(coroutine)


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def commit(n: int) -> str:
    """A ``TreeHash``-shaped id. ``commit(1)`` is ``0000…0001``."""
    return f"{n:040x}"


def work_item(
    item_id: str = WORK_ITEM_ID,
    *,
    external_id: str = "ext-1",
    source: IngestSource = IngestSource.CLI,
    title: str = "Make the thing work",
    raw_text: str = "make the thing work please",
    created: float = 0.0,
) -> WorkItem:
    return WorkItem(
        id=item_id,
        type=WorkItemType.TASK,
        title=title,
        raw_text=raw_text,
        submitter=Submitter(source=source, external_id="someone", trusted=True),
        source_ref=SourceRef(source=source, external_id=external_id),
        created_at=at(created),
    )


def notification(
    key: str,
    *,
    kind: NotificationKind = NotificationKind.PROGRESS,
    channel: str = "#builds",
    text: str = "working on it",
    thread: str | None = None,
) -> Notification:
    return Notification(
        kind=kind,
        channel=channel,
        text=text,
        thread=thread,
        idempotency_key=key,
    )


def profile(repo_id: str = REPO_ID) -> RepoProfile:
    return RepoProfile(id=repo_id, name="test-repo", remote_url="https://forge.invalid/test-repo")


def runner_request(
    stage_id: str = "code",
    *,
    run_id: str = RUN_ID,
    attempt: int = 1,
    require_non_empty_diff: bool = True,
    plan: str = "add a function",
) -> RunnerRequest:
    return RunnerRequest(
        run_id=run_id,
        stage_id=stage_id,
        work_item_id=WORK_ITEM_ID,
        worktree_path=WORKTREE,
        branch=f"clawdence/{stage_id}",
        base_commit=commit(1),
        profile=profile(),
        contract=VerificationContract(
            kind=ContractKind.TEST_AFTER,
            require_non_empty_diff=require_non_empty_diff,
        ),
        budget=Budget(),
        plan=plan,
        idempotency_key=f"{run_id}:{stage_id}:{attempt}",
        created_at=at(0),
    )


def runner_result(
    stage_id: str = "code",
    *,
    run_id: str = RUN_ID,
    outcome: RunnerOutcome = RunnerOutcome.SUCCEEDED,
    tree_hash: str | None = None,
    files_changed: int = 1,
    commits_ahead: int = 1,
    started: float = 0.0,
    finished: float = 10.0,
) -> RunnerResult:
    """A result whose defaults pass ``validate_result``.

    Defaulting to *valid* is deliberate: the validation tests build an invalid
    one by naming the single field they are breaking, which keeps each of them
    about one rule.
    """
    produced = outcome in (
        RunnerOutcome.SUCCEEDED,
        RunnerOutcome.TESTS_FAILED,
        RunnerOutcome.DROPPED_COMMIT,
    )
    return RunnerResult(
        run_id=run_id,
        stage_id=stage_id,
        outcome=outcome,
        tree_hash=(commit(2) if produced else None) if tree_hash is None else tree_hash,
        diff=DiffStat(files_changed=files_changed, insertions=files_changed, deletions=0),
        # A default that is *consistent* with the diff above: a result claiming
        # two changed files and no commits behind them is §3.7a's dropped
        # commit, and every test using this as a baseline would be asserting
        # against that rather than against the happy path.
        commits_ahead=commits_ahead,
        started_at=at(started),
        finished_at=at(finished),
    )


def knowledge(
    item_id: str,
    text: str,
    *,
    kind: KnowledgeKind = KnowledgeKind.DISCOVERY,
    repo_id: str | None = REPO_ID,
    source: str = "test",
    created: float = 0.0,
) -> KnowledgeItem:
    return KnowledgeItem(
        id=item_id,
        kind=kind,
        text=text,
        repo_id=repo_id,
        source=source,
        created_at=at(created),
    )


def texts(items: Sequence[Any]) -> tuple[str, ...]:
    """``(hit.item.id, …)`` — retrieval assertions read better as ids."""
    return tuple(hit.item.id for hit in items)
