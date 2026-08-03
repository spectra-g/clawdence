"""Verification: what *done* means, whether it was met, and what happens if not.

The second of v1's three structural problems lived here. ``_check_tdd_verdict``
was 405 lines with outside-in TDD welded into every branch, and TDD appeared 175
times across the orchestrator — so the process could not be varied per work item
and a repository that does not work that way could not use the system at all.
The replacement is four contracts behind a registry, and "TDD is optional" is a
property of the lookup rather than a claim in a README.

The layering is one-directional, as everywhere else in this codebase::

    reporters ─ evidence
        └─ contracts ─ halt
              └─ recheck

``reporters`` and ``evidence`` are pure functions over data and know nothing
about each other. ``contracts`` judges one attempt. ``halt`` decides what to do
about a judgement. ``recheck`` is the only module that has anything to do with
running a command, and it does not run one — it sequences them and takes an
executor from the tier that owns isolation.

Three properties are structural rather than enforced by care, because all three
were correctness holes in v1:

**Evidence binds to a tree.** A ``VerificationResult`` is evidence for exactly
the commit it names. Every mutation invalidates it — rebase, force-push, amend,
base advance — because the check is string equality against a hash rather than a
list of events somebody remembered to enumerate. Without it, S15b's auto-rebase
merges code whose tests ran against a base it never landed on, with every
dashboard green (``evidence``).

**Only the assertion reaches the model.** A failing suite emits thousands of
lines; forwarding them exhausts the step's context budget, and truncating them
drops the assertion — which is how a retry loop burns to the cost cap without
ever showing the model the error. The reporter output the ``RepoProfile``
declares is parsed, and what survives is the test name, the file, the line, the
message and three frames (``reporters``).

**No exhausted retry force-proceeds.** ``on_exhausted`` is a one-value
``Literal`` in the domain, ``RESUMPTIONS`` admits ``APPROVE`` from no state, and
a test iterates the whole ``HaltState`` enum so a state added later fails the
suite until somebody decides what it admits (``halt``).

**Boundaries.** Nothing here talks to a person: which conditions halt, what a
halted run records and which resumptions each state admits are this package's;
the verb surface, who may use it, and the audit trail are S17's, derived from
this table rather than invented per call site (DECIDED 2026-08-03). Nothing here
merges, rebases or aggregates — that is S15b's, and it is the caller of
``evidence.require_fresh``. Multi-model consensus is S12b's and is not here.
"""

from __future__ import annotations

from clawdence.verify.contracts import (
    DEFAULT_RULES,
    Attempt,
    ContractRule,
    Registry,
    evaluate,
    explain,
)
from clawdence.verify.evidence import (
    Stale,
    StaleEvidence,
    Staleness,
    check,
    invalidated_by,
    is_fresh,
    require_fresh,
)
from clawdence.verify.halt import (
    BLOCKING,
    RESUMPTIONS,
    Decision,
    Halt,
    Proceed,
    Retry,
    admits,
    decide,
    stale,
)
from clawdence.verify.recheck import (
    Command,
    CommandResult,
    Executor,
    Recheck,
    Rechecked,
    into_attempt,
    run,
    sequential,
)
from clawdence.verify.reporters import (
    MAX_FAILURES,
    MAX_FRAMES,
    MAX_MESSAGE_CHARS,
    MAX_REPORT_BYTES,
    REPORT_PATHS,
    ReportError,
    collect,
    merge,
    parse,
)

__all__ = [
    "BLOCKING",
    "DEFAULT_RULES",
    "MAX_FAILURES",
    "MAX_FRAMES",
    "MAX_MESSAGE_CHARS",
    "MAX_REPORT_BYTES",
    "REPORT_PATHS",
    "RESUMPTIONS",
    "Attempt",
    "Command",
    "CommandResult",
    "ContractRule",
    "Decision",
    "Executor",
    "Halt",
    "Proceed",
    "Recheck",
    "Rechecked",
    "Registry",
    "ReportError",
    "Retry",
    "Stale",
    "StaleEvidence",
    "Staleness",
    "admits",
    "check",
    "collect",
    "decide",
    "evaluate",
    "explain",
    "into_attempt",
    "invalidated_by",
    "is_fresh",
    "merge",
    "parse",
    "require_fresh",
    "run",
    "sequential",
    "stale",
]
