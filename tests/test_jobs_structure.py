"""Architecture guards for decomposed durable jobs and replay execution."""

import ast
import inspect

import ctrlrtn.jobs as facade
import ctrlrtn.replay_job as replay_facade


def test_jobs_package_is_an_import_facade():
    tree = ast.parse(inspect.getsource(facade))
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )
    assert facade.__all__ == [
        "CANCELLED",
        "FAILED",
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "TERMINAL",
        "Job",
        "JobCancelled",
        "JobContext",
        "JobHandler",
        "Worker",
        "new_job_id",
    ]


def test_job_capabilities_stay_in_their_modules():
    assert facade.Job.__module__ == "ctrlrtn.jobs.models"
    assert facade.JobContext.__module__ == "ctrlrtn.jobs.context"
    assert facade.Worker.__module__ == "ctrlrtn.jobs.worker"


def test_replay_job_preserves_its_compatibility_surface():
    tree = ast.parse(inspect.getsource(replay_facade))
    assert not any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )
    assert replay_facade.__all__ == [
        "KIND",
        "ReplayJobPlan",
        "prepare_replay_job",
        "run_replay_job",
    ]
    assert replay_facade.ReplayJobPlan.__module__ == ("ctrlrtn.jobs.replay")
    assert replay_facade.run_replay_job.__module__ == ("ctrlrtn.jobs.replay")
