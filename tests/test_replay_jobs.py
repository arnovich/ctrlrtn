"""Background replay submission, execution, monitoring, and export."""

from __future__ import annotations

import json

import httpx
import pytest

from ctrlrtn.cli import commands as cli
from ctrlrtn.cli.commands import main
from ctrlrtn.eval.dataset_manifest import create_dataset_manifest
from ctrlrtn.jobs import Job, Worker
from ctrlrtn.jobs import replay as replay_job
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace


def _seed(
    store,
    *,
    use_case="tag:editor",
    model="base",
    workflow=None,
    workflow_version=None,
    step=None,
    task_id="task:1",
):
    store._insert(
        Trace(
            method="POST",
            path="/v1/messages",
            query="",
            request_headers={},
            request_body=json.dumps(
                {
                    "model": model,
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "edit"}],
                }
            ).encode(),
            status_code=200,
            response_headers={},
            response_body=b"{}",
            latency_ms=1,
            model=model,
            use_case_key=use_case,
            task_id=task_id,
            workflow=workflow,
            workflow_version=workflow_version,
            step=step,
        )
    )


def test_background_replay_freezes_only_the_exact_step_scope(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "jobs.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    _seed(
        store,
        workflow="pipeline",
        workflow_version="v1",
        step="draft",
    )
    _seed(
        store,
        workflow="pipeline",
        workflow_version="v1",
        step="review",
        task_id="task:2",
    )
    draft_id = store.requests_for_use_case(
        "tag:editor",
        workflow="pipeline",
        workflow_version="v1",
        step="draft",
    )[0]["id"]
    store.close()

    main(
        [
            "replay-eval",
            "tag:editor",
            "candidate",
            "--baseline",
            "base",
            "--workflow",
            "pipeline",
            "--workflow-version",
            "v1",
            "--step",
            "draft",
            "--background",
            "--yes",
        ]
    )
    job = SqliteTraceStore(path).jobs()[0]
    assert job.config["trace_ids"] == [draft_id]
    assert job.config["step"] == "draft"


def test_background_replay_freezes_trace_ids_without_credentials(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "jobs.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = SqliteTraceStore(path)
    _seed(store)
    trace_id = store.recent(1)[0]["id"]
    store.close()

    main(
        [
            "replay-eval",
            "tag:editor",
            "candidate",
            "--baseline",
            "base",
            "--background",
            "--yes",
        ]
    )

    assert "Queued job:" in capsys.readouterr().out
    store = SqliteTraceStore(path)
    job = store.jobs()[0]
    assert job.kind == replay_job.KIND
    assert job.config["trace_ids"] == [trace_id]
    assert "api_key" not in job.config
    assert job.progress_total == 1


def test_background_replay_uses_only_verified_manifest_evaluation_split(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "jobs.db")
    manifest_path = tmp_path / "dataset.json"
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    for task_id in ("task:a", "task:b", "task:c"):
        _seed(store, task_id=task_id)
    rows = store.dataset_rows_for_use_case("tag:editor")
    manifest = create_dataset_manifest(
        rows,
        use_case="tag:editor",
        train_percent=67,
        salt="replay-test",
    )
    manifest_path.write_text(json.dumps(manifest))
    evaluation_ids = {
        entry["trace_id"]
        for entry in manifest["entries"]
        if entry["split"] == "evaluation"
    }
    training_ids = {
        entry["trace_id"]
        for entry in manifest["entries"]
        if entry["split"] == "train"
    }
    store.close()

    main(
        [
            "replay-eval",
            "tag:editor",
            "candidate",
            "--baseline",
            "base",
            "--dataset-manifest",
            str(manifest_path),
            "--background",
            "--yes",
        ]
    )
    assert "verified evaluation split" in capsys.readouterr().out
    store = SqliteTraceStore(path)
    job = store.jobs()[0]
    assert set(job.config["trace_ids"]) == evaluation_ids
    assert not set(job.config["trace_ids"]) & training_ids
    assert job.config["dataset"] == {
        "manifest_sha256": manifest["manifest_sha256"],
        "split": "evaluation",
        "n_traces": len(evaluation_ids),
    }
    assert {item["trace_id"] for item in job.config["dataset_bindings"]} == (
        evaluation_ids
    )
    store.close()


def test_manifest_background_job_rechecks_payload_before_spending(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "jobs.db")
    store = SqliteTraceStore(path)
    for task_id in ("task:a", "task:b"):
        _seed(store, task_id=task_id)
    rows = store.dataset_rows_for_use_case("tag:editor")
    manifest = create_dataset_manifest(
        rows, use_case="tag:editor", train_percent=50, salt="worker-test"
    )
    plan = replay_job.prepare_replay_job(
        store,
        use_case="tag:editor",
        candidate_model="candidate",
        baseline_model="base",
        judge_model="judge",
        margin=1.0,
        limit=50,
        replicates=2,
        max_tokens=None,
        dataset_manifest=manifest,
    )
    store.create_job(plan.job)
    trace_id = plan.job.config["trace_ids"][0]
    store._conn.execute(
        "UPDATE traces SET request_body = ? WHERE id = ?",
        (b"changed", trace_id),
    )
    store._conn.commit()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")

    Worker(
        store,
        {replay_job.KIND: replay_job.run_replay_job},
        worker_id="worker:manifest",
    ).run_once()
    job = store.job(plan.job.job_id)
    assert job.status == "failed"
    assert "payload changed" in job.error
    store.close()


def test_manifest_replay_rejects_use_case_mismatch_before_queue(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "jobs.db")
    manifest_path = tmp_path / "dataset.json"
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    _seed(store, task_id="task:a")
    _seed(store, task_id="task:b")
    manifest = create_dataset_manifest(
        store.dataset_rows_for_use_case("tag:editor"),
        use_case="tag:editor",
        train_percent=50,
        salt="mismatch",
    )
    manifest_path.write_text(json.dumps(manifest))
    store.close()

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "replay-eval",
                "tag:other",
                "candidate",
                "--baseline",
                "base",
                "--dataset-manifest",
                str(manifest_path),
                "--background",
                "--yes",
            ]
        )
    assert "use-case does not match" in capsys.readouterr().err
    assert SqliteTraceStore(path).jobs() == []


def test_background_dry_run_does_not_queue(tmp_path, monkeypatch):
    path = str(tmp_path / "jobs.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    _seed(store)
    store.close()

    main(
        [
            "replay-eval",
            "tag:editor",
            "candidate",
            "--baseline",
            "base",
            "--background",
        ]
    )
    assert SqliteTraceStore(path).jobs() == []


def test_worker_command_runs_registered_replay_handler(tmp_path, monkeypatch):
    path = str(tmp_path / "jobs.db")
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    store.create_job(Job(replay_job.KIND, {}, job_id="job:a"))
    store.close()

    def handler(context, config):
        context.progress(1, 1, "done")
        return {"verdict": "UNDERPOWERED"}

    monkeypatch.setattr(cli, "run_replay_job", handler)
    main(["worker", "--once"])
    job = SqliteTraceStore(path).job("job:a")
    assert job.status == "succeeded"
    assert job.result == {"verdict": "UNDERPOWERED"}


def test_real_replay_handler_persists_evidence(tmp_path, monkeypatch):
    path = str(tmp_path / "jobs.db")
    store = SqliteTraceStore(path)
    _seed(store)
    trace_id = store.recent(1)[0]["id"]
    store.create_job(
        Job(
            replay_job.KIND,
            {
                "version": 1,
                "use_case": "tag:editor",
                "baseline_model": "base",
                "candidate_model": "candidate",
                "judge_model": "judge",
                "margin": 1.0,
                "replicates": 2,
                "max_tokens": None,
                "trace_ids": [trace_id],
            },
            job_id="job:a",
        )
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-not-persisted")

    def handler(request):
        payload = json.loads(request.content)
        prompt = payload["messages"][0]["content"]
        text = (
            '{"score_a": 8, "score_b": 8}'
            if "# Response A" in prompt
            else "good answer"
        )
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": text}]}
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        replay_job.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )
    Worker(
        store,
        {replay_job.KIND: replay_job.run_replay_job},
        worker_id="worker:a",
    ).run_once()

    job = store.job("job:a")
    assert job.status == "succeeded"
    assert job.result["use_case"] == "tag:editor"
    assert job.result["verdict"] == "UNDERPOWERED"
    assert job.progress_current == 1


def test_jobs_export_writes_completed_evidence(tmp_path, monkeypatch):
    path = str(tmp_path / "jobs.db")
    out = tmp_path / "evidence.json"
    monkeypatch.setenv("CTRLRTN_DB", path)
    store = SqliteTraceStore(path)
    store.create_job(Job("test", {}, job_id="job:a"))
    store.claim_job("worker:a", stale_before=0)
    store.complete_job("job:a", "worker:a", {"verdict": "NON_INFERIOR"})
    store.close()

    main(["jobs", "export", "job:a", str(out)])
    assert json.loads(out.read_text()) == {"verdict": "NON_INFERIOR"}
