"""Dataset lineage CLI commands: freeze and verify replay manifests."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import NoReturn

from ctrlrtn.eval.dataset_manifest import (
    create_dataset_manifest,
    verify_dataset_manifest,
)
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore

DatabasePath = Callable[[], str]
Fail = Callable[[str], NoReturn]
WriteJson = Callable[[str, dict, str], None]


class DatasetCommands:
    """Handlers for inert dataset manifests used by held-out replay evals."""

    def __init__(
        self,
        database_path: DatabasePath,
        fail: Fail,
        write_json: WriteJson,
    ) -> None:
        self._database_path = database_path
        self._fail = fail
        self._write_json = write_json

    def _dataset_create(self, args: argparse.Namespace) -> None:
        if args.limit < 1:
            self._fail("--limit must be at least 1")
        scope = (args.workflow, args.workflow_version, args.step)
        if any(scope) and not all(scope):
            self._fail(
                "--workflow, --workflow-version, and --step are required together"
            )
        store = SqliteTraceStore(self._database_path(), read_only=True)
        try:
            rows = store.dataset_rows_for_use_case(
                args.use_case,
                args.limit,
                workflow=args.workflow,
                workflow_version=args.workflow_version,
                step=args.step,
            )
        finally:
            store.close()
        manifest = create_dataset_manifest(
            rows,
            use_case=args.use_case,
            train_percent=args.train_percent,
            salt=args.salt,
            workflow=args.workflow,
            workflow_version=args.workflow_version,
            step=args.step,
        )
        self._write_json(args.output, manifest, "dataset manifest")
        counts = manifest["counts"]
        print(
            f"Wrote lineage-only manifest {args.output}: {counts['traces']} traces, "
            f"{counts['train_clusters']} train cluster(s), "
            f"{counts['evaluation_clusters']} evaluation cluster(s)."
        )
        print("No request or response payloads were exported.")

    def _dataset_verify(self, args: argparse.Namespace) -> None:
        try:
            with open(args.path, encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, ValueError) as exc:
            self._fail(f"could not read dataset manifest: {exc}")
        verified = verify_dataset_manifest(manifest)
        trace_ids = [item["trace_id"] for item in verified["entries"]]
        store = SqliteTraceStore(self._database_path(), read_only=True)
        try:
            rows = store.dataset_rows_by_ids(trace_ids)
        finally:
            store.close()
        verify_dataset_manifest(verified, rows)
        print(
            f"Verified {args.path}: {len(trace_ids)} exact live trace payload "
            "binding(s), with disjoint task-clustered train/evaluation splits."
        )
