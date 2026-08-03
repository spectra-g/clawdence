"""S3c ``workflow graph``: the file drawn, with nothing resolved."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from clawdence.domain import Workflow
from clawdence.engine import load_workflow, parse_workflow, render_mermaid, render_tree


def load(body: str) -> Workflow:
    return parse_workflow(dedent(body), origin="graph.yaml")


NESTED = """\
    schema_version: 1
    name: nested
    version: 2.1.0
    description: Fan out, then join.
    stages:
      - id: decompose
        type: script
        command: [make, plan]
      - id: build
        type: for_each
        items: $decompose.json.parsed.items
        max_parallel: 3
        stages:
          - id: build-item
            type: script
            command: [make, one, '${item.json.name}']
      - id: join
        type: script
        when: '$build.json.count > 0'
        command: [make, join, '${build.json.count}']
"""


class TestTree:
    def test_the_header_names_the_workflow_and_the_schema(self) -> None:
        tree = render_tree(load(NESTED))
        assert tree.splitlines()[0] == "nested@2.1.0  (schema 1)"
        assert "Fan out, then join." in tree

    def test_stages_appear_in_order_with_their_type(self) -> None:
        lines = [line.strip() for line in render_tree(load(NESTED)).splitlines()]
        assert any(line.startswith("decompose  script") for line in lines)
        assert any(line.startswith("build      for_each") for line in lines)

    def test_a_nested_scope_is_indented_under_its_composition(self) -> None:
        tree = render_tree(load(NESTED))
        parent = next(line for line in tree.splitlines() if line.lstrip().startswith("build "))
        child = next(line for line in tree.splitlines() if "build-item" in line)
        assert len(child) - len(child.lstrip()) > len(parent) - len(parent.lstrip())

    def test_a_guard_is_printed_as_written_not_evaluated(self) -> None:
        assert "when      $build.json.count > 0" in render_tree(load(NESTED))

    def test_the_data_a_stage_reads_is_named(self) -> None:
        """The other graph in the same file, and the one that catches a typo.

        A stage whose ``reads`` is empty when the author thought it was handed
        the plan is the finding this line exists for.
        """
        tree = render_tree(load(NESTED))
        assert "reads     decompose" in tree
        assert "reads     build" in tree

    def test_policy_is_shown_only_when_it_is_not_the_default(self) -> None:
        tree = render_tree(
            load(
                """\
                schema_version: 1
                name: policy
                version: 1.0.0
                stages:
                  - id: plain
                    type: script
                    command: [make]
                  - id: careful
                    type: script
                    command: [make]
                    on_error: continue
                    timeout_seconds: 30
                    retry:
                      max_attempts: 3
                      backoff_seconds: 1.5
                """
            )
        )
        plain, careful = tree.split("careful")
        assert "retry" not in plain and "timeout" not in plain
        assert "retry     up to 3 attempts, 1.5s apart" in careful
        assert "timeout   30s" in careful
        assert "on_error  continue" in careful

    def test_a_sub_workflow_is_listed_with_its_inputs(self) -> None:
        tree = render_tree(
            load(
                """\
                schema_version: 1
                name: caller
                version: 1.0.0
                stages:
                  - id: seed
                    type: script
                    command: [make]
                  - id: call
                    type: workflow
                    workflow: release
                    inputs:
                      tag: $seed.json.stdout
                sub_workflows:
                  release:
                    inputs: [tag]
                    stages:
                      - id: publish
                        type: script
                        command: [make, release, '${tag.json}']
                """
            )
        )
        assert "sub-workflow release  (tag)" in tree
        assert "publish" in tree
        assert "calls release" in tree

    def test_every_shipped_example_renders(self) -> None:
        for path in sorted(Path("examples").glob("*.yaml")):
            assert render_tree(load_workflow(path))


class TestMermaid:
    def test_it_is_a_flowchart_with_one_node_per_stage(self) -> None:
        chart = render_mermaid(load(NESTED))
        assert chart.splitlines()[0] == "flowchart TD"
        assert '"decompose<br/>script"' in chart
        assert '"join<br/>script"' in chart

    def test_a_composition_becomes_a_subgraph_around_its_body(self) -> None:
        chart = render_mermaid(load(NESTED))
        assert "subgraph s1_build[" in chart
        assert "build-item<br/>script" in chart
        assert "end" in chart

    def test_the_guard_rides_on_the_edge_into_the_stage_it_guards(self) -> None:
        chart = render_mermaid(load(NESTED))
        assert 's1_build -->|"$build.json.count #gt; 0"| s2_join' in chart

    def test_quotes_and_angles_are_escaped_into_mermaid_entities(self) -> None:
        chart = render_mermaid(
            load(
                """\
                schema_version: 1
                name: escaping
                version: 1.0.0
                stages:
                  - id: a
                    type: script
                    command: [make]
                  - id: b
                    type: script
                    when: '$a.json.parsed.size != "L"'
                    command: [make]
                """
            )
        )
        assert "#quot;L#quot;" in chart
        assert '!= "L"' not in chart

    def test_stage_ids_that_sanitise_alike_stay_distinct(self) -> None:
        """`build-item` and `build_item` are two stages, not one node."""
        chart = render_mermaid(
            load(
                """\
                schema_version: 1
                name: collision
                version: 1.0.0
                stages:
                  - id: build-item
                    type: script
                    command: [make]
                  - id: build_item
                    type: script
                    command: [make]
                """
            )
        )
        nodes = {line.strip().split("[")[0] for line in chart.splitlines() if "<br/>" in line}
        assert len(nodes) == 2

    def test_a_sub_workflow_call_points_at_its_definition(self) -> None:
        chart = render_mermaid(
            load(
                """\
                schema_version: 1
                name: caller
                version: 1.0.0
                stages:
                  - id: call
                    type: workflow
                    workflow: release
                    inputs: {}
                sub_workflows:
                  release:
                    stages:
                      - id: publish
                        type: script
                        command: [make]
                """
            )
        )
        assert "subgraph sub_release[" in chart
        assert "-.-> sub_release" in chart

    def test_parallel_branches_each_get_their_own_box(self) -> None:
        chart = render_mermaid(
            load(
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
                            command: [make]
                      - id: right
                        stages:
                          - id: b
                            type: script
                            command: [make]
                """
            )
        )
        assert chart.count("subgraph") == 3
        assert '"left"' in chart and '"right"' in chart

    def test_every_shipped_example_renders(self) -> None:
        for path in sorted(Path("examples").glob("*.yaml")):
            assert render_mermaid(load_workflow(path)).startswith("flowchart TD")


BRANCHED = """\
    schema_version: 1
    name: branched
    version: 1.0.0
    stages:
      - id: split
        type: parallel
        max_parallel: 2
        branches:
          - id: left
            stages:
              - id: a
                type: script
                cwd: /tmp
                stdin: 'hello'
                command: [make, a]
          - id: right
            stages:
              - id: b
                type: approval
                prompt: 'Ship it?'
                required_approver: someone
      - id: settle
        type: repeat
        max_iterations: 4
        until: '$poll.json.parsed.done == true'
        stages:
          - id: poll
            type: script
            command: [make, poll]
"""


class TestTheOtherStepTypes:
    """Everything S3b added, and the gate S17 will implement."""

    def test_the_tree_names_branches_gates_and_loops(self) -> None:
        tree = render_tree(load(BRANCHED))
        assert "branches left, right, up to 2 at once" in tree
        assert "branch left" in tree and "branch right" in tree
        assert "human gate, approver someone" in tree
        assert "until $poll.json.parsed.done == true, at most 4 times" in tree

    def test_a_loop_condition_counts_as_data_the_stage_reads(self) -> None:
        assert "reads     poll" in render_tree(load(BRANCHED))

    def test_mermaid_boxes_a_loop_with_its_bound(self) -> None:
        chart = render_mermaid(load(BRANCHED))
        assert "repeat ≤4" in chart
        assert '{{"b<br/>approval"}}' in chart
