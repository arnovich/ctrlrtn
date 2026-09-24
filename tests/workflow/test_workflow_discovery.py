"""Legacy task traffic yields analysis-only recurring workflow families."""

from __future__ import annotations

import json

from ctrlrtn.cli import commands as cli
from ctrlrtn.recorder.sqlite.store import SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.discovery import (
    build_observed_task_structures,
    discover_workflow_families,
    render_workflow_discovery,
)


def _row(
    trace_id: int,
    task: str | None,
    route: str,
    *,
    request: dict | None = None,
    response: dict | None = None,
) -> dict:
    return {
        "id": trace_id,
        "ts": float(trace_id),
        "task_id": task,
        "use_case_key": f"tag:{route}",
        "request_body": json.dumps(request or {}).encode(),
        "response_body": json.dumps(response or {}).encode(),
    }


def _flow(task: str, offset: int, *, review: bool = False) -> list[dict]:
    tool_id = f"tool-{task}"
    rows = [
        _row(
            offset,
            task,
            "researcher",
            response={
                "content": [
                    {"type": "tool_use", "id": tool_id, "name": "search"}
                ]
            },
        ),
        _row(
            offset + 1,
            task,
            "writer",
            request={
                "messages": [
                    {
                        "content": [
                            {"type": "tool_result", "tool_use_id": tool_id}
                        ]
                    }
                ]
            },
        ),
    ]
    if review:
        rows.append(_row(offset + 2, task, "reviewer"))
    return rows


def test_normalizes_ids_and_clusters_optional_structural_variant():
    traces = [
        *_flow("task-a", 1),
        *_flow("task-b", 10),
        *_flow("task-c", 20, review=True),
        _row(30, "task-other", "unrelated"),
        _row(31, "task-other", "unrelated"),
    ]
    report = discover_workflow_families(traces, similarity=0.75)

    assert report.total_tasks == 4
    assert report.eligible_tasks == 4
    assert report.unclustered_tasks == 1
    assert len(report.families) == 1
    family = report.families[0]
    assert family.support == 3 and family.variants == 2
    assert family.linked_task_rate == 1.0
    assert family.cohesion > 0.8
    assert {node.label for node in family.nodes} == {
        "tag:researcher[search]",
        "tag:writer",
        "tag:reviewer",
    }
    assert family.edges[0].tasks == 3
    assert len(report.assignments) == 4
    assigned = [
        item for item in report.assignments if item.status == "assigned"
    ]
    assert len(assigned) == 3
    assert all(item.family_id == family.family_id for item in assigned)
    assert all(0.75 <= item.match_score <= 1 for item in assigned)
    assert all(
        item.identity_source == "explicit-task" for item in report.assignments
    )
    assert all("task-" not in digest for digest in family.member_task_digests)
    text = render_workflow_discovery(report)
    assert "structural hypotheses" in text
    assert "cannot authorize routing or execution" in text


def test_ambiguous_tool_producer_is_skipped_without_inventing_edge():
    traces = [
        _row(
            1,
            "task-a",
            "one",
            response={
                "content": [{"type": "tool_use", "id": "same", "name": "x"}]
            },
        ),
        _row(
            2,
            "task-a",
            "two",
            response={
                "content": [{"type": "tool_use", "id": "same", "name": "x"}]
            },
        ),
        _row(
            3,
            "task-a",
            "three",
            request={
                "messages": [
                    {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "same"}
                        ]
                    }
                ]
            },
        ),
    ]
    structures, total, ambiguous = build_observed_task_structures(traces)
    assert total == 1 and ambiguous == 1
    assert structures[0].edges == ()


def test_recovers_anthropic_streamed_tool_chain_from_next_request_history():
    traces = [
        {
            **_row(1, "task-a", "researcher"),
            "response_body": b'event: content_block_start\ndata: {"partial":true}\n',
        },
        _row(
            2,
            "task-a",
            "writer",
            request={
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "streamed-tool",
                                "name": "search",
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "streamed-tool",
                            }
                        ],
                    },
                ]
            },
        ),
    ]

    structures, _, ambiguous = build_observed_task_structures(traces)

    assert ambiguous == 0
    assert structures[0].nodes == (
        ("tag:researcher[search]", 1),
        ("tag:writer", 1),
    )
    assert structures[0].edges == (
        ("tag:researcher[search]", "tag:writer", "tool-result", 1),
    )


def test_recovers_openai_streamed_tool_chain_from_next_request_history():
    traces = [
        {**_row(1, "task-a", "planner"), "response_body": b"data: [DONE]\n"},
        _row(
            2,
            "task-a",
            "executor",
            request={
                "messages": [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call-1"},
                ]
            },
        ),
    ]

    structures, _, ambiguous = build_observed_task_structures(traces)

    assert ambiguous == 0
    assert structures[0].edges == (
        ("tag:planner[lookup]", "tag:executor", "tool-result", 1),
    )


def test_request_history_conflict_with_response_producer_stays_ambiguous():
    traces = [
        _row(
            1,
            "task-a",
            "actual-producer",
            response={
                "content": [
                    {"type": "tool_use", "id": "same", "name": "search"}
                ]
            },
        ),
        _row(2, "task-a", "intervening"),
        _row(
            3,
            "task-a",
            "consumer",
            request={
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "same", "name": "search"}
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "same"}
                        ],
                    },
                ]
            },
        ),
    ]

    structures, _, ambiguous = build_observed_task_structures(traces)

    assert ambiguous == 1
    assert structures[0].edges == ()


def test_correlates_unscoped_trajectories_by_exact_conversation_prefix():
    traces = []
    for offset, topic in ((1, "alpha"), (10, "beta")):
        first = {"messages": [{"role": "user", "content": topic}]}
        second = {
            "messages": [
                *first["messages"],
                {"role": "assistant", "content": f"draft {topic}"},
                {"role": "user", "content": "review"},
            ]
        }
        traces.extend(
            [
                _row(offset, None, "writer", request=first),
                _row(offset + 1, None, "reviewer", request=second),
            ]
        )

    report = discover_workflow_families(traces)

    assert report.synthetic_tasks == 2
    assert report.synthetic_traces == 4
    assert report.uncorrelated_traces == 0
    assert report.ambiguous_correlations == 0
    assert report.total_tasks == 2
    assert report.families[0].support == 2


def test_exact_tool_id_disambiguates_identical_concurrent_histories():
    initial = {"messages": [{"role": "user", "content": "same"}]}
    traces = [
        _row(
            1,
            None,
            "planner",
            request=initial,
            response={
                "content": [
                    {"type": "tool_use", "id": "call-a", "name": "search"}
                ]
            },
        ),
        _row(
            2,
            None,
            "planner",
            request=initial,
            response={
                "content": [
                    {"type": "tool_use", "id": "call-b", "name": "search"}
                ]
            },
        ),
        _row(
            3,
            None,
            "writer",
            request={
                "messages": [
                    *initial["messages"],
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-a",
                                "name": "search",
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "call-a"}
                        ],
                    },
                ]
            },
        ),
    ]

    structures, total, _ = build_observed_task_structures(traces)

    assert total == 2
    assert sorted(len(structure.trace_ids) for structure in structures) == [
        1,
        2,
    ]
    linked = next(item for item in structures if len(item.trace_ids) == 2)
    assert linked.edges == (
        ("tag:planner[search]", "tag:writer", "tool-result", 1),
    )


def test_concurrent_identical_prefixes_and_proximity_remain_uncorrelated():
    initial = {"messages": [{"role": "user", "content": "same"}]}
    extended = {
        "messages": [
            *initial["messages"],
            {"role": "assistant", "content": "one"},
        ]
    }
    traces = [
        _row(1, None, "same-route", request=initial),
        _row(2, None, "same-route", request=initial),
        _row(3, None, "same-route", request=extended),
        _row(4, None, "same-route"),
    ]

    report = discover_workflow_families(traces)

    assert report.ambiguous_correlations == 1
    assert report.synthetic_tasks == 4
    assert report.uncorrelated_traces == 4
    assert report.eligible_tasks == 0
    assert report.families == ()
    assert "4 uncorrelated · 1 ambiguous" in render_workflow_discovery(report)


def test_eligible_synthetic_fragment_retains_correlation_ambiguity():
    initial = {"messages": [{"role": "user", "content": "same"}]}
    ambiguous_history = {
        "messages": [
            *initial["messages"],
            {"role": "assistant", "content": "continued"},
        ]
    }
    traces = [
        _row(1, None, "root", request=initial),
        _row(2, None, "root", request=initial),
        _row(
            3,
            None,
            "producer",
            request=ambiguous_history,
            response={
                "content": [
                    {"type": "tool_use", "id": "unique", "name": "search"}
                ]
            },
        ),
        _row(
            4,
            None,
            "consumer",
            request={
                "messages": [
                    {
                        "content": [
                            {"type": "tool_result", "tool_use_id": "unique"}
                        ]
                    }
                ]
            },
        ),
    ]

    report = discover_workflow_families(traces)

    ambiguous = next(
        item for item in report.assignments if item.trace_count == 2
    )
    assert ambiguous.status == "unclustered"
    assert ambiguous.identity_source == "ambiguous"
    assert ambiguous.ambiguous


def test_tool_links_follow_recorded_time_not_trace_id_and_inputs_fail_closed():
    traces = [
        {
            **_row(
                50,
                "task-a",
                "producer",
                response={
                    "content": [
                        {"type": "tool_use", "id": "tool", "name": "search"}
                    ]
                },
            ),
            "ts": 1.0,
        },
        {
            **_row(
                10,
                "task-a",
                "consumer",
                request={
                    "messages": [
                        {
                            "content": [
                                {"type": "tool_result", "tool_use_id": "tool"}
                            ]
                        }
                    ]
                },
            ),
            "ts": 2.0,
        },
    ]
    structures, _, _ = build_observed_task_structures(traces)
    assert structures[0].edges[0][:3] == (
        "tag:producer[search]",
        "tag:consumer",
        "tool-result",
    )

    import pytest

    with pytest.raises(ValueError, match="min_support"):
        discover_workflow_families([], min_support=True)
    with pytest.raises(ValueError, match="similarity"):
        discover_workflow_families([], similarity=True)


def _trace(task: str, route: str, request: dict, response: dict) -> Trace:
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=json.dumps(request).encode(),
        status_code=200,
        response_headers={},
        response_body=json.dumps(response).encode(),
        latency_ms=1,
        task_id=task,
        use_case_key=f"tag:{route}",
    )


def test_store_and_cli_discover_only_unlabelled_legacy_tasks(
    tmp_path, monkeypatch, capsys
):
    path = str(tmp_path / "router.db")
    store = SqliteTraceStore(path)
    for task in ("task-a", "task-b"):
        tool_id = f"tool-{task}"
        store._insert(
            _trace(
                task,
                "researcher",
                {},
                {
                    "content": [
                        {"type": "tool_use", "id": tool_id, "name": "search"}
                    ]
                },
            )
        )
        store._insert(
            _trace(
                task,
                "writer",
                {
                    "messages": [
                        {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_id,
                                }
                            ]
                        }
                    ]
                },
                {},
            )
        )
    explicit = _trace("declared", "writer", {}, {})
    explicit.workflow = "declared-flow"
    explicit.workflow_version = "v1"
    explicit.step = "write"
    explicit.step_run_id = "run-write"
    explicit.step_attempt = 1
    store._insert(explicit)
    store._insert(_trace("declared", "legacy-fragment", {}, {}))
    assert len(store.workflow_discovery_inputs()) == 4
    latest_complete = store.workflow_discovery_inputs(1)
    assert len(latest_complete) == 2
    assert {row["task_id"] for row in latest_complete} == {"task-b"}
    store._insert(
        _trace(
            "temporary",
            "unscoped",
            {"messages": [{"role": "user", "content": "unscoped"}]},
            {},
        )
    )
    store._conn.execute(
        "UPDATE traces SET task_id = NULL WHERE task_id = 'temporary'"
    )
    unscoped = store.workflow_discovery_inputs(1)
    assert any(row["task_id"] is None for row in unscoped)
    store.close()

    monkeypatch.setattr(cli, "_db_path", lambda: path)
    cli.main(["workflow", "discover"])
    output = capsys.readouterr().out
    assert "support=2" in output
    assert "linked-tasks=100.0%" in output
    assert "analysis-only" in output

    reader = SqliteTraceStore(path, read_only=True)
    try:
        family = discover_workflow_families(
            reader.workflow_discovery_inputs()
        ).families[0]
    finally:
        reader.close()
    proposal = tmp_path / "identification.json"
    cli.main(
        [
            "workflow",
            "identify",
            family.family_id,
            "legacy-flow",
            "observed:v1",
            "--output",
            str(proposal),
        ]
    )
    assert "nothing was activated" in capsys.readouterr().out
    cli.main(["workflow", "identify-verify", str(proposal)])
    assert "No workflow" in capsys.readouterr().out
