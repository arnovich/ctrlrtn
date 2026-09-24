"""Offline, evidence-only reconstruction of workflow data-flow edges."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

ALGORITHM = "tool-id-link/v1"


@dataclass(frozen=True)
class InferredWorkflowEdge:
    """One analysis-only data-flow edge reconstructed from tool-call IDs.

    It records its source and target trace IDs, the ``tool-id-link/v1``
    algorithm, a confidence, and whether explicit lifecycle facts later
    ``confirmed``, ``contradicted`` or left it ``unverified``. Only the
    SHA-256 digest of the tool ID is kept as evidence. Inferred edges are
    forbidden as routing, experiment or execution-control inputs.
    """

    edge_id: str
    task_id: str
    workflow: str
    workflow_version: str
    source_step_run_id: str
    target_step_run_id: str
    source_trace_id: int
    target_trace_id: int
    evidence_hash: str
    confidence: float
    confirmation: str
    algorithm: str = ALGORITHM


@dataclass(frozen=True)
class InferenceReport:
    """Inferred edges plus their accuracy against explicit ground truth.

    Precision and recall count only targets that have explicit lifecycle
    facts; ``ambiguous_evidence`` counts consumed tool IDs skipped because
    more than one producer matched.
    """

    edges: tuple[InferredWorkflowEdge, ...]
    explicit_edges: int
    true_positive: int
    false_positive: int
    false_negative: int
    ambiguous_evidence: int

    @property
    def precision(self) -> float | None:
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else None

    @property
    def recall(self) -> float | None:
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else None


def _json(raw: bytes | None) -> object:
    try:
        return json.loads(raw or b"")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _produced_tool_ids(raw: bytes | None) -> set[str]:
    body = _json(raw)
    if not isinstance(body, dict):
        return set()
    found: set[str] = set()
    for block in body.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            value = block.get("id")
            if isinstance(value, str) and value:
                found.add(value)
    for choice in body.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                value = call.get("id")
                if isinstance(value, str) and value:
                    found.add(value)
    return found


def _consumed_tool_ids(raw: bytes | None) -> set[str]:
    body = _json(raw)
    if not isinstance(body, dict):
        return set()
    found: set[str] = set()
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        value = message.get("tool_call_id")
        if isinstance(value, str) and value:
            found.add(value)
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                value = block.get("tool_use_id")
                if isinstance(value, str) and value:
                    found.add(value)
    return found


def infer_workflow_edges(
    traces: list[dict],
    explicit_dependencies: set[tuple[str, str, str]],
    explicit_step_runs: set[tuple[str, str]] | None = None,
) -> InferenceReport:
    """Infer exact tool-id links and score only explicitly instrumented tasks.

    ``explicit_dependencies`` contains ``(task_id, source_run, target_run)``.
    A target with lifecycle ground truth but no matching dependency contradicts
    an inference; targets without ground truth remain unverified.
    """
    producers: dict[tuple[str, str], list[dict]] = {}
    explicit_targets = explicit_step_runs or {
        (task, target) for task, _, target in explicit_dependencies
    }
    for trace in traces:
        task = trace.get("task_id")
        run = trace.get("step_run_id")
        if not task or not run:
            continue
        for tool_id in _produced_tool_ids(trace.get("response_body")):
            producers.setdefault((task, tool_id), []).append(trace)

    edges: list[InferredWorkflowEdge] = []
    ambiguous = 0
    seen: set[tuple[str, str, str, str]] = set()
    consumed: set[tuple[str, str]] = set()
    for target in traces:
        task = target.get("task_id")
        target_run = target.get("step_run_id")
        if not task or not target_run:
            continue
        for tool_id in _consumed_tool_ids(target.get("request_body")):
            evidence_key = (task, tool_id)
            if evidence_key in consumed:
                continue
            consumed.add(evidence_key)
            candidates = [
                row
                for row in producers.get((task, tool_id), [])
                if row["id"] < target["id"]
                and row.get("step_run_id") != target_run
                and row.get("workflow") == target.get("workflow")
                and row.get("workflow_version")
                == target.get("workflow_version")
            ]
            if len(candidates) != 1:
                ambiguous += bool(candidates)
                continue
            source = candidates[0]
            source_run = source["step_run_id"]
            evidence_hash = hashlib.sha256(tool_id.encode()).hexdigest()
            key = (task, source_run, target_run, evidence_hash)
            if key in seen:
                continue
            seen.add(key)
            explicit_key = (task, source_run, target_run)
            confirmation = (
                "confirmed"
                if explicit_key in explicit_dependencies
                else (
                    "contradicted"
                    if (task, target_run) in explicit_targets
                    else "unverified"
                )
            )
            edge_id = (
                "inf:"
                + hashlib.sha256(
                    f"{ALGORITHM}:{task}:{source_run}:{target_run}:{evidence_hash}".encode()
                ).hexdigest()[:32]
            )
            edges.append(
                InferredWorkflowEdge(
                    edge_id=edge_id,
                    task_id=task,
                    workflow=target["workflow"],
                    workflow_version=target["workflow_version"],
                    source_step_run_id=source_run,
                    target_step_run_id=target_run,
                    source_trace_id=source["id"],
                    target_trace_id=target["id"],
                    evidence_hash=evidence_hash,
                    confidence=1.0,
                    confirmation=confirmation,
                )
            )
    inferred_explicit = {
        (edge.task_id, edge.source_step_run_id, edge.target_step_run_id)
        for edge in edges
        if (edge.task_id, edge.target_step_run_id) in explicit_targets
    }
    true_positive = len(inferred_explicit & explicit_dependencies)
    false_positive = len(inferred_explicit - explicit_dependencies)
    false_negative = len(explicit_dependencies - inferred_explicit)
    return InferenceReport(
        edges=tuple(edges),
        explicit_edges=len(explicit_dependencies),
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
        ambiguous_evidence=ambiguous,
    )
