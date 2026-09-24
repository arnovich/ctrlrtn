"""Small in-memory store used by tests and local experiments."""

from __future__ import annotations

from ctrlrtn.control_config import ControlRevision
from ctrlrtn.policy.experiment import CANDIDATE, Experiment
from ctrlrtn.policy.fallback import ApprovedFallback
from ctrlrtn.policy.route import Route, WorkflowRoute
from ctrlrtn.policy.shadow import ShadowExperiment, ShadowStats
from ctrlrtn.recorder.models import (
    UNKEYED,
    UNSESSIONED,
    UNTASKED,
    Outcome,
    SessionSummary,
    TaskSummary,
    UseCaseRanking,
    sort_by_spend,
)
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.pricing import price_for
from ctrlrtn.workflow.identity import WorkflowEvent
from ctrlrtn.workflow.metrics import (
    WorkflowStepMetric,
    build_workflow_step_metrics,
)
from ctrlrtn.workflow.tool_operation import ToolOperationEvent


class ControlMemoryMixin:
    """Apply in-memory experiment, route, fallback, and shadow control."""

    def create_experiment(self, experiment: Experiment) -> None:
        # Test/dev only: this check-then-append is not atomic. Safe because
        # InMemory is single-process, single-threaded; the cross-process
        # invariant is the DB's job (SqliteTraceStore's partial unique index).
        if experiment.experiment_id in {
            e.experiment_id for e in self._experiments
        }:
            raise ValueError(
                f"experiment {experiment.experiment_id!r} already exists"
            )
        if experiment.is_running and experiment.use_case_key in {
            e.use_case_key for e in self._experiments if e.is_running
        }:
            raise ValueError(
                "a running experiment already exists for use-case "
                f"{experiment.use_case_key!r}"
            )
        if (
            experiment.is_running
            and experiment.use_case_key in self.running_shadow_experiments()
        ):
            raise ValueError(
                "a running shadow already exists for this use-case"
            )
        self._experiments.append(experiment)

    def stop_experiment(self, experiment_id: str) -> bool:
        stopped = False
        for i, e in enumerate(self._experiments):
            if e.experiment_id == experiment_id and e.is_running:
                self._experiments[i] = e.stopped()
                stopped = True
        return stopped

    def adopt_experiment(self, experiment_id: str, route: Route) -> bool:
        experiment = self.experiment(experiment_id)
        if experiment is None:
            return False
        if (
            route.use_case_key != experiment.use_case_key
            or route.model != experiment.candidate_model
            or route.provider != experiment.candidate_provider
        ):
            raise ValueError("adoption route does not match the experiment")
        self.stop_experiment(experiment_id)
        self.set_route(route)
        return True

    def running_experiments(self) -> dict[str, Experiment]:
        return {e.use_case_key: e for e in self._experiments if e.is_running}

    def experiments(self, limit: int = 50) -> list[Experiment]:
        # Newest first == reverse insertion order (matches SQLite's rowid DESC);
        # insertion order is monotonic where wall-clock created_epoch is not.
        return list(reversed(self._experiments))[:limit]

    def experiment(self, experiment_id: str) -> Experiment | None:
        return next(
            (e for e in self._experiments if e.experiment_id == experiment_id),
            None,
        )

    def set_route(self, route: Route) -> None:
        self._routes[route.use_case_key] = route

    def clear_route(self, use_case_key: str) -> bool:
        return self._routes.pop(use_case_key, None) is not None

    def routes(self) -> list[Route]:
        return sorted(self._routes.values(), key=lambda r: -r.ts)

    def set_workflow_route(self, route: WorkflowRoute) -> None:
        self._workflow_routes[route.key] = route

    def workflow_routes(self) -> list[WorkflowRoute]:
        return sorted(self._workflow_routes.values(), key=lambda row: -row.ts)

    def set_fallback(self, fallback: ApprovedFallback) -> None:
        self._fallbacks[fallback.use_case_key] = fallback

    def clear_fallback(self, use_case_key: str) -> bool:
        return self._fallbacks.pop(use_case_key, None) is not None

    def fallbacks(self) -> list[ApprovedFallback]:
        return sorted(
            self._fallbacks.values(), key=lambda fallback: -fallback.approved_at
        )

    def create_shadow_experiment(self, experiment: ShadowExperiment) -> None:
        if experiment.shadow_id in {row.shadow_id for row in self._shadows}:
            raise ValueError("shadow id already exists")
        if experiment.use_case_key in self.running_shadow_experiments():
            raise ValueError(
                "a running shadow already exists for this use-case"
            )
        if experiment.use_case_key in self.running_experiments():
            raise ValueError(
                "a live experiment already exists for this use-case"
            )
        self._shadows.append(experiment)
        self._shadow_stats[experiment.shadow_id] = ShadowStats(
            experiment.shadow_id
        )

    def stop_shadow_experiment(self, shadow_id: str) -> bool:
        for index, row in enumerate(self._shadows):
            if row.shadow_id == shadow_id and row.is_running:
                self._shadows[index] = row.stopped()
                return True
        return False

    def shadow_experiments(self, limit: int = 50) -> list[ShadowExperiment]:
        return list(reversed(self._shadows))[:limit]

    def running_shadow_experiments(self) -> dict[str, ShadowExperiment]:
        return {
            row.use_case_key: row for row in self._shadows if row.is_running
        }

    def increment_shadow_stats(
        self,
        shadow_id: str,
        *,
        submitted: int = 0,
        completed: int = 0,
        failed: int = 0,
        dropped: int = 0,
    ) -> None:
        row = self._shadow_stats[shadow_id]
        self._shadow_stats[shadow_id] = ShadowStats(
            shadow_id,
            row.submitted + submitted,
            row.completed + completed,
            row.failed + failed,
            row.dropped + dropped,
        )

    def shadow_stats(self, shadow_id: str) -> ShadowStats | None:
        return self._shadow_stats.get(shadow_id)
