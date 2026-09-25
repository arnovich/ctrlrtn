"""Paired non-inferiority test for shadow-replay scores.

Replay yields paired ``(baseline, candidate)`` scores on the *same* inputs, so
the per-input/topic variance cancels — a paired test on the per-input difference
is far tighter (and reaches a usable sample size far sooner) than an unpaired
one. Replay is a **batch analysed once**, not a live trickle, so a fixed-sample
bootstrap lower bound is appropriate here; the always-valid / confidence-sequence
machinery is only needed for the live A/B path (see
``docs/evaluation.md``).

Convention: ``diff = candidate - baseline`` per pairing (higher = candidate
better). The candidate is **non-inferior** if it is not worse than baseline by
more than ``margin`` — ``mean(diff) >= -margin`` — concluded only when the
one-sided lower confidence bound on ``mean(diff)`` clears ``-margin``. ``margin``
is in the judge's score units, so it must match the declared scale.

Two correctness points the design depends on:

* **Coverage.** The lower bound is **BCa** (bias-corrected & accelerated), not a
  raw percentile — the percentile bootstrap undercovers at n≈20–60, and always
  in the anti-conservative direction (it would conclude non-inferiority too
  easily, i.e. a bad downgrade). ``test_ni`` includes a Monte-Carlo coverage
  check at the boundary as the empirical guard.
* **Independence.** The bootstrap resamples *clusters*, not individual pairings.
  Replayed inputs from the same task/day are correlated; resampling them
  independently understates variance (again anti-conservative). Pass ``clusters``
  (a label per diff); without it, each pairing is its own cluster and **the
  caller must guarantee independence**.

Even after this, low task volume limits power — a positive conclusion still
needs live calibration (judge reliability, the declared scale) before it drives
any real downgrade.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from statistics import NormalDist

# Below this many resampling units, never conclude — underpowered, and a lucky
# batch could cross the boundary. Surfaced honestly (``underpowered``), not
# silently.
_MIN_UNITS = 20
_NORMAL = NormalDist()


@dataclass
class NIResult:
    """The paired non-inferiority verdict for one replay batch.
    ``non_inferior`` is ``True`` only when the batch had enough independent
    units and the BCa lower bound cleared ``-margin``; an ``underpowered``
    result concludes nothing either way."""

    non_inferior: bool
    mean_diff: float  # candidate - baseline; >0 means candidate scored higher
    lower_bound: float  # one-sided (1 - confidence) lower bound on mean_diff
    n: int  # number of pairings
    n_units: int  # number of independent clusters resampled
    margin: float
    confidence: float
    underpowered: bool  # too few units to conclude


def paired_ni(
    diffs: list[float],
    *,
    margin: float,
    clusters: list | None = None,
    confidence: float = 0.95,
    n_boot: int = 5000,
    seed: int = 0,
) -> NIResult:
    """Decide non-inferiority from per-pairing ``candidate - baseline`` diffs.

    ``clusters`` gives a correlation-group label per diff (e.g. task id);
    resampling is done over these. Omit only when pairings are independent.
    """
    if margin <= 0:
        raise ValueError("margin must be > 0")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be in (0, 1)")
    alpha = 1.0 - confidence
    if n_boot * alpha < 100:
        raise ValueError(
            "n_boot too small for this confidence (need alpha*n_boot >= 100)"
        )
    if clusters is not None and len(clusters) != len(diffs):
        raise ValueError("clusters must align 1:1 with diffs")

    n = len(diffs)
    mean_diff = sum(diffs) / n if n else 0.0
    units = _group_units(diffs, clusters)
    n_units = len(units)
    underpowered = n_units < _MIN_UNITS
    lower = (
        float("-inf")
        if underpowered
        else _bca_lower(units, mean_diff, alpha, n_boot, seed)
    )
    return NIResult(
        non_inferior=(not underpowered) and lower > -margin,
        mean_diff=mean_diff,
        lower_bound=lower,
        n=n,
        n_units=n_units,
        margin=margin,
        confidence=confidence,
        underpowered=underpowered,
    )


def _group_units(
    diffs: list[float], clusters: list | None
) -> list[list[float]]:
    """Group diffs into resampling units. No clusters -> one diff per unit."""
    if clusters is None:
        return [[d] for d in diffs]
    grouped: dict = {}
    for label, value in zip(clusters, diffs, strict=True):
        grouped.setdefault(label, []).append(value)
    return list(grouped.values())


def _mean_of_units(units: list[list[float]]) -> float:
    total = 0.0
    count = 0
    for unit in units:
        total += sum(unit)
        count += len(unit)
    return total / count if count else 0.0


def _bca_lower(
    units: list[list[float]],
    observed: float,
    alpha: float,
    n_boot: int,
    seed: int,
) -> float:
    """One-sided BCa lower bound on the mean, resampling whole units."""
    rng = random.Random(seed)
    k = len(units)
    boot = []
    for _ in range(n_boot):
        picked = [units[rng.randrange(k)] for _ in range(k)]
        boot.append(_mean_of_units(picked))
    boot.sort()

    # Bias correction z0 from the fraction of bootstrap means below observed.
    n_less = sum(1 for b in boot if b < observed)
    if n_less == 0 or n_less == n_boot:  # degenerate; can't bias-correct
        z0 = 0.0
    else:
        z0 = _NORMAL.inv_cdf(n_less / n_boot)

    # Acceleration via jackknife over units (leave-one-unit-out means).
    jack = [_mean_of_units(units[:i] + units[i + 1 :]) for i in range(k)]
    jbar = sum(jack) / k
    num = sum((jbar - x) ** 3 for x in jack)
    den = 6.0 * (sum((jbar - x) ** 2 for x in jack) ** 1.5)
    a = num / den if den != 0 else 0.0

    z_alpha = _NORMAL.inv_cdf(alpha)
    denom = 1 - a * (z0 + z_alpha)
    adj = z0 + (z0 + z_alpha) / denom if denom != 0 else z0 + z_alpha
    p_lower = _NORMAL.cdf(adj)
    idx = min(max(int(p_lower * n_boot), 0), n_boot - 1)
    return boot[idx]
