"""Step handlers, and mostly the one that is real.

``TestEnvironmentIsolation`` is the security test in this file. The control
plane holds every provider key in the system, and a script step is the first
place in the codebase where repo-shaped work gets a subprocess. If that
subprocess inherits ``os.environ``, every key is one ``env`` away from any
workflow anyone can write.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from clawdence.domain import ScriptStage, StepStatus, StepType
from clawdence.engine import ScriptHandler, StepContext, StepFailure, UnimplementedHandler, execute
from clawdence.engine.handlers import INHERITED_ENV, MAX_CAPTURE_BYTES, default_registry
from tests.engine.factories import RUN_ID, resolver_for, run, script, workflow


def invoke(
    stage: ScriptStage, *, environ: dict[str, str] | None = None, **outputs: object
) -> dict[str, Any]:
    handler = ScriptHandler(environ if environ is not None else {})
    context = StepContext(
        run_id=RUN_ID,
        stage=stage,
        attempt=1,
        resolver=resolver_for(**outputs),  # type: ignore[arg-type]
    )
    outcome = run(handler(context))
    assert isinstance(outcome.output, dict)
    return outcome.output


def python(*code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", *code)


class TestOutputShape:
    """One shape whatever the command did — see ``ScriptHandler``'s docstring."""

    def test_envelope_fields(self) -> None:
        output = invoke(script("s", *python("print('hi')")))
        assert output == {
            "exit_code": 0,
            "stdout": "hi\n",
            "stderr": "",
            "parsed": None,
            "truncated": False,
        }

    def test_json_stdout_is_parsed(self) -> None:
        output = invoke(script("s", *python('print(\'{"verdict": "ok"}\')')))
        assert output["parsed"] == {"verdict": "ok"}

    def test_non_json_stdout_parses_to_none_without_failing(self) -> None:
        output = invoke(script("s", *python("print('not json')")))
        assert output["parsed"] is None
        assert output["exit_code"] == 0

    def test_stderr_is_captured_separately(self) -> None:
        stage = script("s", *python("import sys; print('warn', file=sys.stderr)"))
        assert invoke(stage)["stderr"] == "warn\n"

    def test_stdin_is_delivered(self) -> None:
        stage = script("s", *python("import sys; print(sys.stdin.read().upper())"), stdin="abc")
        assert invoke(stage)["stdout"] == "ABC\n"


class TestCapture:
    """v1's processing log reached 300MB. A step cannot put a build in a row."""

    def test_oversized_stdout_is_truncated_and_flagged(self) -> None:
        stage = script("s", *python(f"print('x' * {MAX_CAPTURE_BYTES * 2})"))
        output = invoke(stage)
        assert output["truncated"] is True
        assert output["stdout"].endswith("[truncated]")
        assert len(output["stdout"]) < MAX_CAPTURE_BYTES + 100

    def test_truncated_output_is_never_parsed(self) -> None:
        # A cut-off document either fails to decode or, worse, decodes to a
        # prefix that is valid JSON and wrong.
        payload = "a" * MAX_CAPTURE_BYTES
        stage = script("s", *python(f"import json; print(json.dumps(['{payload}', 'tail']))"))
        output = invoke(stage)
        assert output["truncated"] is True
        assert output["parsed"] is None

    def test_invalid_utf8_does_not_crash_the_step(self) -> None:
        stage = script("s", *python("import sys; sys.stdout.buffer.write(b'\\xff\\xfe')"))
        assert invoke(stage)["exit_code"] == 0


class TestArgv:
    """No shell, ever."""

    def test_metacharacters_stay_inside_one_argument(self) -> None:
        hostile = "; rm -rf / #"
        stage = script("s", *python("import sys; print(len(sys.argv), sys.argv[1])"), hostile)
        assert invoke(stage)["stdout"] == f"2 {hostile}\n"

    def test_interpolated_value_stays_one_argument(self) -> None:
        stage = script(
            "s",
            *python("import sys; print(len(sys.argv))"),
            "${a.json.text}",
        )
        assert invoke(stage, a={"text": "two words; and a semicolon"})["stdout"] == "2\n"

    def test_argv_zero_is_never_interpolated(self) -> None:
        # The loader rejects this file, so the handler never sees it — but if
        # it did, argv[0] must still be the literal text rather than a value an
        # earlier step chose.
        stage = ScriptStage(id="s", command=("${a.json.exe}", "-c", "pass"))
        with pytest.raises(StepFailure) as caught:
            invoke(stage, a={"exe": sys.executable})
        assert caught.value.kind == "script-not-runnable"

    def test_missing_executable_is_not_retryable(self) -> None:
        stage = ScriptStage(id="s", command=("clawdence-does-not-exist",))
        with pytest.raises(StepFailure) as caught:
            invoke(stage)
        assert caught.value.retryable is False


class TestEnvironmentIsolation:
    """A script step gets the environment it was given, not the one we hold."""

    def test_control_plane_secrets_do_not_reach_the_child(self) -> None:
        stage = script("s", *python("import os; print(os.environ.get('ANTHROPIC_API_KEY', ''))"))
        output = invoke(stage, environ={"ANTHROPIC_API_KEY": "sk-secret", "PATH": "/usr/bin"})
        assert output["stdout"] == "\n"

    def test_declared_env_reaches_the_child(self) -> None:
        stage = script(
            "s",
            *python("import os; print(os.environ['GREETING'])"),
            env={"GREETING": "hello"},
        )
        assert invoke(stage)["stdout"] == "hello\n"

    def test_declared_env_is_interpolated(self) -> None:
        stage = script(
            "s",
            *python("import os; print(os.environ['SIZE'])"),
            env={"SIZE": "${a.json.size}"},
        )
        assert invoke(stage, a={"size": "M"})["stdout"] == "M\n"

    def test_allowlisted_variables_are_inherited(self) -> None:
        stage = script("s", *python("import os; print(os.environ.get('PATH', 'unset'))"))
        output = invoke(stage, environ={"PATH": "/usr/bin", "SECRET": "x"})
        assert output["stdout"] == "/usr/bin\n"

    def test_the_allowlist_names_nothing_credential_shaped(self) -> None:
        # A regression guard on the list itself: widening it is a decision, and
        # a decision that adds "*_KEY" or "*_TOKEN" should fail here first.
        assert not [
            name
            for name in INHERITED_ENV
            if any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASS", "CRED"))
        ]


class TestFailure:
    def test_non_zero_exit_is_a_retryable_failure(self) -> None:
        stage = script("s", *python("raise SystemExit(3)"))
        with pytest.raises(StepFailure) as caught:
            invoke(stage)
        assert caught.value.kind == "script-exit"
        assert caught.value.retryable is True
        assert "exited 3" in caught.value.message

    def test_interpolation_failure_is_not_retryable(self) -> None:
        # Attempt two would reference the same absent field.
        stage = script("s", *python("pass"), "${a.json.nope}")
        with pytest.raises(StepFailure) as caught:
            invoke(stage, a={"x": 1})
        assert caught.value.kind == "interpolation"
        assert caught.value.retryable is False
        assert "command[3]" in caught.value.message


class TestTimeoutKillsTheChild:
    """A cancelled step must not leave a process behind.

    This is the shape of v1's stale-spawn bug: the orchestrator moved on while
    the work it started kept running, and nothing afterwards agreed about what
    state anything was in. The executor records ``timed_out`` either way — what
    is asserted here is that the subprocess is actually gone.
    """

    def test_the_subprocess_does_not_outlive_the_step(self, tmp_path: Path) -> None:
        marker = tmp_path / "still-alive"
        # Sleeps past the timeout, then writes. If the child survived
        # cancellation, the marker appears while we are waiting below.
        stage = script(
            "s",
            *python(
                "import pathlib, sys, time; time.sleep(1.0); "
                "pathlib.Path(sys.argv[1]).write_text('alive')"
            ),
            str(marker),
            timeout_seconds=0.15,
        )
        report = run(
            execute(
                workflow(stage),
                run_id=RUN_ID,
                work_item_id="wi.test",
                registry=default_registry({}),
            )
        )
        assert report.final["s"].status is StepStatus.TIMED_OUT

        time.sleep(1.3)
        assert not marker.exists()


class TestRegistry:
    @pytest.mark.parametrize(
        ("step_type", "owner"),
        [
            (StepType.AGENT, "S12"),
            (StepType.RUNNER, "a `runner:` section in config.yaml"),
            (StepType.APPROVAL, "S17"),
        ],
    )
    def test_unimplemented_types_refuse_and_name_their_step(
        self, step_type: StepType, owner: str
    ) -> None:
        # A stub that returned success would make a workflow look like it ran,
        # which is the most expensive possible way for an orchestrator to be
        # wrong.
        handler = default_registry().for_type(step_type)
        assert isinstance(handler, UnimplementedHandler)
        context = StepContext(RUN_ID, script("s"), 1, resolver_for())
        with pytest.raises(StepFailure) as caught:
            run(handler(context))
        assert caught.value.kind == "step-type-not-implemented"
        assert owner in caught.value.message
        assert caught.value.retryable is False

    def test_script_is_the_one_real_handler(self) -> None:
        assert isinstance(default_registry().for_type(StepType.SCRIPT), ScriptHandler)
