"""Immutable, inert operator proposals derived from discovered workflow families."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from ctrlrtn.workflow.discovery import DiscoveredWorkflowFamily

VERSION = 1
KIND = "workflow-family-identification-proposal"
AUTHORITY = "analysis_only_no_routing_or_execution"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


class WorkflowProposalError(ValueError):
    """Raised when a workflow proposal cannot be built, written or loaded."""

    pass


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise WorkflowProposalError(f"{label} must be a stable identifier")
    return value


def _step_name(label: str, used: set[str]) -> str:
    base = label.split("[", 1)[0]
    if base.startswith("tag:"):
        base = base[4:]
    base = re.sub(r"[^A-Za-z0-9._:/-]+", "-", base).strip("-").lower()
    base = base or "observed-step"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _digest(document: dict) -> str:
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_workflow_proposal(
    family: DiscoveredWorkflowFamily,
    workflow: str,
    workflow_version: str,
    *,
    created_at: datetime | None = None,
) -> dict:
    """Create a review artifact; it cannot be loaded as control configuration."""
    workflow = _identifier(workflow, "workflow")
    workflow_version = _identifier(workflow_version, "workflow version")
    checked_at = created_at or datetime.now(UTC)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    used: set[str] = set()
    names = {node.label: _step_name(node.label, used) for node in family.nodes}
    predecessors: dict[str, set[str]] = {name: set() for name in names.values()}
    for edge in family.edges:
        source, target = names[edge.source], names[edge.target]
        if source != target:
            predecessors[target].add(source)
    steps = {}
    for node in family.nodes:
        name = names[node.label]
        definition: dict[str, object] = {}
        if predecessors[name]:
            definition["predecessors"] = sorted(predecessors[name])
        allows = {}
        if node.runs > node.tasks:
            allows["retry"] = True
        if allows:
            definition["allows"] = allows
        steps[name] = definition
    document = {
        "version": VERSION,
        "kind": KIND,
        "authority": AUTHORITY,
        "created_at": checked_at.astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "family": {
            "family_id": family.family_id,
            "support": family.support,
            "variants": family.variants,
            "cohesion": family.cohesion,
            "linked_task_rate": family.linked_task_rate,
            "representative_task_digest": family.representative_task_digest,
            "member_task_digests": list(family.member_task_digests),
            "nodes": [
                {
                    "observed_label": node.label,
                    "proposed_step": names[node.label],
                    "tasks": node.tasks,
                    "runs": node.runs,
                }
                for node in family.nodes
            ],
            "edges": [
                {
                    "source": names[edge.source],
                    "target": names[edge.target],
                    "kind": edge.kind,
                    "tasks": edge.tasks,
                    "occurrences": edge.occurrences,
                }
                for edge in family.edges
            ],
        },
        "proposed_control_fragment": {
            "workflows": {workflow: {workflow_version: {"steps": steps}}}
        },
        "review": {
            "required": True,
            "notes": "Rename steps and review every inferred predecessor before merging into routing config.",
        },
    }
    document["artifact_sha256"] = _digest(document)
    return document


def write_workflow_proposal(path: str | Path, proposal: dict) -> None:
    target = Path(path)
    try:
        with target.open("x", encoding="utf-8") as handle:
            json.dump(proposal, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError:
        raise WorkflowProposalError(
            f"workflow proposal already exists: {target}"
        ) from None
    except OSError as exc:
        raise WorkflowProposalError(
            f"could not write workflow proposal: {exc}"
        ) from None


def load_workflow_proposal(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowProposalError(
            f"invalid workflow proposal: {exc}"
        ) from None
    if not isinstance(value, dict):
        raise WorkflowProposalError("workflow proposal must be an object")
    digest = value.get("artifact_sha256")
    unsigned = {
        key: item for key, item in value.items() if key != "artifact_sha256"
    }
    if value.get("version") != VERSION or value.get("kind") != KIND:
        raise WorkflowProposalError(
            "workflow proposal version or kind is invalid"
        )
    if value.get("authority") != AUTHORITY:
        raise WorkflowProposalError("workflow proposal authority is invalid")
    if not isinstance(digest, str) or digest != _digest(unsigned):
        raise WorkflowProposalError("workflow proposal digest mismatch")
    fragment = value.get("proposed_control_fragment")
    if not isinstance(fragment, dict) or not isinstance(
        fragment.get("workflows"), dict
    ):
        raise WorkflowProposalError(
            "workflow proposal control fragment is invalid"
        )
    return value
