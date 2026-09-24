"""Blinded pairwise judge harness (ctrlrtn.eval.judge)."""

from __future__ import annotations

import json

import pytest

from ctrlrtn.eval.judge import Pairing, build_judge_prompt, judge_pairing


def _fixed(score_a, score_b):
    """A judge that always returns the same scores regardless of content."""
    return lambda prompt: json.dumps({"score_a": score_a, "score_b": score_b})


def _content_judge(prompt):
    """Scores by content: whichever response contains 'GOOD' gets 9, the other
    4. Used to test de-blinding of a true (content-driven) preference."""
    a = prompt.split("# Response A", 1)[1].split("# Response B", 1)[0]
    score_a = 9.0 if "GOOD" in a else 4.0
    score_b = 4.0 if "GOOD" in a else 9.0
    return json.dumps({"score_a": score_a, "score_b": score_b})


def _pairing(baseline="b", candidate="c"):
    return Pairing(
        task="t", baseline_output=baseline, candidate_output=candidate
    )


def test_prompt_never_leaks_arm_or_position():
    prompt = build_judge_prompt("do X", "base out", "cand out")
    assert "baseline" not in prompt.lower()
    assert "candidate" not in prompt.lower()
    assert "Response A" in prompt and "Response B" in prompt


def test_position_bias_cancels_across_replicates():
    # A judge that always prefers position A by 3 points. With one replicate per
    # position, that bias lands on each arm once and cancels in the mean.
    score = judge_pairing(_fixed(8.0, 5.0), _pairing(), replicates=2)
    assert score.baseline == pytest.approx(6.5)
    assert score.candidate == pytest.approx(6.5)
    assert score.diff == pytest.approx(0.0)


def test_position_bias_cancels_at_four_replicates():
    score = judge_pairing(_fixed(8.0, 5.0), _pairing(), replicates=4)
    assert score.diff == pytest.approx(0.0)


def test_true_preference_survives_blinding():
    # Candidate output is genuinely better (contains GOOD); regardless of which
    # position it is shown in, it must score higher after de-blinding.
    score = judge_pairing(
        _content_judge,
        _pairing(baseline="meh", candidate="this is GOOD"),
        replicates=2,
    )
    assert score.candidate == pytest.approx(9.0)
    assert score.baseline == pytest.approx(4.0)
    assert score.diff == pytest.approx(5.0)


def test_baseline_preference_de_blinds_correctly():
    score = judge_pairing(
        _content_judge,
        _pairing(baseline="this is GOOD", candidate="meh"),
        replicates=2,
    )
    assert score.baseline == pytest.approx(9.0)
    assert score.candidate == pytest.approx(4.0)
    assert score.diff == pytest.approx(-5.0)


@pytest.mark.parametrize("bad", [0, 1, 3, 5])
def test_odd_or_low_replicates_rejected(bad):
    with pytest.raises(ValueError):
        judge_pairing(_content_judge, _pairing(), replicates=bad)


def test_scores_parsed_from_prose_wrapped_json():
    judge = lambda p: 'Sure! Here you go: {"score_a": 7, "score_b": 3} done.'
    score = judge_pairing(judge, _pairing(), replicates=2)
    assert score.baseline == pytest.approx(5.0)  # 7 and 3 averaged over swap
    assert score.candidate == pytest.approx(5.0)


def test_prose_with_leading_braces_still_parses():
    judge = lambda p: 'use \\frac{a}{b}, then {"score_a": 6, "score_b": 4}'
    score = judge_pairing(judge, _pairing(), replicates=2)
    assert score.baseline == pytest.approx(5.0)


def test_picks_object_with_both_score_keys():
    judge = lambda p: '{"note": "thinking"} {"score_a": 6, "score_b": 4}'
    score = judge_pairing(judge, _pairing(), replicates=2)
    assert score.baseline == pytest.approx(5.0)


@pytest.mark.parametrize(
    "raw",
    [
        "no json here",
        '{"score_b": 3}',  # missing score_a
        '{"score_a": null, "score_b": 3}',  # non-numeric
        '{"score_a": NaN, "score_b": 3}',  # not finite
        '{"score_a": 50, "score_b": 3}',  # out of 0-10 scale
        '{"score_a": -1, "score_b": 3}',  # below scale
    ],
)
def test_bad_judge_output_raises_valueerror(raw):
    with pytest.raises(ValueError):
        judge_pairing(lambda p: raw, _pairing(), replicates=2)
