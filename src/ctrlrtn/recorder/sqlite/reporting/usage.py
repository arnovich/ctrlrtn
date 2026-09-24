"""SQLite trace, task, session, spend, and experiment reporting queries."""

from __future__ import annotations

import sqlite3

from ctrlrtn.recorder.models import UNKEYED as _UNKEYED
from ctrlrtn.recorder.models import UseCaseRanking

from ..connection import SqliteCapability
from ..queries import (
    _FALLBACK_CALLS_SINCE,
    _PRICING_IDENTITIES_SINCE,
    _RANKINGS,
    _RANKINGS_ARM_FILTER,
    _SESSION_SPEND_STATE,
    _SPEND_BREAKDOWN_SINCE,
    _SPEND_SINCE,
    _TERMINAL_COUNTS_SINCE,
    _USE_CASE_MODELS,
    _where,
)


class UsageReportingSqliteMixin(SqliteCapability):
    """Project recorded traces into offline and monitoring read models."""

    def rankings(
        self, *, baseline_only: bool = False, since: float | None = None
    ) -> list[UseCaseRanking]:
        """Spend and volume per use-case. ``since`` (a Unix timestamp) limits
        it to a trailing window; the default spans all recorded history."""
        clauses = []
        params: list = [_UNKEYED]
        if baseline_only:
            clauses.append(f"({_RANKINGS_ARM_FILTER})")
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        with self._lock:
            rows = self._conn.execute(
                _RANKINGS.format(where=_where(clauses)), tuple(params)
            ).fetchall()
        return [
            UseCaseRanking(
                use_case=row[0],
                calls=int(row[1]),
                input_tokens=int(row[2]),
                output_tokens=int(row[3]),
                avg_latency_ms=float(row[4]) if row[4] is not None else 0.0,
                cost_usd=float(row[5]) if row[5] is not None else 0.0,
                cache_read_tokens=int(row[6]),
                cache_write_tokens=int(row[7]),
            )
            for row in rows
        ]

    def spend_since(self, ts: float) -> float:
        with self._lock:
            row = self._conn.execute(_SPEND_SINCE, (ts,)).fetchone()
        return float(row[0])

    def spend_breakdown_since(
        self, ts: float
    ) -> tuple[float, dict[str, float]]:
        with self._lock:
            rows = self._conn.execute(_SPEND_BREAKDOWN_SINCE, (ts,)).fetchall()
        total = sum(float(row[1]) for row in rows)
        by_use_case = {
            row[0]: float(row[1]) for row in rows if row[0] is not None
        }
        return total, by_use_case

    def session_spend_state(self) -> tuple[dict[str, float], set[str]]:
        with self._lock:
            rows = self._conn.execute(_SESSION_SPEND_STATE).fetchall()
        totals = {row[0]: float(row[1]) for row in rows}
        unknown = {row[0] for row in rows if row[2]}
        return totals, unknown

    def unpriced_calls_since(self, ts: float) -> int:
        from ctrlrtn.telemetry.pricing import price_for

        with self._lock:
            rows = self._conn.execute(
                _PRICING_IDENTITIES_SINCE, (ts,)
            ).fetchall()
        return sum(
            1
            for model, provider_free in rows
            if not provider_free and price_for(model) is None
        )

    def terminal_counts_since(self, ts: float) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(_TERMINAL_COUNTS_SINCE, (ts,)).fetchall()
        return {str(reason): int(count) for reason, count in rows}

    def fallback_calls_since(self, ts: float) -> int:
        with self._lock:
            try:
                row = self._conn.execute(
                    _FALLBACK_CALLS_SINCE, (ts,)
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such column" in str(exc):
                    return 0
                raise
        return int(row[0])

    def use_case_models(self) -> dict[str, str | None]:
        """The most recent model seen per use-case (from each group's latest
        call), keyed the same way as ``rankings``."""
        with self._lock:
            rows = self._conn.execute(_USE_CASE_MODELS, (_UNKEYED,)).fetchall()
        return {row[0]: row[1] for row in rows}
