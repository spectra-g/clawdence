"""Branch names built from untrusted text, and everything they must not become."""

from __future__ import annotations

import pytest

from clawdence.vcs import refs


def test_a_title_becomes_a_readable_slug() -> None:
    assert refs.slugify("Fix: the parser (v2) — again!") == "fix-the-parser-v2-again"


def test_non_ascii_disappears_rather_than_being_transliterated() -> None:
    """The work item id carries the identity, so a lossy title is not a loss.

    A transliteration table is a lot of code whose failure mode is a branch name
    that is wrong in a language nobody on the team reads.
    """
    assert refs.slugify("修正 parser") == "parser"


def test_a_title_of_only_punctuation_slugifies_to_nothing() -> None:
    assert refs.slugify("!!! ??? ---") == ""


def test_truncation_stops_at_a_word_boundary() -> None:
    slug = refs.slugify("alpha beta gamma delta epsilon zeta eta theta", limit=20)
    assert len(slug) <= 20
    assert not slug.endswith("-")
    assert slug == "alpha-beta-gamma"


def test_truncation_does_not_throw_away_most_of_a_long_first_word() -> None:
    """A boundary in the first half is worse than a hard cut: trimming back to it
    would leave three characters of a name that had forty."""
    slug = refs.slugify("a-supercalifragilisticexpialidocious", limit=20)
    assert len(slug) == 20


def test_the_branch_is_a_function_of_the_work_item_not_the_title() -> None:
    """Editing an issue's title must not move the work to a second branch, which
    is what would open a second pull request."""
    first = refs.branch_for("wi.42", "Add a thing")
    second = refs.branch_for("wi.42", "Add a thing (revised)")
    assert first.startswith("clawdence/wi-42-")
    assert second.startswith("clawdence/wi-42-")
    assert refs.branch_for("wi.42") == "clawdence/wi-42"


def test_the_id_comes_first_so_truncation_only_costs_the_description() -> None:
    name = refs.branch_for("wi.42", "x" * 400)
    assert name.startswith("clawdence/wi-42-")
    assert len(name) <= refs.NAME_MAX


@pytest.mark.parametrize(
    "title",
    [
        "--delete everything",
        "../../../etc/passwd",
        "release@{yesterday}",
        "feature.lock",
        "a\nb",
        "spaces and ~carets^ and :colons:",
    ],
)
def test_hostile_titles_produce_ordinary_branch_names(title: str) -> None:
    """Not sanitised — *built*. The output alphabet has no character that means
    anything to git's refspec grammar or to getopt, so there is nothing to
    escape and nothing to get wrong."""
    name = refs.branch_for("wi.1", title)
    assert refs.check_ref_name(name) == name
    assert not name.startswith("-")
    assert ".." not in name
    assert "@{" not in name


def test_an_id_with_no_usable_characters_is_refused() -> None:
    with pytest.raises(refs.InvalidRefError):
        refs.branch_for("...")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "-leading-dash",
        "trailing-slash/",
        "double//slash",
        "UPPER",
        "has space",
        "tilde~here",
        "colon:here",
        "a/b.lock",
        "release@{1}",
        "x" * (refs.NAME_MAX + 1),
    ],
)
def test_check_ref_name_refuses(name: str) -> None:
    with pytest.raises(refs.InvalidRefError):
        refs.check_ref_name(name)


def test_check_ref_name_accepts_a_namespaced_name() -> None:
    assert refs.check_ref_name("clawdence/wi-42-add-a-thing") == "clawdence/wi-42-add-a-thing"


def test_a_prefix_must_name_a_namespace() -> None:
    """Without the slash, ``clawdence`` and ``wi-1`` concatenate into
    ``clawdencewi-1``, which reads as a typo and no branch-protection pattern
    can match."""
    assert refs.check_prefix("bots/") == "bots/"
    assert refs.check_prefix("") == ""
    with pytest.raises(refs.InvalidRefError):
        refs.check_prefix("clawdence")


def test_an_empty_prefix_puts_branches_at_the_top_level() -> None:
    assert refs.branch_for("wi.7", prefix="") == "wi-7"


def test_a_hostile_prefix_is_caught_even_though_it_is_configuration() -> None:
    with pytest.raises(refs.InvalidRefError):
        refs.branch_for("wi.1", "thing", prefix="-x/")
