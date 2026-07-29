"""Ingestion — where work comes from, normalised to one shape.

``ports.ingest`` declares the interface and the two rules every source obeys
(at-least-once delivery ended by acknowledgement, deduplication on a
source-stable key). ``store.intake`` makes an arrival durable and gives it the
four verbs a request has over its life. This package is the layer between: it
turns a particular source's shape into a ``WorkItem``, and holds one adapter per
source.

CLI is the only source here (S10, M1). Slack and GitHub issues are the same
package with a different envelope in front of the same ``Intake`` — and neither
may be enabled until S10b, because a GitHub issue on a public repository is
attacker-controlled text and the ingress boundary is not yet built.

The layering::

    normalise ─ cli
      └─ report

``normalise`` knows nothing about where an item came from beyond the fields it
is handed, which is what keeps the second and third adapters from re-deriving
the title rule, the type default and the "never rewrite the body" rule that
cost v1 its repository routing.
"""

from __future__ import annotations

from clawdence.ingest.cli import (
    REF_PREFIX,
    cli_submitter,
    key,
    mint_ref,
    reply,
    submit,
    whoami,
    withdraw,
)
from clawdence.ingest.normalise import DEFAULT_TYPE, NormaliseError, normalise
from clawdence.ingest.report import (
    render_detail,
    render_json,
    render_listing,
    render_text,
)

__all__ = [
    "DEFAULT_TYPE",
    "REF_PREFIX",
    "NormaliseError",
    "cli_submitter",
    "key",
    "mint_ref",
    "normalise",
    "render_detail",
    "render_json",
    "render_listing",
    "render_text",
    "reply",
    "submit",
    "whoami",
    "withdraw",
]
