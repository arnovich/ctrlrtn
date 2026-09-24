"""Live A/B tripwire: a gross-regression detector over one experiment's own
traffic, with Manski worst-case bounds for tasks that started but never reported
an outcome (the MNAR half the serving-time divergence ceiling can't see).

This is a TRIPWIRE, not a certification. It answers "is the cheaper candidate
GROSSLY worse than the incumbent?" — stop and investigate — not "is it within
5pp?" (the replay non-inferiority path does that). So it is deliberately coarse
and conservative:

  - Each closed task is one unit; the arm is the whole edition's served arm
    (mixed-arm or cross-experiment tasks are *contaminated* and excluded).
  - A task's outcome is a binary success/failure from the app's reported outcome
    (`/ctrlrtn/outcome`), a divergence-ceiling terminal (always a failure), or an
    optional score threshold. A closed task with none of these is *unreported*.
  - Each arm's failure rate carries BOTH sampling uncertainty (a Wilson score
    interval, so a small or noisy sample can't masquerade as a verdict) and
    imputation uncertainty (Manski: unreported ∈ {all-success, all-failure}).
    The two fold into one bound per side:
      * GROSS_REGRESSION  — even the candidate's *best* case (fewest failures,
        sampling-low) beats the baseline's *worst* case by more than the margin.
      * NO_GROSS_REGRESSION — even the candidate's *worst* case is within the
        margin of the baseline's *best* case.
      * INCONCLUSIVE — the bounds straddle: too few, too noisy, or too many
        unreported tasks to tell. Report outcomes / gather more, don't guess.
      * UNDERPOWERED — too few *reported* tasks per arm to conclude anything.
      * NOT_EXERCISED / NO_DATA — the candidate isn't actually being swapped, or
        there is no experiment traffic at all (usually broken x-ctrlrtn-task
        propagation). Never reported as "safe".

Caveats a caller must respect: the Wilson interval is fixed-n, so repeated
`experiment status` peeking is not fully Type-I corrected — treat NO_GROSS as a
screen, not a certificate (run `replay-eval` for a real margin). Tasks are
treated iid; same-day editions share inputs, so a tighter run should cluster by
day. The margin is ABSOLUTE (a 20pp gap); at a low base rate that permits a
large *relative* rise. All of this is pure and time-injected — fully headless.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from statistics import NormalDist, median

from ctrlrtn.policy.experiment import BASELINE, CANDIDATE

# Per-task classification.
SUCCESS = "success"
FAILURE = "failure"  # reported failure OR a divergence-ceiling terminal
UNREPORTED = "unreported"  # closed, no outcome -> Manski
OPEN = "open"  # not yet closed -> excluded from the verdict, counted for power
CONTAMINATED = "contaminated"  # mixed arm / straddles experiments -> excluded

# Verdicts.
NO_GROSS_REGRESSION = "NO_GROSS_REGRESSION"
GROSS_REGRESSION = "GROSS_REGRESSION"
INCONCLUSIVE = "INCONCLUSIVE"
UNDERPOWERED = "UNDERPOWERED"
NOT_EXERCISED = (
    "NOT_EXERCISED"  # candidate not actually swapped (no-op / empty)
)
NO_DATA = "NO_DATA"  # no experiment traffic at all

DEFAULT_IDLE_SECONDS = 45 * 60  # generous: a slow tool call must not split one
DEFAULT_GROSS_MARGIN = 0.20  # "gross" = a 20pp failure-rate gap
DEFAULT_MIN_TASKS_PER_ARM = 30  # reported tasks per arm (eval-design §6)
DEFAULT_CONFIDENCE = 0.95


def _wilson(k: int, n: int, z: float) -> tuple[float, float]:
    """Wilson score interval for a k/n proportion — robust at small n and at
    rates near 0/1 (where the normal approximation fails). ``[0, 1]`` when n=0.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass(frozen=True)
class TaskRecord:
    """One edition's contribution to the tripwire: which arm served it, its
    call count and cost, and its closed/outcome status."""

    task_id: str
    arm: str | None
    calls: int
    status: str
    served_model: str | None
    cost: float = 0.0
    ceiling: bool = False  # the failure is a divergence-ceiling cut-off
    unused_score: bool = False  # had a score but no --fail-below to classify it
    path_digest: str | None = None
    path: tuple[str, ...] = ()


def classify_task(
    row: dict, *, now: float, idle_seconds: float, fail_below: float | None
) -> TaskRecord:
    """Turn one per-task aggregate row (see store.experiment_task_rows) into a
    classified TaskRecord. Order matters: contamination first (never trust a
    mixed unit), then the ceiling (an observed failure), then the app outcome,
    then closure."""
    task_id = row["task_id"]
    calls = int(row["calls"])
    arm = row.get("arm")
    served_model = row.get("served_model")
    cost = float(row.get("cost") or 0.0)
    path_digest = row.get("path_digest")
    path = tuple(row.get("path") or ())

    def rec(status: str, *, ceiling: bool = False, unused_score: bool = False):
        return TaskRecord(
            task_id,
            arm,
            calls,
            status,
            served_model,
            cost,
            ceiling,
            unused_score,
            path_digest,
            path,
        )

    # Contamination: more than one served arm within the experiment, or the task
    # straddles more than one experiment (a reused id after the in-memory binding
    # was dropped by a restart), or an unrecognized arm.
    if (
        int(row["arms"]) != 1
        or int(row["experiments"]) > 1
        or int(row.get("workflow_identities", 1)) != 1
        or int(row.get("missing_workflow_identity", 0)) > 0
        or arm not in (BASELINE, CANDIDATE)
    ):
        return rec(CONTAMINATED)

    if int(row["ceilings"]) > 0:  # a divergence-ceiling terminal is a failure
        return rec(FAILURE, ceiling=True)

    success = row.get("success")
    if success is not None:  # app-reported (SQLite stores bool as 0/1)
        return rec(SUCCESS if success else FAILURE)

    score = row.get("score")
    if score is not None:
        if fail_below is not None:
            return rec(FAILURE if float(score) < fail_below else SUCCESS)
        # a score was reported but there's no threshold to classify it — don't
        # let it masquerade as "no outcome"; flag it so the caller is told.
        closed = (now - float(row["last_ts"])) >= idle_seconds
        return rec(UNREPORTED if closed else OPEN, unused_score=True)

    # No outcome: closed once idle (its worst runs can't still arrive) ->
    # unreported (Manski); otherwise still open, excluded from the verdict.
    closed = (now - float(row["last_ts"])) >= idle_seconds
    return rec(UNREPORTED if closed else OPEN)


@dataclass(frozen=True)
class ArmStats:
    arm: str
    success: int = 0
    failure: int = 0  # reported failures incl. ceiling terminals
    ceiling: int = 0  # subset of failure that were ceiling cut-offs
    unreported: int = 0
    open: int = 0
    unused_scores: int = 0
    cost: float = 0.0
    calls: tuple[int, ...] = ()
    served_models: frozenset[str] = frozenset()
    paths: tuple[tuple[str, int, tuple[str, ...]], ...] = ()
    missing_paths: int = 0

    @property
    def reported(self) -> int:
        """Tasks with an observed outcome (success or failure) — the informative
        units the power gate counts. Unreported tasks carry no signal."""
        return self.success + self.failure

    @property
    def closed(self) -> int:
        return self.success + self.failure + self.unreported

    @property
    def total(self) -> int:
        return self.closed + self.open

    @property
    def median_calls(self) -> float:
        return median(self.calls) if self.calls else 0.0

    @property
    def cost_per_task(self) -> float:
        return self.cost / self.total if self.total else 0.0

    def fail_bounds(self, z: float) -> tuple[float, float]:
        """Combined sampling+imputation failure-rate interval. Low: fewest
        failures (unreported imputed success), sampling-low. High: most failures
        (unreported imputed failure), sampling-high."""
        low = _wilson(self.failure, self.closed, z)[0]
        high = _wilson(self.failure + self.unreported, self.closed, z)[1]
        return low, high


@dataclass(frozen=True)
class TripwireReport:
    verdict: str
    baseline: ArmStats
    candidate: ArmStats
    contaminated: int
    gross_margin: float
    min_tasks_per_arm: int
    confidence: float
    diff_low: float  # candidate - baseline failure-rate interval (may be inert)
    diff_high: float
    warnings: tuple[str, ...] = ()

    @property
    def savings_pct(self) -> float | None:
        """Candidate's per-task cost saving vs baseline (None if not measurable)."""
        b = self.baseline.cost_per_task
        if b <= 0 or not self.candidate.total:
            return None
        return (b - self.candidate.cost_per_task) / b


def _arm_stats(arm: str, records: list[TaskRecord]) -> ArmStats:
    rows = [r for r in records if r.arm == arm and r.status != CONTAMINATED]
    path_counts = Counter(r.path_digest for r in rows if r.path_digest)
    path_labels = {
        r.path_digest: r.path for r in rows if r.path_digest is not None
    }
    return ArmStats(
        arm=arm,
        success=sum(1 for r in rows if r.status == SUCCESS),
        failure=sum(1 for r in rows if r.status == FAILURE),
        ceiling=sum(1 for r in rows if r.ceiling),
        unreported=sum(1 for r in rows if r.status == UNREPORTED),
        open=sum(1 for r in rows if r.status == OPEN),
        unused_scores=sum(1 for r in rows if r.unused_score),
        cost=sum(r.cost for r in rows),
        calls=tuple(r.calls for r in rows),
        served_models=frozenset(
            r.served_model for r in rows if r.served_model is not None
        ),
        paths=tuple(
            (digest, count, path_labels[digest])
            for digest, count in sorted(
                path_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        missing_paths=sum(1 for r in rows if r.path_digest is None),
    )


def _no_op_swap(baseline: ArmStats, candidate: ArmStats) -> bool:
    # The candidate served exactly the same model(s) as the baseline, so the
    # experiment tests nothing (candidate == the use-case's incumbent).
    return bool(
        baseline.served_models
        and candidate.served_models
        and baseline.served_models == candidate.served_models
    )


def _warnings(baseline: ArmStats, candidate: ArmStats) -> list[str]:
    warnings: list[str] = []
    if _no_op_swap(baseline, candidate):
        warnings.append(
            "candidate served the same model as baseline "
            f"({', '.join(sorted(candidate.served_models))}) — no real swap; "
            "is the candidate model different from the incumbent?"
        )
    if candidate.total and candidate.median_calls <= 1:
        warnings.append(
            "candidate tasks make ~1 call each — is x-ctrlrtn-task propagating to "
            "this use-case's sub-agent calls? (see `ctrlrtn propagation`)"
        )
    if (
        baseline.median_calls > 0
        and candidate.median_calls >= 2 * baseline.median_calls
    ):
        warnings.append(
            f"candidate median calls/task ({candidate.median_calls:g}) is "
            f">= 2x baseline ({baseline.median_calls:g}) — possible divergence."
        )
    # Ceiling-dominated candidate failures often mean the cap is set too low for
    # legitimately long editions, not that the model is bad.
    if candidate.failure and candidate.ceiling / candidate.failure > 0.5:
        warnings.append(
            f"{candidate.ceiling}/{candidate.failure} candidate failures are "
            "divergence-ceiling cut-offs — raise --max-calls if editions "
            "legitimately exceed it, or this GROSS may be a cap artifact."
        )
    # Still-open candidate tasks are excluded from the verdict; a looping
    # candidate stays open longest, so an interim NO_GROSS can read optimistic.
    if candidate.open and candidate.open >= candidate.closed:
        warnings.append(
            f"{candidate.open} candidate tasks still open (vs "
            f"{candidate.closed} closed) — interim read; re-check once they idle."
        )
    for arm in (baseline, candidate):
        if arm.total and arm.missing_paths:
            warnings.append(
                f"{arm.arm}: {arm.missing_paths}/{arm.total} tasks have no "
                "exact-version lifecycle path evidence."
            )
        if arm.unused_scores:
            warnings.append(
                f"{arm.arm}: {arm.unused_scores} tasks reported a score but no "
                "--fail-below threshold was given — pass one to classify them."
            )
        if arm.closed and arm.unreported / arm.closed > 0.5:
            warnings.append(
                f"{arm.arm}: {arm.unreported}/{arm.closed} closed tasks have no "
                "reported outcome — POST /ctrlrtn/outcome to tighten the verdict."
            )
    return warnings


def analyze(
    records: list[TaskRecord],
    *,
    gross_margin: float = DEFAULT_GROSS_MARGIN,
    min_tasks_per_arm: int = DEFAULT_MIN_TASKS_PER_ARM,
    confidence: float = DEFAULT_CONFIDENCE,
) -> TripwireReport:
    baseline = _arm_stats(BASELINE, records)
    candidate = _arm_stats(CANDIDATE, records)
    contaminated = sum(1 for r in records if r.status == CONTAMINATED)
    warnings = _warnings(baseline, candidate)
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    cand_low, cand_high = candidate.fail_bounds(z)
    base_low, base_high = baseline.fail_bounds(z)
    diff_low = cand_low - base_high  # most favorable to candidate
    diff_high = cand_high - base_low  # least favorable to candidate

    if candidate.total == 0 and baseline.total == 0:
        # No experiment traffic at all — almost always broken propagation.
        verdict = NO_DATA
    elif _no_op_swap(baseline, candidate) or candidate.total == 0:
        # The candidate arm isn't actually being swapped; never call this "safe".
        verdict = NOT_EXERCISED
    elif (
        candidate.reported < min_tasks_per_arm
        or baseline.reported < min_tasks_per_arm
    ):
        verdict = UNDERPOWERED
    elif diff_low > gross_margin:
        verdict = GROSS_REGRESSION
    elif diff_high <= gross_margin:
        verdict = NO_GROSS_REGRESSION
    else:
        verdict = INCONCLUSIVE

    return TripwireReport(
        verdict=verdict,
        baseline=baseline,
        candidate=candidate,
        contaminated=contaminated,
        gross_margin=gross_margin,
        min_tasks_per_arm=min_tasks_per_arm,
        confidence=confidence,
        diff_low=diff_low,
        diff_high=diff_high,
        warnings=tuple(warnings),
    )


def run_tripwire(
    rows: list[dict],
    *,
    now: float,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
    fail_below: float | None = None,
    gross_margin: float = DEFAULT_GROSS_MARGIN,
    min_tasks_per_arm: int = DEFAULT_MIN_TASKS_PER_ARM,
    confidence: float = DEFAULT_CONFIDENCE,
) -> TripwireReport:
    """Classify per-task rows and produce the tripwire verdict."""
    records = [
        classify_task(
            row, now=now, idle_seconds=idle_seconds, fail_below=fail_below
        )
        for row in rows
    ]
    return analyze(
        records,
        gross_margin=gross_margin,
        min_tasks_per_arm=min_tasks_per_arm,
        confidence=confidence,
    )
