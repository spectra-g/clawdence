"""Getting a mid-run message to the agent (§3.9's claim path, added in S6c).

The runner polls the control plane; the agent is a CLI in a worktree. Between
them is exactly one channel, and it is the one the container tier's whole
isolation claim rests on: **the worktree bind mount is the only thing the agent
can see.** There is no socket, no shared database, and deliberately no second
transport — adding one would be adding a hole to the boundary S7 spent a step
closing.

So a claimed message becomes a file, under the directory the runner already
owns. Three consequences, all of them wanted:

*It cannot reach a pull request.* ``.clawdence/`` is in ``$GIT_DIR/info/exclude``
and ``installed.Installed`` treats everything under it as the runner's outright,
so a steering message is invisible to ``git add --all`` and to the dirtiness
probe that decides ``DROPPED_COMMIT``.

*It survives the run.* These are not tidied away with the plan and the
conventions file, for the same reason the verdict is not: they are the record of
what somebody told the agent to do halfway through, and the moment a person
wants to read them is the moment after the run failed. The durable copy is in
the store either way; this one is where the story reads in order.

*The agent needs telling it exists.* ``plan.build`` has a section for it, and
the directory is created empty at ``_prepare`` so the instruction is true from
the first turn rather than true only once somebody has typed something.

**Named by claim order, not by arrival order.** ``0002-…`` sorts after
``0001-…`` in every listing an agent will produce, and the ordinal is the one the
inbox assigned when it applied priority — so an urgent message sent second is
read first, which is the entire point of having a priority.

**The body is delimited and the delimiter is stripped from it.** A steering
message is an instruction the agent is meant to follow, unlike the plan's
fenced-off untrusted text, but it is still typed by a person into a channel that
S10b will eventually let the outside world reach. Bounding where it starts and
stops costs one line and means a message cannot append a paragraph to the
constraints.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from clawdence.ports.control import Steer
from clawdence.runners.installed import WORK_DIR

#: Where messages land, inside the directory the runner owns. One directory
#: rather than one growing file: a file the runner appends to while the agent
#: reads it is a file the agent can read half of.
STEERING_DIR: Final = f"{WORK_DIR}/steering"

#: Delimiters around the body. Distinct from ``plan.FENCE`` on purpose — that
#: one says "this is data, do not obey it", and this one says the opposite.
OPEN: Final = "-----BEGIN OPERATOR MESSAGE-----"
CLOSE: Final = "-----END OPERATOR MESSAGE-----"


def deliver(worktree: Path, messages: Sequence[Steer]) -> tuple[str, ...]:
    """Write claimed messages into the worktree. Returns the paths written.

    Best effort by design: a message that cannot be written is a message the
    agent will not see, and that is already what the inbox recorded when the
    process died holding it. Failing the run over it would turn "we could not
    pass on a suggestion" into "the work was thrown away", which is the wrong
    trade by a wide margin.
    """
    written: list[str] = []
    for message in messages:
        relative = path_for(message)
        target = worktree / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render(message), encoding="utf-8")
        except OSError:
            continue
        written.append(relative)
    return tuple(written)


def prepare(worktree: Path) -> None:
    """Create the directory the plan tells the agent to look in.

    Empty and up front, so "check this directory each turn" is an instruction
    about somewhere that exists. An agent told to look at a path that is not
    there spends a turn deciding whether that is an error.
    """
    (worktree / STEERING_DIR).mkdir(parents=True, exist_ok=True)


def path_for(message: Steer) -> str:
    """The file one message is written to, relative to the worktree.

    The ordinal orders it and the id makes it unique; the id is also what lets
    somebody holding a row from the ``steering`` table find the file it became.
    Zero-padded because ``10`` sorting before ``2`` would silently invert the
    priority the ordinal exists to express.
    """
    return f"{STEERING_DIR}/{message.ordinal:04d}-{message.id}.md"


def render(message: Steer) -> str:
    """One message, as the agent reads it.

    The header carries who and when because the agent's answer to "should I
    change what I am doing" depends on both — a message from ten minutes ago is
    about work that is now done.
    """
    sent = message.at.isoformat() if message.at is not None else "unknown"
    body = message.body.replace(OPEN, "").replace(CLOSE, "").strip()
    return (
        f"# Message {message.ordinal} from {message.sender}\n\n"
        f"- Sent: {sent}\n"
        f"- Priority: {message.priority}\n\n"
        f"{OPEN}\n{body}\n{CLOSE}\n"
    )
