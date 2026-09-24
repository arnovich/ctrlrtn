"""Inert dataset lineage manifests for future offline model experiments."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

VERSION = 1
KIND = "offline_dataset_lineage_no_payloads"
ALGORITHM = "cluster_sha256_rank_v1"


class DatasetManifestError(ValueError):
    """Invalid, altered, overlapping, or unavailable dataset lineage."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as exc:
        raise DatasetManifestError("manifest must be canonical JSON") from exc


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise DatasetManifestError(f"{label} must be a non-empty string")
    return value


def _cluster(row: dict) -> str:
    task_id = row.get("task_id")
    source = f"task:{task_id}" if task_id is not None else f"trace:{row['id']}"
    return _digest(source.encode())


def create_dataset_manifest(
    rows: list[dict],
    *,
    use_case: str,
    train_percent: int,
    salt: str,
    workflow: str | None = None,
    workflow_version: str | None = None,
    step: str | None = None,
    created_at: datetime | None = None,
) -> dict:
    """Freeze payload hashes and leakage-safe task-cluster partitions."""
    use_case = _identifier(use_case, "use_case")
    salt = _identifier(salt, "salt")
    if isinstance(train_percent, bool) or not 1 <= train_percent <= 99:
        raise DatasetManifestError("train_percent must be between 1 and 99")
    scope_values = (workflow, workflow_version, step)
    if any(value is not None for value in scope_values) and not all(
        isinstance(value, str) and value for value in scope_values
    ):
        raise DatasetManifestError(
            "workflow, workflow_version, and step must be supplied together"
        )
    if not rows:
        raise DatasetManifestError("dataset requires at least two clusters")

    clusters: dict[str, list[dict]] = {}
    seen_ids: set[int] = set()
    for row in rows:
        trace_id = row.get("id")
        if (
            isinstance(trace_id, bool)
            or not isinstance(trace_id, int)
            or trace_id < 1
            or trace_id in seen_ids
        ):
            raise DatasetManifestError(
                "dataset trace IDs must be unique integers"
            )
        seen_ids.add(trace_id)
        request = row.get("request_body")
        response = row.get("response_body")
        if not isinstance(request, bytes) or not isinstance(response, bytes):
            raise DatasetManifestError(
                f"trace {trace_id} payload is unavailable or pruned"
            )
        clusters.setdefault(_cluster(row), []).append(row)
    if len(clusters) < 2:
        raise DatasetManifestError("dataset requires at least two clusters")

    ranked = sorted(
        clusters, key=lambda cluster: _digest(f"{salt}:{cluster}".encode())
    )
    train_count = max(
        1, min(len(ranked) - 1, math.floor(len(ranked) * train_percent / 100))
    )
    train_clusters = set(ranked[:train_count])
    entries = []
    for row in sorted(rows, key=lambda item: item["id"]):
        cluster = _cluster(row)
        entries.append(
            {
                "trace_id": row["id"],
                "cluster_sha256": cluster,
                "split": "train" if cluster in train_clusters else "evaluation",
                "request_sha256": _digest(row["request_body"]),
                "response_sha256": _digest(row["response_body"]),
            }
        )

    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise DatasetManifestError("created_at must include a timezone")
    core = {
        "version": VERSION,
        "kind": KIND,
        "purpose": "local_model_experiment_lineage_only",
        "created_at": timestamp.astimezone(timezone.utc).isoformat(),
        "use_case": use_case,
        "scope": (
            {
                "workflow": workflow,
                "workflow_version": workflow_version,
                "step": step,
            }
            if workflow is not None
            else None
        ),
        "partition": {
            "algorithm": ALGORITHM,
            "train_percent": train_percent,
            "salt": salt,
        },
        "counts": {
            "traces": len(entries),
            "clusters": len(clusters),
            "train_traces": sum(item["split"] == "train" for item in entries),
            "evaluation_traces": sum(
                item["split"] == "evaluation" for item in entries
            ),
            "train_clusters": len(train_clusters),
            "evaluation_clusters": len(clusters) - len(train_clusters),
        },
        "entries": entries,
    }
    return {**core, "manifest_sha256": _digest(_canonical(core))}


def verify_dataset_manifest(
    manifest: object, rows: list[dict] | None = None
) -> dict:
    """Verify artifact semantics and, when supplied, live payload bindings."""
    if not isinstance(manifest, dict):
        raise DatasetManifestError("manifest must be a JSON object")
    allowed = {
        "version",
        "kind",
        "purpose",
        "created_at",
        "use_case",
        "scope",
        "partition",
        "counts",
        "entries",
        "manifest_sha256",
    }
    if set(manifest) != allowed:
        raise DatasetManifestError("manifest fields are incomplete or unknown")
    core = {
        key: value
        for key, value in manifest.items()
        if key != "manifest_sha256"
    }
    if manifest.get("manifest_sha256") != _digest(_canonical(core)):
        raise DatasetManifestError(
            "manifest digest does not match its contents"
        )
    if manifest.get("version") != VERSION or manifest.get("kind") != KIND:
        raise DatasetManifestError(
            "unsupported dataset manifest version or kind"
        )
    if manifest.get("purpose") != "local_model_experiment_lineage_only":
        raise DatasetManifestError("manifest grants an unsupported purpose")
    _identifier(manifest.get("use_case"), "use_case")
    try:
        created = datetime.fromisoformat(manifest["created_at"])
    except (TypeError, ValueError) as exc:
        raise DatasetManifestError("created_at must be RFC3339") from exc
    if created.tzinfo is None:
        raise DatasetManifestError("created_at must include a timezone")
    partition = manifest.get("partition")
    if not isinstance(partition, dict) or set(partition) != {
        "algorithm",
        "train_percent",
        "salt",
    }:
        raise DatasetManifestError("partition is invalid")
    if partition["algorithm"] != ALGORITHM:
        raise DatasetManifestError("partition algorithm is unsupported")
    train_percent = partition["train_percent"]
    if (
        isinstance(train_percent, bool)
        or not isinstance(train_percent, int)
        or not 1 <= train_percent <= 99
    ):
        raise DatasetManifestError("partition train_percent is invalid")
    salt = _identifier(partition["salt"], "partition.salt")
    scope = manifest.get("scope")
    if scope is not None and (
        not isinstance(scope, dict)
        or set(scope) != {"workflow", "workflow_version", "step"}
        or not all(isinstance(value, str) and value for value in scope.values())
    ):
        raise DatasetManifestError("scope is invalid")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise DatasetManifestError("entries must be a list")
    live_by_id = {row["id"]: row for row in rows} if rows is not None else None
    rebuilt_rows = []
    seen_ids: set[int] = set()
    cluster_splits: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "trace_id",
            "cluster_sha256",
            "split",
            "request_sha256",
            "response_sha256",
        }:
            raise DatasetManifestError("dataset entry is invalid")
        trace_id = entry["trace_id"]
        if (
            isinstance(trace_id, bool)
            or not isinstance(trace_id, int)
            or trace_id < 1
            or trace_id in seen_ids
        ):
            raise DatasetManifestError(
                "manifest trace IDs must be unique integers"
            )
        seen_ids.add(trace_id)
        for key in ("cluster_sha256", "request_sha256", "response_sha256"):
            value = entry[key]
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise DatasetManifestError(
                    f"entry {key} is not a SHA-256 digest"
                )
        split = entry["split"]
        if split not in {"train", "evaluation"}:
            raise DatasetManifestError("entry split is invalid")
        previous = cluster_splits.setdefault(entry["cluster_sha256"], split)
        if previous != split:
            raise DatasetManifestError(
                "one cluster appears in both dataset splits"
            )
        if live_by_id is None:
            rebuilt_rows.append(entry)
            continue
        row = live_by_id.get(entry["trace_id"])
        if row is None:
            raise DatasetManifestError(
                f"trace {entry['trace_id']} is unavailable"
            )
        if not isinstance(row.get("request_body"), bytes) or not isinstance(
            row.get("response_body"), bytes
        ):
            raise DatasetManifestError(
                f"trace {entry['trace_id']} payload was pruned"
            )
        if (
            _digest(row["request_body"]) != entry["request_sha256"]
            or _digest(row["response_body"]) != entry["response_sha256"]
        ):
            raise DatasetManifestError(
                f"trace {entry['trace_id']} payload changed"
            )
        if _cluster(row) != entry["cluster_sha256"]:
            raise DatasetManifestError(
                f"trace {entry['trace_id']} cluster changed"
            )
        rebuilt_rows.append(row)

    if len(cluster_splits) < 2 or set(cluster_splits.values()) != {
        "train",
        "evaluation",
    }:
        raise DatasetManifestError(
            "manifest requires non-empty disjoint splits"
        )
    ranked = sorted(
        cluster_splits,
        key=lambda cluster: _digest(f"{salt}:{cluster}".encode()),
    )
    expected_train_count = max(
        1,
        min(
            len(ranked) - 1,
            math.floor(len(ranked) * train_percent / 100),
        ),
    )
    expected_train = set(ranked[:expected_train_count])
    if {
        cluster for cluster, split in cluster_splits.items() if split == "train"
    } != expected_train:
        raise DatasetManifestError("manifest cluster partition is inconsistent")
    expected_counts = {
        "traces": len(entries),
        "clusters": len(cluster_splits),
        "train_traces": sum(item["split"] == "train" for item in entries),
        "evaluation_traces": sum(
            item["split"] == "evaluation" for item in entries
        ),
        "train_clusters": expected_train_count,
        "evaluation_clusters": len(cluster_splits) - expected_train_count,
    }
    if manifest.get("counts") != expected_counts:
        raise DatasetManifestError("manifest counts are inconsistent")

    if live_by_id is not None:
        rebuilt = create_dataset_manifest(
            rebuilt_rows,
            use_case=manifest["use_case"],
            train_percent=train_percent,
            salt=salt,
            workflow=(manifest["scope"] or {}).get("workflow"),
            workflow_version=(manifest["scope"] or {}).get("workflow_version"),
            step=(manifest["scope"] or {}).get("step"),
            created_at=created,
        )
        if rebuilt != manifest:
            raise DatasetManifestError(
                "manifest partition or counts are inconsistent"
            )
    return manifest


def verify_dataset_bindings(entries: list[dict], rows: list[dict]) -> None:
    """Verify selected manifest entries against exact live trace payloads."""
    if not isinstance(entries, list) or not entries:
        raise DatasetManifestError("dataset bindings must not be empty")
    by_id = {row.get("id"): row for row in rows}
    if len(by_id) != len(rows):
        raise DatasetManifestError("live dataset rows contain duplicate IDs")
    for entry in entries:
        if not isinstance(entry, dict):
            raise DatasetManifestError("dataset binding is invalid")
        trace_id = entry.get("trace_id")
        row = by_id.get(trace_id)
        if row is None:
            raise DatasetManifestError(f"trace {trace_id} is unavailable")
        request = row.get("request_body")
        response = row.get("response_body")
        if not isinstance(request, bytes) or not isinstance(response, bytes):
            raise DatasetManifestError(f"trace {trace_id} payload was pruned")
        if _digest(request) != entry.get("request_sha256") or _digest(
            response
        ) != entry.get("response_sha256"):
            raise DatasetManifestError(f"trace {trace_id} payload changed")
        if _cluster(row) != entry.get("cluster_sha256"):
            raise DatasetManifestError(f"trace {trace_id} cluster changed")
