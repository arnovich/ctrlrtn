"""Slice 6b: the live A/B tripwire — closure, arm-purity, Wilson+Manski
gross-regression verdict, plus the store aggregate and CLI wiring that feed it.
"""

from __future__ import annotations

import pytest

from ctrlrtn.cli.commands import main
from ctrlrtn.eval.tripwire import (
    CONTAMINATED,
    FAILURE,
    GROSS_REGRESSION,
    INCONCLUSIVE,
    NO_DATA,
    NO_GROSS_REGRESSION,
    NOT_EXERCISED,
    OPEN,
    SUCCESS,
    UNDERPOWERED,
    UNREPORTED,
    TaskRecord,
    analyze,
    classify_task,
    run_tripwire,
)
from ctrlrtn.policy.experiment import BASELINE, CANDIDATE, Experiment
from ctrlrtn.recorder.store import Outcome, SqliteTraceStore
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.workflow.identity import WorkflowEvent, WorkflowIdentity

_NOW = 10_000.0
_IDLE = 100.0
_N = 35  # comfortably above the min-30 reported-per-arm power gate


def _row(
    task_id="t",
    arm=CANDIDATE,
    *,
    calls=3,
    cost=0.0,
    arms=1,
    experiments=1,
    ceilings=0,
    last_ts=0.0,  # far in the past -> closed under _IDLE
    success=None,
    score=None,
    served_model="m",
):
    return {
        "task_id": task_id,
        "arm": arm,
        "calls": calls,
        "cost": cost,
        "arms": arms,
        "experiments": experiments,
        "ceilings": ceilings,
        "last_ts": last_ts,
        "success": success,
        "score": score,
        "served_model": served_model,
    }


def _classify(fail_below=None, **kw):
    return classify_task(
        _row(**kw), now=_NOW, idle_seconds=_IDLE, fail_below=fail_below
    )


# --- per-task classification ----------------------------------------------


def test_ceiling_terminal_is_a_failure_flagged_as_ceiling():
    rec = _classify(ceilings=1)
    assert rec.status == FAILURE and rec.ceiling is True


def test_reported_success_and_failure():
    assert _classify(success=True).status == SUCCESS
    assert _classify(success=False).status == FAILURE


def test_score_threshold_only_when_given():
    assert _classify(score=3.0, fail_below=5.0).status == FAILURE
    assert _classify(score=8.0, fail_below=5.0).status == SUCCESS
    # a success flag wins over the score threshold
    assert _classify(success=True, score=1.0, fail_below=5.0).status == SUCCESS


def test_score_without_threshold_is_unreported_but_flagged():
    rec = _classify(score=3.0)  # no fail_below
    assert rec.status == UNREPORTED and rec.unused_score is True


def test_no_outcome_is_unreported_when_idle_else_open():
    assert _classify(last_ts=0.0).status == UNREPORTED
    assert _classify(last_ts=_NOW - 10).status == OPEN


def test_contamination_excludes_the_task():
    assert _classify(arms=2).status == CONTAMINATED  # mixed arm
    assert _classify(experiments=2).status == CONTAMINATED  # straddles two
    assert _classify(arm="weird").status == CONTAMINATED  # unknown arm


# --- Wilson + Manski verdict ----------------------------------------------


def _recs(
    arm, status, n, *, calls=3, served_model="m", ceiling=False, cost=0.0
):
    return [
        TaskRecord(
            f"{arm}-{status}-{i}",
            arm,
            calls,
            status,
            served_model,
            cost,
            ceiling,
        )
        for i in range(n)
    ]


def test_underpowered_below_min_reported():
    recs = _recs(BASELINE, SUCCESS, 10, served_model="opus") + _recs(
        CANDIDATE, SUCCESS, 10, served_model="haiku"
    )
    assert analyze(recs).verdict == UNDERPOWERED


def test_unreported_tasks_do_not_count_toward_power():
    # 35 closed but only 5 reported per arm -> still underpowered
    recs = (
        _recs(BASELINE, SUCCESS, 5, served_model="opus")
        + _recs(BASELINE, UNREPORTED, 30, served_model="opus")
        + _recs(CANDIDATE, SUCCESS, 5, served_model="haiku")
        + _recs(CANDIDATE, UNREPORTED, 30, served_model="haiku")
    )
    assert analyze(recs).verdict == UNDERPOWERED


def test_no_gross_regression_when_both_clean():
    recs = _recs(BASELINE, SUCCESS, _N, served_model="opus") + _recs(
        CANDIDATE, SUCCESS, _N, served_model="haiku"
    )
    assert analyze(recs).verdict == NO_GROSS_REGRESSION


def test_gross_regression_when_candidate_much_worse():
    recs = (
        _recs(BASELINE, SUCCESS, _N, served_model="opus")
        + _recs(CANDIDATE, SUCCESS, 7, served_model="haiku")
        + _recs(CANDIDATE, FAILURE, 28, served_model="haiku")
    )
    assert analyze(recs).verdict == GROSS_REGRESSION  # ~80% vs 0%


def test_inconclusive_when_interval_straddles_the_margin():
    # 33% observed candidate failures vs 0% baseline, but only 30 reported each:
    # the Wilson interval is too wide to call gross OR safe.
    recs = (
        _recs(BASELINE, SUCCESS, 30, served_model="opus")
        + _recs(CANDIDATE, SUCCESS, 20, served_model="haiku")
        + _recs(CANDIDATE, FAILURE, 10, served_model="haiku")
    )
    assert analyze(recs).verdict == INCONCLUSIVE


def test_sampling_noise_does_not_fake_a_verdict():
    # 8/30 candidate vs 4/30 baseline (a plausible draw for genuinely-gross
    # arms) must NOT read NO_GROSS — the missing CI was the old HIGH bug.
    recs = (
        _recs(BASELINE, SUCCESS, 26, served_model="opus")
        + _recs(BASELINE, FAILURE, 4, served_model="opus")
        + _recs(CANDIDATE, SUCCESS, 22, served_model="haiku")
        + _recs(CANDIDATE, FAILURE, 8, served_model="haiku")
    )
    assert analyze(recs).verdict == INCONCLUSIVE


def test_no_data_when_no_experiment_traffic():
    assert analyze([]).verdict == NO_DATA


def test_not_exercised_on_no_op_swap():
    recs = _recs(BASELINE, SUCCESS, _N, served_model="same") + _recs(
        CANDIDATE, SUCCESS, _N, served_model="same"
    )
    report = analyze(recs)
    assert report.verdict == NOT_EXERCISED
    assert any("no real swap" in w for w in report.warnings)


def test_not_exercised_when_candidate_arm_empty():
    recs = _recs(BASELINE, SUCCESS, _N, served_model="opus")
    assert analyze(recs).verdict == NOT_EXERCISED


def test_ceiling_share_is_tracked_and_warned():
    recs = _recs(BASELINE, SUCCESS, _N, served_model="opus") + _recs(
        CANDIDATE, FAILURE, _N, served_model="haiku", ceiling=True
    )
    report = analyze(recs)
    assert report.candidate.ceiling == _N
    assert any("ceiling" in w for w in report.warnings)


def test_contaminated_tasks_are_counted_and_excluded():
    recs = (
        _recs(BASELINE, SUCCESS, _N, served_model="opus")
        + _recs(CANDIDATE, SUCCESS, _N, served_model="haiku")
        + [TaskRecord("c1", None, 3, CONTAMINATED, "x")]
    )
    report = analyze(recs)
    assert report.contaminated == 1 and report.candidate.reported == _N


def test_savings_from_per_task_cost():
    recs = _recs(BASELINE, SUCCESS, _N, served_model="opus", cost=1.0) + _recs(
        CANDIDATE, SUCCESS, _N, served_model="haiku", cost=0.25
    )
    report = analyze(recs)
    assert report.savings_pct == pytest.approx(0.75)  # 75% cheaper/task


def test_unused_score_is_warned():
    rows = [
        _row(f"b{i}", BASELINE, success=True, served_model="opus")
        for i in range(_N)
    ] + [
        _row(f"c{i}", CANDIDATE, score=8.0, served_model="haiku")
        for i in range(_N)
    ]
    report = run_tripwire(rows, now=_NOW, idle_seconds=_IDLE)  # no fail_below
    assert any("no --fail-below" in w for w in report.warnings)


# --- store aggregate + CLI wiring -----------------------------------------


def _trace(
    task_id,
    arm,
    *,
    experiment_id,
    served_model="m",
    terminal_reason=None,
    status=200,
    cost=None,
    ts=0.0,
):
    return Trace(
        method="POST",
        path="/v1/messages",
        query="",
        request_headers={},
        request_body=b"{}",
        status_code=status,
        response_headers={},
        response_body=b"",
        latency_ms=1.0,
        task_id=task_id,
        experiment_id=experiment_id,
        arm=arm,
        served_model=served_model,
        terminal_reason=terminal_reason,
        cost_usd=cost,
        ts=ts,
    )


async def test_experiment_task_rows_aggregates(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "t.db"))
    try:
        store.create_experiment(
            Experiment(
                "tag:editor", "claude-haiku-4-5", 50, experiment_id="exp:e"
            )
        )
        await store.save(
            _trace(
                "t1",
                CANDIDATE,
                experiment_id="exp:e",
                served_model="hk",
                cost=0.1,
            )
        )
        await store.save(
            _trace(
                "t1",
                CANDIDATE,
                experiment_id="exp:e",
                served_model="hk",
                terminal_reason="ceiling",
                status=429,
                cost=0.0,
            )
        )
        await store.save(
            _trace("t2", BASELINE, experiment_id="exp:e", served_model="op")
        )
        await store.save_outcome(Outcome("t2", success=True))
        # a NULL-task_id trace under the experiment must NOT fuse into a task
        await store.save(_trace(None, CANDIDATE, experiment_id="exp:e"))
        rows = {r["task_id"]: r for r in store.experiment_task_rows("exp:e")}
    finally:
        store.close()
    assert None not in rows  # NULL-task rows are excluded, not a phantom task
    assert rows["t1"]["calls"] == 2 and rows["t1"]["arm"] == CANDIDATE
    assert rows["t1"]["arms"] == 1 and rows["t1"]["ceilings"] == 1
    assert rows["t1"]["cost"] == pytest.approx(0.1)
    assert rows["t2"]["success"] is True and rows["t2"]["arm"] == BASELINE


async def test_step_scoped_tripwire_uses_step_events_not_task_outcomes(
    tmp_path,
):
    store = SqliteTraceStore(str(tmp_path / "step.db"))
    identity = WorkflowIdentity("t1", "pipeline", "v1", "draft", "run-draft")
    experiment = Experiment(
        "tag:editor",
        "candidate",
        50,
        experiment_id="exp:step",
        workflow="pipeline",
        workflow_version="v1",
        step="draft",
    )
    trace = _trace("t1", CANDIDATE, experiment_id="exp:step")
    trace.workflow = "pipeline"
    trace.workflow_version = "v1"
    trace.step = "draft"
    trace.step_run_id = "run-draft"
    try:
        store.create_experiment(experiment)
        await store.save(trace)
        await store.save_outcome(Outcome("t1", success=False))
        await store.save_workflow_event(
            WorkflowEvent(identity, "completed", success=True)
        )
        row = store.experiment_task_rows("exp:step")[0]
    finally:
        store.close()
    assert row["success"] is True


def test_workflow_tripwire_records_paths_and_rejects_mixed_versions(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "workflow.db"))
    experiment = Experiment(
        "tag:editor",
        "candidate",
        50,
        experiment_id="exp:workflow",
        workflow="pipeline",
        workflow_version="v1",
    )

    def scoped_trace(task_id, arm, version="v1"):
        trace = _trace(task_id, arm, experiment_id="exp:workflow")
        trace.workflow = "pipeline"
        trace.workflow_version = version
        trace.step = "draft"
        trace.step_run_id = f"{task_id}-draft"
        return trace

    try:
        store.create_experiment(experiment)
        store._insert(scoped_trace("pure", CANDIDATE))
        for index, step in enumerate(("draft", "review"), start=1):
            identity = WorkflowIdentity(
                "pure", "pipeline", "v1", step, f"pure-{step}"
            )
            store._insert_workflow_event(
                WorkflowEvent(identity, "started", ts=float(index))
            )
        store._insert(scoped_trace("mixed", BASELINE))
        store._insert(scoped_trace("mixed", BASELINE, version="v2"))

        rows = {
            row["task_id"]: row
            for row in store.experiment_task_rows("exp:workflow")
        }
    finally:
        store.close()

    assert rows["pure"]["path"] == ("draft#1", "review#1")
    assert len(rows["pure"]["path_digest"]) == 16
    assert rows["pure"]["workflow_identities"] == 1
    assert rows["mixed"]["workflow_identities"] == 2
    assert (
        classify_task(
            rows["mixed"], now=_NOW, idle_seconds=_IDLE, fail_below=None
        ).status
        == CONTAMINATED
    )


async def test_experiment_task_rows_flags_cross_experiment_straddle(tmp_path):
    store = SqliteTraceStore(str(tmp_path / "t.db"))
    try:
        store.create_experiment(
            Experiment("tag:a", "m1", 50, experiment_id="exp:a")
        )
        await store.save(_trace("t9", CANDIDATE, experiment_id="exp:a"))
        await store.save(_trace("t9", BASELINE, experiment_id="exp:b"))
        rows = {r["task_id"]: r for r in store.experiment_task_rows("exp:a")}
    finally:
        store.close()
    assert rows["t9"]["experiments"] == 2


async def test_status_cli_wires_end_to_end(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.db"
    monkeypatch.setenv("CTRLRTN_DB", str(db))
    store = SqliteTraceStore(str(db))
    store.create_experiment(
        Experiment("tag:editor", "claude-haiku-4-5", 50, experiment_id="exp:e")
    )
    await store.save(_trace("t1", CANDIDATE, experiment_id="exp:e"))
    store.close()

    with pytest.raises(SystemExit) as exit_info:
        main(["experiment", "status", "exp:e"])
    assert exit_info.value.code == 3  # one task -> underpowered
    out = capsys.readouterr().out
    assert "VERDICT: UNDERPOWERED" in out and "exp:e" in out


def test_status_cli_unknown_experiment_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CTRLRTN_DB", str(tmp_path / "cli.db"))
    with pytest.raises(SystemExit) as exit_info:
        main(["experiment", "status", "exp:nope"])
    assert exit_info.value.code == 2
    assert "No experiment" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flag,value,needle",
    [
        ("--min-tasks", "1", "min-tasks"),
        ("--gross-margin", "1.5", "gross-margin"),
        ("--idle-minutes", "0", "idle-minutes"),
    ],
)
def test_status_cli_validates_args(
    tmp_path, monkeypatch, capsys, flag, value, needle
):
    monkeypatch.setenv("CTRLRTN_DB", str(tmp_path / "cli.db"))
    with pytest.raises(SystemExit) as exit_info:
        main(["experiment", "status", "exp:e", flag, value])
    assert exit_info.value.code == 2
    assert needle in capsys.readouterr().err


def test_safe_verdict_recommends_route_adopt():
    # The verdict's actionable next step rides the render everywhere it is
    # read (CLI `experiment status` and the console detail pane).
    from ctrlrtn.analysis.report import render_tripwire

    recs = _recs(BASELINE, SUCCESS, _N, served_model="opus") + _recs(
        CANDIDATE, SUCCESS, _N, served_model="haiku"
    )
    exp = Experiment("tag:editor", "haiku", 50, experiment_id="exp:safe")
    text = render_tripwire(analyze(recs), exp)
    assert "recommendation: adopt" in text
    assert "route adopt exp:safe" in text
    # ...and only on the safe verdict: an underpowered run recommends nothing.
    under = _recs(BASELINE, SUCCESS, 5, served_model="opus") + _recs(
        CANDIDATE, SUCCESS, 5, served_model="haiku"
    )
    assert "recommendation: adopt" not in render_tripwire(analyze(under), exp)
