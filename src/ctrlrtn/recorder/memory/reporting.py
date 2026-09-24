"""Small in-memory store used by tests and local experiments."""

from __future__ import annotations

from ctrlrtn.policy.experiment import CANDIDATE
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

from .core import MemoryState


class ReportingMemoryMixin(MemoryState):
    """Project in-memory traces into reporting read models."""

    def _latest_outcomes(self) -> dict[str, Outcome]:
        # Last reported wins, by arrival order (matches SQL ORDER BY id DESC);
        # wall-clock ts is non-monotonic, so it must not decide "latest".
        latest: dict[str, Outcome] = {}
        for o in self.outcomes:
            latest[o.task_id] = o
        return latest

    def rankings(self, *, baseline_only: bool = False) -> list[UseCaseRanking]:
        groups: dict[str, dict[str, float]] = {}
        for trace in self.traces:
            if baseline_only and trace.arm == CANDIDATE:
                continue
            key = trace.use_case_key or UNKEYED
            group = groups.setdefault(
                key,
                {
                    "calls": 0,
                    "in": 0,
                    "out": 0,
                    "lat": 0.0,
                    "cost": 0.0,
                    "cr": 0,
                    "cw": 0,
                },
            )
            group["calls"] += 1
            group["in"] += trace.input_tokens or 0
            group["out"] += trace.output_tokens or 0
            group["lat"] += trace.latency_ms
            group["cost"] += trace.cost_usd or 0.0
            group["cr"] += trace.cache_read_tokens or 0
            group["cw"] += trace.cache_write_tokens or 0
        rows = [
            UseCaseRanking(
                use_case=key,
                calls=int(group["calls"]),
                input_tokens=int(group["in"]),
                output_tokens=int(group["out"]),
                avg_latency_ms=group["lat"] / group["calls"],
                cost_usd=group["cost"],
                cache_read_tokens=int(group["cr"]),
                cache_write_tokens=int(group["cw"]),
            )
            for key, group in groups.items()
        ]
        return sort_by_spend(rows)

    def use_case_models(self) -> dict[str, str]:
        latest: dict[str, tuple[float, str]] = {}
        for trace in self.traces:  # insertion order == id order
            if trace.model is None:
                continue
            key = trace.use_case_key or UNKEYED
            if key not in latest or trace.ts >= latest[key][0]:
                latest[key] = (trace.ts, trace.model)
        return {key: model for key, (_, model) in latest.items()}

    def requests_for_use_case(
        self,
        use_case_key: str,
        limit: int = 50,
        *,
        workflow: str | None = None,
        workflow_version: str | None = None,
        step: str | None = None,
    ) -> list[dict]:
        """Mirror SQLite's replay-input view for test and local use."""

        selected: list[tuple[int, Trace]] = []
        for trace_id, trace in reversed(list(enumerate(self.traces, start=1))):
            if (trace.use_case_key or UNKEYED) != use_case_key:
                continue
            if not 200 <= trace.status_code < 300:
                continue
            if workflow is not None and (
                trace.workflow != workflow
                or trace.workflow_version != workflow_version
            ):
                continue
            if step is not None and trace.step != step:
                continue
            selected.append((trace_id, trace))

        rounds: dict[int, list[tuple[int, Trace]]] = {}
        occurrences: dict[str, int] = {}
        for item in selected:
            _, trace = item
            if trace.task_id is None:
                round_number = 0
            else:
                round_number = occurrences.get(trace.task_id, 0)
                occurrences[trace.task_id] = round_number + 1
            rounds.setdefault(round_number, []).append(item)

        ordered = [
            item
            for round_number in sorted(rounds)
            for item in rounds[round_number]
        ][:limit]
        return [
            {
                "id": trace_id,
                "request_body": trace.request_body,
                "task_id": trace.task_id,
                "path": trace.path,
            }
            for trace_id, trace in ordered
        ]

    def spend_since(self, ts: float) -> float:
        """Recorded cost from ``ts`` onwards; unknown costs count as zero."""
        return sum(
            trace.cost_usd or 0.0 for trace in self.traces if trace.ts >= ts
        )

    def spend_breakdown_since(
        self, ts: float
    ) -> tuple[float, dict[str, float]]:
        total = 0.0
        by_use_case: dict[str, float] = {}
        for trace in self.traces:
            if trace.ts < ts or trace.cost_usd is None:
                continue
            total += trace.cost_usd
            if trace.use_case_key is not None:
                by_use_case[trace.use_case_key] = (
                    by_use_case.get(trace.use_case_key, 0.0) + trace.cost_usd
                )
        return total, by_use_case

    def session_spend_state(self) -> tuple[dict[str, float], set[str]]:
        totals: dict[str, float] = {}
        unknown: set[str] = set()
        for trace in self.traces:
            if trace.session_id is None:
                continue
            if trace.cost_usd is not None:
                totals[trace.session_id] = (
                    totals.get(trace.session_id, 0.0) + trace.cost_usd
                )
            elif trace.terminal_reason is None and not trace.provider_free:
                unknown.add(trace.session_id)
        return totals, unknown

    def unpriced_calls_since(self, ts: float) -> int:
        return sum(
            1
            for trace in self.traces
            if trace.ts >= ts
            and trace.terminal_reason is None
            and not trace.provider_free
            and price_for(trace.served_model or trace.model) is None
        )

    def terminal_counts_since(self, ts: float) -> dict[str, int]:
        counts: dict[str, int] = {}
        for trace in self.traces:
            if trace.ts < ts or trace.terminal_reason is None:
                continue
            counts[trace.terminal_reason] = (
                counts.get(trace.terminal_reason, 0) + 1
            )
        return counts

    def fallback_calls_since(self, ts: float) -> int:
        return sum(
            1
            for trace in self.traces
            if trace.ts >= ts
            and trace.budget_fallback
            and trace.terminal_reason is None
        )

    def tasks(self, limit: int = 50) -> list[TaskSummary]:
        groups: dict[str, dict] = {}
        for trace in self.traces:
            # Mirror SQL COALESCE(task_id, ?): only NULL -> untasked; an empty
            # string stays its own group (so the outcome join matches it too).
            key = UNTASKED if trace.task_id is None else trace.task_id
            group = groups.setdefault(
                key,
                {
                    "calls": 0,
                    "cost": 0.0,
                    "in": 0,
                    "out": 0,
                    "use_cases": set(),
                    "errors": 0,
                },
            )
            group["calls"] += 1
            group["cost"] += trace.cost_usd or 0.0
            group["in"] += trace.input_tokens or 0
            group["out"] += trace.output_tokens or 0
            if trace.status_code >= 400:  # match SQL CASE WHEN status >= 400
                group["errors"] += 1
            if trace.use_case_key is not None:  # match SQL COUNT(DISTINCT ...)
                group["use_cases"].add(trace.use_case_key)
        latest = self._latest_outcomes()
        rows = [
            TaskSummary(
                task_id=key,
                calls=group["calls"],
                cost_usd=group["cost"],
                input_tokens=int(group["in"]),
                output_tokens=int(group["out"]),
                use_cases=len(group["use_cases"]),
                errors=group["errors"],
                success=latest[key].success if key in latest else None,
                score=latest[key].score if key in latest else None,
            )
            for key, group in groups.items()
        ]
        rows.sort(key=lambda t: (-t.cost_usd, -t.calls, t.task_id))
        return rows[:limit]

    def sessions(self, limit: int = 50) -> list[SessionSummary]:
        groups: dict[str, dict] = {}
        for trace in self.traces:
            key = UNSESSIONED if trace.session_id is None else trace.session_id
            group = groups.setdefault(
                key,
                {
                    "calls": 0,
                    "cost": 0.0,
                    "in": 0,
                    "out": 0,
                    "use_cases": set(),
                    "errors": 0,
                    "unknown_cost_calls": 0,
                },
            )
            group["calls"] += 1
            group["cost"] += trace.cost_usd or 0.0
            group["in"] += trace.input_tokens or 0
            group["out"] += trace.output_tokens or 0
            if trace.status_code >= 400:
                group["errors"] += 1
            if (
                trace.cost_usd is None
                and trace.terminal_reason is None
                and not trace.provider_free
            ):
                group["unknown_cost_calls"] += 1
            if trace.use_case_key is not None:
                group["use_cases"].add(trace.use_case_key)
        rows = [
            SessionSummary(
                session_id=key,
                calls=group["calls"],
                cost_usd=group["cost"],
                input_tokens=int(group["in"]),
                output_tokens=int(group["out"]),
                use_cases=len(group["use_cases"]),
                errors=group["errors"],
                unknown_cost_calls=group["unknown_cost_calls"],
            )
            for key, group in groups.items()
        ]
        rows.sort(key=lambda row: (-row.cost_usd, -row.calls, row.session_id))
        return rows[:limit]
