"""S4b.1's durable lifecycle, below any particular adapter."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from clawdence.domain import EventKind, RunStatus
from clawdence.ports import PermanentError, TransientError
from clawdence.store import EffectState, ExternalEffects, StateStore
from tests.conftest import StoreFactory
from tests.store.factories import RUN_ID, TEST_CREDENTIAL, at, make_run


def queue(state: StateStore, *, effect_id: str = "fx.one", key: str = "key.one") -> ExternalEffects:
    state.create_run(make_run(status=RunStatus.DONE))
    effects = ExternalEffects(state, clock=lambda: at(0))
    effects.enqueue(
        effect_id=effect_id,
        idempotency_key=key,
        run_id=RUN_ID,
        kind="publish_pull_request",
        command={"repository_id": "repo.test", "body": "do not put me in audit"},
        max_attempts=3,
    )
    return effects


def test_enqueue_is_idempotent_but_the_command_is_immutable(state: StateStore) -> None:
    effects = queue(state)

    same = effects.enqueue(
        effect_id="fx.a-different-generated-id",
        idempotency_key="key.one",
        run_id=RUN_ID,
        kind="publish_pull_request",
        command={"repository_id": "repo.test", "body": "do not put me in audit"},
        max_attempts=3,
    )

    assert same.id == "fx.one"
    assert len(effects.list()) == 1
    assert len(state.audit.read(run_id=RUN_ID, kinds=[EventKind.EXTERNAL_EFFECT_ENQUEUED])) == 1

    with pytest.raises(ValueError, match="different effect"):
        effects.enqueue(
            effect_id="fx.two",
            idempotency_key="key.one",
            run_id=RUN_ID,
            kind="publish_pull_request",
            command={"repository_id": "repo.test", "body": "changed"},
            max_attempts=3,
        )


def test_commands_and_provider_errors_are_screened_before_storage(state: StateStore) -> None:
    state.create_run(make_run(status=RunStatus.DONE))
    effects = ExternalEffects(state, clock=lambda: at(0))
    queued = effects.enqueue(
        effect_id="fx.secret",
        idempotency_key="key.secret",
        run_id=RUN_ID,
        kind="publish_pull_request",
        command={"body": f"accidentally pasted {TEST_CREDENTIAL}"},
    )
    claimed = effects.claim(queued.id, owner="worker", at=at(1))
    assert claimed is not None
    parked = effects.failed(
        queued.id,
        owner="worker",
        error=PermanentError("rejected", f"provider echoed {TEST_CREDENTIAL}"),
        at=at(2),
    )

    assert parked.command == {"body": "accidentally pasted [redacted]"}
    assert parked.error_detail == "provider echoed [redacted]"


def test_two_drainers_cannot_claim_one_logical_delivery(state: StateStore) -> None:
    effects = queue(state)

    first = effects.claim("fx.one", owner="drainer.a", at=at(1))
    second = effects.claim("fx.one", owner="drainer.b", at=at(1))

    assert first is not None
    assert first.attempts == 1
    assert second is None
    assert effects.require("fx.one").claim_owner == "drainer.a"


def test_two_process_style_drainers_forced_to_race_have_one_winner(
    stores: StoreFactory, tmp_path: Path
) -> None:
    path = tmp_path / "state.db"
    source = stores(path)
    queue(source)
    barrier = Barrier(2)

    def race(owner: str) -> str | None:
        with StateStore.open(path) as contender:
            barrier.wait()
            claimed = ExternalEffects(contender).claim("fx.one", owner=owner, at=at(1))
            return None if claimed is None else claimed.claim_owner

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(race, ("drainer.a", "drainer.b")))

    winners = [owner for owner in results if owner is not None]
    assert len(winners) == 1
    assert ExternalEffects(source).require("fx.one").claim_owner == winners[0]


def test_an_expired_claim_is_recovered_after_a_process_crash(state: StateStore) -> None:
    effects = queue(state)
    assert effects.claim("fx.one", owner="dead", lease_seconds=10, at=at(1)) is not None

    assert effects.claim("fx.one", owner="early", at=at(10)) is None
    recovered = effects.claim("fx.one", owner="replacement", at=at(11))

    assert recovered is not None
    assert recovered.claim_owner == "replacement"
    assert recovered.attempts == 2


def test_transient_failures_back_off_and_exhaustion_parks(state: StateStore) -> None:
    effects = queue(state)
    first = effects.claim("fx.one", owner="worker", at=at(1))
    assert first is not None

    waiting = effects.failed(
        first.id,
        owner="worker",
        error=TransientError("forge-down", "provider body that stays out of audit"),
        at=at(2),
    )

    assert waiting.state is EffectState.PENDING
    assert waiting.next_attempt_at == at(32)
    assert effects.claim(first.id, owner="too-early", at=at(31)) is None

    second = effects.claim(first.id, owner="worker", at=at(32))
    assert second is not None
    waiting = effects.failed(
        first.id,
        owner="worker",
        error=TransientError("forge-down", "again"),
        at=at(33),
    )
    assert waiting.next_attempt_at == at(93)

    third = effects.claim(first.id, owner="worker", at=at(93))
    assert third is not None
    parked = effects.failed(
        first.id,
        owner="worker",
        error=TransientError("forge-down", "exhausted"),
        at=at(94),
    )
    assert parked.state is EffectState.PARKED


def test_permanent_errors_park_immediately_and_operator_retry_is_explicit(
    state: StateStore,
) -> None:
    effects = queue(state)
    claimed = effects.claim("fx.one", owner="worker", at=at(1))
    assert claimed is not None

    parked = effects.failed(
        claimed.id,
        owner="worker",
        error=PermanentError("bad-credentials", "token was rejected"),
        at=at(2),
    )

    assert parked.state is EffectState.PARKED
    assert parked.attempts == 1
    assert effects.due(at=at(10_000)) == ()

    retried = effects.retry(parked.id, at=at(3))
    assert retried.state is EffectState.PENDING
    assert retried.attempts == 0
    assert effects.claim(parked.id, owner="fixed", at=at(3)) is not None


def test_several_effects_belong_to_one_run_and_delivery_is_orthogonal(
    state: StateStore,
) -> None:
    effects = queue(state)
    effects.enqueue(
        effect_id="fx.two",
        idempotency_key="key.two",
        run_id=RUN_ID,
        kind="publish_pull_request",
        command={"repository_id": "repo.test", "body": "second"},
    )
    claimed = effects.claim("fx.one", owner="worker", at=at(1))
    assert claimed is not None
    effects.delivered(claimed.id, owner="worker", at=at(2))

    assert state.require_run(RUN_ID).status is RunStatus.DONE
    assert [effect.state for effect in effects.list(run_id=RUN_ID)] == [
        EffectState.DELIVERED,
        EffectState.PENDING,
    ]


def test_audit_transitions_never_copy_command_or_error_detail(state: StateStore) -> None:
    effects = queue(state)
    claimed = effects.claim("fx.one", owner="worker", at=at(1))
    assert claimed is not None
    effects.failed(
        claimed.id,
        owner="worker",
        error=PermanentError("no-auth", "secret-provider-echo"),
        at=at(2),
    )

    encoded = "\n".join(str(event.payload) for event in state.audit.read(run_id=RUN_ID))
    assert "do not put me in audit" not in encoded
    assert "secret-provider-echo" not in encoded
    assert "no-auth" in encoded


def test_a_clean_database_restore_preserves_and_drains_pending_effects(
    stores: StoreFactory, tmp_path: Path
) -> None:
    source = stores(tmp_path / "source.db")
    queue(source)
    restored = stores(tmp_path / "restored.db")

    source.connection.backup(restored.connection)

    effects = ExternalEffects(restored)
    pending = effects.require("fx.one")
    assert pending.state is EffectState.PENDING
    claimed = effects.claim(pending.id, owner="restored", at=at(1))
    assert claimed is not None
    delivered = effects.delivered(claimed.id, owner="restored", at=at(2))
    assert delivered.state is EffectState.DELIVERED
