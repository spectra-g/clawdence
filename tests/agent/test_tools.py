"""The tool surface, which is empty, and the refusal that makes that a decision."""

from __future__ import annotations

import pytest

from clawdence.agent import ToolSurface, UnknownToolError
from clawdence.ports import ToolSpec


def test_nothing_is_registered_by_default() -> None:
    """Deny by default. An agent step runs in the control plane, which holds every
    credential in the system and must never execute repo code."""
    surface = ToolSurface()
    assert surface.names() == ()
    assert bool(surface) is False


def test_declaring_no_tools_is_fine() -> None:
    assert ToolSurface().resolve(()) == ()


def test_an_unregistered_tool_is_refused_and_points_at_the_runner() -> None:
    """Silently ignoring it would produce a model that keeps announcing it will
    look something up and then inventing the answer — which reads as a model
    problem for as long as it takes somebody to check."""
    with pytest.raises(UnknownToolError) as caught:
        ToolSurface().resolve(("read_file",))
    assert "no tools are registered at all" in str(caught.value)
    assert "'runner' step" in str(caught.value)


def test_a_registered_tool_resolves() -> None:
    spec = ToolSpec(name="ask", description="ask a clarifying question")
    assert ToolSurface({"ask": spec}).resolve(("ask",)) == (spec,)


def test_resolution_keeps_the_order_the_stage_declared() -> None:
    """A prompt listing tools in a different order than the last run is a prompt
    whose cached prefix no longer matches."""
    first = ToolSpec(name="a", description="a")
    second = ToolSpec(name="b", description="b")
    surface = ToolSurface({"b": second, "a": first})
    assert surface.resolve(("a", "b")) == (first, second)
    assert surface.resolve(("b", "a")) == (second, first)


def test_the_error_lists_what_is_available_when_something_is() -> None:
    surface = ToolSurface({"ask": ToolSpec(name="ask", description="ask")})
    with pytest.raises(UnknownToolError, match="available: ask"):
        surface.resolve(("bash",))
