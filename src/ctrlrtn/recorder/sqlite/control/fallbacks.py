"""SQLite experiment, shadow, routing, and control-config persistence."""

from __future__ import annotations

import sqlite3

from ctrlrtn.policy.fallback import ApprovedFallback
from ctrlrtn.recorder.models import UNKEYED as _UNKEYED

from ..queries import (
    _SELECT_FALLBACKS,
    _UPSERT_FALLBACK,
    _USE_CASE_MODEL_BREAKDOWN,
    _USE_CASE_USAGE_SINCE,
    _USE_CASE_USAGE_SINCE_SWAPPED,
    _row_to_fallback,
)


class FallbackControlSqliteMixin:
    """Persist approved fallbacks and their usage projections."""

    def set_fallback(self, fallback: ApprovedFallback) -> None:
        with self._lock:
            self._conn.execute(
                _UPSERT_FALLBACK,
                (
                    fallback.use_case_key,
                    fallback.model,
                    fallback.baseline_model,
                    fallback.evidence_created,
                    fallback.approved_at,
                    fallback.provider,
                ),
            )
            self._conn.commit()

    def clear_fallback(self, use_case_key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM approved_fallbacks WHERE use_case_key = ?",
                (use_case_key,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def fallbacks(self) -> list[ApprovedFallback]:
        with self._lock:
            try:
                rows = self._conn.execute(_SELECT_FALLBACKS).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        return [_row_to_fallback(row) for row in rows]

    def use_case_usage_since(
        self,
        use_case_key: str,
        since_ts: float,
        *,
        swapped_to: str | None = None,
    ) -> dict:
        """The use-case's traffic from ``since_ts`` on. With ``swapped_to``,
        only calls the route actually swapped to that model (the honest basis
        for a realized saving — no-op calls and experiment arms excluded)."""
        with self._lock:
            if swapped_to is None:
                row = self._conn.execute(
                    _USE_CASE_USAGE_SINCE, (_UNKEYED, use_case_key, since_ts)
                ).fetchone()
            else:
                row = self._conn.execute(
                    _USE_CASE_USAGE_SINCE_SWAPPED,
                    (_UNKEYED, use_case_key, since_ts, swapped_to),
                ).fetchone()
        return {
            "calls": int(row[0]),
            "input_tokens": int(row[1]),
            "output_tokens": int(row[2]),
            "cache_read_tokens": int(row[3]),
            "cache_write_tokens": int(row[4]),
            "cost_usd": float(row[5]),
        }

    def use_case_model_breakdown(self, use_case_key: str) -> list[dict]:
        """Which models served this use-case, at what volume and spend —
        the served model where the router swapped, else the requested one."""
        with self._lock:
            rows = self._conn.execute(
                _USE_CASE_MODEL_BREAKDOWN,
                ("(unknown)", "(unknown)", _UNKEYED, use_case_key),
            ).fetchall()
        return [
            {
                "provider": r[0],
                "model": r[1],
                "calls": int(r[2]),
                "cost_usd": float(r[3]),
            }
            for r in rows
        ]
