from ctrlrtn.policy.scope import ExperimentScope
from ctrlrtn.workflow.identity import WorkflowIdentity


def _identity(workflow: str, version: str, step: str) -> WorkflowIdentity:
    return WorkflowIdentity("task-1", workflow, version, step, f"run-{step}")


def test_workflow_scope_matches_every_step_of_exact_version():
    scope = ExperimentScope("fp:x", "pipeline", "v1")

    assert scope.key == "fp:x|pipeline@v1"
    assert scope.matches_identity(_identity("pipeline", "v1", "draft"))
    assert scope.matches_identity(_identity("pipeline", "v1", "review"))
    assert not scope.matches_identity(_identity("pipeline", "v2", "draft"))
    assert not scope.matches_identity(_identity("other", "v1", "draft"))
    assert not scope.matches_identity(None)


def test_step_scope_still_matches_only_one_exact_step():
    scope = ExperimentScope("fp:x", "pipeline", "v1", "draft")

    assert scope.key == "fp:x|pipeline@v1/draft"
    assert scope.matches_identity(_identity("pipeline", "v1", "draft"))
    assert not scope.matches_identity(_identity("pipeline", "v1", "review"))
