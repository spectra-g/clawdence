"""The Docker capability: ``container+docker:socket``, and what it costs.

Some repositories cannot run their own tests without a container daemon.
Testcontainers is the common case and it is not a niche one — a Spring Boot
service whose integration tests bring up Postgres has no offline path, and a
system that cannot run those tests cannot verify a change to that service. So
the capability has to exist. What it must not do is exist quietly.

**This tier is not weaker isolation. It is none, with extra steps.** A process
that can reach the host's Docker daemon can ask it for a container with
``--net=host`` and ``-v /:/host``, which is the host's network namespace and the
host's filesystem, handed to a process the previous tier had confined to one
worktree on a bridge network. Every control ``container`` builds — the plane
split, the dropped capabilities, the read-only root, and the egress allowlist
S7b is going to add — is defeated by that one call. Nothing in this module
mitigates it, and nothing can: the escape is the feature. What this module does
is make the tier *reachable only deliberately*, and make its debris collectable.

Four gates, and each one is a different person being asked:

1. **The profile.** ``RepoProfile.docker_socket_acknowledged`` has to be true, or
   the profile does not validate at all. That is the configuration-time refusal
   §3.2 asks for, taken by whoever writes the profile.
2. **The request.** ``RunnerRequest.trusted_provenance`` has to be true, and it
   is deny-by-default all the way back to ``Submitter.trusted``. §3.3 gates this
   capability on the provenance of the *work*, not on the repository alone:
   "this repository needs a daemon" and "this request may have one" are separate
   facts, and a repository that opted in once must not thereby opt in every
   source that later routes to it. This is the gate that makes S10b's public
   ingestion survivable — public work reaching a socket-tier repository is
   refused rather than downgraded, because a silent downgrade would run the
   repository's tests without the daemon they need and report the failure as the
   agent's.
3. **The runner.** A ``DockerSocketRunner`` has to have been constructed and
   wired. ``ContainerRunner`` refuses the tier outright, so no amount of
   configuration turns the default runner into this one.
4. **Ryuk.** The environment must not disable it — see below. That one is a
   refusal on behalf of the *host*, which is not otherwise represented.

**Path identity is why the mount design was fixed in S7.** Testcontainers does
not create nested containers; it asks the host daemon for *siblings*. When it
gives one of them a volume mount, the path it sends is the path as this process
sees it — and the daemon resolves it on the host. If the worktree were mounted
at ``/workspace`` inside while living at ``/clawdence/work/<runId>`` outside,
every such mount would resolve to a host path that does not exist and the daemon
would helpfully create an empty directory there. Nothing errors. The test simply
sees no fixtures. ``Mount.target`` has defaulted to ``Mount.source`` since S7
precisely so this tier could not be built wrong.

**``TESTCONTAINERS_HOST_OVERRIDE`` is not a convenience.** §3.3 introduces it as
"so tests can reach mapped ports", which undersells it: the sibling containers
publish their ports on the *host*, and ``localhost`` inside this container is
this container. Every connection a test makes to a fixture goes through that
name — and so does the connection Ryuk needs, which is what makes it load
bearing rather than nice. ``host.docker.internal`` exists by default on Docker
Desktop and does not exist on a native Linux daemon, so this tier also passes
``--add-host host.docker.internal:host-gateway``: the name then resolves in both
places, and a tier that worked on the maintainer's laptop and not on a Linux
host would be one whose failure mode is a hung test suite.

**The daemon is not necessarily on this machine, and that broke the obvious
implementation.** ``socket_path`` is the daemon's path to its own socket, not a
path here — on Docker Desktop, Colima, Lima and Rancher, dockerd runs in a VM
and ``/var/run/docker.sock`` on the developer's Mac is either a forwarding
socket with an unrelated owner or absent entirely. Two things follow. The
preflight cannot be ``Path(socket_path).is_socket()``, because that asks about
the wrong filesystem. And the ``--group-add`` this tier needs — the socket is
group-owned, the container runs as an unprivileged user, and without the group
the mount is present and unopenable — cannot come from a local ``stat`` either:
on a Colima host the gid inside the VM was ``991`` while nothing at all existed
at that path outside it. So the group is discovered by asking the daemon, with
one throwaway container per image per process, and that question doubles as the
preflight: a socket the daemon does not have fails there, before either phase,
saying so.

**Ryuk is left on, and being on is checked rather than hoped for.** Ryuk is
testcontainers' own reaper: a small container, started as a sibling, holding a
connection back to the test process. When that connection drops it removes
everything carrying its session's labels — including itself. That is exactly the
"run died, collect its siblings" case, solved from inside the session by
something that knows which containers are the session's, which is knowledge this
system does not have. So ``TESTCONTAINERS_RYUK_DISABLED`` is set to ``false``
here, and a request whose environment sets it true is **refused**: disabling it
leaks a container per fixture per run onto somebody's host, and the leak is
invisible until the disk is full.

**The sweep below is a backstop, and it is deliberately timid.** Ryuk covers the
normal case; what it does not cover is Ryuk itself being killed, or a daemon
restarted mid-run. So this tier snapshots the set of testcontainers *session ids*
on the daemon before the agent starts and removes what appeared during the run —
subject to one rule that costs precision and buys safety. **A session another
in-flight run could also have created is left alone.** Sibling containers carry
no label naming the run that caused them, and none of the testcontainers
implementations offer a supported way to add one, so overlapping runs make
ownership genuinely ambiguous; claiming an ambiguous session would mean one run
tearing down another run's database halfway through its test suite. Ryuk gets
those, a little later. Precision where it is provable, deference where it is not
— which is the reaper's asymmetry applied to somebody else's containers.

**What is deliberately not built.**

*A sibling sweep in ``reaper``.* The reaper collects things this system created
and can prove it created. A testcontainers container on the host may equally
belong to a developer running their own suite on the same machine, and removing
it costs them a debugging session they were in the middle of. Same reasoning as
images: not ours, not swept.

*Rootless Docker-in-Docker.* §3.3's table forbids socket mode for anything that
arrived through public ingestion and offers ``dind-rootless`` as the hardened
alternative. Public ingestion is S10b and does not exist, so the honest position
today is the one §3.3 names as the fallback: testcontainers for trusted work
only, enforced by gate 2, and said out loud. ``IsolationTier`` already carries
the value; nothing implements it, and no runner will accept it until something
does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import ClassVar, Final

from clawdence.domain import BuildSystem, IsolationTier, RunnerRequest
from clawdence.ports._common import Clock, utc_now
from clawdence.ports.control import DEFAULT_POLL_SECONDS, ControlPort
from clawdence.ports.errors import PermanentError
from clawdence.ports.secrets import SecretProvider
from clawdence.runners import worktree as wt
from clawdence.runners.agent import AgentCommand, Environment, Phase
from clawdence.runners.cache import Cache
from clawdence.runners.container import ContainerRunner
from clawdence.runners.engine import ContainerEngine, ContainerSpec, Mount
from clawdence.runners.installed import Installed
from clawdence.runners.stream import LogSink

#: The daemon's socket, **as the daemon's own machine sees it**. That is the
#: distinction that matters: on Docker Desktop, Colima, Lima and Rancher the
#: daemon runs in a VM, and this is the path inside it — which is why the
#: default is right everywhere even where nothing of the sort exists on the
#: developer's Mac. Mounted at the same path in the container, for the duller
#: reason that every client inside looks there by default.
DOCKER_SOCKET: Final = "/var/run/docker.sock"

#: The name a container uses for the machine outside it. Provided by Docker
#: Desktop, absent on a native Linux daemon, and made to exist in both by
#: ``--add-host …:host-gateway``.
HOST_ALIAS: Final = "host.docker.internal"

#: The label every testcontainers implementation stamps on the containers of one
#: session, Ryuk's own included. It is the only correlation available: there is
#: no supported way to add a label of our own, so this is what the sweep groups
#: by, and grouping by session rather than by container is what stops a sweep
#: removing a database while leaving the fixture that depends on it.
SESSION_LABEL: Final = "org.testcontainers.sessionId"

#: Set to ``false`` for every run, and refused if something sets it true.
RYUK_DISABLED_ENV: Final = "TESTCONTAINERS_RYUK_DISABLED"

#: Where the library looks for the host, and therefore how a test reaches both a
#: fixture's published port and Ryuk.
HOST_OVERRIDE_ENV: Final = "TESTCONTAINERS_HOST_OVERRIDE"

#: Where a client inside the container looks for the daemon. Set explicitly
#: rather than left to the default, because the default is only right while the
#: socket is at the standard path — and ``socket_path`` exists so it need not be.
DOCKER_HOST_ENV: Final = "DOCKER_HOST"

#: Spellings of true that a shell, a CI config and a ``.env`` file all produce.
#: Compared against rather than parsed, because getting this wrong in the
#: permissive direction means silently accepting a run with no reaper.
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})

_TIER: Final = IsolationTier.CONTAINER_DOCKER_SOCKET


class DockerSocketRunner(ContainerRunner):
    """Runs the agent with the host daemon's socket in reach. **Not isolation.**

    A subclass rather than a sibling, and that is the whole shape of this step:
    everything the container tier decided — one repository on the filesystem, no
    control-plane credential in the environment, every capability dropped, a
    read-only root, the caps, the labels, the two phases, the reading of
    ``OOMKilled`` — is correct here too and is inherited unchanged. What differs
    is a mount, a group, a hosts entry and four environment variables, plus two
    refusals this tier owes and the one after the run.

    That matters beyond tidiness. A second implementation "for testcontainers"
    would be a second place for the plane split to be got wrong, and it would be
    the *less*-reviewed of the two protecting the repositories that most need
    protecting.
    """

    tier: ClassVar[IsolationTier] = _TIER

    __slots__ = ("_disowned", "_groups", "_host_alias", "_sessions", "_socket", "_socket_group")

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
        control: ControlPort | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        user: str | None = None,
        network: str = "bridge",
        read_only_rootfs: bool = True,
        tmpfs_mb: int = 512,
        allow_unpinned_image: bool = False,
        cache: Cache | None = None,
        repo_store: Path | None = None,
        socket_path: str = DOCKER_SOCKET,
        host_alias: str = HOST_ALIAS,
        socket_group: str | None = None,
    ) -> None:
        super().__init__(
            command,
            image=image,
            images=images,
            engine=engine,
            secrets=secrets,
            sink=sink,
            clock=clock,
            identity=identity,
            environ=environ,
            control=control,
            poll_seconds=poll_seconds,
            user=user,
            network=network,
            read_only_rootfs=read_only_rootfs,
            tmpfs_mb=tmpfs_mb,
            allow_unpinned_image=allow_unpinned_image,
            cache=cache,
            repo_store=repo_store,
        )
        self._socket = Path(socket_path)
        self._host_alias = host_alias
        self._socket_group = socket_group
        # Discovered gid per image, because discovering it means running one.
        # Keyed by image rather than held once: ``_image_for`` is three-level,
        # so one runner serves several images and they are not all the same
        # distribution.
        self._groups: dict[str, str] = {}
        # Pre-run session snapshots, keyed by attempt. The runner instance is
        # shared by every dispatch under the scheduler, so this is also how one
        # run learns that another was in flight while it ran — which is the only
        # thing that makes the sweep safe.
        self._sessions: dict[str, frozenset[str]] = {}
        # Sessions no attempt may claim, because two of them could. Bounded by
        # what the daemon still holds rather than by time — see ``_sweep``.
        self._disowned: set[str] = set()

    # ----------------------------------------------------------------- hooks

    def _check(self, request: RunnerRequest) -> None:
        """The refusal this tier owes before anything starts: **who asked**.

        ``PermanentError``, for the reason the base class gives: this is not a
        run that happened badly, and a second attempt produces the identical
        refusal with the budget one attempt smaller.

        The other thing worth checking — that there is a socket at
        ``socket_path`` — is deliberately *not* here, because this method cannot
        honestly answer it. ``socket_path`` is the daemon's path to its own
        socket, and on every VM-backed setup the daemon is not on this machine.
        ``_prepare`` asks the daemon instead.
        """
        super()._check(request)

        if not request.trusted_provenance:
            raise PermanentError(
                "untrusted-work-may-not-reach-the-daemon",
                f"{request.profile.name!r} is configured for {_TIER.value!r} isolation, but this "
                f"work did not come from a trusted submitter — and a process that can reach the "
                f"host Docker daemon can start a container with the host's filesystem in it "
                f"(§3.3). Refused rather than downgraded to {IsolationTier.CONTAINER.value!r}: "
                f"the repository's tests need a daemon, and running them without one would "
                f"report a missing capability as the agent's failure",
            )

    def _environment(self, request: RunnerRequest) -> Environment:
        """Everything the base class assembles, with the reaper checked.

        Checked *here* rather than in ``_check`` because this is where the last
        word is spoken: ``_inherited`` sets the variable and ``extra_env`` and a
        repository's own configuration are applied over the top of it, so the
        only honest place to ask whether Ryuk survived is after all three.
        """
        environment = super()._environment(request)
        disabled = environment.values.get(RYUK_DISABLED_ENV, "")
        if disabled.strip().lower() in _TRUTHY:
            raise PermanentError(
                "testcontainers-reaper-disabled",
                f"{RYUK_DISABLED_ENV}={disabled!r} would start this run with testcontainers' own "
                f"reaper switched off, and Ryuk is the only thing that knows which of the host's "
                f"containers belong to this run's session (§3.3) — without it every fixture this "
                f"run starts outlives it, on the host, invisibly until the disk fills",
            )
        return environment

    def _inherited(self, request: RunnerRequest) -> dict[str, str]:
        """The container tier's near-empty environment, plus what finds a daemon.

        All four are set for **both** phases even though the socket is mounted
        for only one, and that is not an oversight: a dependency install that
        reaches for a daemon should fail saying it could not find one, which is
        what these produce, rather than fail saying nothing at all.
        """
        env = super()._inherited(request)
        env[DOCKER_HOST_ENV] = f"unix://{self._socket}"
        # Explicitly false rather than merely unset: an image or a base
        # configuration that turned it off would otherwise be inherited, and
        # this variable's default is the one that matters most here.
        env[RYUK_DISABLED_ENV] = "false"
        if self._host_alias:
            env[HOST_OVERRIDE_ENV] = self._host_alias
        return env

    def _spec(
        self,
        request: RunnerRequest,
        worktree: Path,
        phase: Phase,
        argv: tuple[str, ...],
        environment: Environment,
    ) -> ContainerSpec:
        """The container tier's container, plus a way to reach the daemon.

        **The agent phase only.** The setup phase is the repository's
        ``install_command``, which downloads dependencies and runs whatever
        their lockfiles ask it to — a ``postinstall`` script is arbitrary code
        from a transitive dependency, and it is the least trusted thing in the
        run. It has no business with a daemon, and the capability this tier
        exists to grant is for running *tests*. The residue is real and worth
        naming: a repository whose install genuinely needs Docker — a build that
        starts a database to generate code against — has to move that into its
        test command, and will otherwise fail with a connection error rather
        than a hang.
        """
        spec = super()._spec(request, worktree, phase, argv, environment)
        if phase is not Phase.AGENT:
            return spec
        return replace(
            spec,
            # Not read-only. A read-only bind of a socket stops nothing that
            # matters — the danger is the conversation, not the inode — and
            # claiming it as a control would be the worst kind of comment.
            mounts=(*spec.mounts, Mount(source=self._socket)),
            # The container runs as the invoking user (files land on a bind
            # mount, and root-owned files are the next run's problem), and the
            # daemon's socket is group-owned. Without the group the mount is
            # present and unopenable, which fails as a permission error several
            # layers inside somebody's test framework. Resolved in ``_prepare``,
            # because the answer comes from the daemon and this is not async.
            group_add=self._group_for(self._image_for(request)),
            extra_hosts=({self._host_alias: "host-gateway"} if self._host_alias else {}),
        )

    async def _prepare(self, request: RunnerRequest, worktree: Path) -> Installed:
        """Everything the base class does, plus the two things only a daemon
        can answer.

        The snapshot is taken after ``super()``, which is what clears a previous
        attempt's containers — so a session left behind by the attempt this one
        is resuming is correctly seen as pre-existing rather than as ours to
        remove.
        """
        installed = await super()._prepare(request, worktree)
        await self._resolve_group(request)
        self._sessions[request.idempotency_key] = await self._live_sessions()
        return installed

    async def _teardown(self, request: RunnerRequest) -> None:
        """This run's containers, and then the ones it caused."""
        await super()._teardown(request)
        await self._sweep(request)

    # -------------------------------------------------------------- plumbing

    async def _sweep(self, request: RunnerRequest) -> None:
        """Remove the sibling sessions this run — and only this run — started.

        What may be removed is what appeared after this attempt's snapshot,
        minus everything disowned. A session is disowned the moment some *other*
        attempt is in flight that could equally have caused it — which is any
        attempt whose own snapshot predates the session.

        **The disowning is remembered, and that is the part worth stating.** The
        obvious version recomputes it from whoever is still running at teardown,
        and it is wrong in the way that only shows up under load: two overlapping
        runs, the first declines an ambiguous session because the second is still
        going, and then the second claims it because by its own teardown it is
        the only one left. Ambiguity is a property of *when the session
        appeared*, not of who happens to be running when the sweep asks, so it
        has to outlive the run that noticed it. It does not have to outlive the
        container — the set is intersected with what the daemon still has, which
        is what keeps it from growing for the life of the process.

        Safe to call twice, and called twice on purpose: ``_prepare`` runs
        teardown before it takes its snapshot, and an attempt with no snapshot
        has nothing it can prove it caused.
        """
        before = self._sessions.pop(request.idempotency_key, None)
        if before is None:
            return

        found = await self._engine.labelled(SESSION_LABEL)
        now = {session for _, session in found if session}
        self._disowned &= now
        self._disowned |= {session for other in self._sessions.values() for session in now - other}

        claimed = (now - before) - self._disowned
        for name, session in found:
            if session in claimed:
                await self._engine.remove(name)

    async def _live_sessions(self) -> frozenset[str]:
        """Testcontainers session ids the daemon is holding right now.

        An engine that cannot be reached answers with nothing, which makes every
        session look new. That is the wrong way round for a sweep, and it is
        survivable only because the sweep asks the same unreachable engine to
        remove them — so the failure is a sweep that does nothing rather than
        one that removes somebody else's containers.
        """
        found = await self._engine.labelled(SESSION_LABEL)
        return frozenset(session for _, session in found if session)

    async def _resolve_group(self, request: RunnerRequest) -> None:
        """Find out which group owns the daemon's socket, and remember it.

        **Asked of the daemon, not of this machine**, and that is the whole
        reason this is async and lives here rather than in ``_check``. On a
        native Linux daemon the two answers agree and none of this matters. On
        Docker Desktop, Colima, Lima or Rancher the daemon runs in a VM: the
        socket the container mounts is the VM's, its gid is the VM's, and
        ``stat`` on the developer's Mac either finds a forwarding socket with an
        unrelated owner or finds nothing at all. That divergence is invisible
        until a container runs as a non-root user and cannot open a mount that
        is plainly there — which is precisely this tier.

        Doubles as the preflight ``_check`` could not do: the question is asked
        by mounting the socket, so a path the daemon does not have fails here,
        before either phase, with a message that names the cause. The cost is
        one short container per image per process, on the first dispatch.
        """
        image = self._image_for(request)
        if self._socket_group is not None or image in self._groups:
            return

        found = await self._engine.owning_group(image, str(self._socket))
        if found is None:
            raise PermanentError(
                "docker-socket-unreadable",
                f"could not read {self._socket} through the daemon. Either the daemon has no "
                f"socket at that path — it is the *daemon's* path, not this machine's, and they "
                f"differ on every VM-backed setup — or {image!r} has no shell and no `stat` for "
                f"the question to be asked with. Point socket_path at the right one, or pass "
                f"socket_group to skip the question",
            )
        self._groups[image] = found

    def _group_for(self, image: str) -> tuple[str, ...]:
        """The gid to add, configured or discovered. One or none."""
        group = self._socket_group if self._socket_group is not None else self._groups.get(image)
        return (group,) if group else ()
