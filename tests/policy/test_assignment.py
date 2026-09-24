"""Arm assignment: the pure, deterministic bucket -> arm -> ServeDecision path
(live A/B slice 2). No network, no store — just the hashing and the swap rule.
"""

from __future__ import annotations

import pytest

from ctrlrtn.gateway.decision import ServingDecision
from ctrlrtn.policy.experiment import (
    BASELINE,
    CANDIDATE,
    Experiment,
    ServeDecision,
    assign_arm,
    bucket,
    decide,
)
from ctrlrtn.policy.route import RouteDecision


def _exp(split: int, candidate: str = "claude-haiku-4-5") -> Experiment:
    return Experiment("fp:editor", candidate, split, experiment_id="exp:fixed")


def test_experiment_and_route_decisions_share_the_gateway_contract():
    experiment = ServeDecision(
        "exp:x", "fp:editor", "task-1", CANDIDATE, "cheap", "expensive"
    )
    route = RouteDecision("fp:editor", "cheap", "expensive")

    assert isinstance(experiment, ServingDecision)
    assert isinstance(route, ServingDecision)


def _task_with_bucket(experiment_id: str, target: int) -> str:
    """A task id that hashes to exactly ``target`` — for pinning the cut."""
    for i in range(100_000):
        task = f"task-{i}"
        if bucket(experiment_id, task) == target:
            return task
    raise AssertionError(f"no task hit bucket {target}")


# --- bucket ---------------------------------------------------------------


def test_bucket_is_in_range():
    for i in range(200):
        assert 0 <= bucket("exp:fixed", f"task-{i}") <= 99


def test_bucket_is_stable_for_the_same_inputs():
    a = bucket("exp:fixed", "edition-2026-06-30")
    b = bucket("exp:fixed", "edition-2026-06-30")
    assert a == b


def test_bucket_re_randomizes_across_experiments():
    # Keying on experiment_id means a fresh experiment reshuffles tasks: most
    # tasks land in a different bucket under a different experiment_id.
    differ = sum(
        bucket("exp:one", f"t{i}") != bucket("exp:two", f"t{i}")
        for i in range(200)
    )
    assert differ > 180  # ~99% expected; allow a wide margin


def test_bucket_spreads_tasks_not_constant():
    spread = {bucket("exp:fixed", f"t{i}") for i in range(100)}
    assert len(spread) > 30


def test_bucket_golden_value_locks_cross_process_stability():
    # A regression to a salted/host-dependent hash, or a [:8]/endianness/
    # separator change, would move this. sha256 must stay reproducible.
    assert bucket("exp:fixed", "edition-2026-06-30") == 81


def test_bucket_rejects_empty_ids():
    with pytest.raises(ValueError):
        bucket("exp:fixed", "")
    with pytest.raises(ValueError):
        bucket("", "task-1")
    with pytest.raises(ValueError):
        bucket("exp:fixed", None)  # type: ignore[arg-type]


def test_bucket_split_is_roughly_uniform():
    # Over many tasks, ~split% fall below the cut — the swap rule relies on this.
    exp = _exp(30)
    n = 5000
    candidates = sum(
        assign_arm(exp, f"task-{i}") == CANDIDATE for i in range(n)
    )
    frac = candidates / n
    assert 0.25 < frac < 0.35  # 30% ± 5pp


# --- assign_arm -----------------------------------------------------------


def test_assign_arm_boundary_is_strict_less_than():
    # The classic off-by-one: bucket == split_pct must be BASELINE (cut is
    # strictly <). Pin a task on each side of the cut at split=40.
    exp = _exp(40)
    on_cut = _task_with_bucket(exp.experiment_id, 40)
    below = _task_with_bucket(exp.experiment_id, 39)
    assert assign_arm(exp, below) == CANDIDATE  # 39 < 40
    assert assign_arm(exp, on_cut) == BASELINE  # 40 is NOT < 40


def test_split_monotonicity_flips_once_in_the_right_direction():
    # For a fixed task (fixed bucket b), raising split 1->99 flips the arm from
    # baseline to candidate exactly once (at split = b+1), never back.
    task = "edition-77"
    arms = [assign_arm(_exp(s), task) for s in range(1, 100)]  # split 1..99
    transitions = [i for i in range(1, len(arms)) if arms[i] != arms[i - 1]]
    assert len(transitions) <= 1  # never oscillates
    if transitions:
        i = transitions[0]
        assert arms[i - 1] == BASELINE and arms[i] == CANDIDATE


def test_a_task_binds_to_one_arm_across_calls():
    exp = _exp(50)
    task = "edition-42"
    arms = {assign_arm(exp, task) for _ in range(20)}
    assert len(arms) == 1  # stateless => same arm every time


# --- decide ---------------------------------------------------------------


def test_candidate_arm_serves_the_candidate_model():
    exp = _exp(100 - 1, candidate="claude-3-5-haiku")  # 99% candidate
    task = _task_with_bucket(exp.experiment_id, 0)  # bucket 0 => candidate
    d = decide(exp, task, requested_model="claude-opus-4")
    assert d.is_candidate and not d.is_baseline
    assert d.served_model == "claude-3-5-haiku"
    assert d.original_model == "claude-opus-4"  # what the client asked for
    assert (d.experiment_id, d.use_case_key, d.task_id) == (
        "exp:fixed",
        "fp:editor",
        task,
    )


def test_baseline_arm_passes_the_requested_model_through():
    exp = _exp(1)  # 1% candidate => bucket 99 is baseline
    task = _task_with_bucket(exp.experiment_id, 99)
    d = decide(exp, task, requested_model="claude-opus-4")
    assert d.is_baseline and not d.is_candidate
    assert d.served_model == "claude-opus-4"  # unchanged on baseline
    assert d.original_model == "claude-opus-4"


def test_original_model_makes_a_noop_swap_detectable():
    # If the candidate model equals what the client requested, both arms serve
    # the same model — a degenerate experiment. Closure can catch it because
    # served == original on the candidate arm.
    exp = _exp(99, candidate="claude-opus-4")
    task = _task_with_bucket(exp.experiment_id, 0)  # candidate arm
    d = decide(exp, task, requested_model="claude-opus-4")
    assert d.is_candidate
    assert d.served_model == d.original_model == "claude-opus-4"


def test_serve_decision_rejects_an_unknown_arm():
    with pytest.raises(ValueError, match="arm"):
        ServeDecision("exp:x", "fp:e", "task-1", "sideways", "m", "m")


def test_decide_rejects_a_stopped_experiment():
    exp = _exp(50).stopped()
    with pytest.raises(ValueError, match="running"):
        decide(exp, "task-1", "claude-opus-4")


def test_decide_rejects_a_modelless_request():
    with pytest.raises(ValueError, match="requested_model"):
        decide(_exp(50), "task-1", "")
