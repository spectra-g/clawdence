"""Parsing the five reporter formats, and refusing the things that are not one.

Two claims are under test and they pull in opposite directions. **Enough
survives**: the assertion, the file, the line — the things a model needs to fix
the failure. **Not much survives**: not the forty-frame stack, not the plugin
internals, not two hundred failures when one cause produced them. A parser that
kept everything and a parser that kept nothing would both pass one of these.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawdence.domain import TestReporter as Reporter
from clawdence.verify import reporters
from clawdence.verify.reporters import MAX_FAILURES, MAX_FRAMES, ReportError
from tests.verify import reports

PASSING = {
    Reporter.JUNIT_XML: reports.JUNIT_PASSING,
    Reporter.PYTEST_JSON_REPORT: reports.PYTEST_PASSING,
    Reporter.JEST_JSON: reports.JEST_PASSING,
    Reporter.GO_TEST_JSON: reports.GO_PASSING,
    Reporter.CARGO_JSON: reports.CARGO_PASSING,
}

FAILING = {
    Reporter.JUNIT_XML: reports.JUNIT_FAILING,
    Reporter.PYTEST_JSON_REPORT: reports.PYTEST_FAILING,
    Reporter.JEST_JSON: reports.JEST_FAILING,
    Reporter.GO_TEST_JSON: reports.GO_FAILING,
    Reporter.CARGO_JSON: reports.CARGO_FAILING,
}


@pytest.mark.parametrize("reporter", list(PASSING))
def test_a_passing_run_reports_no_failures(reporter: Reporter) -> None:
    evidence = reporters.parse(reporter, PASSING[reporter])

    assert evidence.reporter is reporter
    assert evidence.failed == 0
    assert evidence.failures == ()
    assert evidence.passed >= 2
    assert evidence.total >= 2


@pytest.mark.parametrize("reporter", list(FAILING))
def test_a_failing_run_names_the_failing_test(reporter: Reporter) -> None:
    evidence = reporters.parse(reporter, FAILING[reporter])

    assert evidence.failed == 1
    assert len(evidence.failures) == 1
    assert "prorates" in evidence.failures[0].test_id.lower()


@pytest.mark.parametrize("reporter", list(FAILING))
def test_the_assertion_survives_the_parse(reporter: Reporter) -> None:
    """The one thing that must never be truncated away.

    Every format buries "12.49 is not 12.50" somewhere different — the top of a
    pytest longrepr, the middle of a JUnit ``message`` attribute, the first two
    lines of a Rust panic. A truncation strategy based on position drops it in
    at least one of them, which is the retry loop that never sees the error.
    """
    failure = reporters.parse(reporter, FAILING[reporter]).failures[0]

    assert "12.49" in failure.message
    assert "12.50" in failure.message


@pytest.mark.parametrize("reporter", list(FAILING))
def test_the_framework_internals_do_not(reporter: Reporter) -> None:
    """The other half: what is dropped is as load-bearing as what is kept."""
    failure = reporters.parse(reporter, FAILING[reporter]).failures[0]
    rendered = failure.message + " ".join(failure.frames)

    assert "jest-circus" not in rendered
    assert "_pytest" not in rendered
    assert "NativeMethodAccessorImpl" not in rendered
    assert len(failure.frames) <= MAX_FRAMES


@pytest.mark.parametrize("reporter", [Reporter.JUNIT_XML, Reporter.PYTEST_JSON_REPORT])
def test_the_location_is_extracted(reporter: Reporter) -> None:
    failure = reporters.parse(reporter, FAILING[reporter]).failures[0]

    assert failure.file is not None
    assert failure.line == 88 or failure.line == 41


@pytest.mark.parametrize("reporter", list(FAILING))
def test_skips_are_counted_separately_from_failures(reporter: Reporter) -> None:
    """A skipped test is neither a pass nor a failure, and a contract that
    treated it as either would either block on skips or ignore them."""
    evidence = reporters.parse(reporter, FAILING[reporter])

    assert evidence.skipped == 1
    assert evidence.passed == 1


def test_a_bare_testsuite_root_is_accepted() -> None:
    """Both roots are in the wild and neither is wrong."""
    evidence = reporters.parse(Reporter.JUNIT_XML, reports.JUNIT_BARE_SUITE)

    assert evidence.total == 2
    assert evidence.failed == 1


def test_a_junit_error_counts_as_a_failure() -> None:
    """A test that blew up is as un-passed as one that asserted wrong.

    The counting is deliberate rather than read from the ``failures``
    attribute, which excludes ``errors`` — a suite whose tests all threw would
    otherwise report zero failures.
    """
    evidence = reporters.parse(Reporter.JUNIT_XML, reports.JUNIT_BARE_SUITE)

    assert evidence.failures[0].message.startswith("TypeError")


def test_an_entity_expansion_bomb_is_refused_not_parsed() -> None:
    """Ten lines of XML that become gigabytes of string.

    Refused on the doctype rather than bounded during the parse, because there
    is no parser setting to bound it with — and a test report has no legitimate
    use for a DTD.
    """
    with pytest.raises(ReportError, match="doctype"):
        reporters.parse(Reporter.JUNIT_XML, reports.JUNIT_BILLION_LAUGHS)


def test_malformed_input_is_an_error_not_empty_evidence() -> None:
    """ "The tests passed" and "we could not tell" must not be the same value."""
    with pytest.raises(ReportError, match="well-formed"):
        reporters.parse(Reporter.JUNIT_XML, b"<testsuites><oops>")
    with pytest.raises(ReportError, match="JSON"):
        reporters.parse(Reporter.JEST_JSON, b"{not json")


def test_the_wrong_root_element_is_refused() -> None:
    """A repository that declares JUnit and emits something else has a
    configuration error, and a parser that shrugged would hide it."""
    with pytest.raises(ReportError, match="JUnit"):
        reporters.parse(Reporter.JUNIT_XML, b"<html><body>report</body></html>")


def test_the_none_reporter_has_nothing_to_parse() -> None:
    with pytest.raises(ReportError, match="no reporter"):
        reporters.parse(Reporter.NONE, b"")


def test_an_oversized_report_is_refused_before_it_is_parsed() -> None:
    """A gigabyte of JSON is a denial of service against the control plane."""
    with pytest.raises(ReportError, match="limit"):
        reporters.parse(Reporter.JEST_JSON, b"[" + b" " * (33 * 1024 * 1024))


def test_two_hundred_failures_become_twenty() -> None:
    """One cause produces two hundred failures and the first few show it.

    The count still reports the truth — nothing is hidden, only unquoted — so a
    person reading the record sees 200 and a model reading the prompt sees the
    ones it can act on.
    """
    cases = "".join(
        f'<testcase classname="C" name="t{index}"><failure message="boom {index}"/></testcase>'
        for index in range(200)
    )
    evidence = reporters.parse(
        Reporter.JUNIT_XML,
        f'<testsuite name="big" tests="200">{cases}</testsuite>'.encode(),
    )

    assert evidence.failed == 200
    assert len(evidence.failures) == MAX_FAILURES


def test_a_long_assertion_is_trimmed_with_a_marker() -> None:
    body = json.dumps(
        {
            "summary": {"total": 1, "failed": 1},
            "tests": [
                {
                    "nodeid": "t.py::test_x",
                    "outcome": "failed",
                    "call": {"longrepr": "E   " + "x" * 5000},
                }
            ],
        }
    ).encode()
    failure = reporters.parse(Reporter.PYTEST_JSON_REPORT, body).failures[0]

    assert failure.message.endswith("…")
    assert len(failure.message) <= 2001


def test_unreadable_counts_do_not_cost_the_assertion() -> None:
    """A malformed ``tests="many"`` loses a number in a summary, not the failure.

    The counts and the failures are extracted independently on purpose: the
    summary is the cheap half and the assertion is the half a retry needs.
    """
    evidence = reporters.parse(
        Reporter.JUNIT_XML,
        b'<testsuite name="s" tests="many" time="soon">'
        b'<testcase classname="C" name="t"><failure message="it broke"/></testcase>'
        b"</testsuite>",
    )

    assert evidence.total == 1  # counted, since the attribute was unusable
    assert evidence.failures[0].message == "it broke"
    assert evidence.duration_seconds is None


class TestCollect:
    """Finding the report on disk — where the untrusted-path rules apply."""

    def test_nothing_at_any_known_path_is_an_absence(self, tmp_path: Path) -> None:
        assert reporters.collect(tmp_path, Reporter.JUNIT_XML) is None

    def test_a_report_at_a_conventional_path_is_found(self, tmp_path: Path) -> None:
        (tmp_path / "junit.xml").write_bytes(reports.JUNIT_FAILING)

        evidence = reporters.collect(tmp_path, Reporter.JUNIT_XML)

        assert evidence is not None
        assert evidence.failed == 1

    def test_a_directory_of_per_class_files_is_merged(self, tmp_path: Path) -> None:
        """Every JVM runner writes one XML per class.

        Merged rather than picking one, because "the tests passed" is a claim
        about the suite and one file is a claim about one class — and the class
        that passed is the one that sorts first as often as not.
        """
        directory = tmp_path / "target" / "surefire-reports"
        directory.mkdir(parents=True)
        (directory / "TEST-a.xml").write_bytes(reports.JUNIT_PASSING)
        (directory / "TEST-b.xml").write_bytes(reports.JUNIT_FAILING)

        evidence = reporters.collect(tmp_path, Reporter.JUNIT_XML)

        assert evidence is not None
        assert evidence.total == 6
        assert evidence.failed == 1

    def test_a_symlink_is_refused_rather_than_followed(self, tmp_path: Path) -> None:
        """The control plane does not follow a redirect out of a directory an
        agent could write to."""
        secret = tmp_path / "secret"
        secret.write_bytes(b"<testsuite name='x'/>")
        (tmp_path / "junit.xml").symlink_to(secret)

        with pytest.raises(ReportError, match="symlink"):
            reporters.collect(tmp_path, Reporter.JUNIT_XML)

    def test_the_none_reporter_collects_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "junit.xml").write_bytes(reports.JUNIT_PASSING)

        assert reporters.collect(tmp_path, Reporter.NONE) is None


class TestShapesTheseToolsActuallyEmit:
    """The variants that are not the happy path but are not malformed either."""

    def test_a_junit_failure_with_no_message_attribute(self) -> None:
        """Some writers put everything in the body, so the first line stands in."""
        evidence = reporters.parse(
            Reporter.JUNIT_XML,
            b'<testsuite name="s" tests="1"><testcase classname="C" name="t">'
            b"<failure>ValueError: bad rate\n  at billing.py:12</failure>"
            b"</testcase></testsuite>",
        )

        assert evidence.failures[0].message == "ValueError: bad rate"

    def test_a_pytest_crash_entry_with_no_assertion_lines(self) -> None:
        """A collection error has no ``E`` lines, so the leading text stands in
        rather than the failure arriving with an empty message."""
        body = json.dumps(
            {
                "summary": {"total": 1, "failed": 1},
                "tests": [
                    {
                        "nodeid": "tests/test_x.py::test_a",
                        "outcome": "error",
                        "setup": {"crash": "ImportError: no module named billing"},
                    }
                ],
            }
        ).encode()

        failure = reporters.parse(Reporter.PYTEST_JSON_REPORT, body).failures[0]

        assert failure.message == "ImportError: no module named billing"
        assert failure.file == "tests/test_x.py"  # from the nodeid, not the text

    def test_pytest_failures_are_capped_too(self) -> None:
        body = json.dumps(
            {
                "summary": {"total": 50, "failed": 50},
                "tests": [
                    {
                        "nodeid": f"t.py::test_{index}",
                        "outcome": "failed",
                        "call": {"longrepr": f"E   boom {index}"},
                    }
                    for index in range(50)
                ],
            }
        ).encode()

        evidence = reporters.parse(Reporter.PYTEST_JSON_REPORT, body)

        assert evidence.failed == 50
        assert len(evidence.failures) == MAX_FAILURES

    def test_jest_failures_are_capped_too(self) -> None:
        body = json.dumps(
            {
                "numTotalTests": 50,
                "numFailedTests": 50,
                "testResults": [
                    {
                        "name": "/repo/a.test.ts",
                        "assertionResults": [
                            {
                                "fullName": f"case {index}",
                                "status": "failed",
                                "failureMessages": ["boom"],
                            }
                            for index in range(50)
                        ],
                    }
                ],
            }
        ).encode()

        assert len(reporters.parse(Reporter.JEST_JSON, body).failures) == MAX_FAILURES

    def test_go_package_level_events_are_not_attributed_to_a_test(self) -> None:
        """A build failure has no ``Test`` field, and counting it as one would
        invent a test nobody wrote."""
        evidence = reporters.parse(
            Reporter.GO_TEST_JSON,
            b'{"Action":"output","Output":"# billing [build failed]\\n"}\n'
            b'{"Action":"run","Test":"TestA"}\n'
            b'{"Action":"pass","Test":"TestA","Elapsed":0.1}\n'
            b'{"Action":"fail","Elapsed":0.1}\n',
        )

        assert evidence.total == 1
        assert evidence.passed == 1

    def test_jest_entries_that_are_not_objects_are_skipped(self) -> None:
        body = json.dumps(
            {"numTotalTests": 1, "testResults": ["not an object", {"assertionResults": [7]}]}
        ).encode()

        assert reporters.parse(Reporter.JEST_JSON, body).failures == ()

    def test_a_cargo_run_with_no_suite_event_still_counts_its_failures(self) -> None:
        """A run killed partway writes test events and never the summary."""
        evidence = reporters.parse(
            Reporter.CARGO_JSON,
            b'{"type":"test","event":"failed","name":"billing::x","stdout":"panicked"}\n',
        )

        assert evidence.failed == 1

    @pytest.mark.parametrize(
        ("reporter", "document"),
        [
            (Reporter.PYTEST_JSON_REPORT, b"[]"),
            (Reporter.JEST_JSON, b"[]"),
            (Reporter.GO_TEST_JSON, b"[1, 2]"),
            (Reporter.CARGO_JSON, b'"a string"'),
        ],
    )
    def test_valid_json_of_the_wrong_shape_is_refused(
        self, reporter: Reporter, document: bytes
    ) -> None:
        with pytest.raises(ReportError, match="not a JSON object"):
            reporters.parse(reporter, document)


class TestReadingFromDisk:
    """The size and type checks that run before anything is parsed."""

    def test_a_file_over_the_cap_is_refused_before_it_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reporters, "MAX_REPORT_BYTES", 32)
        (tmp_path / "junit.xml").write_bytes(reports.JUNIT_PASSING)

        with pytest.raises(ReportError, match="limit"):
            reporters.collect(tmp_path, Reporter.JUNIT_XML)

    def test_a_directory_with_no_reports_in_it_is_an_absence(self, tmp_path: Path) -> None:
        (tmp_path / "target" / "surefire-reports").mkdir(parents=True)

        assert reporters.collect(tmp_path, Reporter.JUNIT_XML) is None

    def test_a_symlinked_file_inside_a_report_directory_is_skipped(self, tmp_path: Path) -> None:
        """The check runs at every level that is ours to check, not only the top."""
        directory = tmp_path / "target" / "surefire-reports"
        directory.mkdir(parents=True)
        (directory / "TEST-real.xml").write_bytes(reports.JUNIT_PASSING)
        (directory / "TEST-link.xml").symlink_to(tmp_path / "elsewhere.xml")

        evidence = reporters.collect(tmp_path, Reporter.JUNIT_XML)

        assert evidence is not None
        assert evidence.total == 3  # only the real file


def test_merging_re_caps_the_failures() -> None:
    """Two hundred files contributing one failure each must not smuggle past
    the cap by arriving separately."""
    parts = [
        reporters.parse(
            Reporter.JUNIT_XML,
            f'<testsuite name="s{index}" tests="1">'
            f'<testcase classname="C" name="t{index}"><failure message="boom"/></testcase>'
            f"</testsuite>".encode(),
        )
        for index in range(200)
    ]

    merged = reporters.merge(parts)

    assert merged.failed == 200
    assert len(merged.failures) == MAX_FAILURES


def test_merging_nothing_is_an_error() -> None:
    with pytest.raises(ReportError, match="no report files"):
        reporters.merge([])
