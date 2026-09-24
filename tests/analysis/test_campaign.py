"""The campaign report: replay-json serialization, repricing, table + chart."""

from __future__ import annotations

import json

import pytest

from ctrlrtn.analysis.campaign import (
    NON_INFERIOR,
    NOT_NON_INFERIOR,
    UNDERPOWERED,
    build_campaign_report,
    render_campaign_markdown,
    render_campaign_svg,
    replay_report_json,
)
from ctrlrtn.cli import commands as cli
from ctrlrtn.eval.ni import NIResult
from ctrlrtn.eval.replay import ReplayReport
from ctrlrtn.policy.experiment import BASELINE, CANDIDATE, Experiment
from ctrlrtn.recorder.store import Outcome, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry import pricing

_SONNET = "claude-sonnet-4-5"
_HAIKU = "claude-haiku-4-5"


def _trace(
    use_case,
    *,
    model=_SONNET,
    cost=1.0,
    input_tokens=1000,
    output_tokens=500,
    cache_read=0,
    task_id=None,
    experiment_id=None,
    arm=None,
    ts=0.0,
):
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=200,
        response_headers={},
        response_body=b"",
        latency_ms=10.0,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        use_case_key=use_case,
        cost_usd=cost,
        task_id=task_id,
        experiment_id=experiment_id,
        arm=arm,
        ts=ts,
    )


def _ni(*, non_inferior=True, underpowered=False, lower=0.1):
    return NIResult(
        non_inferior=non_inferior,
        mean_diff=-0.2,
        lower_bound=lower,
        n=40,
        n_units=20,
        margin=1.0,
        confidence=0.95,
        underpowered=underpowered,
    )


def _replay_report(**ni_kwargs):
    return ReplayReport(
        result=_ni(**ni_kwargs),
        n_pairings=40,
        n_failed=1,
        n_blank=0,
        mean_diff=-0.2,
        baseline_model=_SONNET,
        candidate_model=_HAIKU,
    )


@pytest.fixture
def store():
    s = SqliteTraceStore(":memory:")
    yield s
    s.close()


def _seed_role(store, use_case, calls=4, **kw):
    for _ in range(calls):
        store._insert(_trace(use_case, **kw))


# --- replay_report_json ------------------------------------------------------


def test_replay_json_maps_the_three_verdicts():
    ok = replay_report_json(_replay_report(), "tag:editor")
    assert ok["verdict"] == NON_INFERIOR
    assert ok["use_case"] == "tag:editor"
    assert ok["candidate_model"] == _HAIKU
    assert ok["n_pairings"] == 40 and ok["n_failed"] == 1
    under = replay_report_json(
        _replay_report(non_inferior=False, underpowered=True), "tag:x"
    )
    assert under["verdict"] == UNDERPOWERED
    worse = replay_report_json(_replay_report(non_inferior=False), "tag:x")
    assert worse["verdict"] == NOT_NON_INFERIOR


def test_replay_json_serializes_minus_inf_bound_as_none():
    blob = replay_report_json(
        _replay_report(non_inferior=False, lower=float("-inf")), "tag:x"
    )
    assert blob["lower_bound"] is None
    json.dumps(blob)  # and the whole blob is valid JSON


def test_replay_json_binds_dataset_evaluation_provenance():
    dataset = {
        "manifest_sha256": "a" * 64,
        "split": "evaluation",
        "n_traces": 20,
    }
    blob = replay_report_json(_replay_report(), "tag:x", dataset=dataset)
    assert blob["dataset"] == dataset


def test_step_scoped_replay_is_labelled_and_not_promoted_to_role(store):
    _seed_role(store, "tag:editor")
    replay = replay_report_json(
        _replay_report(),
        "tag:editor",
        scope={
            "workflow": "pipeline",
            "workflow_version": "v1",
            "step": "draft",
        },
    )
    assert replay["claim"] == "workflow_step"
    row = build_campaign_report(store, [replay])[0]
    assert row.replay is None


# --- build_campaign_report ---------------------------------------------------


def test_report_reprices_the_recorded_mix_at_candidate_rates(store):
    _seed_role(store, "tag:editor", cache_read=2000)
    replay = replay_report_json(_replay_report(), "tag:editor")
    rows = build_campaign_report(store, [replay])
    (row,) = rows
    assert row.baseline_model == _SONNET
    assert row.candidate_model == _HAIKU
    # Exactly the price-table math, cache-aware, over the summed token mix.
    price = pricing.price_for(_HAIKU)
    expected = (
        4 * (1000 * price.input + 500 * price.output + 2000 * price.cache_read)
    ) / 1_000_000
    assert row.repriced_usd == pytest.approx(expected)
    assert row.savings_pct is not None and 0 < row.savings_pct < 1
    assert row.replay_verdict == NON_INFERIOR


def test_report_includes_tags_and_skips_unkeyed(store):
    _seed_role(store, "tag:editor")
    _seed_role(store, None)  # unkeyed noise
    _seed_role(store, "fp:abc123")  # fingerprint key without replay/experiment
    rows = build_campaign_report(store, [])
    assert [r.use_case for r in rows] == ["tag:editor"]
    # ...but a replay verdict pulls an fp: key in.
    replay = replay_report_json(_replay_report(), "fp:abc123")
    rows = build_campaign_report(store, [replay])
    assert {r.use_case for r in rows} == {"tag:editor", "fp:abc123"}


def test_report_flags_an_unpriced_candidate_instead_of_zero(store):
    _seed_role(store, "tag:editor")
    replay = replay_report_json(_replay_report(), "tag:editor")
    replay["candidate_model"] = "some-unpriced-model"
    (row,) = build_campaign_report(store, [replay])
    assert row.repriced_usd is None and row.savings_pct is None
    assert any("no price table entry" in w for w in row.warnings)


def test_report_folds_in_a_live_experiment(store):
    store.create_experiment(
        Experiment("tag:editor", _HAIKU, 50, experiment_id="exp:e")
    )
    for i in range(3):
        store._insert(
            _trace(
                "tag:editor",
                task_id=f"b{i}",
                experiment_id="exp:e",
                arm=BASELINE,
                cost=0.9,
            )
        )
        store._insert_outcome(Outcome(f"b{i}", success=True))
        store._insert(
            _trace(
                "tag:editor",
                model=_HAIKU,
                task_id=f"c{i}",
                experiment_id="exp:e",
                arm=CANDIDATE,
                cost=0.2,
            )
        )
        store._insert_outcome(Outcome(f"c{i}", success=True))
    (row,) = build_campaign_report(store, [])
    assert row.experiment_id == "exp:e"
    assert row.tripwire_verdict is not None
    assert row.baseline_cost_per_task == pytest.approx(0.9)
    assert row.candidate_cost_per_task == pytest.approx(0.2)
    # Candidate model inferred from the experiment when no replay json given.
    assert row.candidate_model == _HAIKU


# --- renders -------------------------------------------------------------------


def test_markdown_table_counts_only_blessed_savings(store):
    _seed_role(store, "tag:editor")
    _seed_role(store, "tag:analyst")
    blessed = replay_report_json(_replay_report(), "tag:editor")
    worse = replay_report_json(
        _replay_report(non_inferior=False), "tag:analyst"
    )
    text = render_campaign_markdown(
        build_campaign_report(store, [blessed, worse])
    )
    assert "tag:editor" in text and "✓ non-inferior" in text
    assert "tag:analyst" in text and "✗ worse" in text
    # The headline saving counts ONLY the blessed role: its $4 recorded spend
    # reprices to ~$0.01 on haiku, so ~$3.99 of the $8 total (50%) — the
    # unblessed analyst's potential saving must NOT be counted. NOTE: the
    # golden $3.99 depends on prices.toml's claude-haiku-4-5 entry ($1/$5 per
    # M) — a price-table bump legitimately moves this number.
    assert "$3.99 of $8.00 (50%)" in text


def test_markdown_on_an_empty_db():
    s = SqliteTraceStore(":memory:")
    try:
        assert "No campaign data" in render_campaign_markdown(
            build_campaign_report(s, [])
        )
    finally:
        s.close()


def test_svg_colors_only_blessed_savings_green(store):
    _seed_role(store, "tag:editor")
    _seed_role(store, "tag:analyst")
    blessed = replay_report_json(_replay_report(), "tag:editor")
    worse = replay_report_json(
        _replay_report(non_inferior=False), "tag:analyst"
    )
    svg = render_campaign_svg(build_campaign_report(store, [blessed, worse]))
    assert svg.startswith("<svg")
    assert "tag:editor" in svg and "tag:analyst" in svg
    # Count BAR fills (rects), not text fills — the subtitle is muted too.
    assert svg.count('height="16" fill="#3fa66f"') == 1  # green: blessed
    assert svg.count('height="16" fill="#9aa4ad"') == 1  # muted: unblessed


def test_svg_escapes_hostile_use_case_names(store):
    _seed_role(store, "tag:<evil>&co")
    svg = render_campaign_svg(build_campaign_report(store, []))
    assert "<evil>" not in svg and "&lt;evil&gt;&amp;co" in svg


# --- the CLI command -----------------------------------------------------------


def test_campaign_report_command_writes_md_and_svg(tmp_path, monkeypatch):
    db = str(tmp_path / "campaign.db")
    writer = SqliteTraceStore(db)
    _seed_role(writer, "tag:editor")
    writer.close()
    replay_path = tmp_path / "editor.json"
    replay_path.write_text(
        json.dumps(replay_report_json(_replay_report(), "tag:editor"))
    )
    monkeypatch.setattr(cli, "_db_path", lambda: db)
    md, svg = tmp_path / "report.md", tmp_path / "chart.svg"
    cli.main(
        [
            "campaign-report",
            "--replay-json",
            str(replay_path),
            "--md",
            str(md),
            "--svg",
            str(svg),
        ]
    )
    assert "tag:editor" in md.read_text()
    assert svg.read_text().startswith("<svg")


def test_campaign_report_fails_cleanly_without_a_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_db_path", lambda: str(tmp_path / "nope.db"))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["campaign-report"])
    assert exit_info.value.code == 2


# --- review fixes: contamination, orphans, injection, exit codes ---------------


def test_recorded_cost_excludes_candidate_arm_traffic(store):
    # Phase 3 runs a 50/50 experiment; the cheap candidate calls must NOT
    # dilute the "what does this role cost today" bar (or its token mix).
    _seed_role(store, "tag:editor", calls=2, cost=1.0)
    _seed_role(
        store,
        "tag:editor",
        calls=2,
        model=_HAIKU,
        cost=0.01,
        arm=CANDIDATE,
        experiment_id="exp:e",
    )
    store.create_experiment(
        Experiment("tag:editor", _HAIKU, 50, experiment_id="exp:e")
    )
    (row,) = build_campaign_report(store, [])
    assert row.calls == 2  # baseline calls only
    assert row.cost_usd == pytest.approx(2.0)  # not 2.02


def test_an_orphan_replay_verdict_surfaces_instead_of_vanishing(store):
    # A paid verdict for a use-case with no recorded traffic (typo, or wrong
    # db_path) must show up with a warning, not silently disappear.
    replay = replay_report_json(_replay_report(), "tag:editors")  # typo
    rows = build_campaign_report(store, [replay])
    (row,) = rows
    assert row.use_case == "tag:editors" and row.calls == 0
    assert any("NO recorded traffic" in w for w in row.warnings)
    assert "NO recorded traffic" in render_campaign_markdown(rows)


def test_markdown_escapes_a_pipe_in_a_client_controlled_tag(store):
    # tag: comes from the x-ctrlrtn-route header — "edi|tor" must not add a
    # column to the published table.
    _seed_role(store, "tag:edi|tor")
    text = render_campaign_markdown(build_campaign_report(store, []))
    assert "tag:edi\\|tor" in text
    row_line = next(line for line in text.splitlines() if "edi" in line)
    assert row_line.count(" | ") == 6  # still exactly 7 columns


def test_replay_eval_rejects_an_unwritable_json_path_before_spending(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "_db_path", lambda: str(tmp_path / "any.db"))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            [
                "replay-eval",
                "tag:editor",
                _HAIKU,
                "--json",
                str(tmp_path / "no-such-dir" / "out.json"),
            ]
        )
    assert exit_info.value.code == 2  # operator error, not a verdict code


def test_duplicate_replay_jsons_for_one_use_case_fail_loudly(
    tmp_path, monkeypatch
):
    db = str(tmp_path / "campaign.db")
    writer = SqliteTraceStore(db)
    _seed_role(writer, "tag:editor")
    writer.close()
    blob = json.dumps(replay_report_json(_replay_report(), "tag:editor"))
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(blob)
    b.write_text(blob)
    monkeypatch.setattr(cli, "_db_path", lambda: db)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["campaign-report", "--replay-json", str(a), str(b)])
    assert exit_info.value.code == 2


def test_campaign_report_only_filters_and_rejects_typos(tmp_path, monkeypatch):
    db = str(tmp_path / "campaign.db")
    writer = SqliteTraceStore(db)
    _seed_role(writer, "tag:editor")
    _seed_role(writer, "fp:legacy1234")
    writer.create_experiment(
        Experiment("fp:legacy1234", _HAIKU, 50, experiment_id="exp:old")
    )
    writer.close()
    monkeypatch.setattr(cli, "_db_path", lambda: db)
    md = tmp_path / "report.md"
    cli.main(["campaign-report", "--only", "tag:editor", "--md", str(md)])
    text = md.read_text()
    assert "tag:editor" in text and "fp:legacy1234" not in text
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["campaign-report", "--only", "tag:typo"])
    assert exit_info.value.code == 2
