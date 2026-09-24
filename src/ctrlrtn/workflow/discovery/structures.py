"""Normalized task structures and structural similarity."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from ctrlrtn.workflow.discovery.correlation import (
    _consumed_tool_ids,
    _produced_tools,
    _request_history_tools,
    correlate_unscoped_traces,
)
from ctrlrtn.workflow.discovery.models import ObservedTaskStructure

_MAX_LABEL = 128


def _task_digest(task_id: str) -> str:
    return hashlib.sha256(task_id.encode()).hexdigest()[:16]


def _label(value: object) -> str:
    raw = str(value)
    cleaned = "".join(
        character if character.isprintable() and character not in "[]" else "?"
        for character in raw
    )
    if len(cleaned) <= _MAX_LABEL:
        return cleaned
    digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"{cleaned[: _MAX_LABEL - 9]}:{digest}"


def _fingerprint(
    nodes: Counter[str], edges: Counter[tuple[str, str, str]]
) -> str:
    document = {
        "nodes": sorted((label, count) for label, count in nodes.items()),
        "edges": sorted((*edge, count) for edge, count in edges.items()),
    }
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_observed_task_structures(
    traces: list[dict],
) -> tuple[tuple[ObservedTaskStructure, ...], int, int]:
    """Normalize task-scoped calls and exact tool links without inventing order."""
    traces, _correlation = correlate_unscoped_traces(traces)
    tasks: dict[str, list[dict]] = {}
    for trace in traces:
        task_id = trace.get("task_id")
        if isinstance(task_id, str) and task_id:
            tasks.setdefault(task_id, []).append(trace)
    structures = []
    ambiguous = 0
    for task_id, task_traces in sorted(tasks.items()):
        ordered = sorted(task_traces, key=lambda row: (row["ts"], row["id"]))
        positions = {
            int(trace["id"]): index for index, trace in enumerate(ordered)
        }
        labels: dict[int, str] = {}
        producers: dict[str, list[tuple[int, str]]] = {}
        consumers: dict[str, list[int]] = {}
        response_tools: dict[int, dict[str, str]] = {}
        history_tools: dict[int, dict[str, str]] = {}
        for trace in ordered:
            trace_id = int(trace["id"])
            produced = _produced_tools(trace.get("response_body"))
            history = _request_history_tools(trace.get("request_body"))
            response_tools[trace_id] = produced
            history_tools[trace_id] = history
            base = _label(trace.get("use_case_key") or "unclassified")
            for tool_id, name in produced.items():
                producers.setdefault(tool_id, []).append((trace_id, name))
            for tool_id in _consumed_tool_ids(trace.get("request_body")):
                consumers.setdefault(tool_id, []).append(trace_id)

        # A streamed response may not be parseable as one JSON object, but the
        # next request commonly echoes the assistant tool call and its result.
        # Attribute only a newly observed, consumed call to the immediately
        # preceding provider trace; conflicting response evidence remains
        # ambiguous below.
        seen_history_ids: set[str] = set()
        for index, trace in enumerate(ordered):
            trace_id = int(trace["id"])
            history = history_tools[trace_id]
            consumed = _consumed_tool_ids(trace.get("request_body"))
            if index:
                source_id = int(ordered[index - 1]["id"])
                for tool_id in (set(history) & consumed) - seen_history_ids:
                    candidate = (source_id, history[tool_id])
                    if candidate not in producers.setdefault(tool_id, []):
                        producers[tool_id].append(candidate)
                    response_tools[source_id].setdefault(
                        tool_id, history[tool_id]
                    )
            seen_history_ids.update(history)

        for trace in ordered:
            trace_id = int(trace["id"])
            base = _label(trace.get("use_case_key") or "unclassified")
            tools = ",".join(
                sorted(
                    {_label(name) for name in response_tools[trace_id].values()}
                )
            )
            labels[trace_id] = f"{base}[{tools}]" if tools else base
        nodes = Counter(labels.values())
        edges: Counter[tuple[str, str, str]] = Counter()
        for tool_id, targets in consumers.items():
            sources = producers.get(tool_id, [])
            if len(sources) != 1:
                if sources:
                    ambiguous += 1
                continue
            source_id, _name = sources[0]
            later = [
                target
                for target in targets
                if positions[target] > positions[source_id]
            ]
            if not later:
                continue
            target_id = min(later, key=positions.__getitem__)
            edges[(labels[source_id], labels[target_id], "tool-result")] += 1
        structures.append(
            ObservedTaskStructure(
                _task_digest(task_id),
                tuple(labels),
                tuple(sorted(nodes.items())),
                tuple(sorted((*edge, count) for edge, count in edges.items())),
                _fingerprint(nodes, edges),
                (
                    "explicit-task"
                    if not task_id.startswith("synthetic:")
                    else next(
                        (
                            source
                            for source in (
                                "ambiguous",
                                "tool-id",
                                "conversation-prefix",
                            )
                            if any(
                                trace.get("_synthetic_correlation") == source
                                for trace in ordered
                            )
                        ),
                        "synthetic-fragment",
                    )
                ),
                any(
                    trace.get("_synthetic_correlation") == "ambiguous"
                    for trace in ordered
                ),
                tuple(labels[int(trace["id"])] for trace in ordered),
            )
        )
    return tuple(structures), len(tasks), ambiguous


def _counter(structure: ObservedTaskStructure, kind: str) -> Counter:
    if kind == "nodes":
        return Counter(dict(structure.nodes))
    return Counter({edge[:3]: edge[3] for edge in structure.edges})


def _jaccard(left: Counter, right: Counter) -> float:
    keys = set(left) | set(right)
    denominator = sum(max(left[key], right[key]) for key in keys)
    return (
        sum(min(left[key], right[key]) for key in keys) / denominator
        if denominator
        else 1.0
    )


def structure_similarity(
    left: ObservedTaskStructure, right: ObservedTaskStructure
) -> float:
    node_score = _jaccard(_counter(left, "nodes"), _counter(right, "nodes"))
    left_edges, right_edges = _counter(left, "edges"), _counter(right, "edges")
    if not left_edges and not right_edges:
        return node_score
    return 0.6 * node_score + 0.4 * _jaccard(left_edges, right_edges)
