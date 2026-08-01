"""The warm dependency cache: where it lives, what points at it, and who owns it.

These are the claims a package manager depends on being true, and every one of
them is a way the naive version fails silently. A cache directory computed from
a repo id containing a colon is a directory the run cannot create on half the
platforms it will meet; a Maven variable holding a bare path is a goal named
after a directory; a directory created by the daemon is a directory the
container cannot write to. None of those raise — they produce a run that
reinstalls everything, every time, and reports success.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from clawdence.domain import BuildSystem, RepoProfile
from clawdence.runners.cache import CACHE_HOME_ENV, Cache, cache_home
from tests.runners.conftest import container_profile


def profile_for(build_system: BuildSystem, repo_id: str = "repo.fixture") -> RepoProfile:
    return container_profile(id=repo_id, build_system=build_system)


# --------------------------------------------------------------------------- #
# Where it lives
# --------------------------------------------------------------------------- #


def test_the_cache_home_is_taken_from_the_environment_when_set(tmp_path: Path) -> None:
    assert cache_home({CACHE_HOME_ENV: str(tmp_path)}) == tmp_path


def test_the_cache_home_respects_xdg_before_falling_back_to_home() -> None:
    """Both, in that order. ``XDG_CACHE_HOME`` is the platform's answer to this
    exact question, and ignoring it puts regenerable data where the user said
    not to."""
    assert cache_home({"XDG_CACHE_HOME": "/xdg"}) == Path("/xdg/clawdence/deps")
    assert cache_home({"HOME": "/home/someone"}) == Path("/home/someone/.cache/clawdence/deps")


def test_a_repo_id_with_path_structure_in_it_does_not_become_path_structure(
    tmp_path: Path,
) -> None:
    """``RepoId`` admits ``:`` and ``.``; a directory name should not.

    A colon is legal on POSIX, illegal on Windows, and a separator to enough
    tooling that leaving it in is borrowing trouble. What matters more is that
    nothing in the id can introduce a *level*: a cache directory is always
    exactly one child of the root.
    """
    cache = Cache(root=tmp_path)
    directory = cache.directory(profile_for(BuildSystem.NPM, "acme:web.frontend"))

    assert directory.parent == tmp_path
    assert ":" not in directory.name
    assert "/" not in directory.name


def test_two_repos_that_differ_only_where_the_name_is_sanitised_do_not_share(
    tmp_path: Path,
) -> None:
    """The readable half is lossy, and the digest is what makes it correct.

    ``acme:web`` and ``acme.web`` both sanitise to ``acme-web``. Sharing a cache
    between two repositories is not merely untidy — it is two build tools
    writing to one lock file.
    """
    cache = Cache(root=tmp_path)

    first = cache.directory(profile_for(BuildSystem.NPM, "acme:web"))
    second = cache.directory(profile_for(BuildSystem.NPM, "acme.web"))

    assert first != second


def test_changing_build_system_starts_a_new_cache(tmp_path: Path) -> None:
    """A yarn cache laid out for yarn is not a pnpm store, and a repository that
    migrated should not inherit one shaped for the tool it left."""
    cache = Cache(root=tmp_path)

    assert cache.directory(profile_for(BuildSystem.YARN)) != cache.directory(
        profile_for(BuildSystem.PNPM)
    )


# --------------------------------------------------------------------------- #
# What points at it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("build_system", "variable"),
    [
        (BuildSystem.NPM, "npm_config_cache"),
        (BuildSystem.YARN, "YARN_CACHE_FOLDER"),
        (BuildSystem.PNPM, "PNPM_STORE_DIR"),
        (BuildSystem.UV, "UV_CACHE_DIR"),
        (BuildSystem.POETRY, "POETRY_CACHE_DIR"),
        (BuildSystem.PIP, "PIP_CACHE_DIR"),
        (BuildSystem.GRADLE, "GRADLE_USER_HOME"),
        (BuildSystem.CARGO, "CARGO_HOME"),
    ],
)
def test_each_build_system_names_its_own_cache_variable(
    tmp_path: Path, build_system: BuildSystem, variable: str
) -> None:
    plan = Cache(root=tmp_path).plan(profile_for(build_system))

    assert plan is not None
    assert variable in plan.env
    assert Path(plan.env[variable]).is_absolute()


def test_go_gets_both_of_its_caches(tmp_path: Path) -> None:
    """Go splits downloads from build output across two variables, and caching
    only the module cache leaves every compile cold."""
    plan = Cache(root=tmp_path).plan(profile_for(BuildSystem.GO))

    assert plan is not None
    assert set(plan.env) == {"GOMODCACHE", "GOCACHE"}
    assert plan.env["GOMODCACHE"] != plan.env["GOCACHE"]


def test_maven_gets_a_flag_rather_than_a_bare_path(tmp_path: Path) -> None:
    """``MAVEN_ARGS`` is a command line, not a directory.

    Setting it to a path would hand Maven a goal named after a directory, and
    Maven's response to an unknown goal is to fail the build — which would turn
    "we enabled caching" into "this repository stopped installing".
    """
    plan = Cache(root=tmp_path).plan(profile_for(BuildSystem.MAVEN))

    assert plan is not None
    assert plan.env["MAVEN_ARGS"].startswith("-Dmaven.repo.local=")


def test_a_repository_with_no_recognised_build_system_gets_no_cache(tmp_path: Path) -> None:
    """No guess. Pointing an unknown toolchain's variables somewhere would be
    inventing a layout for a tool nobody has identified."""
    assert Cache(root=tmp_path).plan(profile_for(BuildSystem.UNKNOWN)) is None


def test_caching_can_be_turned_off_without_editing_a_profile(tmp_path: Path) -> None:
    assert Cache(root=tmp_path, enabled=False).plan(profile_for(BuildSystem.NPM)) is None


# --------------------------------------------------------------------------- #
# Who owns it
# --------------------------------------------------------------------------- #


def test_prepare_creates_the_directories_before_anything_mounts_them(tmp_path: Path) -> None:
    """The ownership property, as far as a test can state it.

    What actually matters is that these exist *before* the daemon is asked to
    bind-mount them: a bind-mount source the daemon has to create is created as
    root, and the container then runs as the invoking user and cannot write to
    its own cache. The live suite is where the ownership half is checked; here
    the claim is that ``prepare`` leaves nothing for the daemon to create.
    """
    plan = Cache(root=tmp_path).plan(profile_for(BuildSystem.GO))
    assert plan is not None

    plan.prepare()

    assert plan.subdirectories
    for path in plan.subdirectories:
        assert path.is_dir()


def test_maven_does_not_get_a_directory_named_after_its_flag(tmp_path: Path) -> None:
    """The reason ``subdirectories`` exists separately from ``env``.

    A ``mkdir`` over the environment's values would create
    ``-Dmaven.repo.local=/…/m2`` and leave the real cache directory absent —
    which is the daemon-creates-it failure, arrived at from the other side.
    """
    plan = Cache(root=tmp_path).plan(profile_for(BuildSystem.MAVEN))
    assert plan is not None

    plan.prepare()

    assert (plan.directory / "m2").is_dir()
    assert not any("-D" in child.name for child in plan.directory.iterdir())


def test_prepare_marks_the_cache_as_used_even_when_it_writes_nothing(tmp_path: Path) -> None:
    """Last used, not last written — which is what the reaper reads.

    A repository whose dependencies are already complete reads its cache and
    changes nothing in it. Left to the package manager's writes, a cache in
    daily use would look untouched for a month, and the reaper would agree and
    delete it.
    """
    plan = Cache(root=tmp_path).plan(profile_for(BuildSystem.UV))
    assert plan is not None
    plan.prepare()

    stale = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    os.utime(plan.directory, (stale, stale))
    plan.prepare()

    assert plan.directory.stat().st_mtime > stale
