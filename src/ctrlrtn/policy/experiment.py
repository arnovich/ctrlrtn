"""A live A/B experiment: one candidate model tested against the incumbent for
one use-case.

This is the *tripwire* path: a running experiment splits a use-case's traffic
by task, serves the candidate to one arm, and exists to catch gross regression.
It never certifies non-inferiority, which task-level A/B cannot do at realistic
volumes; that verdict comes from paired offline replay (``docs/evaluation.md``).
The model here is deliberately inert data:
it carries the assignment parameters, the enforced call ceiling and the
recorded cost ceiling; the serving
logic that reads it lives in the gateway, and persistence in the store.

An experiment is **immutable once created**. Changing the split would re-bucket
in-flight tasks (a task half-baseline, half-candidate), so "change the split"
means stop this experiment and start a new ``experiment_id``.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field, replace

from ctrlrtn.policy.scope import ExperimentScope

RUNNING = "running"
STOPPED = "stopped"
_STATUSES = frozenset({RUNNING, STOPPED})

BASELINE = "baseline"
CANDIDATE = "candidate"

# Per-(task, candidate arm) divergence backstops. Only max_calls_per_task is
# Enforced in the serving hot path: once a candidate makes more than this many
# calls within one task it is cut off with a counted failure, a coarse
# loop/runaway backstop that also bounds spend. Set it below the client's own
# max-turns limit, or the client stops the loop before the ceiling fires; the
# real divergence signal is the per-arm calls/task distribution at analysis
# time, not this cap.
#
# There is no per-task cost ceiling: cost is only known after the response, in
# enrichment, so the pre-request hook cannot price a call. The daily global and
# per-use-case budgets and the kill switch are the spend controls.
DEFAULT_MAX_CALLS_PER_TASK = 60


def _new_experiment_id() -> str:
    return "exp:" + uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class Experiment:
    """One candidate test for a use-case. Frozen: create + stop, never edit."""

    use_case_key: str
    candidate_model: str
    split_pct: int
    experiment_id: str = field(default_factory=_new_experiment_id)
    status: str = RUNNING
    created_epoch: float = field(default_factory=time.time)
    max_calls_per_task: int = DEFAULT_MAX_CALLS_PER_TASK
    candidate_provider: str | None = None
    workflow: str | None = None
    workflow_version: str | None = None
    step: str | None = None

    def __post_init__(self) -> None:
        if not self.use_case_key:
            raise ValueError("use_case_key is required")
        ExperimentScope(
            self.use_case_key, self.workflow, self.workflow_version, self.step
        )
        if not self.candidate_model:
            raise ValueError("candidate_model is required")
        if self.candidate_provider is not None and not self.candidate_provider:
            raise ValueError("candidate_provider must be non-empty when set")
        # 1..99 keeps both arms non-empty in BUCKET space. At small n a narrow
        # split can still yield ~0 tasks on an arm (0.01 x tens of tasks < 1);
        # the CLI (slice 6) warns on low expected tasks/arm from real volume.
        if not 1 <= self.split_pct <= 99:
            raise ValueError(
                f"split_pct must be 1..99 (got {self.split_pct}); it is the "
                "candidate's traffic share and both arms must be non-empty"
            )
        if self.status not in _STATUSES:
            raise ValueError(f"status must be one of {sorted(_STATUSES)}")
        if self.max_calls_per_task < 1:
            raise ValueError("max_calls_per_task must be >= 1")
        # Normalize the timestamp so a store that round-trips through REAL and
        # one that keeps the object in memory agree on the type.
        object.__setattr__(self, "created_epoch", float(self.created_epoch))

    @classmethod
    def from_stored(
        cls,
        *,
        experiment_id: str,
        use_case_key: str,
        candidate_model: str,
        split_pct: int,
        status: str,
        created_epoch: float,
        max_calls_per_task: int,
        candidate_provider: str | None = None,
        workflow: str | None = None,
        workflow_version: str | None = None,
        step: str | None = None,
    ) -> Experiment:
        """Rebuild from already-persisted fields **without** re-validating.

        Stored rows passed validation at write time and are trusted, so a future
        tightening of the rules can't brick old rows (or crash the gateway) on
        load. Use only for deserialization from the store.
        """
        obj = object.__new__(cls)
        for name, value in {
            "experiment_id": experiment_id,
            "use_case_key": use_case_key,
            "candidate_model": candidate_model,
            "split_pct": split_pct,
            "status": status,
            "created_epoch": created_epoch,
            "max_calls_per_task": max_calls_per_task,
            "candidate_provider": candidate_provider,
            "workflow": workflow,
            "workflow_version": workflow_version,
            "step": step,
        }.items():
            object.__setattr__(obj, name, value)
        return obj

    @property
    def is_running(self) -> bool:
        return self.status == RUNNING

    @property
    def scope(self) -> ExperimentScope:
        return ExperimentScope(
            self.use_case_key, self.workflow, self.workflow_version, self.step
        )

    def stopped(self) -> Experiment:
        """A copy marked stopped (the store persists the status flip)."""
        return replace(self, status=STOPPED)


@dataclass(frozen=True)
class ServeDecision:
    """What the serving path decided for one request: which experiment and arm
    it fell into, the model actually sent upstream (the candidate model on the
    candidate arm, the requested model otherwise), and the model the client
    originally requested. Threaded into the Trace so closure can attribute the
    call to an arm — and, via ``original_model``, detect a no-op swap (served ==
    original on the candidate arm) or baseline contamination."""

    experiment_id: str
    use_case_key: str
    task_id: str
    arm: str
    served_model: str
    original_model: str
    provider: str | None = None

    def __post_init__(self) -> None:
        if self.arm not in (BASELINE, CANDIDATE):
            raise ValueError(f"arm must be {BASELINE!r} or {CANDIDATE!r}")

    @property
    def is_candidate(self) -> bool:
        return self.arm == CANDIDATE

    @property
    def is_baseline(self) -> bool:
        return self.arm == BASELINE


def bucket(experiment_id: str, task_id: str) -> int:
    """A stable 0..99 bucket for a task within an experiment.

    Stateless and deterministic across processes/hosts (sha256 of UTF-8, unlike
    the salted builtin ``hash()``): every call of a task hashes to the same
    bucket, so a whole task binds to one arm. Keying on the experiment_id
    re-randomizes per experiment (a task unlucky in one isn't unlucky in the
    next).

    Assignment is UNSTRATIFIED — arms balance only in expectation, so at small n
    a day/regime can land lopsided. That is variance, not bias, and fine for a
    gross-regression tripwire; stratified/blocked assignment (``docs/evaluation.md``)
    must precede any finer-than-gross read.
    """
    if not experiment_id or not task_id:
        # Fail loud: an untasked call must be gated out upstream (slice 4), never
        # silently collapsed into one bucket -> one arm -> a fabricated "task".
        raise ValueError("bucket requires non-empty experiment_id and task_id")
    # NUL-separate so ids containing ':' can't collide: ("a","b:c") vs ("a:b","c").
    key = experiment_id.encode() + b"\x00" + task_id.encode()
    # Modulo bias is negligible (2^64 % 100 = 16, ~5e-18 excess); immaterial here.
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 100


def assign_arm(experiment: Experiment, task_id: str) -> str:
    """``CANDIDATE`` for the candidate's traffic share, else ``BASELINE``."""
    below = bucket(experiment.experiment_id, task_id) < experiment.split_pct
    return CANDIDATE if below else BASELINE


def decide(
    experiment: Experiment, task_id: str, requested_model: str
) -> ServeDecision:
    """Resolve one request against a RUNNING experiment: pick the arm and the
    model to serve. The requested model rides through unchanged on baseline.

    Preconditions the serving layer enforces (decide fails loud if one leaks):
    the experiment is running; the caller has already selected the *single*
    experiment for this (task, use_case) — decide has no cross-experiment view
    and can't enforce that itself; and task_id / requested_model are non-empty
    (untasked / modelless requests are gated out upstream, slice 4).
    """
    if not experiment.is_running:
        raise ValueError("decide called with a non-running experiment")
    if not requested_model:
        raise ValueError("decide requires a non-empty requested_model")
    arm = assign_arm(experiment, task_id)  # raises on an empty task_id
    served_model = (
        experiment.candidate_model if arm == CANDIDATE else requested_model
    )
    return ServeDecision(
        experiment_id=experiment.experiment_id,
        use_case_key=experiment.use_case_key,
        task_id=task_id,
        arm=arm,
        served_model=served_model,
        original_model=requested_model,
        provider=(experiment.candidate_provider if arm == CANDIDATE else None),
    )
