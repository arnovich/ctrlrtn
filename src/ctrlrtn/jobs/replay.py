"""Durable offline replay-evaluation job handler."""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from ctrlrtn.analysis.campaign import replay_report_json
from ctrlrtn.eval.dataset_manifest import (
    verify_dataset_bindings,
    verify_dataset_manifest,
)
from ctrlrtn.eval.live import (
    JUDGE_TIMEOUT,
    REPLAY_TIMEOUT,
    anthropic_judge_fn,
    anthropic_replay_fn,
)
from ctrlrtn.eval.replay import ReplaySample, run_replay
from ctrlrtn.jobs.models import Job

KIND = "replay_eval"


@dataclass(frozen=True)
class ReplayJobPlan:
    """A validated replay evaluation frozen before any spend: the ``Job`` to
    queue, the exact rows it will replay, and the unit and call counts an
    operator confirms against."""

    job: Job
    rows: list[dict]
    units: int
    untasked: int
    replay_calls: int
    judge_calls: int

    @property
    def total_calls(self) -> int:
        return self.replay_calls + self.judge_calls


def prepare_replay_job(
    store,
    *,
    use_case: str,
    candidate_model: str,
    baseline_model: str | None,
    judge_model: str,
    margin: float,
    limit: int,
    replicates: int,
    max_tokens: int | None,
    workflow: str | None = None,
    workflow_version: str | None = None,
    step: str | None = None,
    dataset_manifest: dict | None = None,
) -> ReplayJobPlan:
    """Validate and freeze an offline replay plan without spending or writing."""
    if not use_case.strip():
        raise ValueError("use-case must not be empty")
    if not candidate_model.strip():
        raise ValueError("candidate model must not be empty")
    if not judge_model.strip():
        raise ValueError("judge model must not be empty")
    if margin <= 0:
        raise ValueError("margin must be > 0")
    if replicates < 2 or replicates % 2 != 0:
        raise ValueError("replicates must be an even number >= 2")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if max_tokens is not None and max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")
    dataset_entries = None
    dataset_provenance = None
    if dataset_manifest is not None:
        verified = verify_dataset_manifest(dataset_manifest)
        if verified["use_case"] != use_case:
            raise ValueError("dataset manifest use-case does not match replay")
        manifest_scope = verified["scope"]
        requested_scope = (
            {
                "workflow": workflow,
                "workflow_version": workflow_version,
                "step": step,
            }
            if workflow is not None
            else None
        )
        if manifest_scope != requested_scope:
            raise ValueError("dataset manifest scope does not match replay")
        all_ids = [entry["trace_id"] for entry in verified["entries"]]
        all_rows = store.dataset_rows_by_ids(all_ids)
        verify_dataset_manifest(verified, all_rows)
        dataset_entries = [
            entry
            for entry in verified["entries"]
            if entry["split"] == "evaluation"
        ]
        evaluation_ids = {entry["trace_id"] for entry in dataset_entries}
        rows = [row for row in all_rows if row["id"] in evaluation_ids]
        dataset_provenance = {
            "manifest_sha256": verified["manifest_sha256"],
            "split": "evaluation",
            "n_traces": len(rows),
        }
    else:
        rows = store.requests_for_use_case(
            use_case,
            limit,
            workflow=workflow,
            workflow_version=workflow_version,
            step=step,
        )
    if not rows:
        raise ValueError(
            f"no replayable successful calls for use-case {use_case!r}"
        )
    baseline = baseline_model or store.use_case_models().get(use_case)
    if not baseline:
        raise ValueError("could not infer the baseline model")
    if baseline == candidate_model:
        raise ValueError("baseline and candidate models must differ")
    units = len({row["task_id"] for row in rows if row["task_id"] is not None})
    untasked = sum(1 for row in rows if row["task_id"] is None)
    units += untasked
    job = Job(
        KIND,
        {
            "version": 3,
            "use_case": use_case,
            "baseline_model": baseline,
            "candidate_model": candidate_model,
            "judge_model": judge_model,
            "margin": margin,
            "replicates": replicates,
            "max_tokens": max_tokens,
            "trace_ids": [row["id"] for row in rows],
            "workflow": workflow,
            "workflow_version": workflow_version,
            "step": step,
            "dataset": dataset_provenance,
            "dataset_bindings": dataset_entries,
        },
        progress_total=len(rows),
        progress_message="queued",
    )
    return ReplayJobPlan(
        job=job,
        rows=rows,
        units=units,
        untasked=untasked,
        replay_calls=len(rows) * 2,
        judge_calls=len(rows) * replicates,
    )


def run_replay_job(context, config: dict) -> dict:
    """Execute a frozen replay sample and return its evidence artifact.

    Credentials deliberately come from the worker environment and never from
    persisted job configuration.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("Set ANTHROPIC_API_KEY in the worker environment")
    trace_ids = config["trace_ids"]
    if config.get("dataset_bindings") is not None:
        rows = context.store.dataset_rows_by_ids(trace_ids)
        verify_dataset_bindings(config["dataset_bindings"], rows)
    else:
        rows = context.store.requests_by_ids(trace_ids)
    samples = [
        ReplaySample(request_body=row["request_body"], cluster=row["task_id"])
        for row in rows
    ]
    context.progress(0, len(samples), "starting replay")
    with (
        httpx.Client(timeout=REPLAY_TIMEOUT) as replay_http,
        httpx.Client(timeout=JUDGE_TIMEOUT) as judge_http,
    ):
        report = run_replay(
            samples,
            baseline_model=config["baseline_model"],
            candidate_model=config["candidate_model"],
            replay_fn=anthropic_replay_fn(
                api_key,
                client=replay_http,
                max_tokens=config.get("max_tokens"),
            ),
            judge_fn=anthropic_judge_fn(
                api_key,
                client=judge_http,
                model=config["judge_model"],
            ),
            margin=config["margin"],
            replicates=config["replicates"],
            progress_fn=context.progress,
        )
    scope = (
        {
            "workflow": config["workflow"],
            "workflow_version": config["workflow_version"],
            "step": config["step"],
        }
        if config.get("workflow")
        else None
    )
    return replay_report_json(
        report, config["use_case"], scope=scope, dataset=config.get("dataset")
    )
