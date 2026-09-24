"""Judge calibration against human labels — an advisory agreement check."""

from __future__ import annotations

import json
import re

import pytest

from ctrlrtn.cli.evaluation.loading import _load_labels
from ctrlrtn.eval.calibration import (
    ALIGNED,
    INSUFFICIENT,
    MISALIGNED,
    LabeledPairing,
    calibrate,
)
from ctrlrtn.eval.judge import Pairing


def _content_judge(prompt: str) -> str:
    """A deterministic judge: each output embeds its own score as ``SCORE_x``, so
    the reply is content-based (position-independent) — de-blinding in
    ``judge_pairing`` then yields a stable candidate-minus-baseline diff."""
    scores = re.findall(r"SCORE_([0-9.]+)", prompt)
    return json.dumps(
        {"score_a": float(scores[0]), "score_b": float(scores[1])}
    )


def _labeled(judge_base, judge_cand, human_base, human_cand, task="t"):
    return LabeledPairing(
        pairing=Pairing(
            task=task,
            baseline_output=f"SCORE_{judge_base}",
            candidate_output=f"SCORE_{judge_cand}",
        ),
        human_baseline=human_base,
        human_candidate=human_cand,
    )


def _agreeing_set(n):
    # Candidate a little worse than baseline; judge and human both see it, with
    # a little spread so there's directional signal, a ~1.0 slope, and a small
    # bias. Every pair is directional (human_diff != 0).
    out = []
    for i in range(n):
        bump = (i % 3) * 0.2
        out.append(_labeled(8.0, 7.0 + bump, 8.0, 7.2 + bump))
    return out


# --- verdicts --------------------------------------------------------------


def test_agreeing_judge_is_aligned():
    report = calibrate(_content_judge, _agreeing_set(24))
    assert report.verdict == ALIGNED
    assert report.agreement_rate == 1.0  # same winner every directional pair
    assert report.agreement_lb > 0.5  # and distinguishable from a coin flip
    assert 0.5 <= report.slope  # not compressing
    assert abs(report.bias) <= report.bias_limit
    assert report.correlation > 0.0


def test_too_few_directional_pairs_is_insufficient():
    report = calibrate(_content_judge, _agreeing_set(10))
    assert report.verdict == INSUFFICIENT
    assert report.n == 10 and report.n_directional == 10


def test_all_human_ties_is_insufficient():
    # No human preference anywhere -> no directional pairs, nothing to check.
    labeled = [_labeled(8.0, 6.0, 7.0, 7.0) for _ in range(24)]
    report = calibrate(_content_judge, labeled)
    assert report.verdict == INSUFFICIENT
    assert report.n_directional == 0


def test_judge_that_disagrees_is_misaligned():
    # Humans prefer the baseline (diff < 0); the judge prefers the candidate
    # (diff > 0). Opposite signs every time -> zero agreement, with spread.
    labeled = []
    for i in range(24):
        bump = (i % 3) * 0.2
        labeled.append(_labeled(7.0, 8.0 + bump, 8.0, 7.0 + bump))
    report = calibrate(_content_judge, labeled)
    assert report.verdict == MISALIGNED
    assert report.agreement_rate == 0.0


def test_random_judge_fails_the_wilson_floor():
    # Exactly half the directional pairs agree -> agreement 0.5, whose Wilson
    # lower bound cannot clear 0.5, so a coin-flip judge is caught.
    labeled = []
    for i in range(24):
        agree = i % 2 == 0
        # human always prefers the baseline; judge agrees on evens only.
        judge_cand = 7.0 if agree else 9.0
        labeled.append(_labeled(8.0, judge_cand, 8.0, 7.0))
    report = calibrate(_content_judge, labeled)
    assert report.agreement_rate == 0.5
    assert report.agreement_lb <= report.min_agreement_lb
    assert report.verdict == MISALIGNED


def test_judge_that_compresses_real_gaps_is_misaligned():
    # The dangerous failure a mean-bias check misses: the judge agrees on the
    # winner every time (agreement 1.0) and has ~0 bias, but reports only 30% of
    # the real gap -> slope 0.3 < 0.5. Only the compression check catches it.
    labeled = []
    for hd in [-3.0, -2.0, -1.0] * 8:  # 24 pairs, spread, all candidate-worse
        labeled.append(_labeled(8.0, 8.0 + 0.3 * hd, 8.0, 8.0 + hd))
    report = calibrate(_content_judge, labeled)
    assert report.verdict == MISALIGNED
    assert report.agreement_rate == 1.0  # not an agreement failure
    assert report.slope < 0.5
    assert abs(report.bias) < 0.5  # nor a bias one — purely compression


def test_judge_that_over_rates_the_candidate_is_misaligned():
    # Sign agrees (both prefer the candidate) but the judge is +1.9 above the
    # human at parity — a pro-candidate bias past the default ceiling.
    labeled = []
    for i in range(24):
        bump = (i % 3) * 0.2
        labeled.append(_labeled(6.0, 8.0 + bump, 7.9, 8.0 + bump))
    report = calibrate(_content_judge, labeled)
    assert report.verdict == MISALIGNED
    assert report.agreement_rate == 1.0  # not an agreement failure — a bias one
    assert report.bias > report.bias_limit


def test_a_wider_margin_absorbs_the_same_bias():
    # bias_limit = margin/2, so margin=6.0 tolerates the +1.9 bias.
    labeled = []
    for i in range(24):
        bump = (i % 3) * 0.2
        labeled.append(_labeled(6.0, 8.0 + bump, 7.9, 8.0 + bump))
    report = calibrate(_content_judge, labeled, margin=6.0)
    assert report.verdict == ALIGNED
    assert report.bias_limit == 3.0


# --- metrics + robustness --------------------------------------------------


def test_win_rates_reflect_the_direction():
    # Judge and human both mark the candidate the loser every time.
    labeled = [_labeled(8.0, 7.0, 8.0, 7.0) for _ in range(12)]
    report = calibrate(_content_judge, labeled)
    assert report.candidate_win_rate_judge == 0.0
    assert report.candidate_win_rate_human == 0.0


def test_under_rating_judge_warns_but_is_not_gated():
    # A conservative judge (under-rates the candidate) is safe for downgrades,
    # so it isn't MISALIGNED on bias, but it earns a warning.
    labeled = []
    for i in range(24):
        bump = (i % 3) * 0.2
        # judge sees the candidate ~1.5 worse than the human does, at parity.
        labeled.append(_labeled(8.0, 6.5 + bump, 8.0, 8.0 + bump))
    report = calibrate(_content_judge, labeled)
    assert report.bias < -report.bias_limit
    assert any("under-rates" in w for w in report.warnings)


def test_failed_judge_calls_are_skipped_and_counted():
    def flaky(prompt: str) -> str:
        if "FAIL" in prompt:
            return "no scores in here at all"  # -> ValueError in the judge
        return _content_judge(prompt)

    labeled = _agreeing_set(24) + [_labeled(8.0, 7.0, 8.0, 7.0, task="FAIL")]
    report = calibrate(flaky, labeled)
    assert report.n == 24 and report.n_failed == 1
    assert any("skipped" in w for w in report.warnings)


def test_negative_margin_is_rejected():
    with pytest.raises(ValueError, match="margin must be > 0"):
        calibrate(_content_judge, _agreeing_set(24), margin=-1.0)


# --- CLI: label loading (sidecar de-blind), render, end-to-end -------------


def _write_label_files(tmp_path, rows, name="labels.jsonl"):
    """Write a blinded labels file plus its sidecar `.key`, as `calibration-set`
    would (the human never opens the key). `rows` carry the de-blinded intent.
    """
    labels = tmp_path / name
    key = tmp_path / (name + ".key")
    with labels.open("w") as lf, key.open("w") as kf:
        for i, row in enumerate(rows):
            lf.write(
                json.dumps(
                    {
                        "id": i,
                        "task": row.get("task", "t"),
                        "output_a": row["output_a"],
                        "output_b": row["output_b"],
                        "score_a": row["score_a"],
                        "score_b": row["score_b"],
                    }
                )
                + "\n"
            )
            kf.write(
                json.dumps({"id": i, "candidate_is_a": row["candidate_is_a"]})
                + "\n"
            )
    return str(labels), key


def test_load_labels_deblinds_both_orderings(tmp_path):

    path, _ = _write_label_files(
        tmp_path,
        [
            {
                "candidate_is_a": True,
                "output_a": "cand",
                "output_b": "base",
                "score_a": 7.0,
                "score_b": 9.0,
            },
            {
                "candidate_is_a": False,
                "output_a": "base",
                "output_b": "cand",
                "score_a": 9.0,
                "score_b": 6.0,
            },
        ],
    )
    labeled = _load_labels(path)
    assert len(labeled) == 2
    # row 0: candidate shown as A -> its human score is score_a.
    assert labeled[0].pairing.candidate_output == "cand"
    assert (
        labeled[0].human_candidate == 7.0 and labeled[0].human_baseline == 9.0
    )
    # row 1: candidate shown as B -> de-blinds to the same arms.
    assert labeled[1].pairing.candidate_output == "cand"
    assert (
        labeled[1].human_candidate == 6.0 and labeled[1].human_baseline == 9.0
    )


def test_load_labels_rejects_out_of_range_score(tmp_path):

    path, _ = _write_label_files(
        tmp_path,
        [
            {
                "candidate_is_a": True,
                "output_a": "a",
                "output_b": "b",
                "score_a": 11.0,
                "score_b": 5.0,
            }
        ],
    )
    with pytest.raises(SystemExit):
        _load_labels(path)


def test_load_labels_requires_the_sidecar_key(tmp_path):

    path, key = _write_label_files(
        tmp_path,
        [
            {
                "candidate_is_a": True,
                "output_a": "a",
                "output_b": "b",
                "score_a": 6.0,
                "score_b": 5.0,
            }
        ],
    )
    key.unlink()  # lose the de-blind key -> can't un-blind, must fail loudly
    with pytest.raises(SystemExit):
        _load_labels(path)


def test_load_labels_rejects_stringified_bool_in_key(tmp_path):

    path, key = _write_label_files(
        tmp_path,
        [
            {
                "candidate_is_a": True,
                "output_a": "a",
                "output_b": "b",
                "score_a": 6.0,
                "score_b": 5.0,
            }
        ],
    )
    # A stringified "false" would coerce to True and invert the de-blind.
    key.write_text(json.dumps({"id": 0, "candidate_is_a": "false"}) + "\n")
    with pytest.raises(SystemExit):
        _load_labels(path)


def test_render_calibration_smoke():
    from ctrlrtn.cli.render import render_calibration

    text = render_calibration(calibrate(_content_judge, _agreeing_set(24)))
    assert "VERDICT: ALIGNED" in text
    assert "advisory" in text
    assert "agreement" in text and "slope" in text and "bias@parity" in text


def test_calibrate_command_exits_zero_when_aligned(tmp_path, monkeypatch):
    from ctrlrtn.cli import commands as cli

    rows = []
    for i in range(24):
        bump = (i % 3) * 0.2
        rows.append(
            {
                "candidate_is_a": True,
                "output_a": f"SCORE_{7.0 + bump}",  # candidate
                "output_b": "SCORE_8.0",  # baseline
                "score_a": 7.2 + bump,  # human's candidate score
                "score_b": 8.0,
            }
        )
    path, _ = _write_label_files(tmp_path, rows)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    # Stub the network judge with the deterministic content judge (no calls).
    monkeypatch.setattr(
        cli, "anthropic_judge_fn", lambda *a, **k: _content_judge
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["calibrate", path])
    assert exit_info.value.code == 0  # ALIGNED


def test_calibrate_command_exits_one_when_misaligned(tmp_path, monkeypatch):
    from ctrlrtn.cli import commands as cli

    # A compressing judge: reports 30% of the real gap -> slope 0.3 -> MISALIGNED.
    rows = []
    for hd in [-3.0, -2.0, -1.0] * 8:
        rows.append(
            {
                "candidate_is_a": True,
                "output_a": f"SCORE_{8.0 + 0.3 * hd}",  # candidate (judged)
                "output_b": "SCORE_8.0",  # baseline
                "score_a": 8.0 + hd,  # human's candidate score
                "score_b": 8.0,
            }
        )
    path, _ = _write_label_files(tmp_path, rows)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(
        cli, "anthropic_judge_fn", lambda *a, **k: _content_judge
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["calibrate", path])
    assert exit_info.value.code == 1  # MISALIGNED


def test_calibration_set_streams_and_survives_failures(tmp_path, monkeypatch):
    """`calibration-set` must not abort or discard the paid batch when one input
    fails or replays blank, and the two files it writes must stay joinable."""
    from ctrlrtn.cli import commands as cli

    bodies = [
        json.dumps(
            {"messages": [{"role": "user", "content": f"in-{i}"}], "marker": i}
        ).encode()
        for i in range(4)
    ]

    class FakeStore:
        def __init__(self, *a, **k):
            pass

        def requests_for_use_case(self, use_case, n):
            return [{"request_body": b} for b in bodies]

        def use_case_models(self):
            return {"uc": "base-model"}

        def close(self):
            pass

    def fake_replay_fn(api_key, *, client, max_tokens):
        def replay(body, model):
            marker = json.loads(body)["marker"]
            if marker == 1:
                raise RuntimeError("mid-batch failure")  # skipped, not fatal
            if marker == 2:
                return ""  # both arms blank -> skipped
            return f"SCORE_{7.0 if model == 'cand' else 8.0}"

        return replay

    monkeypatch.setattr(cli, "SqliteTraceStore", FakeStore)
    monkeypatch.setattr(cli, "anthropic_replay_fn", fake_replay_fn)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    out = tmp_path / "labels.jsonl"
    cli.main(["calibration-set", "uc", "cand", "--out", str(out), "--yes"])

    # markers 0 and 3 survive (1 raised, 2 blank) -> 2 contiguous pairs.
    labels = [json.loads(x) for x in out.read_text().splitlines()]
    keys = [
        json.loads(x)
        for x in (tmp_path / "labels.jsonl.key").read_text().splitlines()
    ]
    assert [r["id"] for r in labels] == [0, 1]
    assert [k["id"] for k in keys] == [0, 1]
    assert "candidate_is_a" not in labels[0]  # blind intact in the human file

    # The survivors round-trip through the loader once scored.
    for r in labels:
        r["score_a"], r["score_b"] = 7.0, 8.0
    out.write_text("\n".join(json.dumps(r) for r in labels) + "\n")
    loaded = _load_labels(str(out))
    assert len(loaded) == 2
    assert all(lp.pairing.candidate_output == "SCORE_7.0" for lp in loaded)
