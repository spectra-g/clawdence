"""Where credentials come from, and what they are allowed to look like.

The control plane holds every provider key in the system (ARCHITECTURE Zone 2),
so the interesting question is not "how do we fetch a token" — it is "how does a
token avoid ending up in a log line, an audit payload, a repr in a traceback, or
a JSON dump of a config object". Four answers here, in decreasing order of how
much they matter:

**Secrets are addressed by name.** ``RepoProfile.mcp_servers`` already holds
``bearer_token_env_var`` rather than a token, because a profile is written to
disk and printed by ``clawdence probe``. This module is the other half of that
decision: the name is the thing that travels through the domain model, and it is
resolved as late as possible, by the one component that is allowed to.

**A resolved secret is not a ``str``.** ``Secret`` wraps the value and its repr
names the secret instead of showing it. Getting the value out takes an explicit
``.reveal()``, which is greppable — "show me every place a credential becomes an
ordinary string" is one search, and it is short. A bare ``str`` gives you no such
question to ask, and interpolates into f-strings, exception messages and
``model_dump()`` output without anybody choosing that.

**The default provider holds nothing.** ``NullSecrets`` raises for every lookup.
A control plane started without credentials should fail at the first step that
needs one, naming it, rather than inherit whatever happens to be in the ambient
environment of whoever ran it.

**Lookups are explicit about absence.** ``resolve`` raises; ``find`` returns
``None``. Callers that can degrade (notify without a token → log instead) use
the second and say so; callers that cannot use the first and get a permanent
error naming the missing variable.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Final, Protocol, final

from clawdence.ports.errors import SecretNotFoundError

#: Substituted for a secret's value wherever redaction happens. Shared so the
#: audit redactor (S4b) and anything that formats a ``Secret`` agree on one
#: marker — a log full of three different placeholders is a log nobody can grep
#: for "did we leak something here".
REDACTED: Final = "[redacted]"


@final
class Secret:
    """A credential that has to be asked for by name to become a string.

    Not a ``DomainModel``: this type must never be serialisable, and every
    domain type is. Not a ``NamedTuple`` or a plain frozen dataclass either —
    both generate a repr from their fields, which is exactly the leak.
    """

    __slots__ = ("_name", "_value")

    def __init__(self, name: str, value: str) -> None:
        self._name = name
        self._value = value

    @property
    def name(self) -> str:
        """What it is called. Safe to log; that is the point of names."""
        return self._name

    def reveal(self) -> str:
        """The value. Call this as late as possible and assign it to nothing."""
        return self._value

    def __repr__(self) -> str:
        return f"Secret({self._name!r}, {REDACTED})"

    def __str__(self) -> str:
        # Same as repr, because the difference between the two is precisely
        # where a value leaks: `f"token={secret}"` is the mistake this prevents.
        return self.__repr__()

    def __eq__(self, other: object) -> bool:
        # Comparing two secrets is legitimate (a rotation check, a test). It
        # compares values, not names, because two names for one credential are
        # still one credential.
        if not isinstance(other, Secret):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        """An empty secret is falsy — an env var set to "" is not a credential."""
        return bool(self._value)


class SecretProvider(Protocol):
    """Resolves a secret name to its value.

    Deliberately tiny. Storage, rotation and per-repo scoping are the
    implementation's business; every caller wants one of two methods.
    """

    def resolve(self, name: str) -> Secret:
        """The secret, or ``SecretNotFoundError`` if it is not configured."""
        ...

    def find(self, name: str) -> Secret | None:
        """The secret, or ``None``. For callers that can degrade."""
        ...

    def names(self) -> frozenset[str]:
        """Every name this provider can resolve.

        Names only, never values — this is what lets startup check that a repo
        profile's declared ``bearer_token_env_var`` is actually present, before
        a run gets far enough to need it.
        """
        ...


class NullSecrets:
    """Holds nothing and admits it. The default.

    A provider that silently resolves nothing would make an unconfigured system
    look configured until the first step that needed a key, which in this system
    is several LLM calls in.
    """

    __slots__ = ()

    def resolve(self, name: str) -> Secret:
        raise SecretNotFoundError(name)

    def find(self, name: str) -> Secret | None:
        return None

    def names(self) -> frozenset[str]:
        return frozenset()


class EnvSecrets:
    """Reads from a process environment, restricted to declared names.

    The allowlist is not decoration. Without it this is `os.environ`, and any
    caller that can choose the name it asks for can read `AWS_SECRET_ACCESS_KEY`
    — including a caller whose name came out of a workflow file, which is data a
    repository owner writes. ``allowed=None`` means every name currently set,
    and is for the single-operator case where the process environment *is* the
    configuration; anything assembling a provider from user input passes a list.
    """

    __slots__ = ("_allowed", "_environ")

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        allowed: Iterable[str] | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._allowed = None if allowed is None else frozenset(allowed)

    def resolve(self, name: str) -> Secret:
        secret = self.find(name)
        if secret is None:
            raise SecretNotFoundError(name)
        return secret

    def find(self, name: str) -> Secret | None:
        if self._allowed is not None and name not in self._allowed:
            return None
        value = self._environ.get(name)
        # An empty variable is treated as absent. `EXPORT TOKEN=` is how a
        # credential goes missing in a shell script, and a provider that hands
        # back "" turns that into a 401 from a service instead of a clear
        # "nothing named TOKEN is configured".
        return None if not value else Secret(name, value)

    def names(self) -> frozenset[str]:
        present = {name for name, value in self._environ.items() if value}
        return frozenset(present if self._allowed is None else present & self._allowed)


class StaticSecrets:
    """A fixed mapping. For tests, and for a config file that has been read.

    Values go in as ordinary strings and come out as ``Secret``s, which is the
    one place in the system where that conversion is supposed to happen.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = {name: value for name, value in (values or {}).items() if value}

    def resolve(self, name: str) -> Secret:
        secret = self.find(name)
        if secret is None:
            raise SecretNotFoundError(name)
        return secret

    def find(self, name: str) -> Secret | None:
        value = self._values.get(name)
        return None if value is None else Secret(name, value)

    def names(self) -> frozenset[str]:
        return frozenset(self._values)
