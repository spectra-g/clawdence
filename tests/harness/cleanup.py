"""Releasing what a test made, including when the test failed.

A leaked container is the worst kind of test failure because it is not a test
failure: the run that leaks it passes, and the *next* run fails somewhere
unrelated with a port already bound or a disk that is full. v1 had no answer to
this beyond remembering, which is why its integration tests were run by hand.

Three properties, and each is the reason for one line of the implementation:

**Reverse order.** A worktree lives inside a directory, and a container has the
worktree bind-mounted. Releasing in the order things were made deletes the
directory out from under the container. Reverse order — a stack — is the only
ordering that follows from "later things were built on earlier things".

**A failing release does not stop the others.** This code runs *because*
something went wrong; a teardown that gives up at the first error leaks
everything after it, which is precisely when leaking matters most. Failures are
collected and reported together.

**Outstanding resources fail the session.** A ``Reaper`` that quietly released
nothing would look identical to one that had nothing to release. The
session-scoped check in ``conftest`` is what makes the guarantee observable.

S7 and S8 register container teardown here. Nothing about this module knows what
a container is, which is the point — it is a stack of callables and a promise
about when they run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Leak:
    """A resource whose release raised. Reported, never swallowed."""

    what: str
    error: Exception

    def describe(self) -> str:
        return f"{self.what}: {type(self.error).__name__}: {self.error}"


@dataclass(slots=True)
class Reaper:
    """A stack of things to release.

    ``register`` takes a description as well as a callable because the failure
    mode this exists for is discovered days later, in a log, by someone who did
    not write the test. "cleanup failed" is not actionable; "container
    clawdence-run-abc123: APIError: 409" is.
    """

    _registered: list[tuple[str, Callable[[], None]]] = field(default_factory=list, init=False)

    def register(self, what: str, release: Callable[[], None]) -> None:
        self._registered.append((what, release))

    def release_all(self) -> Sequence[Leak]:
        """Release everything, newest first. Returns what failed.

        Empties the stack whatever happens, including for the entries that
        raised: a release that failed once will fail identically on a second
        attempt, and retrying it at session end would turn one report into two.

        ``Exception`` and not ``BaseException`` — a Ctrl-C part way through a
        teardown should stop the teardown, not be collected as a leak report
        and then swallowed.
        """
        leaks: list[Leak] = []
        while self._registered:
            what, release = self._registered.pop()
            try:
                release()
            except Exception as exc:
                leaks.append(Leak(what=what, error=exc))
        return tuple(leaks)

    @property
    def outstanding(self) -> Sequence[str]:
        """What is still registered, newest first."""
        return tuple(what for what, _ in reversed(self._registered))

    def __len__(self) -> int:
        return len(self._registered)
