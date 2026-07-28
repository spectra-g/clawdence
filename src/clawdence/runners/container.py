"""A ``RunnerPort`` that runs the agent CLI inside an ephemeral container.

**This is the default tier**, and it is the first one where the trust boundary in
§3.1 is enforced by something other than good intentions. The host tier's answer
to "what stops the agent reading the control plane's database" is *nothing*; this
tier's answer is that the database is not in the container's filesystem. One
mount goes in — the worktree — and that is the whole of what the agent can see.

What each control is actually for, since a list of flags reads as ceremony:

**One mount, and it is the worktree.** §3.1's "all repos ✅ / one worktree ❌" is
this line and no other. The other repositories in the registry are not protected
by a permission check that could be wrong; they are absent from the filesystem
the process runs on. The plan's verification asks for that to be asserted from
*inside* a container rather than from the argv we built, which is what
``tests/runners/test_container_live.py`` does.

**No docker socket, at any tier this class serves.** A process that can reach the
host daemon can ``docker run --net=host -v /:/host``, which is host root by
another spelling — it escapes the network namespace S7b is about to build and
mounts the filesystem this tier just removed. ``IsolationTier`` has a separate
value for that mode precisely so it cannot be arrived at by editing a flag, and
this runner refuses it the same way it refuses ``host``.

**Path identity.** The worktree is mounted at the same absolute path it has on
the host. That is not tidiness: testcontainers hands host paths to the daemon
when it mounts volumes for sibling containers (§3.3), so a differing path breaks
those mounts silently in S8. It also means ``_inspect`` needs no translation —
the tree the control plane reads afterwards is the tree the agent wrote, with no
copy step to lose a file in.

**Not ``--rm``.** The container's exit state is read before it is removed,
because ``OOMKilled`` is the one thing the daemon can tell us that a bare process
cannot. S6 wrote ``Completion.oom_killed`` and left it ``False`` with a comment
saying a container would fill it in; this is that. The host tier's inference —
"a ``SIGKILL`` nobody admits to sending" — reports an operator's ``kill -9`` as
an OOM kill and says so. Here it is a fact.

**Two honest gaps, both named rather than papered over.**

*Egress is not restricted.* The container runs on a normal bridge network,
because an agent that cannot reach an LLM API is not an agent. ``RepoProfile
.egress`` is therefore **not consulted by this tier** — it becomes real in S7b,
and until then a repository configured with ``allow_git_remote=False`` still has
a container that can reach a git remote. Setting ``network="none"`` at
construction is the only enforcement available today, and it is only useful for
a CLI that needs no network at all.

*The disk cap is usually absent.* ``ResourceCaps.disk_mb`` reaches the engine
only where the storage driver supports a quota, which is not the common case (see
``ContainerEngine.supports_storage_quota``). What is capped everywhere is
``/tmp``, which is a tmpfs with a size. The worktree is a host bind mount and no
container flag bounds it; that is the rest of S7's problem, and it is written down
here so nobody reads ``disk_mb`` as enforced.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import replace
from hashlib import blake2s
from pathlib import Path
from typing import ClassVar, Final

from clawdence.domain import BuildSystem, IsolationTier, RunnerRequest
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.errors import PermanentError
from clawdence.ports.secrets import SecretProvider
from clawdence.runners import worktree as wt
from clawdence.runners.agent import (
    HOME_DIR,
    AgentCommand,
    AgentRunner,
    Launch,
    PlanDelivery,
)
from clawdence.runners.engine import (
    ContainerEngine,
    ContainerSpec,
    Mount,
    client_environment,
)
from clawdence.runners.outcome import Completion
from clawdence.runners.stream import LogSink

#: Canonical host-side home for worktrees, from §3.3. Nothing here enforces it —
#: the runner mounts whatever path it is given, at that same path — but S11
#: creates worktrees under it, and having one name for it means the convention is
#: stated in code rather than in a paragraph somebody has to find.
WORK_ROOT: Final = "/clawdence/work"

#: Prefix and label namespace. The labels exist for the reaper in the rest of S7:
#: a container that outlived its run is identifiable as ours without parsing its
#: name for meaning.
NAME_PREFIX: Final = "clawdence"
LABEL_NAMESPACE: Final = "dev.clawdence"

#: Mount options for the container's ``/tmp``. ``nosuid``/``nodev`` because a
#: writable directory that can hold a setuid binary or a device node is a
#: writable directory with a way out of it, and a size because otherwise "fill
#: the disk" means the host's.
_TMPFS_OPTIONS: Final = "rw,nosuid,nodev,noexec,mode=1777,size={size}m"

#: Exit statuses the engine client uses to say *it* failed, before the image's
#: command ever ran: 125 the client itself, 126 not executable, 127 not found.
#: Distinguished from an agent's own non-zero exit for the same reason the host
#: tier distinguishes a missing binary — nothing ran, so nothing about the
#: repository is implied.
_CLIENT_FAILURE_STATUSES: Final = frozenset({125, 126, 127})

#: Tiers this class serves. ``CONTAINER_DOCKER_SOCKET`` is deliberately not here
#: and is not a flag away: see the module docstring.
_TIER: Final = IsolationTier.CONTAINER


class ContainerRunner(AgentRunner):
    """Runs the agent CLI in an ephemeral container. ``IsolationTier.CONTAINER``.

    The image is runner configuration rather than a per-request field, with
    ``RepoProfile.runner_image`` overriding it, because §3.8's two audiences want
    opposite things: a solo user wants a default that works, and a corporate
    adopter has a base image they are required to use and no ability to publish
    it anywhere this project can reach.

    Digest pinning is enforced, not encouraged. A tag is a mutable pointer, and a
    runner that resolves one at dispatch executes whatever was pushed over it
    since the last run — which is a supply-chain change nobody reviewed, applied
    to a process that has the repository open.
    """

    tier: ClassVar[IsolationTier] = _TIER

    __slots__ = (
        "_allow_unpinned_image",
        "_engine",
        "_image",
        "_images",
        "_network",
        "_read_only_rootfs",
        "_tmpfs_mb",
        "_user",
    )

    def __init__(
        self,
        command: AgentCommand,
        *,
        image: str,
        images: Mapping[BuildSystem, str] | None = None,
        engine: ContainerEngine | None = None,
        secrets: SecretProvider | None = None,
        sink: LogSink | None = None,
        clock: Clock = utc_now,
        identity: wt.GitIdentity = wt.DEFAULT_IDENTITY,
        environ: Mapping[str, str] | None = None,
        user: str | None = None,
        network: str = "bridge",
        read_only_rootfs: bool = True,
        tmpfs_mb: int = 512,
        allow_unpinned_image: bool = False,
    ) -> None:
        super().__init__(
            command,
            secrets=secrets,
            sink=sink,
            clock=clock,
            identity=identity,
            environ=environ,
        )
        self._image = image
        self._images = dict(images or {})
        self._engine = engine if engine is not None else ContainerEngine()
        self._network = network
        self._read_only_rootfs = read_only_rootfs
        self._tmpfs_mb = tmpfs_mb
        self._allow_unpinned_image = allow_unpinned_image
        # Files land on a host bind mount, so they land owned by whoever the
        # container ran as. Root-owned files in a worktree the control plane then
        # runs git in is not a theoretical tidiness problem: it is the next run
        # failing to clean up after this one.
        self._user = user if user is not None else f"{os.getuid()}:{os.getgid()}"

    # ----------------------------------------------------------------- hooks

    def _check(self, request: RunnerRequest) -> None:
        image = self._image_for(request)
        if "@sha256:" not in image and not self._allow_unpinned_image:
            raise PermanentError(
                "unpinned-runner-image",
                f"{image!r} is a tag, not a digest — a tag is a mutable pointer and the runner "
                f"would execute whatever was pushed over it since the last run (§3.8). Pin it as "
                f"name@sha256:… or pass allow_unpinned_image=True and accept that",
            )
        _check_mountable(Path(request.worktree_path))

    def _inherited(self, request: RunnerRequest) -> dict[str, str]:
        """Almost nothing, and that is the difference from the host tier.

        The image supplies ``PATH`` and the toolchain; forwarding the control
        plane's would describe a filesystem the container does not have, and
        forwarding its ``HOME`` would name a directory that is not mounted. The
        agent's home is instead inside the worktree, under the directory already
        hidden from git — a CLI whose first act is writing a config file needs
        somewhere writable, and ``--read-only`` means the image is not it.
        """
        return {
            "HOME": f"{request.worktree_path}/{HOME_DIR}",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

    def _launch(self, request: RunnerRequest, worktree: Path, prompt: str) -> Launch:
        environment = self._environment(request)
        visible = {
            name: value
            for name, value in environment.values.items()
            if name not in environment.secret_names
        }
        spec = ContainerSpec(
            name=container_name(request),
            image=self._image_for(request),
            argv=self._cli_argv(request, prompt),
            workdir=request.worktree_path,
            env=visible,
            passthrough_env=tuple(sorted(environment.secret_names)),
            mounts=(Mount(source=worktree),),
            labels=self._labels(request),
            caps=request.profile.caps,
            user=self._user,
            network=self._network,
            read_only_rootfs=self._read_only_rootfs,
            # A path inside the container, not on this machine — the linter's
            # "insecure temporary directory" is about the host's /tmp, and this
            # tmpfs exists so the container never touches it.
            tmpfs={"/tmp": _TMPFS_OPTIONS.format(size=self._tmpfs_mb)},  # noqa: S108
            interactive=self._command.delivery is PlanDelivery.STDIN,
        )
        client = client_environment(self._environ)
        # The credentials, in the client's environment rather than its argv. The
        # engine reads them out by name; the process list never sees them.
        client.update({name: environment.values[name] for name in environment.secret_names})
        return Launch(argv=self._engine.run_argv(spec), env=client, cwd=worktree)

    async def _prepare(self, request: RunnerRequest, worktree: Path) -> str | None:
        """Clear a container this attempt's name already belongs to, then set up.

        The name is derived from the idempotency key, so a crashed control plane
        that is resuming (S4) collides with its own previous attempt's container.
        Removing it first turns "the engine says that name is taken" — which
        would be a ``STARTUP_FAILED`` on every retry, permanently — into a fresh
        run. It is also why the name is derived rather than random: a random name
        never collides and never cleans up either.
        """
        await self._teardown(request)
        return await super()._prepare(request, worktree)

    async def _halt(self, request: RunnerRequest, process: asyncio.subprocess.Process) -> None:
        """Stop the container first, and only then the client that asked for it.

        This order is the whole point, and getting it backwards produced a bug
        worth recording. The client is not the container's parent — the daemon
        is — so killing the client stops nothing; the agent keeps working, keeps
        the run's stdout open, and the reap of the client blocks until *it*
        finishes, because a pipe is not closed while anyone still holds it. A
        timeout of thirty seconds then took as long as the agent felt like
        taking, and a budget kill did not stop the spending.

        Removing the container is what actually ends the work. The client's own
        death follows from it, and the kill below is for the case where it
        somehow does not.
        """
        await self._engine.remove(container_name(request))
        await super()._halt(request, process)

    async def _observe(self, request: RunnerRequest, completion: Completion) -> Completion:
        """Ask the daemon what it saw, before the container is removed.

        Two things it knows that the client's exit status does not: whether the
        kernel OOM-killed the process, and whether there was a container at all.
        The second is what separates "the agent failed" from "the image is not
        there" — same non-zero exit, opposite handling, and v1 conflated exactly
        this class of thing into "runner failed".
        """
        state = await self._engine.state(container_name(request))
        if state is None:
            return self._never_started(completion)
        return replace(
            completion,
            exit_code=state.exit_code if state.exit_code is not None else completion.exit_code,
            oom_killed=state.oom_killed,
        )

    async def _teardown(self, request: RunnerRequest) -> None:
        await self._engine.remove(container_name(request))

    # -------------------------------------------------------------- plumbing

    def _never_started(self, completion: Completion) -> Completion:
        """No container, so decide whether that means *nothing ran*.

        A client-level exit status with no container behind it is the engine
        saying it could not start one. Anything else — a container an operator
        removed while the run was going, a daemon restarted mid-run — leaves the
        completion alone, because the process did run and its exit status is
        still the best account of it.
        """
        if completion.exit_code not in _CLIENT_FAILURE_STATUSES:
            return completion
        return replace(
            completion,
            startup_error=(
                f"the container engine exited {completion.exit_code} without starting a "
                f"container — the image is missing, or the command in it is not executable"
            ),
        )

    def _image_for(self, request: RunnerRequest) -> str:
        """The repo's image, this build system's, or the default. In that order.

        Three levels because §3.8's users are three: one who overrides nothing,
        one who wants a JDK for their Java repositories, and one whose security
        team publishes the only image they are allowed to run.
        """
        override = request.profile.runner_image
        if override:
            return override
        return self._images.get(request.profile.build_system, self._image)

    def _labels(self, request: RunnerRequest) -> dict[str, str]:
        return {
            f"{LABEL_NAMESPACE}/run-id": request.run_id,
            f"{LABEL_NAMESPACE}/stage-id": request.stage_id,
            f"{LABEL_NAMESPACE}/work-item-id": request.work_item_id,
            f"{LABEL_NAMESPACE}/attempt": request.idempotency_key,
        }


def container_name(request: RunnerRequest) -> str:
    """A legal, stable, collision-free container name for one attempt.

    Stable because ``_prepare`` uses it to clear a previous attempt's leftovers
    and ``_observe`` uses it to ask what happened; derived from the idempotency
    key because that is what "one attempt" already means everywhere else.

    The stage id goes in unescaped, which is safe rather than lucky: ``StageId``
    is a ``Slug``, and a slug's alphabet is already a subset of what container
    names accept. The digest is what makes the name unique — the idempotency key
    is an ``Identifier`` and *that* alphabet is not, so it cannot be used
    directly and truncating it would collide across attempts.
    """
    digest = blake2s(request.idempotency_key.encode("utf-8"), digest_size=8).hexdigest()
    return f"{NAME_PREFIX}-{request.stage_id}-{digest}"


def _check_mountable(worktree: Path) -> None:
    """Refuse to bind-mount somewhere that would hand over more than a worktree.

    A ``worktree_path`` is the one field of the request that becomes a mount, so
    it is the one field where a wrong value is not a failed run but a container
    with the operator's home directory in it. The domain type already requires an
    absolute path; what it cannot say is that the path is a *worktree* rather
    than the root of everything.

    Depth rather than a denylist of names: ``/``, ``/home`` and ``/Users`` are
    three spellings of the same mistake and the next platform has a fourth, while
    "at least two components" admits ``/clawdence/work/<runId>`` and every
    plausible development checkout.
    """
    parts = [part for part in worktree.parts if part not in ("/", "")]
    if len(parts) < 2:
        raise PermanentError(
            "worktree-too-shallow",
            f"refusing to bind-mount {worktree} into a runner — it is a filesystem root or a "
            f"top-level directory, and the container is supposed to receive one worktree "
            f"rather than everything under it (§3.1)",
        )
