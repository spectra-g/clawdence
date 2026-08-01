"""The first external-effect handler: publish one pinned pull request.

The generic store knows nothing about GitHub.  This handler validates the
immutable command and performs operations whose adapter contract is
idempotent: create the named branch at the pinned hash, push that same hash,
then find-or-open one pull request for the branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from clawdence.domain import (
    DomainModel,
    PullRequestPolicy,
    RepoId,
    TreeHash,
    WorkItemId,
)
from clawdence.ports.errors import PortError
from clawdence.ports.vcs import PullRequest, VcsPort
from clawdence.store import EffectKind, ExternalEffect, ExternalEffects
from clawdence.triage.config import ConfigError, Deployment
from clawdence.vcs import RepoStore


class PublishPullRequestCommand(DomainModel):
    """Everything delivery needs, and nothing from the complete item/workflow."""

    repository_id: RepoId
    work_item_id: WorkItemId
    branch: str
    base_commit: TreeHash
    head_commit: TreeHash
    title: str
    body: str
    base_branch: str
    policy: PullRequestPolicy


@dataclass(slots=True)
class PublicationEffectHandler:
    deployment: Deployment
    effects: ExternalEffects
    repos: RepoStore
    vcs: VcsPort
    owner: str

    async def deliver(
        self,
        effect: ExternalEffect,
        *,
        inspect_path: Path | None = None,
    ) -> PullRequest | None:
        """Deliver an already-claimed effect and settle its durable state."""
        if effect.kind != EffectKind.PUBLISH_PULL_REQUEST:
            self.effects.park(
                effect.id,
                owner=self.owner,
                error_kind="unsupported-effect-kind",
                error_detail=f"no handler is installed for {effect.kind!r}",
            )
            return None
        try:
            command = PublishPullRequestCommand.model_validate(effect.command)
        except ValidationError as exc:
            self.effects.park(
                effect.id,
                owner=self.owner,
                error_kind="invalid-effect-command",
                error_detail=str(exc),
            )
            return None
        try:
            profile = self.deployment.profile(command.repository_id)
        except ConfigError as exc:
            self.effects.park(
                effect.id,
                owner=self.owner,
                error_kind="repository-not-configured",
                error_detail=str(exc),
            )
            return None

        source = inspect_path or self.repos.mirror(profile)
        try:
            await self.vcs.create_branch(
                command.repository_id,
                command.branch,
                from_commit=command.head_commit,
            )
            await self.vcs.push(
                command.repository_id,
                command.branch,
                worktree_path=str(source),
                expect_commit=command.head_commit,
            )
            pull = await self.vcs.open_pull_request(
                command.repository_id,
                title=command.title,
                body=command.body,
                head_branch=command.branch,
                base_branch=command.base_branch,
                work_item_id=command.work_item_id,
                policy=command.policy,
            )
        except PortError as exc:
            self.effects.failed(effect.id, owner=self.owner, error=exc)
            return None

        self.effects.delivered(effect.id, owner=self.owner)
        return pull
