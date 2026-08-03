"""Loading and validating workflow files.

The theme is the one in the loader's docstring: **a workflow that will fail
should fail before it costs anything**. Every test here is a mistake that
Lobster would report mid-run, after the stages ahead of it had already called
an LLM.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from clawdence.domain import ScriptStage, StepType, Workflow
from clawdence.engine import WorkflowLoadError, load_workflow, parse_workflow, validate_references
from clawdence.engine.loader import SCHEMA_POLICY_DOC

MINIMAL = """
name: demo
version: 1.0.0
stages:
  - id: build
    type: script
    command: [make, build]
"""


def load(body: str) -> Workflow:
    return parse_workflow(dedent(body), origin="demo.yaml")


def refuses(body: str, match: str | None = None) -> WorkflowLoadError:
    with pytest.raises(WorkflowLoadError, match=match) as caught:
        load(body)
    return caught.value


class TestHappyPath:
    def test_builds_the_domain_model(self) -> None:
        wf = load(MINIMAL)
        assert wf.name == "demo"
        assert wf.stages[0].type is StepType.SCRIPT
        assert isinstance(wf.stages[0], ScriptStage)
        assert wf.stages[0].command == ("make", "build")

    def test_reads_a_file(self, tmp_path: Path) -> None:
        path = tmp_path / "wf.yaml"
        path.write_text(MINIMAL, encoding="utf-8")
        assert load_workflow(path).name == "demo"

    def test_a_missing_file_is_a_load_error_not_an_oserror(self, tmp_path: Path) -> None:
        with pytest.raises(WorkflowLoadError, match="cannot be read"):
            load_workflow(tmp_path / "absent.yaml")

    def test_the_example_workflow_loads(self) -> None:
        # The file the README tells people to run first.
        assert load_workflow(Path("examples/toy.yaml")).name == "toy"


class TestYamlProblems:
    def test_the_message_names_the_line(self) -> None:
        error = refuses("name: demo\n  version: oops\n", r"demo\.yaml:2:")
        assert error.origin == "demo.yaml"
        assert error.line == 2

    def test_unquoted_bang_is_diagnosed_as_a_yaml_tag(self) -> None:
        # The S0 spike lost time to exactly this: YAML reads a leading "!" as a
        # tag, not as negation, and Lobster's reaction was a warning plus
        # silently wrong behaviour.
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - id: a
                type: script
                command: [make]
                when: !$a.succeeded
            """
        )
        assert error.hint is not None
        assert "YAML tag" in error.hint

    def test_a_tab_is_diagnosed(self) -> None:
        error = refuses("name: demo\nstages:\n\t- id: a\n")
        assert error.hint is not None
        assert "tabs" in error.hint

    def test_empty_file(self) -> None:
        refuses("\n", "is empty")

    def test_a_list_at_the_top_level(self) -> None:
        refuses("- name: demo\n", "must be a mapping")


class TestSchemaVersion:
    def test_a_newer_version_says_to_upgrade(self) -> None:
        error = refuses("schema_version: 99\n" + MINIMAL, "newer than this build")
        assert error.hint is not None
        assert "upgrade clawdence" in error.hint

    def test_an_older_version_says_to_migrate(self) -> None:
        error = refuses("schema_version: 0\n" + MINIMAL, "older than this build")
        assert error.hint is not None
        assert "migrate the file" in error.hint

    def test_a_non_integer_version(self) -> None:
        refuses("schema_version: '1'\n" + MINIMAL, "must be an integer")

    def test_omitting_it_means_the_current_version(self) -> None:
        assert load(MINIMAL).schema_version == 1


class TestModelValidation:
    def test_a_typo_in_a_field_name_is_rejected(self) -> None:
        # extra="forbid" reaching the loader: a typo'd `timeout_second` is an
        # error, not a stage with no timeout.
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - id: a
                type: script
                command: [make]
                timeout_second: 30
            """,
            "does not match the workflow schema",
        )
        assert "timeout_second" in str(error)

    def test_duplicate_stage_ids(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [make]}
              - {id: a, type: script, command: [make]}
            """,
            "duplicate stage id",
        )

    def test_an_unknown_step_type(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: sorcery, command: [make]}
            """,
            "does not match the workflow schema",
        )

    def test_the_error_lists_one_line_per_problem(self) -> None:
        error = refuses("name: DEMO\nversion: v1\nstages: []\n", "does not match")
        assert str(error).count("\n") >= 3


class TestReferenceChecking:
    """Every reference must name a stage declared *earlier*."""

    def test_a_backward_reference_is_fine(self) -> None:
        workflow = load(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [make]}
              - {id: b, type: script, command: [make], when: '$a.succeeded'}
            """
        )
        assert workflow.stages[1].when == "$a.succeeded"

    def test_a_forward_reference_explains_why(self) -> None:
        # As a runtime lookup this would resolve to MISSING, compare unequal,
        # and leave a guard that silently never fires.
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [make], when: '$b.succeeded'}
              - {id: b, type: script, command: [make]}
            """,
            "declared later",
        )
        assert error.stage_id == "a"

    def test_an_unknown_stage_lists_what_is_available(self) -> None:
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [make]}
              - {id: b, type: script, command: [make], when: '$typo.succeeded'}
            """,
            "which no stage declares",
        )
        assert error.hint is not None
        assert "a" in error.hint

    def test_a_stage_cannot_reference_itself(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [make], when: '$a.succeeded'}
            """,
            "the stage itself",
        )

    def test_an_unparseable_condition_points_at_the_character(self) -> None:
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [make]}
              - {id: b, type: script, command: [make], when: '$a.json.x == 1 and true'}
            """,
            "invalid 'when' condition",
        )
        assert error.hint is not None
        assert "^" in error.hint

    def test_placeholders_are_checked_too(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [echo, '${typo.json.x}']}
            """,
            "which no stage declares",
        )

    @pytest.mark.parametrize(
        "stage",
        [
            "{id: b, type: script, command: [echo, '${typo.json.x}']}",
            "{id: b, type: script, command: [echo], env: {V: '${typo.json.x}'}}",
            "{id: b, type: script, command: [echo], cwd: '/w/${typo.json.x}'}",
            "{id: b, type: script, command: [echo], stdin: '${typo.json.x}'}",
        ],
    )
    def test_every_interpolatable_field_is_walked(self, stage: str) -> None:
        refuses(
            f"""
            name: demo
            version: 1.0.0
            stages:
              - {{id: a, type: script, command: [make]}}
              - {stage}
            """,
            "which no stage declares",
        )

    def test_approval_prompts_are_walked(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [make]}
              - {id: b, type: approval, prompt: 'ok? ${typo.json.x}'}
            """,
            "which no stage declares",
        )

    def test_a_malformed_placeholder(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [echo, '${unclosed']}
            """,
            "invalid placeholder",
        )


class TestArgvZero:
    """Which executable runs is the author's decision, not a step's output."""

    def test_a_placeholder_in_command_zero_is_refused(self) -> None:
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [make]}
              - {id: b, type: script, command: ['${a.json.exe}', build]}
            """,
            r"interpolates command\[0\]",
        )
        assert error.hint is not None
        assert "must not choose it" in error.hint

    def test_a_placeholder_in_a_later_element_is_allowed(self) -> None:
        workflow = load(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: a, type: script, command: [make]}
              - {id: b, type: script, command: [make, '${a.json.target}']}
            """
        )
        stage = workflow.stages[1]
        assert isinstance(stage, ScriptStage)
        assert stage.command == ("make", "${a.json.target}")


class TestValidateReferencesDirectly:
    """The check a caller building a ``Workflow`` in Python gets too."""

    def test_accepts_a_valid_workflow(self) -> None:
        validate_references(load(MINIMAL))

    def test_rejects_a_hand_built_forward_reference(self) -> None:
        wf = Workflow(
            name="demo",
            version="1.0.0",
            stages=(
                ScriptStage(id="a", command=("make",), when="$b.succeeded"),
                ScriptStage(id="b", command=("make",)),
            ),
        )
        with pytest.raises(WorkflowLoadError, match="declared later"):
            validate_references(wf)


class TestTheRequest:
    """``request`` is the work item the run is for, and it is not a stage.

    S11 added it because a process has to be able to see what it was asked to do,
    and until then a workflow could only get one by having a ``script`` stage echo
    a hardcoded string — which is what ``examples/`` shipped and what their
    comments admitted to.
    """

    def test_it_is_available_before_any_stage_has_run(self) -> None:
        """The one reference that names nothing declared earlier and is still
        valid, because it was there before the first stage started."""
        workflow = load(
            """
            name: demo
            version: 1.0.0
            stages:
              - id: build
                type: script
                command: [echo, "${request.json.text}"]
            """
        )
        assert workflow.stages[0].command[1] == "${request.json.text}"  # type: ignore[union-attr]

    def test_a_stage_may_not_be_called_request(self) -> None:
        """Shadowing it would silently change what every reference below it meant."""
        with pytest.raises(WorkflowLoadError, match="which is the name of the work item"):
            load(
                """
                name: demo
                version: 1.0.0
                stages:
                  - id: request
                    type: script
                    command: ["/bin/true"]
                """
            )

    def test_only_the_json_facet_is_readable(self) -> None:
        """A work item never ran, so it did not succeed or fail.

        ``$request.succeeded`` would resolve to something, and whatever that
        something was would be a guard that silently always fires or never does.
        """
        with pytest.raises(WorkflowLoadError, match="never ran, succeeded or failed"):
            load(
                """
                name: demo
                version: 1.0.0
                stages:
                  - id: build
                    type: script
                    when: '$request.succeeded'
                    command: ["/bin/true"]
                """
            )

    def test_a_runner_plan_is_checked_like_every_other_template(self) -> None:
        """``RunnerStage.plan`` arrived with S11 and is in the closed list of
        interpolable fields, so a typo in it fails at load rather than at
        dispatch — after a worktree has been checked out."""
        with pytest.raises(WorkflowLoadError, match="which no stage declares"):
            load(
                """
                name: demo
                version: 1.0.0
                stages:
                  - id: code
                    type: runner
                    plan: 'build ${nonesuch.json.text}'
                """
            )


class TestLineNumbers:
    """S3c: every load error points at the line the mistake is on.

    "Stage 'review' refers to a stage that does not exist" is a true sentence
    and, in a 150-line file, an instruction to go and count.
    """

    LINED = (
        "schema_version: 1\n"  # 1
        "name: demo\n"  # 2
        "version: 1.0.0\n"  # 3
        "stages:\n"  # 4
        "  - id: build\n"  # 5
        "    type: script\n"  # 6
        "    command:\n"  # 7
        "      - make\n"  # 8
        "      - '${nonesuch.json.text}'\n"  # 9
        "  - id: check\n"  # 10
        "    type: script\n"  # 11
        "    when: '$absent.succeeded'\n"  # 12
        "    command: [make, check]\n"  # 13
    )

    def test_a_bad_placeholder_points_at_the_argv_element(self) -> None:
        error = refuses(self.LINED, "which no stage declares")
        assert error.line == 9
        assert error.stage_id == "build"
        assert str(error).startswith("demo.yaml:9: stage 'build':")

    def test_a_bad_guard_points_at_the_when(self) -> None:
        body = self.LINED.replace("      - '${nonesuch.json.text}'\n", "      - build\n")
        error = refuses(body, r"\$absent")
        assert error.line == 12

    def test_a_schema_error_points_at_the_field_not_the_stage(self) -> None:
        """pydantic addresses errors by path, and the path is the file's own.

        The discriminator it writes into the middle of that path — ``script`` in
        ``stages.0.script.timeout_seconds`` — was never a key in the document,
        and reporting the stage's line because of it would answer a question
        about one field with the location of forty.
        """
        body = self.LINED.replace(
            "    type: script\n", "    type: script\n    timeout_seconds: 0\n", 1
        )
        error = refuses(body, "does not match the workflow schema")
        assert error.line == 7
        assert "line 7: stages.0.script.timeout_seconds" in error.message

    def test_a_duplicate_id_points_at_the_second_one_and_names_the_first(self) -> None:
        error = refuses(self.LINED.replace("  - id: check\n", "  - id: build\n"), "duplicate")
        assert error.line == 10
        assert error.hint is not None
        assert "first declared on line 5" in error.hint

    def test_an_unsupported_schema_version_points_at_the_declaration(self) -> None:
        error = refuses(self.LINED.replace("schema_version: 1", "schema_version: 9"), "newer")
        assert error.line == 1
        assert error.hint is not None
        assert SCHEMA_POLICY_DOC in error.hint

    def test_a_workflow_built_in_python_has_no_line_to_point_at(self) -> None:
        wf = Workflow(
            name="demo",
            version="1.0.0",
            stages=(ScriptStage(id="a", command=("make",), when="$b.succeeded"),),
        )
        with pytest.raises(WorkflowLoadError) as caught:
            validate_references(wf)
        assert caught.value.line is None
        assert ":None" not in str(caught.value)


class TestCompositionScopes:
    """The nested-scope refusals S3b added, each with the line it is on.

    Every one of these is a message a workflow author reads while writing a
    file, which is what makes them S3c's business as much as S3b's.
    """

    def test_a_nested_duplicate_id_names_the_scope(self) -> None:
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - id: fan
                type: for_each
                items: $request.json.items
                stages:
                  - {id: one, type: script, command: [make]}
                  - {id: one, type: script, command: [make]}
            """,
            "duplicate stage id 'one'",
        )
        assert "root.fan.stages" in error.message

    def test_a_fan_out_variable_may_not_shadow_a_stage(self) -> None:
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: item, type: script, command: [make]}
              - id: fan
                type: for_each
                items: $item.json.parsed.list
                stages:
                  - {id: one, type: script, command: [make]}
            """,
            "fan-out variables shadow",
        )
        assert error.stage_id == "fan"

    def test_a_loop_variable_may_not_shadow_a_stage(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: iteration, type: script, command: [make]}
              - id: settle
                type: repeat
                max_iterations: 2
                until: '$poll.succeeded'
                stages:
                  - {id: poll, type: script, command: [make]}
            """,
            "loop variables shadow",
        )

    def test_a_stage_may_not_take_the_name_of_a_value_in_scope(self) -> None:
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - id: fan
                type: for_each
                items: $request.json.items
                stages:
                  - {id: item, type: script, command: [make]}
            """,
            "which is a value provided to this scope",
        )
        assert error.hint is not None
        assert "silently change meaning" in error.hint

    def test_a_scope_variable_has_no_status_to_read(self) -> None:
        """``$item.succeeded`` is a question about a step, and an item is not one."""
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - id: fan
                type: for_each
                items: $request.json.items
                stages:
                  - {id: one, type: script, command: [make], when: '$item.succeeded'}
            """,
            "which is a scope variable rather than a step",
        )
        assert error.hint is not None
        assert "${item.json}" in error.hint

    def test_an_items_reference_must_be_exact_not_a_template(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - id: fan
                type: for_each
                items: request.json.items
                stages:
                  - {id: one, type: script, command: [make]}
            """,
            "must be an exact",
        )

    def test_an_items_reference_that_names_no_facet_is_refused(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - id: fan
                type: for_each
                items: $request
                stages:
                  - {id: one, type: script, command: [make]}
            """,
            "invalid 'items' reference",
        )

    def test_a_broken_placeholder_in_a_serial_key_is_refused(self) -> None:
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - id: fan
                type: for_each
                items: $request.json.items
                serial_key: '${item.json.repo'
                stages:
                  - {id: one, type: script, command: [make]}
            """,
            "invalid placeholder in serial_key",
        )
        assert error.stage_id == "fan"


class TestSubWorkflows:
    def test_calling_one_that_is_not_defined_lists_what_is(self) -> None:
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: call, type: workflow, workflow: absent, inputs: {}}
            sub_workflows:
              release:
                stages:
                  - {id: publish, type: script, command: [make]}
            """,
            "which is not defined",
        )
        assert error.hint is not None
        assert "release" in error.hint

    def test_inputs_must_match_the_declaration_in_both_directions(self) -> None:
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: call, type: workflow, workflow: release, inputs: {branch: main}}
            sub_workflows:
              release:
                inputs: [tag]
                stages:
                  - {id: publish, type: script, command: [make, '${tag.json}']}
            """,
            "do not match",
        )
        assert "missing tag" in error.message
        assert "unknown branch" in error.message

    def test_the_reserved_request_name_cannot_be_an_input(self) -> None:
        error = refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: call, type: workflow, workflow: release, inputs: {request: x}}
            sub_workflows:
              release:
                inputs: [request]
                stages:
                  - {id: publish, type: script, command: [make]}
            """,
            "reserved input",
        )
        assert error.hint is not None
        assert "$request.json" in error.hint

    def test_a_cycle_is_rejected_before_anything_runs(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: call, type: workflow, workflow: a, inputs: {}}
            sub_workflows:
              a:
                stages:
                  - {id: to_b, type: workflow, workflow: b, inputs: {}}
              b:
                stages:
                  - {id: to_a, type: workflow, workflow: a, inputs: {}}
            """,
            "cyclic sub-workflow call",
        )


class TestTheDuplicateIdPreCheck:
    """It runs on the raw document, so it must never pre-empt a better error.

    Everything it walks may be the wrong shape — that is the state the schema
    error it precedes is about. Each of these files is rejected on its merits,
    by the model, with the message the model would have given anyway.
    """

    def test_stages_that_are_not_a_list(self) -> None:
        refuses("name: demo\nversion: 1.0.0\nstages: nonsense\n", "does not match")

    def test_a_stage_that_is_not_a_mapping(self) -> None:
        refuses("name: demo\nversion: 1.0.0\nstages: [nonsense]\n", "does not match")

    def test_an_id_that_is_not_a_string(self) -> None:
        refuses(
            """
            name: demo
            version: 1.0.0
            stages:
              - {id: 7, type: script, command: [make]}
            """,
            "does not match",
        )
