"""Experiment analysis, calibration, replay, and campaign CLI commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from typing import Any, NoReturn

import httpx

from ctrlrtn.analysis.campaign import (
    replay_report_json,
)
from ctrlrtn.cli.render import (
    render_replay,
)
from ctrlrtn.eval.calibration import (
    ALIGNED,
    INSUFFICIENT,
    MISALIGNED,
)
from ctrlrtn.eval.dataset_manifest import verify_dataset_manifest
from ctrlrtn.eval.live import JUDGE_TIMEOUT, REPLAY_TIMEOUT
from ctrlrtn.eval.ni import _MIN_UNITS as MIN_NI_UNITS
from ctrlrtn.eval.replay import (
    ReplaySample,
    run_replay,
)
from ctrlrtn.eval.tripwire import (
    GROSS_REGRESSION,
    INCONCLUSIVE,
    NO_DATA,
    NO_GROSS_REGRESSION,
    NOT_EXERCISED,
    UNDERPOWERED,
)
from ctrlrtn.jobs.replay import prepare_replay_job
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]
StoreFactory = Callable[..., SqliteTraceStore]
LiveFunctionFactory = Callable[..., Callable[..., Any]]

_TRIPWIRE_EXIT = {
    NO_GROSS_REGRESSION: 0,
    GROSS_REGRESSION: 1,
    INCONCLUSIVE: 3,
    UNDERPOWERED: 3,
    NOT_EXERCISED: 4,
    NO_DATA: 4,
}
_CALIBRATION_EXIT = {ALIGNED: 0, MISALIGNED: 1, INSUFFICIENT: 3}


class ReplayEvaluationCommands:
    """Run foreground or durable replay evaluation workflows."""

    def _replay_eval(self, args: argparse.Namespace) -> None:
        if args.background and args.json_out:
            self._fail(
                "--background cannot write --json directly; after completion "
                "use `ctrlrtn jobs export <job-id> <path>`."
            )
        dataset_manifest = None
        workflow = args.workflow
        workflow_version = args.workflow_version
        step = args.step
        limit = args.limit if args.limit is not None else 50
        if args.dataset_manifest:
            if args.limit is not None:
                self._fail("--limit cannot be combined with --dataset-manifest")
            try:
                with open(args.dataset_manifest, encoding="utf-8") as handle:
                    dataset_manifest = verify_dataset_manifest(
                        json.load(handle)
                    )
            except (OSError, ValueError) as exc:
                self._fail(f"could not read dataset manifest: {exc}")
            manifest_scope = dataset_manifest["scope"]
            explicit_scope = (
                {
                    "workflow": workflow,
                    "workflow_version": workflow_version,
                    "step": step,
                }
                if any((workflow, workflow_version, step))
                else None
            )
            if explicit_scope is not None and explicit_scope != manifest_scope:
                self._fail(
                    "explicit replay scope does not match dataset manifest"
                )
            if manifest_scope:
                workflow = manifest_scope["workflow"]
                workflow_version = manifest_scope["workflow_version"]
                step = manifest_scope["step"]
        if args.json_out:
            parent = os.path.dirname(args.json_out) or "."
            if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
                self._fail(f"--json path is not writable: {args.json_out}")
        store = self._store_factory(self._database_path())
        try:
            try:
                plan = prepare_replay_job(
                    store,
                    use_case=args.use_case,
                    candidate_model=args.candidate,
                    baseline_model=args.baseline,
                    judge_model=args.judge_model,
                    margin=args.margin,
                    limit=limit,
                    replicates=args.replicates,
                    max_tokens=args.max_tokens,
                    workflow=workflow,
                    workflow_version=workflow_version,
                    step=step,
                    dataset_manifest=dataset_manifest,
                )
            except ValueError as exc:
                self._fail(str(exc))
            rows = plan.rows
            baseline = plan.job.config["baseline_model"]
            count = len(rows)
            print(
                f"Plan: replay {count} recorded inputs on 2 arms "
                f"({plan.replay_calls} calls) + judge ({plan.judge_calls} calls "
                f"with {args.judge_model}) = {plan.total_calls} upstream calls "
                "billed to your key."
            )
            if plan.job.config.get("dataset"):
                print(
                    "Dataset: verified evaluation split from manifest "
                    f"{plan.job.config['dataset']['manifest_sha256']}."
                )
            if plan.units < MIN_NI_UNITS:
                print(
                    f"WARNING: these {count} inputs span only {plan.units} "
                    f"independent task(s); the NI test needs >= {MIN_NI_UNITS}, "
                    "so this batch WILL conclude UNDERPOWERED. Record more "
                    "tasks before spending.",
                    file=sys.stderr,
                )
            if plan.untasked:
                print(
                    f"Note: {plan.untasked}/{count} sampled inputs are untagged; "
                    "replay treats each as an independent unit (the paired test "
                    "stays valid). If one run produced several of them, tag with "
                    "x-ctrlrtn-task so they cluster — see `ctrlrtn propagation`.",
                    file=sys.stderr,
                )
            if not args.yes:
                print("Dry run. Re-run with --yes to spend.")
                return
            if args.background:
                job = plan.job
                store.create_job(job)
                print(
                    f"Queued {job.job_id}. Run `ctrlrtn worker`; monitor it "
                    "with `ctrlrtn console` or `ctrlrtn jobs list`."
                )
                return
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                self._fail("Set ANTHROPIC_API_KEY to run a live replay eval.")
            samples = [
                ReplaySample(
                    request_body=row["request_body"], cluster=row["task_id"]
                )
                for row in rows
            ]
            with (
                httpx.Client(timeout=REPLAY_TIMEOUT) as replay_http,
                httpx.Client(timeout=JUDGE_TIMEOUT) as judge_http,
            ):
                report = run_replay(
                    samples,
                    baseline_model=baseline,
                    candidate_model=args.candidate,
                    replay_fn=self._replay_function_factory(
                        api_key,
                        client=replay_http,
                        max_tokens=args.max_tokens,
                    ),
                    judge_fn=self._judge_function_factory(
                        api_key,
                        client=judge_http,
                        model=args.judge_model,
                    ),
                    margin=args.margin,
                    replicates=args.replicates,
                )
            print(render_replay(report))
        finally:
            store.close()
        if args.json_out:
            try:
                with open(args.json_out, "w", encoding="utf-8") as handle:
                    scope = (
                        {
                            "workflow": workflow,
                            "workflow_version": workflow_version,
                            "step": step,
                        }
                        if workflow
                        else None
                    )
                    json.dump(
                        replay_report_json(
                            report,
                            args.use_case,
                            scope=scope,
                            dataset=plan.job.config.get("dataset"),
                        ),
                        handle,
                    )
                    handle.write("\n")
            except OSError as exc:
                self._fail(f"eval ran but --json write failed: {exc}")
            print(f"Wrote {args.json_out}.")
        if report.result.non_inferior:
            raise SystemExit(0)
        raise SystemExit(3 if report.result.underpowered else 1)
