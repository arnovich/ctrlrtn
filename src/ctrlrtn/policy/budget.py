"""Spend-ceiling admission rules and process-local in-flight reservations.

Accounting is deliberately supplied by the caller: the policy never performs
I/O and therefore stays usable from an in-memory gateway snapshot.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ctrlrtn.identify.fingerprint import fingerprint
from ctrlrtn.recorder.trace import Trace
from ctrlrtn.telemetry.pricing import price_for
from ctrlrtn.telemetry.usage import extract_model


@dataclass(frozen=True)
class BudgetPolicy:
    """Optional daily and lifetime session USD ceilings."""

    global_daily_usd: float | None = None
    use_case_daily_usd: dict[str, float] = field(default_factory=dict)
    use_case_fallback_usd: dict[str, float] = field(default_factory=dict)
    session_limit_usd: float | None = None
    reserve_in_flight: bool = False

    def __post_init__(self) -> None:
        values = [
            self.global_daily_usd,
            self.session_limit_usd,
            *self.use_case_daily_usd.values(),
            *self.use_case_fallback_usd.values(),
        ]
        if any(
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            )
            for value in values
        ):
            raise ValueError("budget ceilings must be finite and >= 0")
        if any(
            not isinstance(key, str) or not key
            for key in {
                *self.use_case_daily_usd,
                *self.use_case_fallback_usd,
            }
        ):
            raise ValueError("budget use-case keys must be non-empty strings")
        for key, threshold in self.use_case_fallback_usd.items():
            ceiling = self.use_case_daily_usd.get(key)
            if ceiling is None:
                raise ValueError(
                    "budget fallback thresholds require a matching use-case "
                    "daily ceiling"
                )
            if threshold >= ceiling:
                raise ValueError(
                    "budget fallback threshold must be below its hard ceiling"
                )
        if not isinstance(self.reserve_in_flight, bool):
            raise ValueError("reserve_in_flight must be a boolean")
        if self.reserve_in_flight and not (
            self.global_daily_usd is not None
            or self.use_case_daily_usd
            or self.session_limit_usd is not None
        ):
            raise ValueError("reserve_in_flight requires a budget ceiling")

    @property
    def enabled(self) -> bool:
        return (
            self.global_daily_usd is not None
            or bool(self.use_case_daily_usd)
            or self.session_limit_usd is not None
        )


class SpendSnapshot:
    """In-memory daily spend plus lifetime totals for known session IDs."""

    _day_ordinal: int | None
    total: float
    by_use_case: dict[str, float]
    by_session: dict[str, float]
    unknown_sessions: set[str]

    @staticmethod
    def _day(ts: float) -> int:
        return datetime.fromtimestamp(ts, UTC).date().toordinal()

    def _rollover(self, now: float) -> None:
        day = self._day(now)
        if self._day_ordinal != day:
            self._day_ordinal = day
            self.total = 0.0
            self.by_use_case = {}

    def __init__(self) -> None:
        self._day_ordinal = None
        self.total = 0.0
        self.by_use_case = {}
        self.by_session = {}
        self.unknown_sessions = set()

    def seed(
        self,
        total: float,
        by_use_case: Mapping[str, float],
        *,
        now: float | None = None,
    ) -> None:
        """Load today's persisted totals once at gateway startup."""
        self._rollover(time.time() if now is None else now)
        self.total = total
        self.by_use_case = dict(by_use_case)

    def seed_sessions(
        self,
        by_session: Mapping[str, float],
        *,
        unknown_sessions: set[str] | None = None,
    ) -> None:
        """Load persisted lifetime totals once at gateway startup."""
        self.by_session = dict(by_session)
        self.unknown_sessions = set(unknown_sessions or ())

    def record(self, trace: Trace, *, now: float | None = None) -> None:
        """Include one persisted, enriched trace when it has a known cost."""
        current = time.time() if now is None else now
        self._rollover(current)
        if trace.cost_usd is None:
            if (
                trace.session_id is not None
                and trace.terminal_reason is None
                and not trace.provider_free
            ):
                self.unknown_sessions.add(trace.session_id)
            return
        if trace.session_id is not None:
            self.by_session[trace.session_id] = (
                self.by_session.get(trace.session_id, 0.0) + trace.cost_usd
            )
        if self._day(trace.ts) != self._day(current):
            return
        self.total += trace.cost_usd
        if trace.use_case_key is not None:
            self.by_use_case[trace.use_case_key] = (
                self.by_use_case.get(trace.use_case_key, 0.0) + trace.cost_usd
            )

    def totals(
        self, use_case: str | None, *, now: float | None = None
    ) -> tuple[float, float]:
        self._rollover(time.time() if now is None else now)
        return self.total, self.by_use_case.get(use_case or "", 0.0)

    def session_total(self, session_id: str) -> float:
        return self.by_session.get(session_id, 0.0)

    def session_cost_known(self, session_id: str) -> bool:
        return session_id not in self.unknown_sessions


def utc_day_start(now: float | None = None) -> float:
    """Unix timestamp for midnight UTC on the current/requested day."""
    moment = datetime.fromtimestamp(time.time() if now is None else now, UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    error_type: str | None = None
    scope: str | None = None
    spent_usd: float | None = None
    limit_usd: float | None = None
    reservation_id: int | None = None
    reserved_usd: float = 0.0


@dataclass(frozen=True)
class _Reservation:
    amount_usd: float
    use_case: str | None
    session_id: str | None


_REQUEST_TOKEN_OVERHEAD = 256


def _reservation_cost(body: bytes, model: str) -> float | None:
    """Conservative request-cost estimate, or None without an output bound.

    Request bytes plus a small protocol allowance bound the input estimate; the
    request's explicit output cap wins, otherwise the catalog cap is used.
    """
    price = price_for(model)
    if price is None:
        return None
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    bounds: list[int] = []
    for key in ("max_tokens", "max_completion_tokens"):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        bounds.append(value)
    output_tokens = max(bounds) if bounds else price.max_output
    if output_tokens is None:
        return None

    input_tokens = len(body) + _REQUEST_TOKEN_OVERHEAD
    input_rate = max(price.input, price.cache_read, price.cache_write)
    return (
        input_tokens * input_rate + output_tokens * price.output
    ) / 1_000_000


class BudgetGate:
    """Synchronous admission and reservations over an in-memory snapshot."""

    def __init__(self, policy: BudgetPolicy, snapshot: SpendSnapshot) -> None:
        self.policy = policy
        self.snapshot = snapshot
        self._next_reservation_id = 1
        self._reservations: dict[int, _Reservation] = {}

    @property
    def reserved_usd(self) -> float:
        return sum(r.amount_usd for r in self._reservations.values())

    def _reserved(
        self,
        *,
        use_case: str | None = None,
        session_id: str | None = None,
    ) -> float:
        return sum(
            reservation.amount_usd
            for reservation in self._reservations.values()
            if (use_case is None or reservation.use_case == use_case)
            and (session_id is None or reservation.session_id == session_id)
        )

    def _reserve(
        self,
        amount_usd: float,
        *,
        use_case: str | None,
        session_id: str | None,
    ) -> int:
        reservation_id = self._next_reservation_id
        self._next_reservation_id += 1
        self._reservations[reservation_id] = _Reservation(
            amount_usd, use_case, session_id
        )
        return reservation_id

    def _release(self, reservation_id: int) -> None:
        self._reservations.pop(reservation_id, None)

    def observe(self, trace: Trace, *, now: float | None = None) -> None:
        """Settle an in-flight reservation with the persisted trace's cost."""
        if trace.budget_reservation_id is not None:
            self._release(trace.budget_reservation_id)
        self.snapshot.record(trace, now=now)

    def check(
        self,
        headers: Mapping[str, str],
        original_body: bytes,
        served_body: bytes,
        *,
        provider_free: bool,
        fallback_applied: bool = False,
    ) -> BudgetDecision:
        if not self.policy.enabled:
            return BudgetDecision(True)
        model = extract_model(served_body)
        if model is None:
            return BudgetDecision(True)

        session_id = headers.get("x-ctrlrtn-session") or None
        if self.policy.session_limit_usd is not None and session_id is None:
            return BudgetDecision(False, error_type="ctrlrtn_session_required")

        use_case = (
            fingerprint(headers, original_body)
            if self.policy.use_case_daily_usd
            else None
        )
        use_case_limit = self.policy.use_case_daily_usd.get(use_case or "")
        fallback_limit = self.policy.use_case_fallback_usd.get(use_case or "")
        if (
            self.policy.global_daily_usd is None
            and use_case_limit is None
            and self.policy.session_limit_usd is None
        ):
            return BudgetDecision(True)

        total = 0.0
        use_case_total = 0.0
        if (
            self.policy.global_daily_usd is not None
            or use_case_limit is not None
        ):
            total, use_case_total = self.snapshot.totals(use_case)
            total += self.reserved_usd
            if use_case is not None:
                use_case_total += self._reserved(use_case=use_case)
            if (
                self.policy.global_daily_usd is not None
                and total >= self.policy.global_daily_usd
            ):
                return BudgetDecision(
                    False,
                    error_type="ctrlrtn_budget_exceeded",
                    scope="global",
                    spent_usd=total,
                    limit_usd=self.policy.global_daily_usd,
                )
            if use_case_limit is not None and use_case_total >= use_case_limit:
                return BudgetDecision(
                    False,
                    error_type="ctrlrtn_budget_exceeded",
                    scope=use_case,
                    spent_usd=use_case_total,
                    limit_usd=use_case_limit,
                )
            if (
                fallback_limit is not None
                and not fallback_applied
                and use_case_total >= fallback_limit
            ):
                return BudgetDecision(
                    False,
                    error_type="ctrlrtn_budget_fallback_required",
                    scope=use_case,
                    spent_usd=use_case_total,
                    limit_usd=fallback_limit,
                )
        if session_id is not None and self.policy.session_limit_usd is not None:
            if not self.snapshot.session_cost_known(session_id):
                return BudgetDecision(
                    False, error_type="ctrlrtn_unknown_session_cost"
                )
            session_total = self.snapshot.session_total(session_id)
            session_total += self._reserved(session_id=session_id)
            if session_total >= self.policy.session_limit_usd:
                return BudgetDecision(
                    False,
                    error_type="ctrlrtn_budget_exceeded",
                    scope=f"session:{session_id}",
                    spent_usd=session_total,
                    limit_usd=self.policy.session_limit_usd,
                )

        if not provider_free and price_for(model) is None:
            return BudgetDecision(False, error_type="ctrlrtn_unpriced_model")
        if not self.policy.reserve_in_flight or provider_free:
            return BudgetDecision(True)

        reserved_usd = _reservation_cost(served_body, model)
        if reserved_usd is None:
            return BudgetDecision(
                False, error_type="ctrlrtn_unreservable_request"
            )
        if (
            self.policy.global_daily_usd is not None
            and total + reserved_usd > self.policy.global_daily_usd
        ):
            return BudgetDecision(
                False,
                error_type="ctrlrtn_budget_exceeded",
                scope="global",
                spent_usd=total,
                limit_usd=self.policy.global_daily_usd,
            )
        if use_case_limit is not None and (
            use_case_total + reserved_usd > use_case_limit
        ):
            return BudgetDecision(
                False,
                error_type="ctrlrtn_budget_exceeded",
                scope=use_case,
                spent_usd=use_case_total,
                limit_usd=use_case_limit,
            )
        if session_id is not None and self.policy.session_limit_usd is not None:
            session_total = self.snapshot.session_total(session_id)
            session_total += self._reserved(session_id=session_id)
            if session_total + reserved_usd > self.policy.session_limit_usd:
                return BudgetDecision(
                    False,
                    error_type="ctrlrtn_budget_exceeded",
                    scope=f"session:{session_id}",
                    spent_usd=session_total,
                    limit_usd=self.policy.session_limit_usd,
                )

        reservation_id = self._reserve(
            reserved_usd,
            use_case=use_case if use_case_limit is not None else None,
            session_id=(
                session_id
                if self.policy.session_limit_usd is not None
                else None
            ),
        )
        return BudgetDecision(
            True,
            reservation_id=reservation_id,
            reserved_usd=reserved_usd,
        )
