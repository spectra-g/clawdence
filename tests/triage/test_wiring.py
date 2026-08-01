"""The secret allowlist, which is the only interesting thing in ``wiring``.

Everything else in that module is a constructor. This is the part that decides
what the control plane may read out of its own environment, and getting it wrong
in the permissive direction turns ``EnvSecrets`` back into ``os.environ``.
"""

from __future__ import annotations

from pathlib import Path

from clawdence.domain import RepoProfile
from clawdence.triage import Deployment, load, secret_names, secrets_for
from clawdence.triage.wiring import MODEL_KEY_ENV
from tests.triage.conftest import ConfigWriter


def test_the_allowlist_is_derived_from_what_the_operator_wrote_down(
    write_config: ConfigWriter, widget: RepoProfile
) -> None:
    """Adding a repository with an MCP token extends the allowlist by editing the
    thing that needs it, rather than by remembering a second list."""
    with_mcp = RepoProfile.model_validate(
        widget.model_dump(mode="json")
        | {
            "mcp_servers": [
                {
                    "name": "docs",
                    "url": "https://mcp.invalid",
                    "bearer_token_env_var": "DOCS_TOKEN",
                }
            ]
        }
    )
    deployment = load(write_config(with_mcp, **{"forge_token_env": "FORGE_PAT"}))
    assert secret_names(deployment) == {MODEL_KEY_ENV, "FORGE_PAT", "DOCS_TOKEN"}


def test_a_name_nobody_configured_is_unreadable(config_path: Path) -> None:
    """The allowlist is not decoration. Without it any caller that can choose the
    name it asks for can read ``AWS_SECRET_ACCESS_KEY``."""
    deployment = load(config_path)
    provider = secrets_for(
        deployment, {"AWS_SECRET_ACCESS_KEY": "hunter2", MODEL_KEY_ENV: "sk-test"}
    )
    assert provider.find("AWS_SECRET_ACCESS_KEY") is None
    assert provider.find(MODEL_KEY_ENV) is not None


def test_a_deployment_with_no_forge_credential_asks_for_none(
    deployment: Deployment,
) -> None:
    """``forge_token_env: null`` is a real configuration — ssh remotes and public
    https authenticate nobody — so nothing should go looking for a token."""
    assert deployment.config.forge_token_env is None
    assert secret_names(deployment) == {MODEL_KEY_ENV}


def test_the_runner_key_is_in_the_allowlist_and_the_control_plane_key_is_not_the_same_one(
    write_config: ConfigWriter, widget: RepoProfile
) -> None:
    """§3.1 gives the runner a budgeted key of its own.

    Both names end up readable, and they are different names — which is the
    whole point of ``secret_env`` being a mapping rather than a passthrough.
    """
    config = write_config(widget)
    config.write_text(
        config.read_text(encoding="utf-8")
        + "runner:\n  tier: host\n  argv: [codex]\n"
        + "  secret_env:\n    OPENAI_API_KEY: runner-llm-key\n",
        encoding="utf-8",
    )
    names = secret_names(load(config))
    assert "runner-llm-key" in names
    assert "OPENAI_API_KEY" not in names, "the *name* is allowlisted, not the variable it lands in"
