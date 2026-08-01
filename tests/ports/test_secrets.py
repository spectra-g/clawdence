"""Both providers against the contract, plus what each one does alone."""

from __future__ import annotations

import pytest

from clawdence.ports import (
    REDACTED,
    EnvSecrets,
    NullSecrets,
    Secret,
    SecretNotFoundError,
    StaticSecrets,
)
from tests.ports.contract import SecretProviderContract


class TestStaticSecrets(SecretProviderContract):
    @pytest.fixture
    def secrets(self) -> StaticSecrets:
        return StaticSecrets(self.known)


class TestEnvSecrets(SecretProviderContract):
    @pytest.fixture
    def secrets(self) -> EnvSecrets:
        # A mapping rather than the real environment: a test that read
        # ``os.environ`` would pass or fail depending on the shell that started
        # it, and would be one ``monkeypatch`` away from reading a real key.
        return EnvSecrets({**self.known, "EMPTY": ""})


def test_null_secrets_hold_nothing() -> None:
    provider = NullSecrets()
    assert provider.find("ANYTHING") is None
    assert provider.names() == frozenset()
    with pytest.raises(SecretNotFoundError):
        provider.resolve("ANYTHING")


def test_an_empty_variable_counts_as_absent() -> None:
    """``export TOKEN=`` is how a credential goes missing in a shell script.

    Handing back ``""`` turns that into a 401 from a service — an error a long
    way from its cause — rather than "nothing named TOKEN is configured".
    """
    provider = EnvSecrets({"TOKEN": ""})
    assert provider.find("TOKEN") is None
    assert "TOKEN" not in provider.names()


def test_the_allowlist_bounds_what_can_be_read() -> None:
    """Without it this is ``os.environ``, and any caller that chooses the name
    it asks for can read ``AWS_SECRET_ACCESS_KEY`` — including one whose name
    came out of a workflow file, which a repository owner writes."""
    provider = EnvSecrets({"ALLOWED": "yes", "AWS_SECRET_ACCESS_KEY": "no"}, allowed=["ALLOWED"])
    assert provider.resolve("ALLOWED").reveal() == "yes"
    assert provider.find("AWS_SECRET_ACCESS_KEY") is None
    assert provider.names() == frozenset({"ALLOWED"})


def test_static_secrets_drop_empty_values() -> None:
    assert StaticSecrets({"A": "x", "B": ""}).names() == frozenset({"A"})


def test_the_redaction_marker_is_what_appears() -> None:
    """One marker, shared with the audit redactor, so "did we leak here" is a
    single grep rather than three spellings nobody remembers."""
    assert REDACTED in repr(Secret("TOKEN", "value"))


def test_secrets_compare_by_value_not_name() -> None:
    """Two names for one credential are still one credential — which is what a
    rotation check is asking about."""
    assert Secret("A", "same") == Secret("B", "same")
    assert Secret("A", "same") != Secret("A", "different")
    assert Secret("A", "same") != "same"


def test_an_empty_secret_is_falsy() -> None:
    assert not Secret("TOKEN", "")
    assert Secret("TOKEN", "x")


def test_secrets_hash_by_value() -> None:
    assert len({Secret("A", "same"), Secret("B", "same")}) == 1
