"""What an adapter raises when it cannot do the thing.

One rule, and it is the whole reason this module exists: **the adapter declares
whether a failure is worth retrying; the caller never guesses.** v1 decided
retryability at the call site, by matching on exception types and, in three
places, on substrings of the message — so a rate limit and a malformed payload
were retried identically, and adding a provider meant editing every caller that
had an opinion about its errors. The adapter is the only code that knows the
difference between "GitHub is having a minute" and "that repository does not
exist", so the answer travels with the exception.

The shape deliberately mirrors ``engine.StepFailure`` — ``kind``, ``message``,
``retryable`` — because a port failure inside a step becomes exactly that, and a
translation that has to invent a field is a translation that will invent it
differently in each handler.

``kind`` is a stable, dash-cased slug and is what anything downstream branches
on. ``message`` is for humans and nothing reads it. That split is also what
keeps the audit trail safe to write: S4 records error *kinds*, never messages,
because a message is where a provider's echo of the request — and therefore a
pasted key — ends up.
"""

from __future__ import annotations


class PortError(Exception):
    """An adapter could not complete an operation.

    Never raised directly; the two subclasses below fix ``retryable`` so that
    every raise site has to have decided which it is.
    """

    #: Set by subclasses. Not a constructor argument, because "is this worth a
    #: second attempt" is a property of the failure, not of the moment.
    retryable: bool

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}")


class TransientError(PortError):
    """The operation may succeed if repeated: timeout, rate limit, 5xx, a
    connection reset. Retrying spends time and may change the answer."""

    retryable = True


class PermanentError(PortError):
    """Repeating this changes nothing: bad credentials, an unknown repository,
    a malformed request, a permission the token does not have.

    Retrying a permanent failure is how a bounded retry policy turns one clear
    error into three identical ones and a delay.
    """

    retryable = False


class SecretNotFoundError(PermanentError):
    """A secret was requested by name and the provider does not hold it.

    Permanent on purpose: an unconfigured credential is a deployment problem,
    and retrying makes it look like a flaky service for another two attempts.
    The name is safe to put in the message — that is why secrets are addressed
    by name in the first place.
    """

    def __init__(self, name: str) -> None:
        super().__init__("secret-not-found", f"no secret named {name!r} is configured")
        self.name = name


class OutboxFullError(PermanentError):
    """A queued port's backlog hit its bound and refused the message.

    Refusing is the deliberate half. An unbounded queue in front of a service
    that has been down for a day is v1's 300MB processing log with extra steps:
    the failure surfaces as memory, hours after the cause, and everything that
    could have said "the tracker is down" was busy being buffered.
    """

    def __init__(self, capacity: int) -> None:
        super().__init__("outbox-full", f"the outbox is holding its limit of {capacity} messages")
        self.capacity = capacity
