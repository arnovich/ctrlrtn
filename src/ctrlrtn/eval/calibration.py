"""Judge calibration against human labels — an ADVISORY agreement check.

The shadow-replay judge (``judge.py``) is blinded and position-bias-cancelled,
but a residual remains: it can re-identify an arm from output *style* and carry
a differential bias no A/B swap removes. Before a judge's non-inferiority
verdict is trusted, it helps to see whether it agrees with humans on the same
pairings — the ``docs/history/eval-design.md`` §6/§9 calibration step.

This is a DIAGNOSTIC, not a gate. It does NOT wire into ``replay-eval``: nothing
here refuses a verdict or de-biases the NI test — an operator reads the report
and decides by hand. (Automatic gating + offset correction is deferred; see the
verdict gloss.) Given pairings a human has scored on the same 0-10 scale the
judge uses, it runs the judge via the very same ``judge_pairing`` the eval uses
(so it checks the judge as-deployed) and reports:

* **agreement** — how often the judge picks the human's winner, with a Wilson
  lower bound so a handful of lucky pairs can't read as alignment (a random
  judge sits at 0.5; the bound must clear it);
* **slope** — the regression of judge diff on human diff. Slope < 1 means the
  judge *compresses* real quality gaps — the dangerous failure a mean-bias check
  misses, since a compressor that halves every gap still has ~0 mean bias yet
  under-reports the gap at the decision boundary;
* **bias at parity** — the regression intercept: the judge's diff when humans
  call it a tie. Positive = it favours the CANDIDATE at true parity, the
  direction that waves a downgrade through;
* **correlation** — Spearman, reported as a diagnostic only (not gated).

Scope/known limits (advisory, so surfaced not silently assumed): labelled pairs
are treated as i.i.d. — edition clustering (which ``ni.py`` corrects for) is not
modelled, so with correlated labels the effective N is smaller than ``n``. The
judge call is injected (``JudgeFn``) so the module is pure and headless-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ctrlrtn.eval.judge import JudgeFn, Pairing, judge_pairing

# Advisory verdicts (a readout, not an authorization to downgrade).
ALIGNED = "ALIGNED"  # agrees with humans within tolerance
MISALIGNED = "MISALIGNED"  # random-indistinguishable, biased, or compressing
INSUFFICIENT = "INSUFFICIENT"  # too few *directional* pairs to say anything

# The floor is on DIRECTIONAL pairs (a human preference), not total labels: a
# near-parity candidate produces many human ties that carry no signal, and the
# agreement estimate rests only on the directional ones. Mirrors ni.py's floor.
_MIN_DIRECTIONAL = 20
# The Wilson lower bound on agreement must clear this to be distinguishable from
# a coin-flip judge.
_MIN_AGREEMENT_LB = 0.5
# Below this regression slope the judge compresses real gaps dangerously.
_MIN_SLOPE = 0.5
# Score-unit ceiling on candidate-favouring bias-at-parity when no NI margin is
# given (with a margin, half of it — a bias of margin/2 already materially
# raises the false-pass rate).
_MAX_BIAS = 0.5
# One-sided z for a ~95% Wilson lower bound.
_WILSON_Z = 1.645


@dataclass
class LabeledPairing:
    """One replayed pairing with a human's 0-10 score for each arm (de-blinded:
    the human labelled anonymous A/B; the loader maps back to the arms via the
    sidecar key)."""

    pairing: Pairing
    human_baseline: float
    human_candidate: float

    @property
    def human_diff(self) -> float:
        """``candidate - baseline`` as the human scored it (>0: human preferred
        the candidate)."""
        return self.human_candidate - self.human_baseline


@dataclass
class CalibrationReport:
    """How well the judge agrees with the human labels. Advisory — no consumer
    gates on it automatically."""

    verdict: str
    n: int  # labelled pairs the judge scored
    n_failed: int  # labelled pairs a judge call raised on (skipped)
    n_directional: int  # pairs where the human expressed a preference
    agreement_rate: float  # judge picks the human's winner (of n_directional)
    agreement_lb: float  # Wilson lower bound on agreement_rate
    correlation: float  # Spearman(judge_diff, human_diff); diagnostic only
    slope: float  # regression of judge_diff on human_diff; <1 = compression
    bias: float  # regression intercept; >0 = pro-candidate at parity
    bias_limit: float
    candidate_win_rate_judge: float
    candidate_win_rate_human: float
    min_directional: int
    min_agreement_lb: float
    min_slope: float
    warnings: list[str] = field(default_factory=list)


def calibrate(
    judge_fn: JudgeFn,
    labeled: list[LabeledPairing],
    *,
    replicates: int = 2,
    margin: float | None = None,
    min_directional: int = _MIN_DIRECTIONAL,
) -> CalibrationReport:
    """Score each labelled pairing with the judge and compare to the human.

    ``margin`` (the NI margin, on the 0-10 judge scale) sets the bias ceiling at
    ``margin / 2``; without it, ``_MAX_BIAS`` is used.
    """
    if margin is not None and margin <= 0:
        raise ValueError("margin must be > 0")

    judge_diffs: list[float] = []
    human_diffs: list[float] = []
    warnings: list[str] = []
    n_failed = 0
    for item in labeled:
        try:
            score = judge_pairing(judge_fn, item.pairing, replicates=replicates)
        except Exception as exc:  # noqa: BLE001 - one bad call, not the pass
            # A malformed reply (ValueError), or the injected judge itself
            # raising (a transient HTTP error, etc.), skips just this pairing —
            # it must not abort scoring the rest (mirrors run_replay).
            n_failed += 1
            warnings.append(f"judge failed on a pairing: {exc}")
            continue
        judge_diffs.append(score.diff)
        human_diffs.append(item.human_diff)

    bias_limit = (margin / 2.0) if margin is not None else _MAX_BIAS
    directional = [
        (jd, hd)
        for jd, hd in zip(judge_diffs, human_diffs, strict=True)
        if hd != 0
    ]
    n_directional = len(directional)
    agreements = sum(1 for jd, hd in directional if _same_sign(jd, hd))
    agreement_rate = agreements / n_directional if n_directional else 0.0
    agreement_lb = _wilson_lower(agreements, n_directional)
    slope, bias = _regression(human_diffs, judge_diffs)

    if bias < -bias_limit:
        warnings.append(
            f"judge under-rates the candidate (bias {bias:+.2f} at parity); "
            "conservative, but it may block good downgrades"
        )
    if len(set(human_diffs)) < 2:
        warnings.append(
            "human labels have no spread; the compression (slope) check is "
            "unreliable"
        )
    if n_failed:
        warnings.append(f"{n_failed} pairing(s) skipped after a judge error")

    return CalibrationReport(
        verdict=_verdict(
            n_directional=n_directional,
            agreement_lb=agreement_lb,
            bias=bias,
            slope=slope,
            bias_limit=bias_limit,
            min_directional=min_directional,
        ),
        n=len(judge_diffs),
        n_failed=n_failed,
        n_directional=n_directional,
        agreement_rate=agreement_rate,
        agreement_lb=agreement_lb,
        correlation=_spearman(judge_diffs, human_diffs),
        slope=slope,
        bias=bias,
        bias_limit=bias_limit,
        candidate_win_rate_judge=_win_rate(judge_diffs),
        candidate_win_rate_human=_win_rate(human_diffs),
        min_directional=min_directional,
        min_agreement_lb=_MIN_AGREEMENT_LB,
        min_slope=_MIN_SLOPE,
        warnings=warnings,
    )


def _verdict(
    *,
    n_directional: int,
    agreement_lb: float,
    bias: float,
    slope: float,
    bias_limit: float,
    min_directional: int,
) -> str:
    if n_directional < min_directional:
        return INSUFFICIENT
    if (
        agreement_lb <= _MIN_AGREEMENT_LB  # not better than a coin flip
        or bias > bias_limit  # over-rates the candidate at parity
        or slope < _MIN_SLOPE  # compresses real quality gaps
    ):
        return MISALIGNED
    return ALIGNED


def _same_sign(a: float, b: float) -> bool:
    return (a > 0 and b > 0) or (a < 0 and b < 0)


def _win_rate(diffs: list[float]) -> float:
    """Fraction of pairs the candidate wins (diff > 0), ignoring ties."""
    decided = [d for d in diffs if d != 0]
    return sum(1 for d in decided if d > 0) / len(decided) if decided else 0.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _wilson_lower(successes: int, n: int, z: float = _WILSON_Z) -> float:
    """One-sided Wilson score lower bound on a binomial proportion. 0.0 for the
    empty sample (no evidence of any agreement)."""
    if n == 0:
        return 0.0
    p = successes / n
    z2 = z * z
    center = p + z2 / (2 * n)
    half = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5)
    return (center - half) / (1 + z2 / n)


def _regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """(slope, intercept) of ``ys ~ a + b*xs`` — here judge diff on human diff.
    Falls back to slope 1.0 (no compression penalty) when ``xs`` has no variance,
    with the intercept carrying the additive shift instead."""
    n = len(xs)
    if n < 2:
        return 1.0, 0.0
    mx, my = _mean(xs), _mean(ys)
    var_x = sum((x - mx) ** 2 for x in xs)
    if var_x == 0:
        return 1.0, my - mx
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    slope = cov / var_x
    return slope, my - slope * mx


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation: Pearson on average-tie ranks. 0.0 when undefined (fewer
    than two points, or no variance in either variable)."""
    if len(xs) < 2:
        return 0.0
    return _pearson(_ranks(xs), _ranks(ys))


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        average = (i + j - 1) / 2.0 + 1.0  # 1-based average rank for the tie
        for k in range(i, j):
            ranks[order[k]] = average
        i = j
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    mx, my = _mean(xs), _mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    var_x = sum((x - mx) ** 2 for x in xs)
    var_y = sum((y - my) ** 2 for y in ys)
    denom = (var_x * var_y) ** 0.5
    return cov / denom if denom else 0.0
