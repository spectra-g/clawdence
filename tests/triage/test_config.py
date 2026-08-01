"""The composition root as a file, and the mistakes it refuses to make.

Almost every test here is about a refusal, which is the right shape for a
configuration loader: the successful case is one assertion, and the value is
entirely in whether a misconfiguration is caught at load time or discovered eight
minutes into a run that has already called a model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawdence.domain import IsolationTier, RepoProfile, WorkItemType
from clawdence.ports.secrets import StaticSecrets
from clawdence.triage import Config, ConfigError, default_config_path, load, parse
from clawdence.triage.wiring import runner as build_runner
from tests.triage.conftest import PORTAL, WIDGET, ConfigWriter


def test_a_deployment_is_its_repositories(config_path: Path) -> None:
    deployment = load(config_path)
    assert set(deployment.profiles) == {WIDGET, PORTAL}
    assert deployment.profile(WIDGET).name == "widget"


def test_paths_are_relative_to_the_file_and_not_to_the_shell(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configuration that means something different depending on where the
    operator was standing works in a shell and fails under systemd."""
    monkeypatch.chdir(tmp_path)
    deployment = load(config_path)
    assert deployment.repo_store == config_path.parent / "mirrors"
    assert deployment.work_root.is_absolute()


def test_a_home_relative_path_is_expanded(write_config: ConfigWriter, widget: RepoProfile) -> None:
    """``~`` is the one thing an operator most reasonably wants to name, and it
    means nothing to ``Path`` until somebody expands it."""
    config = write_config(widget)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "  repo_store: mirrors", "  repo_store: ~/.clawdence-test/mirrors"
        ),
        encoding="utf-8",
    )
    deployment = load(config)
    assert "~" not in str(deployment.repo_store)
    assert deployment.repo_store.is_absolute()


def test_a_missing_file_says_what_it_was_for(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="which repositories exist"):
        load(tmp_path / "nothing.yaml")


def test_two_profiles_may_not_claim_one_id(write_config: ConfigWriter, widget: RepoProfile) -> None:
    """Sharing an id means sharing an object store and a branch namespace.

    ``mirror_name`` puts a digest of the id in the directory name precisely to
    stop two *different* ids from colliding; this is the same collision arriving
    from the other direction, and only the loader can see it.
    """
    twin = widget.model_copy(update={"name": "widget-again"})
    with pytest.raises(ConfigError, match="share one object store"):
        load(write_config(widget, twin))


def test_a_profile_that_is_not_a_profile_is_refused_by_name(
    write_config: ConfigWriter,
) -> None:
    config = write_config()
    (config.parent / "profiles" / f"{WIDGET}.json").write_text(
        json.dumps({"id": WIDGET, "name": "widget"}), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="remote_url"):
        load(config)


def test_an_unreadable_profile_names_the_path(write_config: ConfigWriter) -> None:
    config = write_config()
    (config.parent / "profiles" / f"{WIDGET}.json").unlink()
    with pytest.raises(ConfigError, match="cannot be read"):
        load(config)


def test_an_unknown_key_is_a_typo_and_not_a_feature() -> None:
    """``extra="forbid"`` earning its keep: a misspelled ``work_root`` that was
    silently ignored would put worktrees where the reaper is not looking."""
    with pytest.raises(ConfigError, match="work_roots"):
        parse("schema_version: 1\npaths:\n  work_roots: /tmp/x\n")


def test_a_newer_schema_version_is_refused_rather_than_half_read() -> None:
    with pytest.raises(ConfigError, match="newer"):
        parse("schema_version: 99\n")


def test_an_empty_file_is_not_an_empty_deployment() -> None:
    with pytest.raises(ConfigError, match="is empty"):
        parse("")


def test_a_workflow_name_is_one_path_component(config_path: Path) -> None:
    """A ``workflow_override`` can reach this from a request.

    Anything that traverses is a request choosing which file the control plane
    executes, which is the one thing the closed-set argument in ``routing`` does
    not cover — so it is refused here instead.
    """
    deployment = load(config_path)
    assert deployment.workflow_path("sprint").name == "sprint.yaml"
    for hostile in ("../../etc/passwd", "/etc/passwd", "..", ""):
        with pytest.raises(ConfigError, match="not a workflow name"):
            deployment.workflow_path(hostile)


def test_an_unconfigured_repository_lists_the_ones_that_are(config_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"repo\.portal, repo\.widget"):
        load(config_path).profile("repo.nope")


def test_the_default_routes_cover_every_work_item_type() -> None:
    """A type with no route falls back, but falling back should be the exception.

    If a member is added to ``WorkItemType`` and nothing here changes, this fails
    — which is the reminder to decide what it routes to rather than letting it
    inherit ``sprint`` silently.
    """
    routes = Config().routing.by_type
    assert set(routes) == set(WorkItemType)


def test_the_config_lives_beside_the_state_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One deployment is one directory. Pointing the database at a test home and
    leaving the configuration in the real one is worth making impossible."""
    monkeypatch.setenv("CLAWDENCE_HOME", str(tmp_path))
    assert default_config_path().parent == tmp_path


# ------------------------------------------------------------------- runner


def test_a_container_runner_without_an_image_refuses_and_says_why() -> None:
    config = parse("schema_version: 1\nrunner:\n  tier: container\n  argv: [codex, exec]\n").runner
    assert config is not None
    with pytest.raises(ConfigError, match="no default image"):
        build_runner(config, StaticSecrets())


def test_the_socket_tier_is_not_a_deployment_wide_setting() -> None:
    """§3.2 asks for socket mode to be opt-in *per repository*.

    A deployment-wide setting would apply it to every repository at once, which
    is the thing ``RepoProfile.docker_socket_acknowledged`` exists to prevent —
    so naming it here is refused rather than obeyed.
    """
    config = parse(
        "schema_version: 1\nrunner:\n  tier: 'container+docker:socket'\n  argv: [codex]\n"
    ).runner
    assert config is not None
    with pytest.raises(ConfigError, match="per-repository decision"):
        build_runner(config, StaticSecrets())


def test_half_a_price_sheet_is_refused() -> None:
    """A budget enforced against half of what a run cost is worse than none."""
    config = parse(
        "schema_version: 1\nrunner:\n  tier: host\n  argv: [codex]\n  input_usd_per_mtok: 3\n"
    ).runner
    assert config is not None
    with pytest.raises(ConfigError, match="go together"):
        build_runner(config, StaticSecrets())


def test_a_host_runner_needs_nothing_but_argv() -> None:
    config = parse("schema_version: 1\nrunner:\n  tier: host\n  argv: [codex]\n").runner
    assert config is not None
    assert build_runner(config, StaticSecrets()).tier is IsolationTier.HOST  # type: ignore[attr-defined]


def test_an_unknown_delivery_lists_the_ones_that_exist() -> None:
    config = parse(
        "schema_version: 1\nrunner:\n  tier: host\n  argv: [codex]\n  delivery: telepathy\n"
    ).runner
    assert config is not None
    with pytest.raises(ConfigError, match="'stdin'"):
        build_runner(config, StaticSecrets())


def test_a_directory_is_not_a_configuration_file(tmp_path: Path) -> None:
    """Reading a directory raises ``IsADirectoryError``, not ``FileNotFoundError``,
    so it needs its own arm — and an operator who pointed ``--config`` at the
    deployment directory rather than at the file in it has made this exact typo."""
    with pytest.raises(ConfigError, match="cannot be read"):
        load(tmp_path)


def test_yaml_that_does_not_parse_is_named_as_yaml() -> None:
    with pytest.raises(ConfigError, match="config"):
        parse("paths:\n  - [unclosed\n")


def test_a_list_at_the_top_level_is_refused() -> None:
    with pytest.raises(ConfigError, match="must be a mapping"):
        parse("- one\n- two\n")


def test_a_non_integer_schema_version_says_so() -> None:
    with pytest.raises(ConfigError, match="must be an integer"):
        parse("schema_version: '1'\n")


def test_an_older_schema_version_is_refused_too() -> None:
    """Both directions. An older file is not one this build can migrate silently,
    and half-reading it means running with a repository list nobody wrote."""
    with pytest.raises(ConfigError, match="older"):
        parse("schema_version: 0\n")


def test_a_profile_that_is_not_parseable_names_the_file(write_config: ConfigWriter) -> None:
    config = write_config()
    (config.parent / "profiles" / f"{WIDGET}.json").write_text("{[not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON or YAML"):
        load(config)


def test_an_unknown_accumulation_lists_the_ones_that_exist() -> None:
    config = parse(
        "schema_version: 1\nrunner:\n  tier: host\n  argv: [codex]\n  accumulation: vibes\n"
    ).runner
    assert config is not None
    with pytest.raises(ConfigError, match="cumulative"):
        build_runner(config, StaticSecrets())


def test_a_pinned_container_image_builds_a_container_runner() -> None:
    """The one path through ``wiring.runner`` that produces the tier M1's goal
    names. Digest-pinned because the runner refuses a tag, which is its own test
    in ``tests/runners`` and not restated here."""
    config = parse(
        "schema_version: 1\nrunner:\n  tier: container\n"
        "  image: ghcr.io/example/runner@sha256:" + "0" * 64 + "\n"
        "  argv: [codex, exec]\n"
        "  input_usd_per_mtok: 3\n  output_usd_per_mtok: 15\n"
    ).runner
    assert config is not None
    assert build_runner(config, StaticSecrets()).tier is IsolationTier.CONTAINER  # type: ignore[attr-defined]
