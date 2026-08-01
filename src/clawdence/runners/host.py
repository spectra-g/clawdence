"""A ``RunnerPort`` that runs the agent CLI as a child process on this machine.

**This tier has no isolation and is not a default.** It runs model-generated code
as the user running the control plane, on the filesystem the control plane's
state database is on. The plan calls it "local dev only; never default"; what
enforces that here is a check, not a comment — ``dispatch`` refuses a request
whose ``RepoProfile`` asks for any other tier, because the failure mode being
guarded against is a repository configured for ``container`` quietly executing on
the host after somebody wired the wrong adapter at startup.

Given that, the interesting question is not "what does this stop the agent from
doing" — the answer is nothing — but **"what does the control plane hand over,
and what does it believe on the way back".** Both answers live in ``agent``,
which is nearly all of this tier; what is left here is the two things that are
actually specific to running something on your own machine:

*The environment is an allowlist of the control plane's own.* Never
``os.environ``. The control plane holds every credential in the system
(ARCHITECTURE Zone 2), and passing its environment to a child would put all of
them one ``env`` away from a process running code from a repository. The Slack
token, the tracker credentials and the push credentials are not merely absent by
convention: ``_forbidden`` refuses to build an environment containing them at
all, so a caller that later fills ``extra_env`` from the wrong dictionary fails
at dispatch rather than leaking quietly.

*There is nothing to allocate and nothing to ask.* No teardown, and ``_observe``
adds nothing, because a bare process has no supervisor to be told by. That is
exactly the gap the container tier closes: ``Completion.oom_killed`` is a fact
there and an inference here, and ``outcome`` says so where it guesses.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Final

from clawdence.domain import IsolationTier, RunnerRequest
from clawdence.runners.agent import AgentRunner, Launch, Phase, PlanDelivery
from clawdence.vcs.store import mirror_name

#: Passed through from the control plane's own environment. The engine's
#: ``ScriptHandler`` allowlist, plus what a build needs to find its toolchain and
#: somewhere to write temporary files. Deliberately dull: nothing here names a
#: credential.
INHERITED_ENV: Final[tuple[str, ...]] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "SHELL",
    "USER",
    "XDG_CACHE_HOME",
)


class HostRunner(AgentRunner):
    """Runs the agent CLI on this machine. ``IsolationTier.HOST`` only."""

    tier: ClassVar[IsolationTier] = IsolationTier.HOST

    __slots__ = ()

    def _inherited(self, request: RunnerRequest) -> dict[str, str]:
        return {name: self._environ[name] for name in INHERITED_ENV if name in self._environ}

    def _launch(
        self, request: RunnerRequest, worktree: Path, phase: Phase, argv: tuple[str, ...]
    ) -> Launch:
        """The command, as itself. Both phases, and the phase is not consulted.

        There is no intermediate process here, so there is nothing for the
        distinction to change: the install command and the agent CLI are both
        just children of the control plane, with the same environment and the
        same working directory. The dependency cache needs no mount either —
        the directory the environment names is a directory on this machine, and
        this tier is already on it.
        """
        return Launch(argv=argv, env=self._environment(request).values, cwd=worktree)

    def _cli_argv(self, request: RunnerRequest, prompt: str) -> tuple[str, ...]:
        """Give Codex the one external Git directory this worktree writes.

        Clawdence uses linked worktrees. Their checkout is inside the workspace,
        but their index, branch ref and object database are in the repository's
        mirror. Codex's workspace sandbox therefore permits ordinary file edits
        and rejects ``git commit`` unless that mirror is an additional writable
        root.

        This is intentionally Codex-specific. Other agent CLIs do not accept
        ``--add-dir``, and passing an OpenAI flag to a generic command would turn
        a working runner into a startup failure. ``--full-auto`` is removed when
        present: current Codex keeps it only as a deprecated compatibility path,
        while the explicit workspace-write mode says which boundary this runner
        relies on.
        """
        argv = super()._cli_argv(request, prompt)
        root = self._command.writable_git_root
        if root is None or Path(self._command.argv[0]).name != "codex":
            return argv

        explicit = tuple(arg for arg in argv if arg != "--full-auto")
        tail_size = int(self._command.delivery is not PlanDelivery.STDIN)
        body = list(explicit[:-tail_size] if tail_size else explicit)
        tail = explicit[-tail_size:] if tail_size else ()

        # Approval policy is a top-level Codex option, so it must precede the
        # ``exec`` subcommand. A non-interactive runner has nobody who can answer
        # a prompt; denying escalation is the deterministic equivalent of the
        # old full-auto behavior inside the explicit sandbox.
        if not any(
            arg in ("--ask-for-approval", "-a") or arg.startswith("--ask-for-approval=")
            for arg in body
        ):
            executable = len(request.profile.exec_prefix)
            body[executable + 1 : executable + 1] = ["--ask-for-approval", "never"]
        if not any(arg in ("--sandbox", "-s") or arg.startswith("--sandbox=") for arg in body):
            body.extend(("--sandbox", "workspace-write"))
        mirror = root / mirror_name(request.profile.id)
        return (*body, "--add-dir", str(mirror), *tail)
