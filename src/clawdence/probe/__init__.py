"""The project probe — a proposed ``RepoProfile``, derived from the repository.

Replaces most of v1's hand-written ``repo-registry.json``, where every field was
a thing somebody had to know and keep current. The probe reads the repository
instead, and the difference that matters is not the typing saved: it is that the
*isolation tier* stops being a guess made by whoever set the repo up, and
becomes an inference from evidence, reviewed by a human who is shown the
evidence (§3.5).

Three properties hold across the whole package, and each one is somewhere a
different version of this would have gone wrong:

**It proposes; it never applies.** Nothing is written, nothing is registered,
and no run is affected. The output is JSON for a person to read and commit.

**It leaves a field empty rather than guessing.** An empty ``test_command`` asks
its reviewer a question. A plausible wrong one answers it, and gets committed.

**It cannot grant the Docker socket.** The tier that mounts the host daemon
needs ``docker_socket_acknowledged``, which is a person accepting something
equivalent to host root, and no inference from a lockfile is allowed to stand in
for that. So the probe detects ``needs_docker``, says so loudly, and proposes a
tier without a daemon in it — see ``assemble``.
"""

from __future__ import annotations

from clawdence.probe.assemble import ProbeResult, probe
from clawdence.probe.findings import Finding, Level
from clawdence.probe.report import render_json, render_profile, render_text
from clawdence.probe.scan import (
    GIT_TIMEOUT_SECONDS,
    MAX_FILE_BYTES,
    MAX_FILES_READ,
    MAX_MEMBERS,
    SKIP_DIRS,
    ProbeError,
)

__all__ = [
    "GIT_TIMEOUT_SECONDS",
    "MAX_FILES_READ",
    "MAX_FILE_BYTES",
    "MAX_MEMBERS",
    "SKIP_DIRS",
    "Finding",
    "Level",
    "ProbeError",
    "ProbeResult",
    "probe",
    "render_json",
    "render_profile",
    "render_text",
]
