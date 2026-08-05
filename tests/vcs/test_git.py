"""The shared git invocation, and the one thing only the control plane does.

The hardening itself is exercised by ``tests/runners/test_worktree`` against a
real repository and did not move when the code did. What is new at S15 is
authentication, and every test here is about a token going somewhere it should
not: an argv, a config file the wrong process can read, or a host nobody chose.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from clawdence.ports.secrets import Secret
from clawdence.vcs import git as g
from tests.harness.repos import FixtureRepo
from tests.ports.factories import run

TOKEN = Secret("CLAWDENCE_FORGE_TOKEN", "ghp-not-a-real-token")


def test_no_token_yields_the_plain_environment() -> None:
    """Callers do not branch on whether there is a credential. An ssh remote and
    a public repository both need none, and a function that returned ``None``
    would push that decision into every call site."""
    with g.authenticated(None, remote_url="https://forge.invalid/a/b") as env:
        assert env == g.BASE_ENV
    with g.authenticated(Secret("EMPTY", ""), remote_url="https://forge.invalid/a/b") as env:
        assert env == g.BASE_ENV


def test_ssh_is_batch_only_and_inherits_only_the_agent_socket() -> None:
    caller = {
        "SSH_AUTH_SOCK": "/private/tmp/agent.sock",
        "GITHUB_TOKEN": "must-not-cross",
        "AWS_SECRET_ACCESS_KEY": "must-not-cross-either",
    }
    with g.authenticated(
        TOKEN,
        remote_url="git@github.com:acme/widget.git",
        environ=caller,
        ssh_path="/usr/bin/ssh",
    ) as env:
        assert env["SSH_AUTH_SOCK"] == caller["SSH_AUTH_SOCK"]
        assert env["GIT_SSH_COMMAND"] == "/usr/bin/ssh -o BatchMode=yes"
        assert "GITHUB_TOKEN" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_ssh_without_a_loaded_agent_still_cannot_prompt() -> None:
    with g.authenticated(
        None, remote_url="ssh://git@github.com/acme/widget.git", environ={}
    ) as env:
        assert "SSH_AUTH_SOCK" not in env
        assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


# ------------------------------------------------------- which ssh, exactly


def test_the_ssh_binary_is_named_absolutely(tmp_path: Path) -> None:
    """The environment handed to git has no ``PATH``, so a bare ``ssh`` is
    resolved by a shell's compiled-in default — which on macOS prefers a Homebrew
    OpenSSH over Apple's, and only Apple's implements ``UseKeychain``. Same key,
    same config, two different answers."""
    chosen = tmp_path / "bin" / "ssh"
    chosen.parent.mkdir()
    chosen.touch(mode=0o755)

    with g.authenticated(
        None,
        remote_url="git@github.com:acme/widget.git",
        environ={"PATH": str(chosen.parent)},
    ) as env:
        assert env["GIT_SSH_COMMAND"] == f"{chosen} -o BatchMode=yes"


def test_a_pinned_ssh_wins_over_the_callers_path(tmp_path: Path) -> None:
    """The escape hatch for a host whose ``PATH`` finds the wrong one."""
    assert g.ssh_command("/usr/bin/ssh", {"PATH": str(tmp_path)}) == "/usr/bin/ssh -o BatchMode=yes"


def test_a_path_with_a_space_survives_the_shell_git_runs_it_through() -> None:
    """``GIT_SSH_COMMAND`` is a command line, not an argv, so an unquoted
    ``/Applications/My Tools/ssh`` is two arguments and neither is a program."""
    assert g.ssh_command("/opt/my tools/ssh") == "'/opt/my tools/ssh' -o BatchMode=yes"


def test_an_unfindable_ssh_leaves_gits_own_behaviour_alone(tmp_path: Path) -> None:
    """Nothing on the search path is not this function's failure to report: the
    child may still see one, and refusing here would break a working deployment
    over a lookup that was only ever advisory."""
    assert g.ssh_command(None, {"PATH": str(tmp_path)}) == "ssh -o BatchMode=yes"


def test_the_token_reaches_git_through_a_file_and_not_the_environment() -> None:
    """``ps`` is world-readable and an argv is in it. The environment only ever
    carries the *path* to a config file; the credential is in the file.

    The assertion below is about where the secret is, not about it being hidden:
    the file holds the basic-auth encoding of the token, which is the token, and
    what protects it is the mode the next test checks.
    """
    with g.authenticated(TOKEN, remote_url="https://forge.invalid/a/b") as env:
        assert not any(TOKEN.reveal() in value for value in env.values())
        assert set(env) - set(g.BASE_ENV) == set()

        config = Path(env["GIT_CONFIG_GLOBAL"])
        encoded = base64.b64encode(f"x-access-token:{TOKEN.reveal()}".encode()).decode()
        assert encoded in config.read_text(encoding="utf-8")


def test_the_config_file_is_readable_only_by_this_user() -> None:
    with g.authenticated(TOKEN, remote_url="https://forge.invalid/a/b") as env:
        config = Path(env["GIT_CONFIG_GLOBAL"])
        assert config.stat().st_mode & 0o077 == 0


def test_the_config_file_is_removed_even_when_the_body_raises() -> None:
    """It holds a forge credential and it is under the system temp root. One left
    behind is a readable copy of a token sitting there until a reboot."""
    seen: Path | None = None
    with pytest.raises(RuntimeError):
        with g.authenticated(TOKEN, remote_url="https://forge.invalid/a/b") as env:
            seen = Path(env["GIT_CONFIG_GLOBAL"])
            raise RuntimeError("the operation failed")
    assert seen is not None
    assert not seen.exists()


def test_the_header_is_scoped_to_one_origin() -> None:
    """An unscoped ``http.extraHeader`` is attached to every request git makes,
    and git makes requests to hosts a repository names — a submodule URL is
    content somebody else wrote."""
    with g.authenticated(TOKEN, remote_url="https://forge.invalid/a/b") as env:
        text = Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
    assert '[http "https://forge.invalid/"]' in text
    assert "extraHeader = Authorization: Basic " in text


def test_the_credential_helper_is_cleared() -> None:
    """Nothing should be consulting a keychain on our behalf, and a helper that
    prompts is a hang rather than a failure."""
    with g.authenticated(TOKEN, remote_url="https://forge.invalid/a/b") as env:
        text = Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
    assert "[credential]" in text
    assert "helper =" in text


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://github.com/acme/widget", "https://github.com/"),
        ("https://github.com/acme/widget.git", "https://github.com/"),
        ("https://ghe.example:8443/acme/widget", "https://ghe.example:8443/"),
    ],
)
def test_origin_of_keeps_the_authority_and_nothing_else(url: str, origin: str) -> None:
    assert g.origin_of(url) == origin


def test_the_origin_ends_in_a_slash() -> None:
    """Git matches ``[http "<prefix>"]`` by prefix, so ``https://github.com``
    without one also matches ``https://github.com.evil.example`` — the same
    parse-don't-prefix-match bug the model provider's ``require_secure`` had."""
    origin = g.origin_of("https://github.com/acme/widget")
    assert not "https://github.com.evil.example/x".startswith(origin)


def test_a_relative_remote_has_no_origin_to_scope_to() -> None:
    with pytest.raises(ValueError, match="absolute URL"):
        g.origin_of("../sibling.git")


def test_stdin_reaches_the_batch_plumbing(origin: FixtureRepo) -> None:
    """``cat-file --batch-check`` answers a hundred questions in one process, and
    the hygiene audit is the caller that needs it."""
    blob = run(g.git(origin.path, "rev-parse", "HEAD^{tree}"))
    answer = run(
        g.git(
            origin.path, "cat-file", "--batch-check=%(objecttype)", stdin=f"{blob}\n", strip=False
        )
    )
    assert answer.strip() == "tree"


def test_stdin_is_closed_by_default(origin: FixtureRepo) -> None:
    """A git command that reads from an inherited terminal is a run that hangs,
    not one that fails."""
    assert run(g.git(origin.path, "cat-file", "--batch-check=%(objecttype)", strip=False)) == ""


def test_the_operators_own_git_config_is_ignored() -> None:
    assert g.BASE_ENV["GIT_CONFIG_GLOBAL"] == os.devnull
    assert g.BASE_ENV["GIT_CONFIG_SYSTEM"] == os.devnull
    assert g.BASE_ENV["GIT_TERMINAL_PROMPT"] == "0"


def test_commit_plumbing_has_an_identity_without_git_config(origin: FixtureRepo) -> None:
    """CI accounts often have no full name for Git to infer.

    Global and system config are deliberately disabled, so the wrapper itself
    supplies the honest non-person identity used by low-level ``commit-tree``
    calls as well as ordinary commits.
    """
    parent = run(g.git(origin.path, "rev-parse", "HEAD"))
    tree = run(g.git(origin.path, "rev-parse", "HEAD^{tree}"))
    commit = run(g.git(origin.path, "commit-tree", tree, "-p", parent, "-m", "plumbing"))

    assert (
        run(g.git(origin.path, "show", "-s", "--format=%an <%ae>|%cn <%ce>", commit))
        == "Clawdence runner <runner@clawdence.invalid>|"
        "Clawdence runner <runner@clawdence.invalid>"
    )


def test_an_explicit_author_also_becomes_the_committer() -> None:
    selected = g.with_identity(
        {**g.BASE_ENV, "GIT_AUTHOR_NAME": "Chosen", "GIT_AUTHOR_EMAIL": "chosen@example.invalid"}
    )

    assert selected["GIT_AUTHOR_NAME"] == "Chosen"
    assert selected["GIT_COMMITTER_NAME"] == "Chosen"
    assert selected["GIT_COMMITTER_EMAIL"] == "chosen@example.invalid"


def test_the_hardening_names_every_config_that_executes_something() -> None:
    """Each of these is a way repository-local configuration gets git to run a
    program: two run one outright, and the third lets a URL name one."""
    joined = " ".join(g.HARDENING)
    assert "core.fsmonitor=false" in joined
    assert "core.hooksPath=/dev/null" in joined
    assert "protocol.ext.allow=never" in joined
