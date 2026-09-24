"""Paired non-inferiority test (ctrlrtn.eval.ni)."""

from __future__ import annotations

import random

import pytest

from ctrlrtn.eval.ni import paired_ni


def test_equal_arms_are_non_inferior():
    diffs = [0.0, 0.1, -0.1, 0.0, 0.2, -0.2] * 10  # n=60, mean 0
    result = paired_ni(diffs, margin=0.5)
    assert result.non_inferior
    assert result.lower_bound > -0.5
    assert not result.underpowered
    assert result.n_units == 60


def test_clearly_worse_candidate_is_not_non_inferior():
    diffs = [-2.0, -1.8, -2.2, -2.0, -1.9] * 12  # n=60, mean ~ -2
    result = paired_ni(diffs, margin=0.5)
    assert not result.non_inferior
    assert result.mean_diff < -1.5
    assert result.lower_bound < -0.5


def test_within_margin_low_variance_concludes():
    diffs = [-0.3, -0.3, -0.31, -0.29, -0.3] * 12  # n=60, tight, inside 0.5
    result = paired_ni(diffs, margin=0.5)
    assert result.non_inferior


def test_within_margin_but_noisy_does_not_conclude():
    diffs = [-3.0, 2.4] * 30  # n=60, mean -0.3 but large spread
    result = paired_ni(diffs, margin=0.5)
    assert not result.non_inferior


def test_underpowered_never_concludes():
    diffs = [0.0, 0.0, 0.0]  # n=3, below the minimum units
    result = paired_ni(diffs, margin=0.5)
    assert not result.non_inferior
    assert result.underpowered
    assert result.lower_bound == float("-inf")


def test_mean_diff_sign_and_direction():
    result = paired_ni([1.0] * 30, margin=0.5)
    assert result.mean_diff == pytest.approx(1.0)  # candidate scored higher
    assert result.non_inferior


def test_deterministic_for_a_seed():
    diffs = [-0.4, 0.1, -0.2, 0.3, -0.5] * 8
    a = paired_ni(diffs, margin=0.5, seed=7)
    b = paired_ni(diffs, margin=0.5, seed=7)
    assert a.lower_bound == b.lower_bound


def test_nonpositive_margin_rejected():
    with pytest.raises(ValueError):
        paired_ni([0.0] * 30, margin=0.0)


@pytest.mark.parametrize("bad", [0.0, 1.0, 1.5, -0.1])
def test_confidence_out_of_range_rejected(bad):
    with pytest.raises(ValueError):
        paired_ni([0.0] * 30, margin=0.5, confidence=bad)


def test_too_few_bootstrap_resamples_rejected():
    # alpha*n_boot must be >= 100 for a stable lower quantile.
    with pytest.raises(ValueError):
        paired_ni([0.0] * 30, margin=0.5, confidence=0.95, n_boot=100)


def test_clusters_must_align_with_diffs():
    with pytest.raises(ValueError):
        paired_ni([0.0] * 30, margin=0.5, clusters=[1, 2, 3])


def test_clustering_reduces_effective_sample_size():
    # 60 near-identical diffs in 5 correlated clusters. Treated as independent
    # they look like n=60 and conclude; resampled by cluster they are n_units=5,
    # which is underpowered — the honest answer for correlated replay.
    diffs = [-0.3] * 60
    flat = paired_ni(diffs, margin=0.5)
    clustered = paired_ni(
        diffs, margin=0.5, clusters=[i // 12 for i in range(60)]
    )
    assert flat.non_inferior  # naive (independent) view concludes
    assert clustered.n_units == 5
    assert clustered.underpowered  # cluster view: too few independent units
    assert not clustered.non_inferior


def test_coverage_at_the_boundary_is_not_anticonservative():
    # The key guard (statistician's request): when the candidate is EXACTLY at
    # the margin (true mean diff = -margin), a one-sided 95% test should wrongly
    # conclude non-inferiority only ~5% of the time. A grossly anti-conservative
    # method (which would license bad downgrades) blows past that.
    margin = 0.5
    batches = 120
    n = 40
    false_ni = 0
    for b in range(batches):
        rng = random.Random(1000 + b)
        diffs = [rng.gauss(-margin, 1.0) for _ in range(n)]
        result = paired_ni(diffs, margin=margin, n_boot=2000, seed=b)
        if result.non_inferior:
            false_ni += 1
    rate = false_ni / batches
    assert rate <= 0.15  # nominal 0.05; loose bound for MC + small-n slack
