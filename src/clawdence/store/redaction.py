"""Secret screening at the state-store boundary.

The store receives human-authored text as well as structured adapter payloads.
Both are screened before they become durable: known credential shapes are
masked wherever they occur, and values under credential-named mapping keys are
masked even when the value has an unfamiliar shape.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import JsonValue

from clawdence.ports.secrets import REDACTED

SECRET_FIELD: Final = re.compile(
    r"(?:authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|bearer|"
    r"client[-_]?secret|secret|password|credential)",
    re.IGNORECASE,
)

# These have identifying prefixes or an exact provider-defined shape, so
# masking them does not turn ordinary prose into a field of false positives.
# Structured values get the broader key-name rule above.
SECRET_VALUE: Final = re.compile(
    r"(?:"
    r"sk-ant-[A-Za-z0-9_-]{16,}|"
    r"sk-(?:live_|test_)?[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{16,}|"
    r"glpat-[A-Za-z0-9_-]{16,}|"
    r"npm_[A-Za-z0-9]{16,}|"
    r"xox[abposr]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{35}"
    r")"
)


def redact_text(value: str) -> str:
    """Mask recognisable credentials embedded in arbitrary text."""
    return SECRET_VALUE.sub(REDACTED, value)


def redact_value(value: JsonValue) -> JsonValue:
    """Recursively screen a JSON value without mutating the caller's object."""
    if isinstance(value, dict):
        return {
            key: REDACTED if SECRET_FIELD.search(key) else redact_value(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_value(child) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact(payload: JsonValue) -> tuple[JsonValue, bool]:
    """The production ``Redactor``: screen a payload and attest that it ran."""
    return redact_value(payload), True
