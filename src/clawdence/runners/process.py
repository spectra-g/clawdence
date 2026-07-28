"""Ending a child process, and letting go of one that will not end.

Four small functions, in their own module because two layers need them and
neither should import the other: ``agent`` stops the thing it spawned, and
``engine`` stops a control call that stopped answering. Both learned the same
lesson from the same bug.

**Killing a process is not the same as being finished with it.** A child that
spawned something of its own — a build daemon, a background test server, a
container's client, ``sh`` running ``sleep &`` — handed that grandchild the same
stdout, and the pipe is not closed while anyone still holds it. Waiting for the
transport therefore waits for the *grandchild*, which nothing here killed and
nothing here knows about.

Unbounded, that is a run which never returns a result: not a failure, not a
timeout, nothing for the watchdog to recover, because the dispatch is still
politely waiting on a process that died long ago. So the wait is bounded, and
when it expires the descriptors are released rather than left for a finaliser to
complain about after the event loop has closed.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Final

#: How long to wait for a killed child to be reaped before giving up on it. Far
#: longer than a reap, far shorter than a shift.
REAP_TIMEOUT_SECONDS: Final = 10.0


def kill(process: asyncio.subprocess.Process) -> None:
    """``SIGKILL``, unless it has already exited.

    The race the watchdog creates: a step is declared overdue at the moment it
    finishes. Killing what is already gone must not raise.
    """
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()


async def kill_and_reap(process: asyncio.subprocess.Process) -> None:
    """Kill a child whose work is over, then wait for it — but not forever.

    Both halves, for the reason ``engine.handlers`` spells out: without the kill
    the process outlives the run, and without the wait it is a zombie whose pipes
    stay open, so a transport finalises after the event loop has closed and the
    suite reports a warning from a thread nobody is looking at. The bound is for
    the grandchild this cannot see; see the module docstring.
    """
    if process.returncode is not None:
        return
    kill(process)
    with contextlib.suppress(asyncio.CancelledError):
        try:
            await asyncio.wait_for(process.wait(), timeout=REAP_TIMEOUT_SECONDS)
        except TimeoutError:
            abandon(process)


def abandon(process: asyncio.subprocess.Process) -> None:
    """Let go of a child that will not be reaped, and of its pipes.

    Giving up on the wait is not enough on its own: the read ends are still open
    on our side, and a transport still holding them when the event loop closes is
    the "unclosed transport" warning this suite treats as an error — reported
    from a finaliser, long after the run it belongs to, which is the worst place
    to learn about it.

    ``_transport`` is private and there is no public equivalent: ``Process``
    exposes ``kill`` and ``wait`` and nothing that means "I am done with you".
    Reaching for it is deliberate and narrow — it is the only way to release the
    descriptors of a process already killed and deliberately not waited for.
    """
    transport = getattr(process, "_transport", None)
    if transport is None:  # pragma: no cover - every asyncio Process has one
        return
    with contextlib.suppress(OSError, RuntimeError):
        transport.close()
