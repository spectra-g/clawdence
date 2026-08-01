"""The reaper — releasing in the right order, even when a release fails."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.harness.cleanup import Reaper


def _raise(error: BaseException) -> Callable[[], None]:
    def release() -> None:
        raise error

    return release


def test_releases_newest_first() -> None:
    """A worktree lives inside a directory and a container has it mounted.
    Releasing in creation order deletes the directory out from under both."""
    released: list[str] = []
    reaper = Reaper()
    reaper.register("directory", lambda: released.append("directory"))
    reaper.register("worktree", lambda: released.append("worktree"))
    reaper.register("container", lambda: released.append("container"))

    assert reaper.release_all() == ()
    assert released == ["container", "worktree", "directory"]


def test_a_failing_release_does_not_stop_the_rest() -> None:
    """This code runs *because* something went wrong. A teardown that gives up
    at the first error leaks everything after it — exactly when it matters."""
    released: list[str] = []
    reaper = Reaper()
    reaper.register("first", lambda: released.append("first"))
    reaper.register("broken", _raise(RuntimeError("daemon said 409")))
    reaper.register("last", lambda: released.append("last"))

    leaks = reaper.release_all()
    assert released == ["last", "first"]
    assert [leak.what for leak in leaks] == ["broken"]


def test_a_leak_says_what_and_why() -> None:
    """Discovered days later, in a log, by someone who did not write the test.
    "cleanup failed" is not actionable."""
    reaper = Reaper()
    reaper.register("container clawdence-run-abc123", _raise(RuntimeError("409")))
    leak = reaper.release_all()[0]
    assert leak.describe() == "container clawdence-run-abc123: RuntimeError: 409"


def test_the_stack_is_emptied_even_by_a_failure() -> None:
    """A release that failed once fails identically the second time, so
    retrying at session end would turn one report into two."""
    reaper = Reaper()
    reaper.register("broken", _raise(RuntimeError("no")))
    assert len(reaper.release_all()) == 1
    assert reaper.release_all() == ()
    assert len(reaper) == 0


def test_outstanding_reports_what_is_still_held() -> None:
    reaper = Reaper()
    reaper.register("first", lambda: None)
    reaper.register("second", lambda: None)
    assert reaper.outstanding == ("second", "first")
    assert len(reaper) == 2


def test_a_keyboard_interrupt_stops_the_teardown() -> None:
    """``Exception``, not ``BaseException``: a Ctrl-C part way through cleanup
    should stop it, not be collected as a leak report and swallowed."""
    reaper = Reaper()
    reaper.register("ignored", lambda: None)
    reaper.register("interrupted", _raise(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        reaper.release_all()


def test_the_fixture_registers_and_releases(reaper: Reaper) -> None:
    """The fixture in ``conftest`` asserts on the leaks at teardown, so this
    only has to show that registration reaches it."""
    reaper.register("nothing", lambda: None)
    assert reaper.outstanding == ("nothing",)
