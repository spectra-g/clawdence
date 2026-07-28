"""Clawdence — workflow-driven orchestration for AI coding agents.

The scaffold carries no behaviour yet: the domain model and the workflow engine
are the next two pieces of work.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("clawdence")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
