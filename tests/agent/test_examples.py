"""The shipped example workflows, held to what they claim.

Examples are the first thing anybody reads and the last thing anybody tests, and a
broken one is worse than none: it teaches a wrong shape and it does it with the
authority of being in the repository. These load them, and then check every agent
stage against the *real* prompt registry, the real schema registry and the real
provider catalogue — so an example naming a role we do not ship, a schema nobody
registered, a model that has left the catalogue, or a capability that model does
not have is a red build rather than a surprise for the first person to try it.

Nothing here spends anything. ``validate_stage`` is the part of the agent step that
runs before a request is built, which is exactly why it is a separate function.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawdence.agent import AnthropicModels, PromptRegistry, ResponseSchemas, validate_stage
from clawdence.domain import AgentStage, StepType, Workflow
from clawdence.engine import load_workflow
from clawdence.ports import NullSecrets

EXAMPLES = Path("examples")


def workflows() -> list[Path]:
    found = sorted(EXAMPLES.glob("*.yaml"))
    assert found, "the examples directory is empty, which is itself the bug"
    return found


def catalogue() -> AnthropicModels:
    """The real adapter, holding no credential. ``describe`` needs none."""
    return AnthropicModels(NullSecrets())


@pytest.mark.parametrize("path", workflows(), ids=lambda path: path.stem)
def test_every_example_loads(path: Path) -> None:
    """Which also proves every reference in it names an earlier stage, every
    condition parses, and every placeholder is well formed — the loader does all
    three before returning."""
    assert isinstance(load_workflow(path), Workflow)


@pytest.mark.parametrize("path", workflows(), ids=lambda path: path.stem)
def test_every_agent_stage_in_an_example_could_actually_run(path: Path) -> None:
    """Role, schema, tools, model and capabilities, all checked against what ships.

    This is the test that would have caught the version of ``sprint.yaml`` that
    asked for ``senior-dev``, which is a role v1 had and this does not."""
    prompts = PromptRegistry()
    schemas = ResponseSchemas()
    models = catalogue()

    for stage in load_workflow(path).stages:
        if isinstance(stage, AgentStage):
            validate_stage(stage, model=models, prompts=prompts, schemas=schemas)


def test_the_two_agent_workflows_differ_only_in_their_stages() -> None:
    """ "Same engine, different YAML" — the flexibility goal, demonstrated rather
    than asserted. One process has four agent steps and a runner, one has two agent
    steps and no runner, one has none at all; not one of the three is a special case
    anywhere in the code.
    """
    spike = load_workflow(EXAMPLES / "spike.yaml")
    sprint = load_workflow(EXAMPLES / "sprint.yaml")

    def kinds(workflow: Workflow) -> list[StepType]:
        return [stage.type for stage in workflow.stages]

    assert kinds(spike).count(StepType.AGENT) == 2
    assert StepType.RUNNER not in kinds(spike)
    assert kinds(sprint).count(StepType.AGENT) == 4
    assert StepType.RUNNER in kinds(sprint)

    # And the workflow with no agent steps at all still exists and still loads,
    # which is the other half of the same claim.
    assert StepType.AGENT not in kinds(load_workflow(EXAMPLES / "toy.yaml"))


def test_every_agent_stage_in_an_example_carries_a_budget() -> None:
    """Not a property of the engine — a property of an example, and the one that
    decides whether copying it is safe. An agent step with no cap is legal and is
    not what anybody should learn from a file in this repository."""
    for path in workflows():
        for stage in load_workflow(path).stages:
            if isinstance(stage, AgentStage):
                assert stage.budget is not None, f"{path.name}:{stage.id} has no budget"
                assert stage.timeout_seconds is not None, f"{path.name}:{stage.id} has no timeout"


def test_every_agent_stage_in_an_example_pins_temperature() -> None:
    """So that changing the prompt is the only thing that changes the answer.
    S21b's evals depend on it, and an example that did not do it would teach the
    habit of not doing it."""
    for path in workflows():
        for stage in load_workflow(path).stages:
            if isinstance(stage, AgentStage):
                assert stage.model.temperature is not None, f"{path.name}:{stage.id}"
