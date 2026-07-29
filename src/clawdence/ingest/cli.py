"""The CLI ingestion adapter — ``clawdence submit``.

The first ``IngestPort`` source, and the one that makes the durable intake
necessary rather than tidy: a submission is one process and the thing that will
act on it is another, so "have I seen this before" cannot be answered from
memory. Slack and GitHub (S10, M2) hold a connection and could have cheated;
this one cannot, which is why it is worth building first.

**The reference is the idempotency key, and the user owns it.** ``--ref`` names
the *request*; two invocations carrying the same one are the same request said
twice. Without it a fresh reference is minted per invocation, so typing the
command twice creates two work items — which is what typing a command twice
means. The tempting alternative, hashing the text, was rejected for the reason
S9 rejected every plausible guess: it silently refuses two people who genuinely
asked for the same thing on the same day, and it does so invisibly.

**A CLI submitter is trusted, and that is a statement about the CLI.**
``Submitter.trusted`` is deny-by-default because a public issue is a stranger's
text. Whoever runs this command already has a shell on the control plane, the
state database and the runner's credentials; a trust flag is not what is
standing between them and the system. Marking it otherwise would be theatre, and
it would make the flag mean "we do not know" in the one case where we do. Every
*other* source is a different answer, and S10b is where the mapping stops being
one line.
"""

from __future__ import annotations

import getpass
import os
import secrets
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from clawdence.domain import IngestSource, Submitter, WorkItemType
from clawdence.ingest.normalise import DEFAULT_TYPE, normalise
from clawdence.store.intake import Admission, Intake, Turn

#: Prefix on a minted reference, so a listing shows at a glance which requests
#: named themselves and which were named by whoever submitted them.
REF_PREFIX: Final = "cli."

#: Used when the process has no login name — a container, a cron job, an
#: environment where ``getpass`` has nothing to read. Recorded as that rather
#: than as a plausible username, because "who asked for this" answered wrongly
#: is worse than answered not at all.
UNKNOWN_USER: Final = "unknown"


def whoami() -> str:
    """The submitting identity for this process.

    ``getpass.getuser`` reads ``LOGNAME``/``USER``/``LNAME``/``USERNAME`` and
    then falls back to the password database, and it *raises* when none of them
    resolve rather than returning a default — which happens in exactly the
    unattended environments this will eventually run in.
    """
    try:
        return getpass.getuser()
    except (KeyError, OSError):  # pragma: no cover - needs a login-less process
        return os.environ.get("USER") or UNKNOWN_USER


def cli_submitter(name: str | None = None) -> Submitter:
    """Who this invocation is on behalf of. See the module docstring on trust."""
    who = (name or whoami()).strip() or UNKNOWN_USER
    return Submitter(
        source=IngestSource.CLI,
        external_id=who,
        display_name=who,
        trusted=True,
    )


def mint_ref() -> str:
    """A reference for a request that did not name itself."""
    return f"{REF_PREFIX}{secrets.token_hex(6)}"


def submit(
    intake: Intake,
    *,
    text: str,
    at: datetime,
    ref: str | None = None,
    title: str | None = None,
    item_type: WorkItemType = DEFAULT_TYPE,
    conversation_id: str | None = None,
    labels: Sequence[str] = (),
    workflow_override: str | None = None,
    submitter: Submitter | None = None,
    amend: bool = False,
) -> Admission:
    """Submit — or amend — one request from the command line.

    ``amend`` picks the verb rather than the content deciding it. From a webhook
    an edit and a redelivery are indistinguishable until the text is compared,
    and intake compares them; from a command line the person typing knows which
    one they meant, and saying so buys the refusal that matters — amending a
    reference that was never submitted is a typo, and creating a work item out
    of a typo is how the typo becomes work.
    """
    item = normalise(
        source=IngestSource.CLI,
        external_id=ref or mint_ref(),
        raw_text=text,
        submitter=submitter or cli_submitter(),
        at=at,
        title=title,
        item_type=item_type,
        conversation_id=conversation_id,
        labels=labels,
        workflow_override=workflow_override,
    )
    return intake.amend(item, at=at) if amend else intake.submit(item, at=at)


def withdraw(
    intake: Intake,
    ref: str,
    *,
    at: datetime,
    reason: str,
) -> Admission:
    """Take a CLI request back, by the reference it was submitted under."""
    return intake.withdraw(key(ref), reason=reason, at=at)


def reply(
    intake: Intake,
    conversation_id: str,
    *,
    body: str,
    at: datetime,
    author: str | None = None,
) -> tuple[Admission, Turn]:
    """Continue a conversation. Never opens a new request — see ``Intake.reply``."""
    return intake.reply(
        source=IngestSource.CLI,
        conversation_id=conversation_id,
        body=body,
        author=author or whoami(),
        at=at,
    )


def key(ref: str) -> str:
    """The dedupe key a CLI reference lives under.

    ``ports.ingest.dedupe_key`` derives this from a whole ``WorkItem``, which
    the surfaces that only have a reference in hand do not have. Same rule, one
    of the two arguments already known.
    """
    return f"{IngestSource.CLI.value}:{ref}"
