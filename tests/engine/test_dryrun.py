"""S3c ``workflow test``: the run that costs nothing and touches nothing."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from clawdence.domain import StepStatus, Workflow
from clawdence.engine import (
    DryRunReport,
    load_workflow,
    parse_workflow,
    render_dry_run,
    render_dry_run_json,
    synthesise,
)
from clawdence.engine.dryrun import FAN_OUT_SAMPLE, dry_run
from tests.engine.factories import run


def load(body: str) -> Workflow:
    return parse_workflow(dedent(body), origin="dry.yaml")


def rehearse(body: str, **kwargs: object) -> DryRunReport:
    return run(dry_run(load(body), **kwargs))  # type: ignore[arg-type]


def statuses(report: DryRunReport) -> dict[str, StepStatus]:
    return {result.stage_id: result.status for result in report.run.attempts}


GUARDED = """\
    schema_version: 1
    name: guarded
    version: 1.0.0
    stages:
      - id: assess
        type: script
        command: [make, assess]
      - id: implement
        type: script
        when: '$assess.json.parsed.confidence >= 0.5 && $assess.json.parsed.size != "L"'
        command: [make, build, '${assess.json.parsed.size}']
      - id: split
        type: script
        when: '$assess.json.parsed.size == "L"'
        command: [make, split]
"""


class TestNothingRuns:
    def test_a_command_that_would_fail_does_not(self, tmp_path: Path) -> None:
        """The one property everything else here depends on.

        The command names a binary that does not exist and a file it would
        create; a rehearsal that reported success while either happened would be
        worse than useless.
        """
        witness = tmp_path / "witness"
        report = rehearse(
            f"""\
            schema_version: 1
            name: inert
            version: 1.0.0
            stages:
              - id: destroy
                type: script
                command: [/nonexistent/binary, --write, '{witness}']
            """
        )
        assert report.succeeded
        assert not witness.exists()

    def test_an_agent_step_needs_no_provider(self) -> None:
        report = rehearse(
            """\
            schema_version: 1
            name: modelled
            version: 1.0.0
            stages:
              - id: think
                type: agent
                role: architect
                task: 'Plan this.'
                model:
                  model: claude-opus-5
            """
        )
        assert report.succeeded
        step = report.steps[0]
        assert step.detail["role"] == "architect"
        assert step.detail["model"] == "claude-opus-5"

    def test_a_runner_step_needs_no_repository(self) -> None:
        report = rehearse(
            """\
            schema_version: 1
            name: shipped
            version: 1.0.0
            stages:
              - id: code
                type: runner
                plan: 'Do the work.'
            """
        )
        assert report.succeeded
        assert report.steps[0].detail["plan"] == "Do the work."

    def test_an_approval_step_does_not_wait_for_a_person(self) -> None:
        report = rehearse(
            """\
            schema_version: 1
            name: gated
            version: 1.0.0
            stages:
              - id: gate
                type: approval
                prompt: 'Ship it?'
                required_approver: someone
            """
        )
        assert report.succeeded
        assert report.steps[0].detail["required_approver"] == "someone"


class TestTheHappyPath:
    def test_a_guard_reading_an_invented_value_fires(self) -> None:
        report = rehearse(GUARDED)
        assert statuses(report)["implement"] is StepStatus.SUCCEEDED

    def test_the_earlier_of_two_incompatible_guards_wins(self) -> None:
        """`size != "L"` and `size == "L"` cannot both hold.

        The file is written to take the first branch, so that is the one walked;
        the alternative is a rehearsal that demonstrates the fallback nobody
        asked about.
        """
        report = rehearse(GUARDED)
        assert statuses(report)["split"] is StepStatus.SKIPPED

    def test_an_ordering_comparison_gets_a_value_that_satisfies_it(self) -> None:
        invented = synthesise(load(GUARDED)).outputs
        assert invented["assess"] == {
            "parsed": {"confidence": 0.5, "size": "<assess.json.parsed.size>"}
        }

    def test_a_strict_comparison_clears_the_bound(self) -> None:
        invented = synthesise(
            load(
                """\
                schema_version: 1
                name: strict
                version: 1.0.0
                stages:
                  - id: count
                    type: script
                    command: [make, count]
                  - id: act
                    type: script
                    when: '$count.json.parsed.files > 3'
                    command: [make, act]
                """
            )
        ).outputs
        assert invented["count"] == {"parsed": {"files": 4}}

    def test_a_reference_nobody_compares_gets_a_visible_placeholder(self) -> None:
        invented = synthesise(
            load(
                """\
                schema_version: 1
                name: plain
                version: 1.0.0
                stages:
                  - id: plan
                    type: script
                    command: [make, plan]
                  - id: build
                    type: script
                    command: [make, build, '${plan.json.parsed.summary}']
                """
            )
        ).outputs
        assert invented["plan"]["parsed"]["summary"] == "<plan.json.parsed.summary>"  # type: ignore[index,call-overload]

    def test_the_request_is_invented_only_when_something_reads_it(self) -> None:
        reads = load(
            """\
            schema_version: 1
            name: reader
            version: 1.0.0
            stages:
              - id: echo
                type: script
                command: [make, echo, '${request.json.text}']
            """
        )
        assert synthesise(reads).request == {"text": "<request.json.text>"}
        assert synthesise(load(GUARDED)).request is None


class TestComposition:
    def test_a_fan_out_gets_a_list_shaped_by_what_the_body_reads(self) -> None:
        invented = synthesise(
            load(
                """\
                schema_version: 1
                name: fanned
                version: 1.0.0
                stages:
                  - id: decompose
                    type: script
                    command: [make, plan]
                  - id: build
                    type: for_each
                    items: $decompose.json.parsed.items
                    stages:
                      - id: one
                        type: script
                        command: [make, one, '${item.json.name}']
                """
            )
        ).outputs
        items = invented["decompose"]["parsed"]["items"]  # type: ignore[index,call-overload]
        assert items == [{"name": "<item.json.name>"}] * FAN_OUT_SAMPLE

    def test_the_loop_body_runs_once_per_invented_item(self) -> None:
        report = rehearse(
            """\
            schema_version: 1
            name: fanned
            version: 1.0.0
            stages:
              - id: decompose
                type: script
                command: [make, plan]
              - id: build
                type: for_each
                items: $decompose.json.parsed.items
                stages:
                  - id: one
                    type: script
                    command: [make, one, '${item.json.name}']
            """
        )
        assert report.succeeded
        assert sum(step.definition_id == "one" for step in report.steps) == FAN_OUT_SAMPLE

    def test_a_repeat_leaves_the_loop_instead_of_exhausting_it(self) -> None:
        """Spinning to the bound and failing would report a working cap as broken."""
        report = rehearse(
            """\
            schema_version: 1
            name: looped
            version: 1.0.0
            stages:
              - id: attempt
                type: repeat
                max_iterations: 5
                until: '$check.json.parsed.ok == true'
                stages:
                  - id: check
                    type: script
                    command: [make, check]
            """
        )
        assert report.succeeded
        assert sum(step.definition_id == "check" for step in report.steps) == 1

    def test_a_sub_workflow_is_walked_with_its_inputs(self) -> None:
        report = rehearse(
            """\
            schema_version: 1
            name: caller
            version: 1.0.0
            stages:
              - id: seed
                type: script
                command: [make, seed]
              - id: call
                type: workflow
                workflow: release
                inputs:
                  tag: $seed.json.parsed.tag
            sub_workflows:
              release:
                inputs: [tag]
                stages:
                  - id: publish
                    type: script
                    command: [make, release, '${tag.json}']
            """
        )
        assert report.succeeded
        publish = next(step for step in report.steps if step.definition_id == "publish")
        assert publish.detail["argv"] == ["make", "release", "<seed.json.parsed.tag>"]


class TestSteering:
    def test_an_override_walks_the_other_branch(self) -> None:
        report = rehearse(GUARDED, outputs={"assess": {"parsed": {"size": "L"}}})
        assert statuses(report)["split"] is StepStatus.SUCCEEDED
        assert statuses(report)["implement"] is StepStatus.SKIPPED

    def test_an_override_layers_over_what_was_invented(self) -> None:
        """Overriding one field must not blank the rest of the stage."""
        report = rehearse(GUARDED, outputs={"assess": {"parsed": {"size": "L"}}})
        assert report.synthesis.outputs["assess"] == {
            "parsed": {"confidence": 0.5, "size": "L"},
        }

    def test_a_supplied_request_is_used_instead_of_an_invented_one(self) -> None:
        report = rehearse(
            """\
            schema_version: 1
            name: reader
            version: 1.0.0
            stages:
              - id: echo
                type: script
                command: [make, echo, '${request.json.text}']
            """,
            request={"text": "add a health endpoint"},
        )
        assert report.steps[0].detail["argv"] == ["make", "echo", "add a health endpoint"]


class TestReporting:
    def test_a_reference_to_a_skipped_stage_is_named_not_fatal(self) -> None:
        """Reading a branch that was not taken is the finding, not a reason to stop.

        A value was invented for `split`, but `split` never ran, so the read
        resolves to nothing — which is exactly what would happen in a real run
        and exactly what the author wants to be told about.
        """
        report = rehearse(
            GUARDED
            + """\
      - id: publish
        type: script
        command: [make, publish, '${split.json.parsed.report}']
"""
        )
        assert report.succeeded
        assert [(found.stage_id, found.reference) for found in report.unresolved] == [
            ("publish", "split.json.parsed.report")
        ]

    def test_the_text_report_says_what_was_invented(self) -> None:
        text = render_dry_run(rehearse(GUARDED))
        assert "no model call, no repository, no state" in text
        assert "values invented for this run" in text
        assert "assess.json.parsed.confidence  0.5" in text

    def test_the_text_report_shows_the_expanded_command(self) -> None:
        text = render_dry_run(rehearse(GUARDED))
        assert "argv  make build '<assess.json.parsed.size>'" in text

    def test_the_json_report_carries_the_whole_plan(self) -> None:
        payload = json.loads(render_dry_run_json(rehearse(GUARDED)))
        assert payload["run"]["workflow"]["name"] == "guarded"
        assert [step["definition_id"] for step in payload["planned"]] == ["assess", "implement"]
        assert payload["invented"]["outputs"]["assess"]["parsed"]["confidence"] == 0.5
        assert payload["unresolved"] == []

    def test_env_values_are_not_printed_only_their_names(self) -> None:
        """An `env:` value expands from step output; a rehearsal must not leak it."""
        report = rehearse(
            """\
            schema_version: 1
            name: secretive
            version: 1.0.0
            stages:
              - id: seed
                type: script
                command: [make, seed]
              - id: use
                type: script
                env:
                  TOKEN: '${seed.json.parsed.token}'
                command: [make, use]
            """
        )
        use = next(step for step in report.steps if step.definition_id == "use")
        assert use.detail["env"] == ["TOKEN"]
        assert "<seed.json.parsed.token>" not in json.dumps(dict(use.detail))


class TestShippedExamples:
    def test_every_example_rehearses_to_the_end(self) -> None:
        """The plan's own acceptance criterion, over every file we ship.

        `toy.yaml` has a stage that fails on purpose in a real run; a rehearsal
        cannot know that, and reporting it as a failure would mean every honest
        workflow with a failure branch looked broken here.
        """
        for path in sorted(Path("examples").glob("*.yaml")):
            report = run(dry_run(load_workflow(path)))
            assert report.succeeded, path
            assert render_dry_run(report)


class TestValueRules:
    """What each shape of comparison is worth, one rule per test."""

    def invented(self, condition: str) -> object:
        workflow = load(
            f"""\
            schema_version: 1
            name: rules
            version: 1.0.0
            stages:
              - id: a
                type: script
                command: [make, a]
              - id: b
                type: script
                when: '{condition}'
                command: [make, b]
            """
        )
        return synthesise(workflow).outputs.get("a")

    def test_equality_takes_the_literal(self) -> None:
        assert self.invented('$a.json.parsed.size == "M"') == {"parsed": {"size": "M"}}

    def test_a_bare_reference_is_made_true(self) -> None:
        assert self.invented("$a.json.parsed.ok") == {"parsed": {"ok": True}}

    def test_a_negated_reference_is_made_false(self) -> None:
        assert self.invented("!$a.json.parsed.ok") == {"parsed": {"ok": False}}

    def test_a_negated_equality_becomes_a_prohibition(self) -> None:
        # `!($x == "L")` constrains x exactly as `$x != "L"` does, so the
        # placeholder stands and no later `== "L"` may overrule it.
        assert self.invented('!($a.json.parsed.size == "L")') == {
            "parsed": {"size": "<a.json.parsed.size>"}
        }

    def test_a_literal_on_the_left_is_read_from_the_other_side(self) -> None:
        assert self.invented("3 < $a.json.parsed.files") == {"parsed": {"files": 4}}

    def test_a_strict_upper_bound_clears_it_downward(self) -> None:
        assert self.invented("$a.json.parsed.files < 3") == {"parsed": {"files": 2}}

    def test_a_non_strict_upper_bound_takes_the_bound(self) -> None:
        assert self.invented("$a.json.parsed.files <= 3") == {"parsed": {"files": 3}}

    def test_ordering_a_string_only_answers_the_non_strict_forms(self) -> None:
        assert self.invented('$a.json.parsed.name >= "m"') == {"parsed": {"name": "m"}}
        assert self.invented('$a.json.parsed.name > "m"') == {
            "parsed": {"name": "<a.json.parsed.name>"}
        }

    def test_a_constant_condition_constrains_nothing(self) -> None:
        assert self.invented("true") is None

    def test_comparing_two_references_shapes_both_and_values_neither(self) -> None:
        workflow = load(
            """\
            schema_version: 1
            name: pair
            version: 1.0.0
            stages:
              - id: a
                type: script
                command: [make, a]
              - id: b
                type: script
                command: [make, b]
              - id: c
                type: script
                when: '$a.json.parsed.x == $b.json.parsed.y'
                command: [make, c]
            """
        )
        invented = synthesise(workflow).outputs
        assert invented["a"] == {"parsed": {"x": "<a.json.parsed.x>"}}
        assert invented["b"] == {"parsed": {"y": "<b.json.parsed.y>"}}

    def test_cwd_and_stdin_are_expanded_like_every_other_template(self) -> None:
        report = rehearse(
            """\
            schema_version: 1
            name: piped
            version: 1.0.0
            stages:
              - id: a
                type: script
                command: [make, a]
              - id: b
                type: script
                cwd: '${a.json.parsed.dir}'
                stdin: '${a.json.parsed.body}'
                command: [make, b]
            """
        )
        assert report.steps[1].detail["cwd"] == "<a.json.parsed.dir>"


class TestMoreComposition:
    def test_parallel_branches_are_each_walked(self) -> None:
        report = rehearse(
            """\
            schema_version: 1
            name: fanned
            version: 1.0.0
            stages:
              - id: split
                type: parallel
                branches:
                  - id: left
                    stages:
                      - id: a
                        type: script
                        command: [make, a]
                  - id: right
                    stages:
                      - id: b
                        type: script
                        command: [make, b]
              - id: join
                type: script
                when: '$split.json.branches.left.a.status == "succeeded"'
                command: [make, join]
            """
        )
        assert report.succeeded
        assert {step.definition_id for step in report.steps} == {"a", "b", "join"}

    def test_a_fan_out_over_something_that_is_not_a_list_fails_and_says_so(self) -> None:
        """The rehearsal reports a real defect rather than papering over it.

        Overriding `decompose` with a scalar is how an author asks "what if the
        decomposer returns the wrong shape" — and the answer is the failure the
        real run would have, for free.
        """
        report = rehearse(
            """\
            schema_version: 1
            name: fanned
            version: 1.0.0
            stages:
              - id: decompose
                type: script
                command: [make, plan]
              - id: build
                type: for_each
                items: $decompose.json.parsed.items
                stages:
                  - id: one
                    type: script
                    command: [make, one, '${item.json.name}']
            """,
            outputs={"decompose": {"parsed": {"items": "not a list"}}},
        )
        assert not report.succeeded
        assert "failed: build" in render_dry_run(report)


class TestApprovalReporting:
    def test_a_gate_response_is_invented_and_shown(self) -> None:
        report = rehearse(
            """\
            schema_version: 1
            name: gated
            version: 1.0.0
            stages:
              - id: gate
                type: approval
                prompt: 'Ship ${request.json.title}?'
              - id: ship
                type: script
                when: '$gate.response.decision == "approve"'
                command: [make, ship]
            """
        )
        text = render_dry_run(report)
        assert statuses(report)["ship"] is StepStatus.SUCCEEDED
        assert "ask   Ship <request.json.title>?" in text
        assert 'gate.response.decision  "approve"' in text

    def test_an_unresolved_reference_is_listed_in_the_text_report(self) -> None:
        report = rehearse(
            GUARDED
            + """\
      - id: publish
        type: script
        command: [make, publish, '${split.json.parsed.report}']
"""
        )
        text = render_dry_run(report)
        assert "references that resolved to nothing even so:" in text
        assert "split.json.parsed.report in publish.command[2]" in text


def test_a_rehearsal_never_spends_a_declared_backoff() -> None:
    """`sleep` is stubbed out, not shortened.

    A workflow declaring `backoff_seconds: 300` is declaring what a *real* run
    should wait. A rehearsal that honoured it would take five minutes to tell
    you about a typo.
    """
    from clawdence.engine.dryrun import _no_sleep

    assert run(_no_sleep(3600.0)) is None
