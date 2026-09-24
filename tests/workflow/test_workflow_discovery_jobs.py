"""Durable passive workflow discovery submission, execution, and artifacts."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ctrlrtn.cli import commands as cli
from ctrlrtn.jobs import CANCELLED, FAILED, SUCCEEDED, Worker
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.discovery_job import (
    KIND,
    WorkflowDiscoveryJobError,
    WorkflowDiscoveryScope,
    build_workflow_discovery_artifact,
    compare_workflow_discovery_artifacts,
    prepare_workflow_discovery_job,
    projections_from_workflow_discovery_artifact,
    render_workflow_discovery_comparison,
    report_from_workflow_discovery_artifact,
    run_workflow_discovery_job,
    verify_workflow_discovery_artifact,
)
from ctrlrtn.workflow.discovery_projection import (
    render_discovered_family_dag,
)


def _seed(store: SqliteTraceStore) -> None:
    for task_index, task_id in enumerate(("task:a", "task:b")):
        tool_id = f"tool-{task_index}"
        for call_index, (route, request, response) in enumerate(
            (
                (
                    "tag:researcher",
                    {"messages": [{"role": "user", "content": task_id}]},
                    {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": "search",
                            }
                        ]
                    },
                ),
                (
                    "tag:writer",
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                    }
                                ],
                            }
                        ]
                    },
                    {},
                ),
            )
        ):
            store._insert(
                Trace(
                    method="POST",
                    path="/v1/messages",
                    query="",
                    request_headers={},
                    request_body=json.dumps(request).encode(),
                    status_code=200,
                    response_headers={},
                    response_body=json.dumps(response).encode(),
                    latency_ms=1,
                    task_id=task_id,
                    use_case_key=route,
                    ts=float(task_index * 10 + call_index),
                )
            )


def _seed_provider_cohort(
    store: SqliteTraceStore,
    provider: str,
    model: str,
    offset: int,
    *,
    experiment_id: str | None = None,
    arm: str | None = None,
) -> None:
    for task_index in range(2):
        task_id = f"{provider}:{task_index}"
        tool_id = f"{provider}-tool-{task_index}"
        for call_index, (route, request, response) in enumerate(
            (
                (
                    "tag:researcher",
                    {"messages": [{"role": "user", "content": task_id}]},
                    {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": "search",
                            }
                        ]
                    },
                ),
                (
                    "tag:writer",
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                    }
                                ],
                            }
                        ]
                    },
                    {},
                ),
            )
        ):
            store._insert(
                Trace(
                    method="POST",
                    path="/v1/messages",
                    query="",
                    request_headers={},
                    request_body=json.dumps(request).encode(),
                    status_code=200,
                    response_headers={},
                    response_body=json.dumps(response).encode(),
                    latency_ms=1,
                    provider=provider,
                    model=model,
                    served_model=model,
                    experiment_id=experiment_id,
                    arm=arm,
                    task_id=task_id,
                    use_case_key=route,
                    ts=float(offset + task_index * 10 + call_index),
                )
            )


def test_job_freezes_inputs_survives_restart_and_builds_verified_artifact(
    tmp_path,
):
    path = str(tmp_path / "router.db")
    store = SqliteTraceStore(path)
    _seed(store)
    plan = prepare_workflow_discovery_job(store)
    store.create_job(plan.job)
    frozen_ids = plan.job.config["trace_ids"]
    assert len(frozen_ids) == 4
    assert len(plan.job.config["trace_bindings"]) == 4
    store.close()

    worker_store = SqliteTraceStore(path)
    assert Worker(
        worker_store,
        {KIND: run_workflow_discovery_job},
        worker_id="worker:discovery",
    ).run_once()
    worker_store.close()

    reader = SqliteTraceStore(path, read_only=True)
    job = reader.job(plan.job.job_id)
    reader.close()
    assert job.status == SUCCEEDED
    assert job.progress_current == job.progress_total
    artifact = verify_workflow_discovery_artifact(job.result)
    assert artifact["input"]["traces"] == 4
    assert "trace_ids" not in artifact["input"]
    report = report_from_workflow_discovery_artifact(artifact)
    assert report.families[0].support == 2
    assert len(report.assignments) == 2
    assert artifact["parameters"] == {
        "limit": 1000,
        "min_support": 2,
        "similarity": 0.75,
    }
    assert artifact["selection"]["selected_explicit_tasks"] == 2
    assert artifact["selection"]["selected_traces"] == 4
    assert artifact["selection"]["selection_strategy"] == (
        "most-recent-task-last-call/v1"
    )
    projection = projections_from_workflow_discovery_artifact(artifact)[0]
    assert projection.tasks == 2
    assert projection.calls == 4
    assert projection.unknown_outcomes == 2
    assert projection.tool_operations == (("search", 2),)
    assert projection.paths[0].tasks == 2
    assert projection.representative_timeline == (
        "tag:researcher[search]",
        "tag:writer",
    )
    assert len(projection.task_timelines) == 2
    assert all(item.outcome == "unknown" for item in projection.task_timelines)


def test_cli_renders_and_exports_discovered_family_projection(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "router.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    _seed(store)
    plan = prepare_workflow_discovery_job(store)
    store.create_job(plan.job)
    Worker(store, {KIND: run_workflow_discovery_job}).run_once()
    job = store.job(plan.job.job_id)
    family_id = (
        report_from_workflow_discovery_artifact(job.result)
        .families[0]
        .family_id
    )
    store.close()

    cli.main(["workflow", "discovery-project", job.job_id, family_id])
    output = capsys.readouterr().out
    assert "path frequencies:" in output
    assert "unknown outcomes=2" in output
    assert "task timelines (digests only):" in output

    mermaid = tmp_path / "family.mmd"
    cli.main(
        [
            "workflow",
            "discovery-project",
            job.job_id,
            family_id,
            "--format",
            "mermaid",
            "--output",
            str(mermaid),
        ]
    )
    assert mermaid.read_text().startswith("flowchart TD")
    assert "Analysis-only inferred projection" in mermaid.read_text()

    exported = tmp_path / "family.json"
    cli.main(
        [
            "workflow",
            "discovery-project",
            job.job_id,
            family_id,
            "--format",
            "json",
            "--output",
            str(exported),
        ]
    )
    document = json.loads(exported.read_text())
    assert document["authority"] == "analysis_only_no_routing_or_execution"


def test_worker_rejects_changed_frozen_input(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    _seed(store)
    plan = prepare_workflow_discovery_job(store)
    store.create_job(plan.job)
    store._conn.execute(
        "UPDATE traces SET response_body = ? WHERE id = ?",
        (b"changed", plan.job.config["trace_ids"][0]),
    )
    store._conn.commit()

    Worker(
        store,
        {KIND: run_workflow_discovery_job},
        worker_id="worker:changed",
    ).run_once()

    job = store.job(plan.job.job_id)
    assert job.status == FAILED
    assert "changed" in job.error


def test_running_discovery_observes_cancellation(tmp_path, monkeypatch):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    _seed(store)
    plan = prepare_workflow_discovery_job(store)
    store.create_job(plan.job)

    def cancel_during_discovery(rows, **kwargs):
        assert store.request_job_cancel(plan.job.job_id)
        kwargs["progress"](0, 1, "cancel checkpoint")

    monkeypatch.setattr(
        "ctrlrtn.workflow.discovery_job.handler.discover_workflow_families",
        cancel_during_discovery,
    )
    Worker(
        store,
        {KIND: run_workflow_discovery_job},
        worker_id="worker:cancel",
    ).run_once()

    assert store.job(plan.job.job_id).status == CANCELLED


def test_artifact_tampering_is_detected(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    _seed(store)
    plan = prepare_workflow_discovery_job(store)
    store.create_job(plan.job)
    Worker(store, {KIND: run_workflow_discovery_job}).run_once()
    artifact = store.job(plan.job.job_id).result
    artifact["report"]["eligible_tasks"] = 999

    with pytest.raises(WorkflowDiscoveryJobError, match="digest mismatch"):
        verify_workflow_discovery_artifact(artifact)


def test_job_plan_enforces_processing_bounds(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    _seed(store)

    with pytest.raises(WorkflowDiscoveryJobError, match="must not exceed"):
        prepare_workflow_discovery_job(store, limit=2001)


def test_scale_selection_is_bounded_and_reports_denominators(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    for task_index in range(300):
        for call_index in range(2):
            store._insert(
                Trace(
                    method="POST",
                    path="/v1/messages",
                    query="",
                    request_headers={},
                    request_body=b"{}",
                    status_code=200,
                    response_headers={},
                    response_body=b"{}",
                    latency_ms=1,
                    task_id=f"scale:{task_index}",
                    use_case_key=(
                        None if task_index == 299 else f"tag:step-{call_index}"
                    ),
                    ts=float(task_index * 2 + call_index + 1),
                )
            )
    rows = store.workflow_discovery_inputs(40)
    diagnostics = store.workflow_discovery_input_diagnostics(rows)

    assert len(rows) == 80
    assert diagnostics.available_explicit_tasks == 300
    assert diagnostics.selected_explicit_tasks == 40
    assert diagnostics.truncated_explicit_tasks == 260
    assert diagnostics.unkeyable_selected_traces == 2
    assert diagnostics.selection_strategy == "most-recent-task-last-call/v1"


def test_discovery_indexes_exist_on_upgraded_store(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    indexes = {
        row[1] for row in store._conn.execute("PRAGMA index_list(traces)")
    }
    assert {
        "ix_traces_discovery_task",
        "ix_traces_discovery_last",
        "ix_traces_discovery_provider",
        "ix_traces_discovery_model",
        "ix_traces_discovery_experiment",
    } <= indexes


def test_retention_marks_completed_discovery_source_invalid(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "router.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    _seed(store)
    plan = prepare_workflow_discovery_job(store)
    store.create_job(plan.job)
    Worker(store, {KIND: run_workflow_discovery_job}).run_once()
    artifact = store.job(plan.job.job_id).result

    planned = store.prune_trace_payloads(100, apply=False)
    assert planned.invalidated_discovery_jobs == 1
    assert store.workflow_discovery_invalidation(plan.job.job_id) is None
    applied = store.prune_trace_payloads(100, apply=True)
    assert applied.invalidated_discovery_jobs == 1
    invalidation = store.workflow_discovery_invalidation(plan.job.job_id)
    assert invalidation["pruned_traces"] == 4
    assert verify_workflow_discovery_artifact(artifact) == artifact

    rows = store.workflow_discovery_inputs()
    diagnostics = store.workflow_discovery_input_diagnostics(rows)
    assert diagnostics.pruned_selected_traces == 4
    store.close()

    export = tmp_path / "stale.json"
    with pytest.raises(SystemExit, match="2"):
        cli.main(["jobs", "export", plan.job.job_id, str(export)])
    assert "source was invalidated" in capsys.readouterr().err
    cli.main(
        [
            "jobs",
            "export",
            plan.job.job_id,
            str(export),
            "--allow-invalidated",
        ]
    )
    assert "WARNING" in capsys.readouterr().out
    assert json.loads(export.read_text()) == artifact


def test_scoped_jobs_keep_whole_tasks_and_compare_one_dimension(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    _seed_provider_cohort(store, "anthropic", "claude", 0)
    _seed_provider_cohort(store, "openai", "gpt", 100)
    plans = []
    for provider in ("anthropic", "openai"):
        plan = prepare_workflow_discovery_job(
            store, scope=WorkflowDiscoveryScope(provider=provider)
        )
        assert plan.traces == 4
        assert plan.job.config["scope"]["provider"] == provider
        store.create_job(plan.job)
        Worker(store, {KIND: run_workflow_discovery_job}).run_once()
        plans.append(plan)

    left = store.job(plans[0].job.job_id).result
    right = store.job(plans[1].job.job_id).result
    assert left["scope"]["provider"] == "anthropic"
    comparison = compare_workflow_discovery_artifacts(left, right)
    assert comparison.scope_relationship == "providers"
    assert comparison.matches[0].similarity == 1.0
    assert (
        "scope relationship: providers"
        in render_workflow_discovery_comparison(comparison)
    )


def test_comparison_rejects_unrelated_scopes_without_override(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    _seed_provider_cohort(store, "anthropic", "claude", 0)
    _seed_provider_cohort(store, "openai", "gpt", 100)
    plans = [
        prepare_workflow_discovery_job(
            store, scope=WorkflowDiscoveryScope(provider="anthropic")
        ),
        prepare_workflow_discovery_job(
            store,
            scope=WorkflowDiscoveryScope(provider="openai", since=5.0),
        ),
    ]
    for plan in plans:
        store.create_job(plan.job)
        Worker(store, {KIND: run_workflow_discovery_job}).run_once()
    artifacts = [store.job(plan.job.job_id).result for plan in plans]

    with pytest.raises(WorkflowDiscoveryJobError, match="allow-unrelated"):
        compare_workflow_discovery_artifacts(*artifacts)
    comparison = compare_workflow_discovery_artifacts(
        *artifacts, allow_unrelated=True
    )
    assert comparison.scope_relationship == "unrelated"


def test_scope_validation_requires_ordered_window_and_experiment_for_arm(
    tmp_path,
):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    _seed(store)
    with pytest.raises(WorkflowDiscoveryJobError, match="before until"):
        prepare_workflow_discovery_job(
            store, scope=WorkflowDiscoveryScope(since=2, until=1)
        )
    with pytest.raises(WorkflowDiscoveryJobError, match="requires experiment"):
        prepare_workflow_discovery_job(
            store, scope=WorkflowDiscoveryScope(arm="candidate")
        )


def test_experiment_arm_scopes_are_explicitly_comparable(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    _seed_provider_cohort(
        store,
        "anthropic",
        "baseline-model",
        0,
        experiment_id="exp:workflow",
        arm="baseline",
    )
    _seed_provider_cohort(
        store,
        "anthropic-candidate",
        "candidate-model",
        100,
        experiment_id="exp:workflow",
        arm="candidate",
    )
    artifacts = []
    for arm in ("baseline", "candidate"):
        plan = prepare_workflow_discovery_job(
            store,
            scope=WorkflowDiscoveryScope(experiment_id="exp:workflow", arm=arm),
        )
        store.create_job(plan.job)
        Worker(store, {KIND: run_workflow_discovery_job}).run_once()
        artifacts.append(store.job(plan.job.job_id).result)
    comparison = compare_workflow_discovery_artifacts(*artifacts)
    assert comparison.scope_relationship == "experiment-arms"


def test_cli_queues_background_discovery_and_worker_runs_it(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "router.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    _seed(store)
    store.close()

    cli.main(["workflow", "discover", "--background"])
    assert "frozen trace(s)" in capsys.readouterr().out
    queued = SqliteTraceStore(path).jobs()[0]
    assert queued.kind == KIND

    cli.main(["worker", "--once"])
    assert SqliteTraceStore(path).job(queued.job_id).status == SUCCEEDED


def test_cli_queues_scoped_discovery(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "router.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    _seed_provider_cohort(store, "anthropic", "claude", 0)
    _seed_provider_cohort(store, "openai", "gpt", 100)
    store.close()

    cli.main(
        [
            "workflow",
            "discover",
            "--background",
            "--provider",
            "anthropic",
            "--model",
            "claude",
        ]
    )
    assert "4 frozen trace(s)" in capsys.readouterr().out
    job = SqliteTraceStore(path).jobs()[0]
    assert job.config["scope"]["provider"] == "anthropic"
    assert job.config["scope"]["model"] == "claude"


def test_snapshot_comparison_matches_shape_not_family_id_and_tracks_tasks(
    tmp_path,
):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    _seed(store)
    first_plan = prepare_workflow_discovery_job(store)
    store.create_job(first_plan.job)
    Worker(
        store, {KIND: run_workflow_discovery_job}, worker_id="worker:first"
    ).run_once()
    first = store.job(first_plan.job.job_id).result

    # Add one member to the known family and a distinct two-task family.
    for index, (task_id, routes) in enumerate(
        (
            ("task:c", ("tag:researcher", "tag:writer")),
            ("task:x", ("tag:planner", "tag:executor")),
            ("task:y", ("tag:planner", "tag:executor")),
        ),
        start=3,
    ):
        for call, route in enumerate(routes):
            tool_id = f"tool-{task_id}"
            request = (
                {
                    "messages": [
                        {
                            "content": [
                                {"type": "tool_result", "tool_use_id": tool_id}
                            ]
                        }
                    ]
                }
                if task_id == "task:c" and call == 1
                else {}
            )
            response = (
                {
                    "content": [
                        {"type": "tool_use", "id": tool_id, "name": "search"}
                    ]
                }
                if task_id == "task:c" and call == 0
                else {}
            )
            store._insert(
                Trace(
                    method="POST",
                    path="/v1/messages",
                    query="",
                    request_headers={},
                    request_body=json.dumps(request).encode(),
                    status_code=200,
                    response_headers={},
                    response_body=json.dumps(response).encode(),
                    latency_ms=1,
                    task_id=task_id,
                    use_case_key=route,
                    ts=float(index * 10 + call),
                )
            )
    second_plan = prepare_workflow_discovery_job(store)
    store.create_job(second_plan.job)
    Worker(
        store, {KIND: run_workflow_discovery_job}, worker_id="worker:second"
    ).run_once()
    second = store.job(second_plan.job.job_id).result

    comparison = compare_workflow_discovery_artifacts(first, second)

    assert comparison.compatible_parameters
    assert len(comparison.matches) == 1
    assert comparison.matches[0].previous_support == 2
    assert comparison.matches[0].current_support == 3
    assert len(comparison.added_families) == 1
    assert comparison.removed_families == ()
    assert comparison.retained_tasks == 2
    assert comparison.moved_tasks == 0
    assert comparison.added_tasks == 3
    assert comparison.removed_tasks == 0
    text = render_workflow_discovery_comparison(comparison)
    assert "support 2 -> 3" in text
    assert "3 added" in text

    previous_report = report_from_workflow_discovery_artifact(first)
    current_report = report_from_workflow_discovery_artifact(second)
    shared_digest = previous_report.assignments[0].task_digest
    moved_report = replace(
        current_report,
        assignments=tuple(
            (
                replace(item, family_id=comparison.added_families[0])
                if item.task_digest == shared_digest
                else item
            )
            for item in current_report.assignments
        ),
    )
    moved_artifact = build_workflow_discovery_artifact(
        moved_report,
        input_sha256=second["input"]["sha256"],
        input_traces=second["input"]["traces"],
        parameters=second["parameters"],
        created_at=second["created_at"],
    )
    moved = compare_workflow_discovery_artifacts(first, moved_artifact)
    assert moved.retained_tasks == 1
    assert moved.moved_tasks == 1


def test_cli_compares_completed_discovery_jobs(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "router.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    _seed(store)
    plans = [prepare_workflow_discovery_job(store) for _ in range(2)]
    for plan in plans:
        store.create_job(plan.job)
        Worker(store, {KIND: run_workflow_discovery_job}).run_once()
    store.close()

    cli.main(
        [
            "workflow",
            "discovery-compare",
            plans[0].job.job_id,
            plans[1].job.job_id,
        ]
    )

    assert "workflow discovery snapshot comparison" in capsys.readouterr().out


def test_discovered_family_dag_renders_weighted_nodes_and_variants(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "router.db"))
    _seed(store)
    plan = prepare_workflow_discovery_job(store)
    store.create_job(plan.job)
    Worker(store, {KIND: run_workflow_discovery_job}).run_once()
    artifact = store.job(plan.job.job_id).result
    store.close()

    projection = projections_from_workflow_discovery_artifact(artifact)[0]
    text = render_discovered_family_dag(projection)

    assert "INFERRED WORKFLOW DAG" in text
    assert "seen in 2/2 tasks" in text
    assert "2/2 tasks →" in text
    assert "OBSERVED VARIANTS" in text
    assert "no execution or routing authority" in text
