"""Shadow-replay runner + pure helpers (ctrlrtn.eval.replay)."""

from __future__ import annotations

import gzip
import json

import pytest

from ctrlrtn.eval.replay import (
    ReplaySample,
    extract_input_summary,
    extract_output_text,
    run_replay,
    swap_model,
)

# --- pure helpers ----------------------------------------------------------


def test_swap_model_sets_the_field():
    body = json.dumps({"model": "claude-sonnet-4-5", "messages": []}).encode()
    out = json.loads(swap_model(body, "claude-haiku-4-5"))
    assert out["model"] == "claude-haiku-4-5"
    assert out["messages"] == []  # other fields preserved


def test_swap_model_rejects_non_object():
    with pytest.raises(ValueError):
        swap_model(b"[1,2,3]", "x")


def test_extract_output_text_anthropic():
    body = json.dumps(
        {
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ]
        }
    ).encode()
    assert extract_output_text(body) == "hello world"


def test_extract_output_text_openai():
    body = json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": "hi there"}}]}
    ).encode()
    assert extract_output_text(body) == "hi there"


def test_extract_output_text_openai_list_content():
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "part1 "},
                            {"type": "text", "text": "part2"},
                        ]
                    }
                }
            ]
        }
    ).encode()
    assert extract_output_text(body) == "part1 part2"


def test_extract_output_renders_tool_use_as_judgeable_text():
    # Agent traffic is mostly all-tool_use turns; the call's arguments ARE the
    # output (the article lives inside write_article's input) and must be
    # judgeable, not treated as blank.
    body = json.dumps(
        {
            "content": [
                {"type": "text", "text": "Filing story."},
                {
                    "type": "tool_use",
                    "id": "t",
                    "name": "write_article",
                    "input": {"title": "Markets rally", "body": "Stocks rose."},
                },
            ]
        }
    ).encode()
    out = extract_output_text(body)
    assert "Filing story." in out
    assert "[tool_use: write_article]" in out
    assert '"title": "Markets rally"' in out and "Stocks rose." in out


def test_tool_use_arguments_render_key_stable():
    # Identical arguments must render byte-identically regardless of dict
    # order, so the judge compares content rather than serialization noise.
    a = json.dumps(
        {
            "content": [
                {"type": "tool_use", "name": "f", "input": {"x": 1, "y": 2}}
            ]
        }
    ).encode()
    b = json.dumps(
        {
            "content": [
                {"type": "tool_use", "name": "f", "input": {"y": 2, "x": 1}}
            ]
        }
    ).encode()
    assert extract_output_text(a) == extract_output_text(b)


def test_extract_output_renders_openai_tool_calls():
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "write_article",
                                    "arguments": '{"title": "T"}',
                                }
                            }
                        ],
                    }
                }
            ]
        }
    ).encode()
    out = extract_output_text(body)
    assert "[tool_use: write_article]" in out and '"title": "T"' in out


def test_extract_output_with_no_text_and_no_tools_is_empty():
    body = json.dumps(
        {"content": [{"type": "thinking", "thinking": "internal"}]}
    ).encode()
    assert extract_output_text(body) == ""  # thinking is not output


def test_swap_model_disables_streaming():
    body = json.dumps({"model": "m", "stream": True, "messages": []}).encode()
    out = json.loads(swap_model(body, "m2"))
    assert "stream" not in out  # replay reads a single JSON body, not SSE


def test_extract_input_summary_keeps_both_ends_when_long():
    head = "FIRST " + "x" * 9000
    tail = "y" * 9000 + " LAST"
    body = json.dumps(
        {"messages": [{"role": "user", "content": head + tail}]}
    ).encode()
    summary = extract_input_summary(body, max_chars=400)
    assert summary.startswith("FIRST")
    assert summary.endswith("LAST")


def test_extract_output_text_handles_gzip():
    raw = json.dumps({"content": [{"type": "text", "text": "zipped"}]}).encode()
    assert extract_output_text(gzip.compress(raw), "gzip") == "zipped"


def test_extract_input_summary_pulls_user_messages_only():
    body = json.dumps(
        {
            "model": "m",
            "system": "you are an editor",  # role, not task
            "messages": [
                {"role": "user", "content": "polish this draft"},
                {"role": "assistant", "content": "ok"},
            ],
        }
    ).encode()
    summary = extract_input_summary(body)
    assert "polish this draft" in summary
    assert "you are an editor" not in summary
    assert "ok" not in summary  # assistant turn excluded


# --- the runner (network steps injected) -----------------------------------


def _samples(n, *, cluster_fn=lambda i: None):
    return [
        ReplaySample(
            request_body=json.dumps(
                {
                    "model": "baseline",
                    "messages": [{"role": "user", "content": f"task {i}"}],
                }
            ).encode(),
            cluster=cluster_fn(i),
        )
        for i in range(n)
    ]


def _content_judge(prompt):
    a = prompt.split("# Response A", 1)[1].split("# Response B", 1)[0]
    score_a = 9.0 if "GOOD" in a else 4.0
    score_b = 4.0 if "GOOD" in a else 9.0
    return json.dumps({"score_a": score_a, "score_b": score_b})


def test_equal_arms_conclude_non_inferior():
    # Both arms produce equally-good output -> judge ties -> non-inferior.
    def replay_fn(body, model):
        return "GOOD answer"  # identical regardless of arm

    report = run_replay(
        _samples(30),
        baseline_model="base",
        candidate_model="cand",
        replay_fn=replay_fn,
        judge_fn=_content_judge,
        margin=0.5,
    )
    assert report.n_pairings == 30
    assert report.result.non_inferior
    assert report.mean_diff == pytest.approx(0.0)


def test_worse_candidate_is_not_non_inferior():
    # Candidate output lacks GOOD -> judge prefers baseline -> diffs negative.
    def replay_fn(body, model):
        return "GOOD answer" if model == "base" else "weak answer"

    report = run_replay(
        _samples(30),
        baseline_model="base",
        candidate_model="cand",
        replay_fn=replay_fn,
        judge_fn=_content_judge,
        margin=0.5,
    )
    assert not report.result.non_inferior
    assert report.mean_diff < 0


def test_replay_fn_receives_each_model():
    seen = []

    def replay_fn(body, model):
        seen.append(model)
        return "GOOD"

    run_replay(
        _samples(2),
        baseline_model="base-m",
        candidate_model="cand-m",
        replay_fn=replay_fn,
        judge_fn=_content_judge,
        margin=0.5,
    )
    assert seen == ["base-m", "cand-m", "base-m", "cand-m"]


def test_progress_callback_runs_after_every_input_including_blank():
    updates = []

    run_replay(
        _samples(3),
        baseline_model="base",
        candidate_model="cand",
        replay_fn=lambda body, model: "",
        judge_fn=_content_judge,
        margin=0.5,
        progress_fn=lambda current, total, message: updates.append(
            (current, total, message)
        ),
    )

    assert [row[:2] for row in updates] == [(1, 3), (2, 3), (3, 3)]


def test_clusters_are_threaded_into_the_ni_test():
    # 24 pairings in 4 tasks -> the NI test sees 4 units, underpowered.
    def replay_fn(body, model):
        return "GOOD answer"

    report = run_replay(
        _samples(24, cluster_fn=lambda i: i // 6),
        baseline_model="base",
        candidate_model="cand",
        replay_fn=replay_fn,
        judge_fn=_content_judge,
        margin=0.5,
    )
    assert report.result.n_units == 4
    assert report.result.underpowered


def test_both_blank_arms_are_dropped_and_batch_refuses():
    # Both arms produce no text -> no signal -> dropped; an all-blank batch
    # must NOT silently conclude non-inferior.
    def replay_fn(body, model):
        return ""

    report = run_replay(
        _samples(30),
        baseline_model="base",
        candidate_model="cand",
        replay_fn=replay_fn,
        judge_fn=_content_judge,
        margin=0.5,
    )
    assert report.n_blank == 30
    assert report.n_pairings == 0
    assert not report.result.non_inferior


def test_blank_candidate_is_kept_and_scored_negative():
    # Candidate empty, baseline real -> not both-blank -> kept and penalised.
    def replay_fn(body, model):
        return "GOOD answer" if model == "base" else ""

    report = run_replay(
        _samples(30),
        baseline_model="base",
        candidate_model="cand",
        replay_fn=replay_fn,
        judge_fn=_content_judge,
        margin=0.5,
    )
    assert report.n_pairings == 30
    assert report.n_blank == 0
    assert not report.result.non_inferior


def test_per_sample_failure_is_isolated():
    # A replay blip on a few samples is skipped, not fatal.
    def replay_fn(body, model):
        if b'task 0"' in body or b'task 1"' in body:  # trailing quote = exact
            raise RuntimeError("network blip")
        return "GOOD answer"

    report = run_replay(
        _samples(30),
        baseline_model="base",
        candidate_model="cand",
        replay_fn=replay_fn,
        judge_fn=_content_judge,
        margin=0.5,
    )
    assert report.n_failed == 2
    assert report.n_pairings == 28
    # the failure reason is captured so a degraded batch can be diagnosed
    assert any("network blip" in reason for reason in report.failures)


def test_too_many_failures_refuses_to_conclude():
    # >20% unusable -> refuse even though the usable pairings tie.
    def replay_fn(body, model):
        # fail the first 12 samples (trailing quote avoids prefix collisions)
        if any(f'task {i}"'.encode() in body for i in range(12)):
            raise RuntimeError("blip")
        return "GOOD answer"

    report = run_replay(
        _samples(30),
        baseline_model="base",
        candidate_model="cand",
        replay_fn=replay_fn,
        judge_fn=_content_judge,
        margin=0.5,
    )
    assert report.n_failed == 12
    assert not report.result.non_inferior


def test_mixed_clusters_keep_none_as_singletons():
    # 18 grouped (3 tasks x 6) + 12 independent (None). None must NOT fuse
    # into one unit: expect 3 task units + 12 singletons = 15.
    def replay_fn(body, model):
        return "GOOD answer"

    samples = _samples(18, cluster_fn=lambda i: i // 6) + _samples(12)
    report = run_replay(
        samples,
        baseline_model="base",
        candidate_model="cand",
        replay_fn=replay_fn,
        judge_fn=_content_judge,
        margin=0.5,
    )
    assert report.result.n_units == 15


def test_odd_replicates_rejected_before_any_replay():
    calls = []

    def replay_fn(body, model):
        calls.append(model)
        return "GOOD"

    with pytest.raises(ValueError):
        run_replay(
            _samples(4),
            baseline_model="base",
            candidate_model="cand",
            replay_fn=replay_fn,
            judge_fn=_content_judge,
            margin=0.5,
            replicates=3,
        )
    assert calls == []  # failed before spending any replay calls


def test_extract_input_summary_includes_tool_results():
    # Agent traffic carries the source data in tool_result blocks; a judge
    # that never sees it can compare style but not catch a fabricated number.
    body = json.dumps(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Write the AAPL story."},
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "AAPL closed at 212.44, up 1.2%",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "t2",
                            "content": [
                                {"type": "text", "text": "volume 48.2M"}
                            ],
                        },
                    ],
                },
                {"role": "assistant", "content": "..."},  # not the task
            ]
        }
    ).encode()
    summary = extract_input_summary(body)
    assert "Write the AAPL story." in summary
    assert "[tool result]" in summary
    assert "AAPL closed at 212.44" in summary  # string content
    assert "volume 48.2M" in summary  # nested content list


def test_require_replayable_names_every_foreign_path():
    from ctrlrtn.eval.replay import require_replayable

    rows = [
        {"path": "/v1/messages"},
        {"path": "/ollama/v1/chat/completions"},
        {"path": "/v1/chat/completions"},
        {"path": "/v1/chat/completions"},
    ]
    with pytest.raises(ValueError) as excinfo:
        require_replayable(rows, "tag:editor")
    message = str(excinfo.value)
    assert "Anthropic Messages API only" in message
    assert "/ollama/v1/chat/completions, /v1/chat/completions" in message

    require_replayable([{"path": "/v1/messages"}], "tag:editor")  # no raise
