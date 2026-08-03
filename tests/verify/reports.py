"""Real-shaped reporter output, one sample per format.

Written out rather than generated, because the thing under test is whether the
parsers survive what these tools *actually* emit — the noise is the point. Each
failing sample carries a stack deep enough that forwarding it whole would be the
context-budget problem the parser exists to prevent, and an assertion buried in
the middle of it that head-or-tail truncation would drop.

Every sample names the same fictional failure — a proration calculation off by a
cent — so a test reading two formats side by side is comparing parsers rather
than fixtures.
"""

from __future__ import annotations

from typing import Final

JUNIT_PASSING: Final = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="BillingTest" tests="3" failures="0" errors="0" skipped="0" time="1.25">
    <testcase classname="com.example.BillingTest" name="prorates_a_partial_month" time="0.4"/>
    <testcase classname="com.example.BillingTest" name="rounds_half_up" time="0.6"/>
    <testcase classname="com.example.BillingTest" name="handles_a_leap_year" time="0.25"/>
  </testsuite>
</testsuites>
"""

JUNIT_FAILING: Final = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="BillingTest" tests="3" failures="1" errors="0" skipped="1" time="1.10">
    <testcase classname="com.example.BillingTest" name="rounds_half_up" time="0.6"/>
    <testcase classname="com.example.BillingTest" name="handles_a_leap_year" time="0.25">
      <skipped/>
    </testcase>
    <testcase classname="com.example.BillingTest" name="prorates_a_partial_month" time="0.25">
      <failure message="expected:&lt;12.50&gt; but was:&lt;12.49&gt;" type="AssertionError">
org.junit.ComparisonFailure: expected:&lt;12.50&gt; but was:&lt;12.49&gt;
	at com.example.Billing.daily(Billing.java:41)
	at com.example.BillingTest.prorates_a_partial_month(BillingTest.java:88)
	at java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke0(Native Method)
	at java.base/jdk.internal.reflect.NativeMethodAccessorImpl.invoke(Unknown Source)
	at org.junit.runners.model.FrameworkMethod.invokeExplosively(FrameworkMethod.java:59)
	at org.junit.runners.ParentRunner.runLeaf(ParentRunner.java:366)
	at org.junit.runners.ParentRunner.run(ParentRunner.java:413)
      </failure>
    </testcase>
  </testsuite>
</testsuites>
"""

#: A bare ``<testsuite>`` root with no counts on it. Both are in the wild.
JUNIT_BARE_SUITE: Final = b"""<?xml version="1.0"?>
<testsuite name="adhoc">
  <testcase classname="Adhoc" name="one"/>
  <testcase classname="Adhoc" name="two">
    <error message="TypeError: cannot add str to int">Traceback follows</error>
  </testcase>
</testsuite>
"""

#: Ten lines of XML that expand to gigabytes. Refused before the parse.
JUNIT_BILLION_LAUGHS: Final = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<testsuite name="&lol3;"><testcase name="x"/></testsuite>
"""

PYTEST_PASSING: Final = b"""{
  "duration": 1.9,
  "summary": {"total": 3, "passed": 3},
  "tests": [
    {"nodeid": "tests/test_billing.py::test_prorates_a_partial_month", "outcome": "passed"},
    {"nodeid": "tests/test_billing.py::test_rounds_half_up", "outcome": "passed"},
    {"nodeid": "tests/test_billing.py::test_handles_a_leap_year", "outcome": "passed"}
  ]
}
"""

PYTEST_FAILING: Final = rb"""{
  "duration": 2.1,
  "summary": {"total": 3, "passed": 1, "failed": 1, "skipped": 1},
  "tests": [
    {"nodeid": "tests/test_billing.py::test_rounds_half_up", "outcome": "passed"},
    {"nodeid": "tests/test_billing.py::test_handles_a_leap_year", "outcome": "skipped"},
    {
      "nodeid": "tests/test_billing.py::test_prorates_a_partial_month",
      "outcome": "failed",
      "call": {
        "longrepr": "    def test_prorates_a_partial_month():\n        billing = Billing(rate=Decimal('375'))\n>       assert billing.prorate(days=1) == Decimal('12.50')\nE       AssertionError: assert Decimal('12.49') == Decimal('12.50')\nE        +  where Decimal('12.49') = <bound method Billing.prorate>(days=1)\n\nbilling/proration.py:41: AssertionError\n../../.venv/lib/python3.12/site-packages/_pytest/python.py:195: in pytest_pyfunc_call\n    result = testfunction(**testargs)\n"
      }
    }
  ]
}
"""

JEST_PASSING: Final = b"""{
  "numTotalTests": 3, "numPassedTests": 3, "numFailedTests": 0, "numPendingTests": 0,
  "testResults": [
    {"name": "/repo/src/billing.test.ts", "assertionResults": [
      {"fullName": "billing prorates a partial month", "status": "passed"},
      {"fullName": "billing rounds half up", "status": "passed"},
      {"fullName": "billing handles a leap year", "status": "passed"}
    ]}
  ]
}
"""

JEST_FAILING: Final = rb"""{
  "numTotalTests": 3, "numPassedTests": 1, "numFailedTests": 1, "numPendingTests": 1,
  "testResults": [
    {"name": "/repo/src/billing.test.ts", "assertionResults": [
      {"fullName": "billing rounds half up", "status": "passed"},
      {"fullName": "billing handles a leap year", "status": "pending"},
      {
        "fullName": "billing prorates a partial month",
        "status": "failed",
        "failureMessages": [
          "expect(received).toBe(expected) // Object.is equality\n\nExpected: 12.50\nReceived: 12.49\n    at Object.<anonymous> (src/billing.test.ts:88:31)\n    at Promise.then.completed (node_modules/jest-circus/build/utils.js:298:28)\n    at new Promise (<anonymous>)\n    at callAsyncCircusFn (node_modules/jest-circus/build/utils.js:231:10)"
        ]
      }
    ]}
  ]
}
"""

GO_PASSING: Final = b"""{"Action":"run","Test":"TestProratesAPartialMonth"}
{"Action":"output","Test":"TestProratesAPartialMonth","Output":"=== RUN   TestProratesAPartialMonth\\n"}
{"Action":"pass","Test":"TestProratesAPartialMonth","Elapsed":0.4}
{"Action":"run","Test":"TestRoundsHalfUp"}
{"Action":"pass","Test":"TestRoundsHalfUp","Elapsed":0.2}
{"Action":"pass","Elapsed":0.7}
"""

GO_FAILING: Final = b"""{"Action":"run","Test":"TestProratesAPartialMonth"}
{"Action":"output","Test":"TestProratesAPartialMonth","Output":"=== RUN   TestProratesAPartialMonth\\n"}
{"Action":"output","Test":"TestProratesAPartialMonth","Output":"    billing_test.go:88: prorate(1) = 12.49, want 12.50\\n"}
{"Action":"output","Test":"TestProratesAPartialMonth","Output":"--- FAIL: TestProratesAPartialMonth (0.40s)\\n"}
{"Action":"fail","Test":"TestProratesAPartialMonth","Elapsed":0.4}
{"Action":"run","Test":"TestRoundsHalfUp"}
{"Action":"pass","Test":"TestRoundsHalfUp","Elapsed":0.2}
{"Action":"run","Test":"TestHandlesALeapYear"}
{"Action":"skip","Test":"TestHandlesALeapYear"}
{"Action":"fail","Elapsed":0.7}
"""

CARGO_PASSING: Final = b"""{"type":"suite","event":"started","test_count":2}
{"type":"test","event":"started","name":"billing::prorates_a_partial_month"}
{"type":"test","event":"ok","name":"billing::prorates_a_partial_month"}
{"type":"test","event":"ok","name":"billing::rounds_half_up"}
{"type":"suite","event":"ok","passed":2,"failed":0,"ignored":0,"exec_time":0.61}
"""

CARGO_FAILING: Final = rb"""{"type":"suite","event":"started","test_count":3}
{"type":"test","event":"ok","name":"billing::rounds_half_up"}
{"type":"test","event":"failed","name":"billing::prorates_a_partial_month","stdout":"assertion `left == right` failed\n  left: 12.49\n right: 12.50\nnote: run with `RUST_BACKTRACE=1` to display a backtrace\nsrc/billing.rs:41:9\n"}
{"type":"suite","event":"failed","passed":1,"failed":1,"ignored":1,"exec_time":0.58}
"""
