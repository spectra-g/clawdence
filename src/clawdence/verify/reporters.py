"""Reading a test run's machine-readable output, and throwing most of it away.

**Throwing most of it away is the feature.** A failing suite emits thousands of
lines of stack traces, captured stdout and HTML dumps. Feeding that back to the
agent exhausts the step's context budget before it has read the plan; truncating
it head-or-tail drops the assertion — which is at the top of a pytest failure and
the bottom of a JUnit one, so neither end is safe — and produces the retry loop
that burns to the cost cap without ever showing the model the error. v1 had the
beginnings of this in ``classify_test_output`` / ``validate_tdd.py``, per format,
in the orchestrator. Here it is one function per format behind ``RepoProfile``,
which declares which one the repository emits.

What survives the parse is what a person debugging would read first: the test's
name, the file and line, the assertion message, and the immediate frames. Deep
frames are noise to a model deciding what to change — a forty-frame pytest
traceback through the plugin machinery says nothing the top three do not.

**This is untrusted input.** The bytes were written by a test run of repository
code, in a workspace an agent could write to. Four controls, in the order they
have to run:

1. **A path is checked before it is opened**, never followed. A symlink at the
   report path pointing at ``/etc/passwd`` would otherwise have the control plane
   read it and put it in a prompt.
2. **It is size-capped before it is parsed**, because a gigabyte of XML is a
   denial of service against the process doing the parsing.
3. **XML is refused if it declares a doctype.** Python's ``xml.etree`` does not
   fetch external entities, but it is expat underneath and expat expands internal
   ones — the billion-laughs attack is ten lines of XML that becomes three
   gigabytes of string. A JUnit report has no legitimate use for a DTD, so the
   cheap and total defence is to refuse one rather than to bound the expansion.
   This is also why there is no ``defusedxml`` dependency: the only thing we
   would use it for is a refusal we can state in four lines.
4. **Everything extracted is bounded** — the number of failures, the length of a
   message, the number of frames. The repository does not get to decide how much
   of the next prompt it occupies.

A parse failure raises ``ReportError`` rather than returning empty evidence. The
difference matters to the contract above: "the tests passed" and "we could not
tell whether the tests passed" are different answers, and only the second is a
``VERIFICATION_ERROR`` halt rather than a failing attempt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final
from xml.etree import ElementTree

from clawdence.domain import FailingAssertion, TestEvidence, TestReporter

#: Refused past this. Larger than any honest report and small enough that
#: parsing one cannot be used to stall the control plane.
MAX_REPORT_BYTES: Final = 32 * 1024 * 1024

#: How many failing assertions reach the model. A suite where two hundred tests
#: broke has one cause, and the first few show it; the counts on ``TestEvidence``
#: still report the true total, so nothing is being hidden — only unquoted.
MAX_FAILURES: Final = 20

#: Per failure. Long enough for a multi-line assertion diff, short enough that
#: twenty of them do not fill a context window.
MAX_MESSAGE_CHARS: Final = 2000

#: Immediate frames only. Three is where a stack stops being about the test and
#: starts being about the framework that ran it.
MAX_FRAMES: Final = 3

#: One frame, after trimming. A path plus a line number plus a call.
MAX_FRAME_CHARS: Final = 300

#: Where each reporter conventionally writes, relative to the worktree root.
#: Ordered: the first match wins, so a repository that has both a modern and a
#: legacy location gets the modern one.
REPORT_PATHS: Final[Mapping[TestReporter, tuple[str, ...]]] = {
    TestReporter.JUNIT_XML: (
        "target/surefire-reports",
        "build/test-results/test",
        "test-results/junit.xml",
        "junit.xml",
    ),
    TestReporter.JEST_JSON: ("jest-results.json", "test-results/jest.json"),
    TestReporter.PYTEST_JSON_REPORT: (".report.json", "test-results/pytest.json"),
    TestReporter.GO_TEST_JSON: ("test-results/go-test.json", "go-test.json"),
    TestReporter.CARGO_JSON: ("test-results/cargo-test.json", "cargo-test.json"),
    TestReporter.NONE: (),
}

#: Frames in a traceback, as every language's stack renderer writes them: a
#: leading ``at``/``File``/tab, or a path-and-line. Used to *find* the frames in
#: a message so they can be split off, not to validate them.
_FRAME = re.compile(
    r"^\s*(?:at\s+\S|File\s+\"|[\w./\\-]+[.:]\d+[:)]|[\w./\\-]+\.(?:py|js|ts|java|go|rs):\d+)"
)

#: Frames belonging to the machinery that *ran* the test rather than to the code
#: under test. Dropped before the ``MAX_FRAMES`` cut, not after — which is the
#: whole difference between "the first three frames" and "the first three useful
#: frames". A jest stack is one line of the user's test followed by twenty of
#: jest-circus, and a JUnit stack reaches reflection internals by frame three, so
#: a naive head-slice spends two thirds of a small budget on lines that name no
#: code anybody can change.
_VENDOR_FRAME = re.compile(
    r"node_modules/|site-packages/|/\.venv/|dist-packages/|"
    r"java\.base/|jdk\.internal\.|sun\.reflect\.|org\.junit\.|org\.gradle\.|"
    r"_pytest/|/pluggy/|/runpy\.py|testing/[\w.]+/testing\.go|/libtest/"
)


class ReportError(ValueError):
    """The report exists and could not be read.

    Distinct from absence, which is ``None`` from ``collect``. "No report" may
    be a repository that runs no tests; "a report we cannot parse" is a
    divergence between what the profile declares and what the repository emits,
    and it halts rather than counting as a failing test.
    """


def parse(reporter: TestReporter, data: bytes) -> TestEvidence:
    """Turn one reporter's bytes into evidence.

    Dispatch on the format the profile declares rather than sniffing the
    content: a repository that says it emits JUnit and emits something else has
    a configuration error worth surfacing, and a parser that guesses would
    silently paper over it.
    """
    if reporter is TestReporter.NONE:
        raise ReportError("no reporter is configured for this repository")
    if len(data) > MAX_REPORT_BYTES:
        raise ReportError(f"report is {len(data)} bytes, over the {MAX_REPORT_BYTES}-byte limit")
    return _PARSERS[reporter](data)


def collect(worktree: Path, reporter: TestReporter) -> TestEvidence | None:
    """Find and parse the report a run left in the worktree.

    ``None`` when there is nothing at any conventional path — an absence, which
    the contract above interprets. A path that exists and cannot be read is a
    ``ReportError``, because that is a different thing to know.
    """
    if reporter is TestReporter.NONE:
        return None
    for candidate in REPORT_PATHS[reporter]:
        found = _resolve(worktree / candidate)
        if found is None:
            continue
        if len(found) == 1:
            return parse(reporter, _read(found[0]))
        # A directory of per-class XML files, which is what every JVM test
        # runner writes. Merged rather than picking one, because "the tests
        # passed" is a claim about the suite and one file is a claim about one
        # class.
        return merge(parse(reporter, _read(path)) for path in found)
    return None


def merge(parts: Iterator[TestEvidence] | Sequence[TestEvidence]) -> TestEvidence:
    """Combine per-file evidence into one claim about the suite.

    Counts add; failures concatenate and are capped again after the join, so a
    directory of two hundred files cannot smuggle past ``MAX_FAILURES`` by
    contributing one failure each.
    """
    collected = list(parts)
    if not collected:
        raise ReportError("no report files to merge")
    failures: list[FailingAssertion] = []
    for part in collected:
        failures.extend(part.failures)
    durations = [part.duration_seconds for part in collected if part.duration_seconds is not None]
    return TestEvidence(
        reporter=collected[0].reporter,
        total=sum(part.total for part in collected),
        passed=sum(part.passed for part in collected),
        failed=sum(part.failed for part in collected),
        skipped=sum(part.skipped for part in collected),
        duration_seconds=sum(durations) if durations else None,
        failures=tuple(failures[:MAX_FAILURES]),
    )


def _resolve(candidate: Path) -> tuple[Path, ...] | None:
    """The report file(s) at a path, or ``None``.

    Symlinks are refused rather than followed, at every level that is ours to
    check: the candidate itself, and each file inside a candidate directory.
    ``is_symlink`` before ``exists``, because ``exists`` follows the link and
    would answer a question about the target.
    """
    if candidate.is_symlink():
        raise ReportError(f"{candidate.name} is a symlink, which the reader will not follow")
    if not candidate.exists():
        return None
    if candidate.is_dir():
        files = sorted(
            path
            for path in candidate.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix == ".xml"
        )
        return tuple(files) or None
    if not candidate.is_file():
        raise ReportError(f"{candidate.name} is not a regular file")
    return (candidate,)


def _read(path: Path) -> bytes:
    size = path.stat().st_size
    if size > MAX_REPORT_BYTES:
        raise ReportError(f"{path.name} is {size} bytes, over the {MAX_REPORT_BYTES}-byte limit")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ReportError(f"{path.name} could not be read: {exc.strerror}") from None


def _xml_root(data: bytes) -> ElementTree.Element:
    """Parse XML that has been checked for a doctype first.

    The check is a byte scan rather than a parser setting because there is no
    parser setting: ``xml.etree`` gives no way to refuse entity expansion, and
    the expansion happens during the parse we would be trying to guard. Scanning
    the prologue is crude and total — a document with no ``<!DOCTYPE`` cannot
    declare an entity, so it cannot expand one.
    """
    prologue = data[:4096].lstrip()
    if b"<!DOCTYPE" in prologue or b"<!ENTITY" in data[:4096]:
        raise ReportError(
            "report declares a doctype, which a test report has no use for and an "
            "entity-expansion attack does"
        )
    try:
        return ElementTree.fromstring(data)  # noqa: S314 - doctype refused above
    except ElementTree.ParseError as exc:
        raise ReportError(f"report is not well-formed XML: {exc}") from None


def _load_json(data: bytes) -> Any:
    try:
        return json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReportError(f"report is not valid JSON: {exc}") from None


def _junit(data: bytes) -> TestEvidence:
    """Surefire, Gradle, pytest ``--junitxml``, and everything that imitates them.

    The root is either ``<testsuites>`` or a bare ``<testsuite>``; both are in
    the wild and neither is wrong, so both are accepted. Counts come from the
    attributes when they are there and from counting elements when they are not
    — Gradle writes them, some ad hoc writers do not, and a total of zero beside
    forty ``<testcase>`` elements is worse than a slower count.
    """
    root = _xml_root(data)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if root.tag not in ("testsuite", "testsuites"):
        raise ReportError(f"expected a JUnit <testsuites> or <testsuite> root, found <{root.tag}>")

    cases = [case for suite in suites for case in suite.iter("testcase")]
    failures: list[FailingAssertion] = []
    failed = skipped = 0
    for case in cases:
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            failed += 1
            if len(failures) < MAX_FAILURES:
                failures.append(_junit_failure(case, problem))
            continue
        if case.find("skipped") is not None:
            skipped += 1

    total = _sum_attr(suites, "tests") or len(cases)
    # `failed` is counted rather than read: a suite element's `failures`
    # attribute excludes `errors`, and a test that blew up is as un-passed as
    # one that asserted wrong.
    return TestEvidence(
        reporter=TestReporter.JUNIT_XML,
        total=total,
        passed=max(total - failed - skipped, 0),
        failed=failed,
        skipped=skipped,
        duration_seconds=_sum_float_attr(suites, "time"),
        failures=tuple(failures),
    )


def _junit_failure(case: ElementTree.Element, problem: ElementTree.Element) -> FailingAssertion:
    """One ``<testcase>`` with a ``<failure>`` or ``<error>`` in it.

    JUnit puts the assertion in the element's ``message`` attribute and the
    stack in its text, which is the one format where the two arrive already
    separated — so the message is taken whole and the text is mined only for
    frames. Where ``message`` is absent (some writers put everything in the
    body) the body's first line stands in for it.
    """
    classname = case.get("classname", "")
    name = case.get("name", "?")
    test_id = f"{classname}.{name}" if classname else name

    body = (problem.text or "").strip()
    message = problem.get("message")
    if message is None:
        message, _, _ = body.partition("\n")
    frames = _frames(body)

    file, line = _location(body)
    return FailingAssertion(
        test_id=test_id[:MAX_MESSAGE_CHARS],
        file=file,
        line=line,
        message=_trim(message or f"{problem.tag} with no message"),
        frames=frames,
    )


def _pytest_json(data: bytes) -> TestEvidence:
    """``pytest --json-report`` (the ``pytest-json-report`` plugin).

    Its summary block is authoritative and its ``tests`` array carries the
    ``longrepr`` — the rendered traceback, which is exactly the thing that must
    not be forwarded whole. The assertion is pytest's ``E   `` lines, which is
    what ``_assertion_lines`` looks for before falling back to the first line.
    """
    document = _load_json(data)
    if not isinstance(document, dict):
        raise ReportError("pytest report is not a JSON object")

    summary = document.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    tests = document.get("tests")
    tests = tests if isinstance(tests, list) else []

    failures: list[FailingAssertion] = []
    for test in tests:
        if not isinstance(test, dict) or test.get("outcome") not in ("failed", "error"):
            continue
        if len(failures) >= MAX_FAILURES:
            break
        failures.append(_pytest_failure(test))

    failed = _as_int(summary.get("failed")) + _as_int(summary.get("error"))
    return TestEvidence(
        reporter=TestReporter.PYTEST_JSON_REPORT,
        total=_as_int(summary.get("total")) or len(tests),
        passed=_as_int(summary.get("passed")),
        failed=failed or len(failures),
        skipped=_as_int(summary.get("skipped")) + _as_int(summary.get("xfailed")),
        duration_seconds=_as_float(document.get("duration")),
        failures=tuple(failures),
    )


def _pytest_failure(test: Mapping[str, Any]) -> FailingAssertion:
    node = str(test.get("nodeid", "?"))
    phase = test.get("call") if isinstance(test.get("call"), dict) else test.get("setup")
    longrepr = ""
    if isinstance(phase, dict):
        longrepr = str(phase.get("longrepr") or phase.get("crash") or "")
    longrepr = longrepr or str(test.get("longrepr") or "")

    file, line = _location(longrepr)
    # The nodeid is `path::test_name`, which is a better file than anything mined
    # out of a traceback when the traceback did not name one.
    if file is None and "::" in node:
        file = node.split("::", 1)[0]
    return FailingAssertion(
        test_id=node[:MAX_MESSAGE_CHARS],
        file=file,
        line=line,
        message=_trim(_assertion_lines(longrepr) or "test failed with no reported assertion"),
        frames=_frames(longrepr),
    )


def _jest_json(data: bytes) -> TestEvidence:
    """``jest --json``.

    Jest reports per file and then per assertion, and puts the whole rendered
    failure — assertion, diff, and stack — in ``failureMessages``. The
    interesting half is the top: Jest writes the expected/received diff first
    and the frames after, so splitting on the first frame keeps the diff and
    drops the stack.
    """
    document = _load_json(data)
    if not isinstance(document, dict):
        raise ReportError("jest report is not a JSON object")

    results = document.get("testResults")
    results = results if isinstance(results, list) else []

    failures: list[FailingAssertion] = []
    for file_result in results:
        if not isinstance(file_result, dict):
            continue
        source = file_result.get("name")
        assertions = file_result.get("assertionResults")
        for assertion in assertions if isinstance(assertions, list) else []:
            if not isinstance(assertion, dict) or assertion.get("status") != "failed":
                continue
            if len(failures) >= MAX_FAILURES:
                break
            failures.append(_jest_failure(assertion, str(source) if source else None))

    return TestEvidence(
        reporter=TestReporter.JEST_JSON,
        total=_as_int(document.get("numTotalTests")),
        passed=_as_int(document.get("numPassedTests")),
        failed=_as_int(document.get("numFailedTests")),
        skipped=_as_int(document.get("numPendingTests")) + _as_int(document.get("numTodoTests")),
        duration_seconds=None,
        failures=tuple(failures),
    )


def _jest_failure(assertion: Mapping[str, Any], source: str | None) -> FailingAssertion:
    title = assertion.get("fullName") or assertion.get("title") or "?"
    messages = assertion.get("failureMessages")
    body = "\n".join(str(item) for item in messages) if isinstance(messages, list) else ""

    file, line = _location(body)
    return FailingAssertion(
        test_id=str(title)[:MAX_MESSAGE_CHARS],
        file=file or source,
        line=line,
        message=_trim(_before_frames(body) or "test failed with no reported assertion"),
        frames=_frames(body),
    )


def _go_test_json(data: bytes) -> TestEvidence:
    """``go test -json`` — newline-delimited events, not a document.

    Output arrives as ``output`` actions interleaved across tests and is only
    attributable once a ``fail``/``pass`` action names the test, so the lines are
    accumulated per test and read at the end. Subtests (``TestX/case``) are
    counted as tests in their own right, which is what ``go test`` itself
    reports.
    """
    lines = data.splitlines()
    outputs: dict[str, list[str]] = {}
    passed = failed = skipped = 0
    failures: list[FailingAssertion] = []
    elapsed = 0.0

    for raw in lines:
        if not raw.strip():
            continue
        event = _load_json(raw)
        if not isinstance(event, dict):
            raise ReportError("go test event is not a JSON object")
        test = event.get("Test")
        if not isinstance(test, str):
            # A package-level event. Its elapsed time is the run's, and its
            # output is build noise nothing here attributes to a test.
            continue
        action = event.get("Action")
        if action == "output":
            outputs.setdefault(test, []).append(str(event.get("Output", "")))
        elif action == "pass":
            passed += 1
            elapsed += _as_float(event.get("Elapsed")) or 0.0
        elif action == "skip":
            skipped += 1
        elif action == "fail":
            failed += 1
            elapsed += _as_float(event.get("Elapsed")) or 0.0
            if len(failures) < MAX_FAILURES:
                failures.append(_go_failure(test, "".join(outputs.get(test, ()))))

    return TestEvidence(
        reporter=TestReporter.GO_TEST_JSON,
        total=passed + failed + skipped,
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=elapsed or None,
        failures=tuple(failures),
    )


def _go_failure(test: str, body: str) -> FailingAssertion:
    # `go test` output is indented under the test name and the first non-header
    # line is `file.go:12: message`, which carries the location and the
    # assertion together.
    file, line = _location(body)
    interesting = [
        stripped
        for stripped in (item.strip() for item in body.splitlines())
        if stripped and not stripped.startswith(("=== RUN", "=== PAUSE", "=== CONT", "--- FAIL"))
    ]
    return FailingAssertion(
        test_id=test[:MAX_MESSAGE_CHARS],
        file=file,
        line=line,
        message=_trim("\n".join(interesting) or "test failed with no output"),
        frames=(),
    )


def _cargo_json(data: bytes) -> TestEvidence:
    """``cargo test -- -Z unstable-options --format json`` — also line-delimited.

    ``libtest``'s events are flatter than Go's: one ``test`` event per outcome,
    carrying its own ``stdout``. The suite event at the end has the totals, and
    is preferred over counting because a filtered run reports what it filtered.
    """
    passed = failed = ignored = total = 0
    duration: float | None = None
    failures: list[FailingAssertion] = []

    for raw in data.splitlines():
        if not raw.strip():
            continue
        event = _load_json(raw)
        if not isinstance(event, dict):
            raise ReportError("cargo test event is not a JSON object")
        kind = event.get("type")
        if kind == "suite" and event.get("event") in ("ok", "failed"):
            passed = _as_int(event.get("passed"))
            failed = _as_int(event.get("failed"))
            ignored = _as_int(event.get("ignored"))
            total = passed + failed + ignored
            duration = _as_float(event.get("exec_time"))
        elif kind == "test" and event.get("event") == "failed":
            if len(failures) < MAX_FAILURES:
                failures.append(_cargo_failure(event))

    return TestEvidence(
        reporter=TestReporter.CARGO_JSON,
        total=total or (passed + failed + ignored),
        passed=passed,
        failed=failed or len(failures),
        skipped=ignored,
        duration_seconds=duration,
        failures=tuple(failures),
    )


def _cargo_failure(event: Mapping[str, Any]) -> FailingAssertion:
    body = str(event.get("stdout") or "")
    file, line = _location(body)
    return FailingAssertion(
        test_id=str(event.get("name", "?"))[:MAX_MESSAGE_CHARS],
        file=file,
        line=line,
        message=_trim(_before_frames(body) or "test panicked with no captured output"),
        frames=_frames(body),
    )


def _assertion_lines(body: str) -> str:
    """pytest's ``E   `` lines — the assertion, without the source context.

    A pytest ``longrepr`` is the failing function's source, then the ``E``-
    prefixed assertion, then a ``file:line: Error`` footer. The ``E`` lines are
    the whole of what a model needs; the source above them is code it can read
    from the repository, and the traceback below is the noise this module
    exists to drop. Falls back to the leading lines when the shape is not
    pytest's, which is what a ``crash`` entry looks like.
    """
    marked = [line[1:].strip() for line in body.splitlines() if line.startswith("E ")]
    if marked:
        return "\n".join(marked)
    return _before_frames(body)


def _before_frames(body: str) -> str:
    """Everything up to the first stack frame.

    Jest and Rust both render the interesting part first — the expected/received
    diff, the panic message — and the stack after it. Cutting at the first frame
    keeps all of the first and none of the second, which no character count can
    do reliably.
    """
    lines = body.splitlines()
    kept: list[str] = []
    for line in lines:
        if _FRAME.match(line) and kept:
            break
        kept.append(line)
    return "\n".join(item for item in kept if item.strip()).strip()


def _frames(body: str) -> tuple[str, ...]:
    """The first few frames that name code somebody could change.

    Vendor frames are dropped before the cut rather than after it — see
    ``_VENDOR_FRAME``. The fallback matters: when *every* frame is vendor, the
    failure is inside the framework and those lines are the only signal there
    is, so returning nothing would be dropping the whole trace to enforce a
    preference. Filtering is for choosing between frames, not for having none.
    """
    found = [line.strip()[:MAX_FRAME_CHARS] for line in body.splitlines() if _FRAME.match(line)]
    ours = [frame for frame in found if not _VENDOR_FRAME.search(frame)]
    return tuple((ours or found)[:MAX_FRAMES])


def _location(body: str) -> tuple[str | None, int | None]:
    """The first ``path:line`` in the text.

    Best effort by construction — the formats that carry a structured location
    are the minority, and a wrong file with a right message is still the useful
    half. Returns ``(None, None)`` rather than guessing when nothing matches.
    """
    match = re.search(r"([\w./\\-]+\.[A-Za-z]{1,5})[:(](\d+)", body)
    if match is None:
        return None, None
    try:
        line = int(match.group(2))
    except ValueError:  # pragma: no cover - the group is \d+
        return match.group(1), None
    return match.group(1), line if line >= 1 else None


def _trim(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= MAX_MESSAGE_CHARS:
        return stripped
    return stripped[:MAX_MESSAGE_CHARS] + "…"


def _sum_attr(elements: Sequence[ElementTree.Element], name: str) -> int:
    return sum(_as_int(element.get(name)) for element in elements)


def _sum_float_attr(elements: Sequence[ElementTree.Element], name: str) -> float | None:
    values = [_as_float(element.get(name)) for element in elements]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _as_int(value: Any) -> int:
    """Coerce, never raise. A count we cannot read is zero, not a parse failure.

    The counts are reported alongside the failures, which are extracted
    independently — so a malformed ``tests="many"`` costs a number in a summary
    and does not cost the assertion the model needs.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    try:
        return max(int(str(value)), 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


_PARSERS: Final[Mapping[TestReporter, Callable[[bytes], TestEvidence]]] = {
    TestReporter.JUNIT_XML: _junit,
    TestReporter.JEST_JSON: _jest_json,
    TestReporter.PYTEST_JSON_REPORT: _pytest_json,
    TestReporter.GO_TEST_JSON: _go_test_json,
    TestReporter.CARGO_JSON: _cargo_json,
}
