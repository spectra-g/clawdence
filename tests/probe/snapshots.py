"""Committed expected output, and the one way to regenerate it.

The probe's whole product is a judgement — which build system, which commands,
whether the tests need a daemon — and the failure mode is not an exception, it
is a *plausible wrong answer*. Assertions on individual fields do not catch
that: they check what somebody thought to check, on the day they wrote it.

A snapshot catches it, because the diff is the review. Changing the Maven
install command shows up as three lines in a pull request, next to the reason,
instead of as behaviour that only surfaces the next time somebody probes a
Maven repo.

Regenerate with ``CLAWDENCE_UPDATE_SNAPSHOTS=1 uv run pytest tests/probe``, and
**read the diff before committing it** — a snapshot updated without being read
is a test that asserts the bug is still there.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

SNAPSHOT_DIR: Final = Path(__file__).parent / "snapshots"

UPDATE_ENV: Final = "CLAWDENCE_UPDATE_SNAPSHOTS"


def updating() -> bool:
    return bool(os.environ.get(UPDATE_ENV))


def assert_matches(name: str, actual: str) -> None:
    """Compare against the committed snapshot, or write it."""
    path = SNAPSHOT_DIR / f"{name}.json"
    payload = actual if actual.endswith("\n") else actual + "\n"

    if updating():
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        return

    assert path.exists(), (
        f"no snapshot at {path}. If this fixture is new, create it with "
        f"`{UPDATE_ENV}=1 uv run pytest tests/probe` and read the result before committing."
    )
    expected = path.read_text(encoding="utf-8")
    assert payload == expected, (
        f"{path.name} does not match what the probe produced. If the change is intended, "
        f"regenerate with `{UPDATE_ENV}=1 uv run pytest tests/probe` and review the diff."
    )
