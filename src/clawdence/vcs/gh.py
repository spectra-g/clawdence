"""``VcsPort`` over ``git`` and the ``gh`` CLI.

Two transports, split on a line that is not arbitrary: **refs go over git, and
everything that is a forge concept goes over ``gh``.** Branches, commits and
pushes are git; pull requests, reviewers, labels and merges are GitHub's model of
a workflow and have no representation in a repository. Doing the first half over
the API as well would mean re-implementing push over a REST endpoint that cannot
express a pack file, and doing the second half over git would mean inventing a
convention for something the forge already owns.

``gh`` rather than the REST API directly, per the plan's "``gh`` first": it
already solves authentication four ways (a token, a keyring, an app, an
enterprise host), it is what the operator has already configured, and every
answer comes back as JSON with ``--json``. The cost is a process per call and a
dependency on a binary, which is the same trade the container tiers make with
``docker``.

Three details carry the correctness weight.

**The base commit is read live, never taken from the pull request.** GitHub's
``baseRefOid`` is what the base branch pointed at when the PR was last
synchronised, which is precisely *not* the question ``merge`` is asking. The
whole failure this port exists to prevent — evidence produced against a tree that
is no longer what would be merged — is invisible if the base is read from the
same object that is stale. So ``get_pull_request`` resolves the base branch with
``ls-remote`` on every call.

**The head check is made twice, once by us and once by the forge.** Comparing
``expect_head`` locally is a read followed by a write, and a push can land
between them. ``gh pr merge --match-head-commit`` hands the same comparison to
the server, where it is atomic with the merge. Ours stays because it produces
``StaleMergeError`` with both hashes in it, which is what an operator needs and
what the forge's error message does not contain.

**Nothing here re-applies pull request metadata.** ``open_pull_request`` is
idempotent on the branch, so the second call finds an existing PR and returns it
untouched — no label re-added, no reviewer re-requested, no draft state reset. A
retry that reasserted the policy would undo a human's edits, and a human editing
a bot's pull request is the system working.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from clawdence.domain import MergeMethod, PullRequestPolicy, RepoProfile
from clawdence.domain.ids import RepoId, TreeHash, WorkItemId
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.errors import PermanentError, TransientError
from clawdence.ports.secrets import NullSecrets, SecretProvider
from clawdence.ports.vcs import Branch, PullRequest, PullRequestState, StaleMergeError
from clawdence.vcs import policy as policy_rules
from clawdence.vcs.git import GitError
from clawdence.vcs.policy import BranchProtection, ForgeCapabilities, Violation
from clawdence.vcs.store import DEFAULT_TOKEN_NAME, RepoStore

#: Variables ``gh`` is given from the control plane's own environment. ``HOME``
#: is the one that matters: without it ``gh`` cannot find the operator's existing
#: login, and a deployment that has run ``gh auth login`` would be told it is not
#: authenticated. Nothing here names a credential — ``GH_TOKEN`` is added from
#: the ``SecretProvider`` when there is one, and only then.
INHERITED_ENV: Final[tuple[str, ...]] = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "XDG_CONFIG_HOME")

#: Fields fetched for every pull request. Named explicitly rather than taken as a
#: default because ``gh`` has no default: ``--json`` with no fields is an error,
#: which is the right shape — a query that says what it needs does not silently
#: start returning more when the tool is upgraded.
_PR_FIELDS: Final = "number,title,state,isDraft,headRefName,baseRefName,headRefOid,url,mergeCommit"

#: gh exits 1 for everything, so the classification is the message. These are the
#: ones worth another attempt: the forge is up but busy, or between deploys.
_TRANSIENT: Final[tuple[str, ...]] = (
    "was submitted too quickly",
    "rate limit",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "504 Gateway",
    "connection reset",
    "timed out",
)

_SSH_REMOTE = re.compile(r"^(?:[^@]+@)?(?P<host>[^:/]+):(?P<path>.+?)(?:\.git)?/?$")


class GhUnavailableError(PermanentError):
    """``gh`` is not installed, or not on the path this process can see."""

    def __init__(self, gh_path: str) -> None:
        super().__init__(
            "gh-unavailable",
            f"{gh_path!r} could not be executed. The GitHub adapter shells out to the gh CLI; "
            f"install it, or wire a different VcsPort",
        )


def repo_slug(remote_url: str) -> str:
    """``owner/name`` from a clone URL, for ``gh --repo``.

    Both forms, because both are what people have in their configuration: an
    https URL and the scp-like ssh form, which is not a URL at all and which
    ``urlsplit`` parses into something confidently wrong.

    The **last two** path components, not "the path must have exactly two". A
    forge URL never has more, but a ``file://`` remote is an absolute path and is
    legitimately deep — and that is not only the test transport, it is how a
    mirror on a shared filesystem is addressed.
    """
    ssh = _SSH_REMOTE.match(remote_url)
    if ssh is not None and "://" not in remote_url:
        path = ssh.group("path")
    elif "://" in remote_url:
        path = urlsplit(remote_url).path
    else:
        raise PermanentError(
            "unrecognised-remote", f"{remote_url!r} is not a URL a forge slug can be read from"
        )

    parts = [part for part in path.strip("/").removesuffix(".git").split("/") if part]
    if len(parts) < 2:
        raise PermanentError(
            "unrecognised-remote",
            f"{remote_url!r} does not name an owner and a repository, which is what --repo needs",
        )
    return "/".join(parts[-2:])


@dataclass(slots=True)
class GhVcs:
    """GitHub, through ``gh`` and ``git``.

    ``profiles`` is how a ``repo_id`` — all the port passes — becomes a remote
    URL, a branch namespace and a merge preference. A mapping supplied by the
    composition root rather than a lookup this performs, because "which
    repositories exist" is configuration and an adapter that discovered them
    would be deciding what the system is allowed to touch.
    """

    store: RepoStore
    profiles: Mapping[str, RepoProfile]
    gh_path: str = "gh"
    secrets: SecretProvider = field(default_factory=NullSecrets)
    token_name: str | None = DEFAULT_TOKEN_NAME
    clock: Clock = utc_now
    environ: Mapping[str, str] | None = None

    # ------------------------------------------------------------------ refs

    async def head(self, repo_id: RepoId, ref: str) -> TreeHash:
        """What the remote has on ``ref``, right now.

        ``ls-remote`` rather than a fetch and a ``rev-parse``: one round trip,
        no objects transferred, and no chance of answering from a mirror that
        was last updated at the start of the run. The port says this is
        deliberately not cached, and a fetch-then-read would be a cache with
        extra steps.
        """
        profile = self._profile(repo_id)
        mirror = await self.store.ensure(profile, fetch=False)
        for candidate in (f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
            found = await self._ls_remote(profile, mirror, candidate)
            if found is not None:
                return found
        raise PermanentError("unknown-ref", f"{repo_id} has no ref {ref!r}")

    async def create_branch(self, repo_id: RepoId, name: str, *, from_commit: TreeHash) -> Branch:
        profile = self._profile(repo_id)
        mirror = await self.store.ensure(profile, fetch=False)
        existing = await self._ls_remote(profile, mirror, f"refs/heads/{name}")
        if existing == from_commit:
            return Branch(repo_id=repo_id, name=name, head=from_commit)
        if existing is not None:
            raise PermanentError(
                "branch-exists",
                f"{repo_id} already has a branch {name!r}, at {existing} rather than {from_commit}",
            )
        await self.store.push(profile, mirror, f"{from_commit}:refs/heads/{name}")
        return Branch(repo_id=repo_id, name=name, head=from_commit)

    async def push(
        self,
        repo_id: RepoId,
        branch: str,
        *,
        worktree_path: str,
        expect_commit: TreeHash,
    ) -> Branch:
        profile = self._profile(repo_id)
        mirror = await self.store.ensure(profile, fetch=False)
        if await self._ls_remote(profile, mirror, f"refs/heads/{branch}") is None:
            raise PermanentError(
                "unknown-ref",
                f"{repo_id} has no branch {branch!r} to push to; create_branch publishes it, and "
                f"a push that created one would put a typo on somebody's repository",
            )

        # From the worktree, because that is where the objects the runner made
        # live, and pushing an explicit source commit rather than a branch name
        # means the ref the runner produced is the ref that lands — not whatever
        # the worktree's HEAD has become since.
        await self.store.push(profile, Path(worktree_path), f"{expect_commit}:refs/heads/{branch}")

        # The remote's own answer, not ours. A push that reported success and a
        # remote that holds something else is exactly the case §3.10 is about:
        # the control plane does not act on a report from below without checking.
        landed = await self._ls_remote(profile, mirror, f"refs/heads/{branch}")
        if landed != expect_commit:
            raise PermanentError(
                "push-not-landed",
                f"{repo_id} reports {branch!r} at {landed} after a push of {expect_commit}",
            )
        return Branch(repo_id=repo_id, name=branch, head=expect_commit)

    # ---------------------------------------------------------- pull requests

    async def open_pull_request(
        self,
        repo_id: RepoId,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        work_item_id: WorkItemId | None = None,
        policy: PullRequestPolicy | None = None,
    ) -> PullRequest:
        profile = self._profile(repo_id)
        existing = await self._find_open(profile, head_branch)
        if existing is not None:
            return await self._hydrate(profile, existing, work_item_id=work_item_id)

        wanted = policy or profile.pull_request
        argv = [
            "pr",
            "create",
            "--head",
            head_branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body",
            body,
        ]
        if wanted.draft:
            argv.append("--draft")
        for reviewer in (*wanted.reviewers, *wanted.team_reviewers):
            argv += ["--reviewer", reviewer]
        for label in wanted.labels:
            argv += ["--label", label]

        try:
            await self._gh(profile, *argv)
        except PermanentError as exc:
            # A reviewer who left the organisation and a label that was renamed
            # both fail the *whole* create, and neither is a reason to fail a run
            # that produced working code. Retry once with the decoration removed
            # and let the pull request exist.
            if not _is_metadata_failure(exc.message) or not (
                wanted.reviewers or wanted.team_reviewers or wanted.labels
            ):
                raise
            await self._gh(
                profile,
                *["pr", "create", "--head", head_branch, "--base", base_branch],
                *["--title", title, "--body", body],
                *(["--draft"] if wanted.draft else []),
            )

        opened = await self._find_open(profile, head_branch)
        if opened is None:  # pragma: no cover - gh reported success and shows nothing
            raise TransientError(
                "pull-request-not-visible",
                f"created a pull request for {head_branch!r} and {repo_id} does not list it yet",
            )
        return await self._hydrate(profile, opened, work_item_id=work_item_id)

    async def get_pull_request(self, repo_id: RepoId, number: int) -> PullRequest | None:
        profile = self._profile(repo_id)
        try:
            raw = await self._gh(profile, "pr", "view", str(number), "--json", _PR_FIELDS)
        except PermanentError as exc:
            if "not find" in exc.message or "no pull requests found" in exc.message.lower():
                return None
            raise
        return await self._hydrate(profile, _decode(raw))

    async def merge(
        self,
        repo_id: RepoId,
        number: int,
        *,
        expect_head: TreeHash,
        expect_base: TreeHash,
        method: MergeMethod | None = None,
    ) -> PullRequest:
        profile = self._profile(repo_id)
        pull = await self.get_pull_request(repo_id, number)
        if pull is None:
            raise PermanentError("unknown-pull-request", f"{repo_id} has no pull request {number}")

        if pull.head_commit != expect_head:
            raise StaleMergeError(
                what="the pull request head", expected=expect_head, actual=pull.head_commit
            )
        if pull.state is PullRequestState.MERGED:
            return pull
        if pull.base_commit != expect_base:
            raise StaleMergeError(
                what=f"the base branch {pull.base_branch!r}",
                expected=expect_base,
                actual=pull.base_commit,
            )
        if pull.state is PullRequestState.CLOSED:
            raise PermanentError("pull-request-closed", f"pull request {number} was closed")
        if pull.draft:
            raise PermanentError(
                "pull-request-draft",
                f"pull request {number} is a draft, which is the repository asking for a human "
                f"to mark it ready; merging one is not something a retry can reach",
            )

        chosen = method or profile.pull_request.merge_method
        await self._gh(
            profile,
            "pr",
            "merge",
            str(number),
            f"--{chosen.value}",
            # The same comparison the block above made, handed to the server so
            # it is atomic with the merge rather than a read racing a write.
            "--match-head-commit",
            expect_head,
        )
        merged = await self.get_pull_request(repo_id, number)
        if merged is None:  # pragma: no cover - it existed a moment ago
            raise TransientError("pull-request-not-visible", f"pull request {number} disappeared")
        return merged

    # --------------------------------------------------------------- policy

    async def check_policy(self, profile: RepoProfile) -> tuple[Violation, ...]:
        """What this repository's settings say about working on it. See ``policy``.

        Every read here degrades to "unknown" rather than failing, because the
        common case is a token with ``repo`` scope and not ``admin:repo_hook``,
        and refusing to work on a repository because we could not read its branch
        protection would refuse most repositories. Unknown checks nothing; the
        merge is still where a genuinely blocking rule is discovered, which is
        what this exists to make rarer rather than impossible.
        """
        capabilities = await self._capabilities(profile)
        protection = await self._protection(profile)
        return policy_rules.evaluate(profile, protection, capabilities)

    async def _capabilities(self, profile: RepoProfile) -> ForgeCapabilities:
        # No ``--jq``. gh would happily run the expression, and the result would
        # be a shape decided by jq's rules — a bare string for a scalar, ``null``
        # for a missing key, an error for a typo in the filter — none of which
        # this code would see as different from a real answer. Reading the whole
        # object keeps every "what if the field is absent" decision in Python,
        # where it is visible.
        raw = await self._maybe(profile, "api", f"repos/{repo_slug(profile.remote_url)}")
        identity = await self._maybe(profile, "api", "user")
        who = _decode(identity) if identity else {}
        login = who.get("login") if isinstance(who, dict) else None
        if raw is None:
            return ForgeCapabilities(login=login)
        data = _decode(raw)
        permissions = data.get("permissions") or {}
        enabled = {
            MergeMethod.SQUASH: data.get("allow_squash_merge"),
            MergeMethod.MERGE: data.get("allow_merge_commit"),
            MergeMethod.REBASE: data.get("allow_rebase_merge"),
        }
        return ForgeCapabilities(
            login=login,
            can_push=bool(permissions.get("push", True)),
            default_branch=data.get("default_branch"),
            merge_methods=frozenset(method for method, on in enabled.items() if on),
        )

    async def _protection(self, profile: RepoProfile) -> BranchProtection:
        branch = profile.default_branch
        raw = await self._maybe(
            profile,
            "api",
            f"repos/{repo_slug(profile.remote_url)}/branches/{branch}/protection",
        )
        if raw is None:
            return BranchProtection(branch=branch)
        data = _decode(raw)
        checks = data.get("required_status_checks") or {}
        reviews = data.get("required_pull_request_reviews") or {}
        restrictions = data.get("restrictions") or {}
        allowances = [
            entry.get("login") or entry.get("slug", "")
            for entry in (*restrictions.get("users", []), *restrictions.get("teams", []))
        ]
        return BranchProtection(
            branch=branch,
            required_status_checks=tuple(checks.get("contexts") or ()),
            required_approving_reviews=int(reviews.get("required_approving_review_count") or 0),
            require_signed_commits=bool(
                (data.get("required_signatures") or {}).get("enabled", False)
            ),
            restricts_pushes=bool(restrictions),
            push_allowances=tuple(name for name in allowances if name),
        )

    # -------------------------------------------------------------- plumbing

    def _profile(self, repo_id: str) -> RepoProfile:
        profile = self.profiles.get(repo_id)
        if profile is None:
            raise PermanentError(
                "unknown-repository",
                f"nothing is configured for {repo_id!r}; the adapter is given the repositories it "
                f"may touch rather than discovering them",
            )
        return profile

    async def _ls_remote(self, profile: RepoProfile, cwd: Path, ref: str) -> str | None:
        raw = await self.store.remote_git(profile, cwd, "ls-remote", "--", "origin", ref)
        for line in raw.splitlines():
            commit, _, found = line.partition("\t")
            if found.strip() == ref or found.strip().endswith(f"/{ref}"):
                return commit.strip()
        return None

    async def _find_open(self, profile: RepoProfile, head_branch: str) -> dict[str, Any] | None:
        raw = await self._gh(
            profile,
            "pr",
            "list",
            "--head",
            head_branch,
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            _PR_FIELDS,
        )
        found = _decode(raw)
        return found[0] if isinstance(found, list) and found else None

    async def _hydrate(
        self,
        profile: RepoProfile,
        data: Mapping[str, Any],
        *,
        work_item_id: WorkItemId | None = None,
    ) -> PullRequest:
        """A ``PullRequest`` from gh's JSON, with the base commit read live.

        ``baseRefOid`` is deliberately not used even though it is right there in
        the payload: it records where the base was when the pull request was last
        synchronised, and the question every merge decision starts from is where
        the base is *now*.
        """
        base_branch = str(data.get("baseRefName") or profile.default_branch)
        mirror = await self.store.ensure(profile, fetch=False)
        base_commit = await self._ls_remote(profile, mirror, f"refs/heads/{base_branch}")
        if base_commit is None:  # pragma: no cover - the PR names it, so it exists
            raise PermanentError("unknown-ref", f"{profile.id} has no branch {base_branch!r}")

        merge_commit = data.get("mergeCommit") or {}
        now = self.clock()
        return PullRequest(
            repo_id=profile.id,
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            state=PullRequestState(str(data.get("state", "OPEN")).lower()),
            draft=bool(data.get("isDraft", False)),
            head_branch=str(data["headRefName"]),
            base_branch=base_branch,
            head_commit=str(data["headRefOid"]),
            base_commit=base_commit,
            url=data.get("url"),
            work_item_id=work_item_id,
            merge_commit=merge_commit.get("oid") if isinstance(merge_commit, dict) else None,
            created_at=now,
            updated_at=now,
        )

    async def _maybe(self, profile: RepoProfile, *args: str) -> str | None:
        """A ``gh`` call whose failure is an answer of "unknown"."""
        try:
            return await self._gh(profile, *args)
        except (PermanentError, TransientError):
            return None

    async def _gh(self, profile: RepoProfile, *args: str) -> str:
        argv = [self.gh_path, *args, "--repo", repo_slug(profile.remote_url)]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment(),
            )
        except OSError as exc:
            raise GhUnavailableError(self.gh_path) from exc
        raw_out, raw_err = await process.communicate()
        if process.returncode != 0:
            stderr = raw_err.decode("utf-8", errors="replace").strip()
            kind, message = f"gh-{args[0]}", f"gh {' '.join(args)}: {stderr or '(no output)'}"
            if any(marker in stderr for marker in _TRANSIENT):
                raise TransientError(kind, message)
            raise PermanentError(kind, message)
        return raw_out.decode("utf-8", errors="replace")

    def environment(self) -> dict[str, str]:
        """What ``gh`` runs with. Boring, and one credential at most.

        Public because it is the thing worth asserting on: "which of this
        process's variables reach a subprocess" is a security claim, and a claim
        checked by reaching through a private name is one that quietly stops
        being checked the day the name changes.

        Built rather than inherited for the reason ``ScriptHandler`` builds one:
        this process holds every provider key in the system, and handing
        ``os.environ`` to a subprocess puts all of them one ``env`` away from
        anything that can read a child's output.
        """
        source = os.environ if self.environ is None else self.environ
        env = {name: source[name] for name in INHERITED_ENV if name in source}
        env["GH_PROMPT_DISABLED"] = "1"
        env["GH_NO_UPDATE_NOTIFIER"] = "1"
        env["NO_COLOR"] = "1"
        token = None if self.token_name is None else self.secrets.find(self.token_name)
        if token is not None:
            env["GH_TOKEN"] = token.reveal()
        return env


async def read_template(store: RepoStore, profile: RepoProfile, commit: str) -> str | None:
    """The repository's pull request template, as of ``commit``.

    Read from the **base commit**, never from the worktree. The worktree is
    output from a model, so a template read from there is text an agent could
    have written for the system to sign; the base commit is what the repository's
    maintainers actually committed.
    """
    path = profile.pull_request.body_template_path
    if not path:
        return None
    mirror = store.mirror(profile)
    with contextlib.suppress(GitError, OSError):
        return await store.git(mirror, "show", f"{commit}:{path}")
    return None


def render_body(summary: str, *, template: str | None = None) -> str:
    """The pull request body: what this run did, and then the repository's form.

    The template is included **verbatim and unfilled**. A pull request template
    is a list of questions the repository asks its authors — did you add tests,
    did you update the changelog, who is affected — and a system that ticked its
    own boxes would be answering them on behalf of somebody who has not looked
    yet. Leaving it blank puts the questions in front of the reviewer, which is
    where they were meant to be.
    """
    if not template or not template.strip():
        return summary
    return f"{summary.rstrip()}\n\n---\n\n{template.lstrip()}"


def _decode(raw: str) -> Any:
    try:
        return json.loads(raw or "null")
    except ValueError as exc:
        raise TransientError(
            "gh-unreadable", f"gh returned something that is not JSON: {raw[:200]!r}"
        ) from exc


def _is_metadata_failure(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in ("could not add label", "not found", "could not request", "reviewer")
    )


__all__ = [
    "INHERITED_ENV",
    "GhUnavailableError",
    "GhVcs",
    "read_template",
    "render_body",
    "repo_slug",
]
