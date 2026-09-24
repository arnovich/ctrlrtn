"""Exact-evidence correlation for traces without explicit task identity."""

from __future__ import annotations

import hashlib
import json

from ctrlrtn.workflow.discovery.models import SyntheticCorrelation


def _json(raw: bytes | None) -> object:
    try:
        return json.loads(raw or b"")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _produced_tools(raw: bytes | None) -> dict[str, str]:
    body = _json(raw)
    if not isinstance(body, dict):
        return {}
    found: dict[str, str] = {}
    for block in body.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_id, name = block.get("id"), block.get("name")
        if isinstance(tool_id, str) and tool_id:
            found[tool_id] = name if isinstance(name, str) and name else "tool"
    for choice in body.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            tool_id = call.get("id")
            function = call.get("function") or {}
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(tool_id, str) and tool_id:
                found[tool_id] = (
                    name if isinstance(name, str) and name else "tool"
                )
    return found


def _request_history_tools(raw: bytes | None) -> dict[str, str]:
    """Tool calls echoed in a later request's ordinary JSON conversation."""
    body = _json(raw)
    if not isinstance(body, dict):
        return {}
    found: dict[str, str] = {}
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            tool_id = call.get("id")
            function = call.get("function") or {}
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(tool_id, str) and tool_id:
                found[tool_id] = (
                    name if isinstance(name, str) and name else "tool"
                )
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id, name = block.get("id"), block.get("name")
            if isinstance(tool_id, str) and tool_id:
                found[tool_id] = (
                    name if isinstance(name, str) and name else "tool"
                )
    return found


def _consumed_tool_ids(raw: bytes | None) -> set[str]:
    body = _json(raw)
    if not isinstance(body, dict):
        return set()
    found: set[str] = set()
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        tool_id = message.get("tool_call_id")
        if isinstance(tool_id, str) and tool_id:
            found.add(tool_id)
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if (
                not isinstance(block, dict)
                or block.get("type") != "tool_result"
            ):
                continue
            tool_id = block.get("tool_use_id")
            if isinstance(tool_id, str) and tool_id:
                found.add(tool_id)
    return found


def _message_history(raw: bytes | None) -> tuple[str, ...]:
    body = _json(raw)
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return ()
    return tuple(
        json.dumps(message, sort_keys=True, separators=(",", ":"))
        for message in body["messages"]
        if isinstance(message, dict)
    )


def _extends(prefix: tuple[str, ...], history: tuple[str, ...]) -> bool:
    return (
        bool(prefix)
        and len(history) > len(prefix)
        and history[: len(prefix)] == prefix
    )


def correlate_unscoped_traces(
    traces: list[dict],
) -> tuple[list[dict], SyntheticCorrelation]:
    """Assign analysis-only task IDs using exact continuity, never proximity."""
    ordered = sorted(traces, key=lambda row: (row["ts"], row["id"]))
    trajectories: list[dict] = []
    correlated: list[dict] = []
    ambiguous = 0
    synthetic_traces = 0
    for original in ordered:
        if isinstance(original.get("task_id"), str) and original["task_id"]:
            correlated.append(dict(original))
            continue
        trace = dict(original)
        history = _message_history(trace.get("request_body"))
        consumed = _consumed_tool_ids(trace.get("request_body"))
        tool_candidates = {
            index
            for index, trajectory in enumerate(trajectories)
            if consumed & trajectory["tool_ids"]
        }
        prefix_candidates = {
            index
            for index, trajectory in enumerate(trajectories)
            if any(
                _extends(prior, history) for prior in trajectory["histories"]
            )
        }
        candidates = tool_candidates or prefix_candidates
        if len(candidates) == 1:
            trajectory = trajectories[next(iter(candidates))]
            evidence = "tool-id" if tool_candidates else "conversation-prefix"
        else:
            ambiguous += len(candidates) > 1
            evidence = "ambiguous" if candidates else "unscoped-root"
            seed = hashlib.sha256(
                f"{trace['id']}:".encode() + (trace.get("request_body") or b"")
            ).hexdigest()[:24]
            trajectory = {
                "task_id": f"synthetic:{seed}",
                "histories": [],
                "tool_ids": set(),
                "size": 0,
            }
            trajectories.append(trajectory)
        trace["task_id"] = trajectory["task_id"]
        trace["_synthetic_correlation"] = evidence
        trajectory["size"] += 1
        if history and history not in trajectory["histories"]:
            trajectory["histories"].append(history)
        trajectory["tool_ids"].update(
            _produced_tools(trace.get("response_body"))
        )
        trajectory["tool_ids"].update(
            _request_history_tools(trace.get("request_body"))
        )
        synthetic_traces += 1
        correlated.append(trace)
    return sorted(
        correlated, key=lambda row: (row["ts"], row["id"])
    ), SyntheticCorrelation(
        tasks=len(trajectories),
        traces=synthetic_traces,
        uncorrelated_traces=sum(
            trajectory["size"]
            for trajectory in trajectories
            if trajectory["size"] == 1
        ),
        ambiguous=ambiguous,
    )
