"""Fixtures for the runner tests: a real repository, and a request pointed at it.

Everything here builds on ``tests/conftest``'s ``repos`` factory, so these run
against a genuine git repository with genuine hashes. That matters more for the
runner than for anything else in the suite: the whole result hangs on a tree hash
and a diff count, and a fixture that invented them would let the interesting
tests pass without ever meeting git's opinion.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clawdence.domain import (
    Budget,
    ContractKind,
    IsolationTier,
    RepoProfile,
    ResourceCaps,
    RunnerRequest,
    VerificationContract,
)
from tests.conftest import RepoFactory
from tests.harness.engine import FakeEngine
from tests.harness.repos import FixtureRepo

START = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

#: A request builder. Everything has a default so a test names only the one
#: thing it is about.
RequestFactory = Callable[..., RunnerRequest]


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def host_profile(**overrides: object) -> RepoProfile:
    """A profile the host runner will accept.

    ``HOST`` is spelled out here rather than defaulted anywhere in the product:
    ``RepoProfile.isolation_tier`` defaults to ``CONTAINER``, and the host runner
    refuses anything else, so every test that runs one has to have said so.
    """
    fields: dict[str, object] = {
        "id": "repo.fixture",
        "name": "fixture",
        "remote_url": "https://forge.invalid/fixture",
        "isolation_tier": IsolationTier.HOST,
    }
    fields.update(overrides)
    return RepoProfile.model_validate(fields)


#: An image reference that is digest-pinned and does not exist. Pinned because
#: the runner refuses a tag (§3.8) and every hermetic test would otherwise be
#: testing that refusal; non-existent because nothing here reaches a registry.
PINNED_IMAGE = "registry.invalid/clawdence/runner@sha256:" + "0" * 64


def container_profile(**overrides: object) -> RepoProfile:
    """A profile the container runner will accept.

    ``CONTAINER`` is the ``RepoProfile`` default, so unlike ``host_profile``
    this is not correcting anything — it exists so a test reads as being about
    the container tier rather than about a default it happens to inherit.
    """
    fields: dict[str, object] = {
        "id": "repo.fixture",
        "name": "fixture",
        "remote_url": "https://forge.invalid/fixture",
        "isolation_tier": IsolationTier.CONTAINER,
    }
    fields.update(overrides)
    return RepoProfile.model_validate(fields)


@pytest.fixture
def fake_engine(tmp_path: Path) -> FakeEngine:
    """A container engine that records what it was asked for, and obeys."""
    return FakeEngine(root=tmp_path / "engine")


@pytest.fixture
def repo(repos: RepoFactory) -> FixtureRepo:
    """One repository, already committed, with a file worth changing."""
    return repos(extra_files={"app.py": "def add(a, b):\n    return a + b\n"})


@pytest.fixture
def request_for(repo: FixtureRepo) -> RequestFactory:
    """Builds requests against ``repo``.

    A factory rather than a request, because the two properties the contract
    suite checks — that a redelivery collides and that a second attempt does not
    — are statements about two requests.
    """

    def build(
        stage_id: str = "code",
        *,
        run_id: str = "run.test",
        attempt: int = 1,
        profile: RepoProfile | None = None,
        contract: VerificationContract | None = None,
        budget: Budget | None = None,
        plan: str = "make add() handle strings",
        carried_stubs: tuple[str, ...] = (),
        worktree: Path | None = None,
        wall_clock_seconds: float | None = None,
    ) -> RunnerRequest:
        resolved = profile or host_profile()
        if wall_clock_seconds is not None:
            resolved = resolved.model_copy(
                update={"caps": ResourceCaps(wall_clock_seconds=wall_clock_seconds)}
            )
        return RunnerRequest(
            run_id=run_id,
            stage_id=stage_id,
            work_item_id="wi.test",
            worktree_path=str(worktree or repo.path),
            branch=f"clawdence/{stage_id}",
            base_commit=repo.head,
            profile=resolved,
            contract=contract or VerificationContract(kind=ContractKind.TEST_AFTER),
            budget=budget or Budget(),
            plan=plan,
            carried_stubs=carried_stubs,
            idempotency_key=f"{run_id}:{stage_id}:{attempt}",
            created_at=at(0),
        )

    return build
