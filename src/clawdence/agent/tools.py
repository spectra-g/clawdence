"""What tool surface an agent step gets — which, at M1, is none.

v1 had eleven ``skills/`` directories with no consistent model of what a given
agent could reach, so the answer to "what can the reviewer do" was "read the
directory listing and hope". S12's brief is to decide that deliberately, and the
deliberate decision is **deny by default, with nothing registered**.

That is a real answer rather than a deferral, and the reasoning is the split the
architecture already makes. Work that needs to touch a repository happens in the
data plane, inside a container, through the runner (S6/S7) — that is where an
agent gets to read files and run commands, and it is bounded by a worktree, a
resource cap and, at S7b, an egress allowlist. An agent *step* runs in the control
plane, which holds every credential in the system and is the one place that must
never execute repo code (ARCHITECTURE §1). Giving it a file-reading tool would put
a model-directed read inside Zone 2, and the first thing anybody would point it at
is the working directory of the process holding the keys.

So the surface is empty, and a stage that declares a tool fails loudly with a
message naming what is missing. What it is *not* is silently ignored: a step that
declared three tools and got none would produce a model that keeps announcing it
will look something up and then inventing the answer, which reads as a model
problem for as long as it takes somebody to check.

``ToolSurface`` exists as a real type rather than a hole, so registering one is a
constructor argument rather than a redesign — and so the refusal has somewhere to
live that a future tool does not have to move.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from clawdence.ports import ToolSpec


class UnknownToolError(LookupError):
    """A stage declared a tool nothing provides."""


class ToolSurface:
    """The tools available to agent steps. Empty unless constructed otherwise."""

    __slots__ = ("_tools",)

    def __init__(self, tools: Mapping[str, ToolSpec] | None = None) -> None:
        self._tools = dict(tools or {})

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def resolve(self, names: Iterable[str]) -> tuple[ToolSpec, ...]:
        """Specs for the declared names, in the order declared.

        Raises ``UnknownToolError`` naming the offender. The order is the stage's,
        not this registry's, because a prompt that lists tools in a different
        order than the last run is a prompt whose cached prefix no longer matches
        — a cost difference, not a correctness one, but a free one to avoid.
        """
        resolved: list[ToolSpec] = []
        for name in names:
            spec = self._tools.get(name)
            if spec is None:
                offered = ", ".join(self.names()) or "none — no tools are registered at all"
                raise UnknownToolError(
                    f"no tool named {name!r} is available to agent steps; available: {offered}. "
                    "Work that needs to read or run a repository belongs in a 'runner' step, "
                    "which executes in the data plane."
                )
            resolved.append(spec)
        return tuple(resolved)

    def __bool__(self) -> bool:
        return bool(self._tools)
