"""Budget-policy configuration parsing."""

from __future__ import annotations

import math

from ctrlrtn.config.models import ConfigError
from ctrlrtn.policy.budget import BudgetPolicy

_BUDGET_KEYS = frozenset(
    {"global", "session", "use_cases", "reserve_in_flight"}
)


def _usd(value: object, label: str, field_name: str, source: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} {field_name} ({source}) must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ConfigError(
            f"{label} {field_name} ({source}) must be a finite non-negative number"
        )
    return result


def _parse_budgets(value: object, source: str) -> BudgetPolicy:
    """Parse YAML-only daily and lifetime session ceilings."""
    if not isinstance(value, dict):
        raise ConfigError(f"config budgets ({source}) must be a mapping")
    unknown = set(value) - _BUDGET_KEYS
    if unknown:
        raise ConfigError(
            f"unknown budget key(s) ({source}): {', '.join(sorted(unknown))}"
        )

    reserve_in_flight = value.get("reserve_in_flight", False)
    if not isinstance(reserve_in_flight, bool):
        raise ConfigError(
            f"budget reserve_in_flight ({source}) must be a boolean"
        )

    global_limit = None
    raw_global = value.get("global")
    if raw_global is not None:
        if not isinstance(raw_global, dict) or set(raw_global) != {"daily_usd"}:
            raise ConfigError(
                f"budget global ({source}) must contain only daily_usd"
            )
        global_limit = _usd(
            raw_global["daily_usd"], "budget global", "daily_usd", source
        )

    session_limit = None
    raw_session = value.get("session")
    if raw_session is not None:
        if not isinstance(raw_session, dict) or set(raw_session) != {
            "limit_usd"
        }:
            raise ConfigError(
                f"budget session ({source}) must contain only limit_usd"
            )
        session_limit = _usd(
            raw_session["limit_usd"],
            "budget session",
            "limit_usd",
            source,
        )

    use_case_limits: dict[str, float] = {}
    raw_use_cases = value.get("use_cases", {})
    if not isinstance(raw_use_cases, dict):
        raise ConfigError(f"budget use_cases ({source}) must be a mapping")
    for key, raw in raw_use_cases.items():
        if not isinstance(key, str) or not key:
            raise ConfigError(
                f"budget use-case key {key!r} ({source}) must be non-empty"
            )
        allowed = {"daily_usd", "fallback_at_usd"}
        if (
            not isinstance(raw, dict)
            or not set(raw).issubset(allowed)
            or "daily_usd" not in raw
        ):
            raise ConfigError(
                f"budget use-case {key!r} ({source}) must contain daily_usd "
                "and optional fallback_at_usd"
            )
        use_case_limits[key] = _usd(
            raw["daily_usd"],
            f"budget use-case {key!r}",
            "daily_usd",
            source,
        )
    fallback_limits = {
        key: _usd(
            raw["fallback_at_usd"],
            f"budget use-case {key!r}",
            "fallback_at_usd",
            source,
        )
        for key, raw in raw_use_cases.items()
        if "fallback_at_usd" in raw
    }
    if reserve_in_flight and (
        global_limit is None and session_limit is None and not use_case_limits
    ):
        raise ConfigError(
            f"budget reserve_in_flight ({source}) requires a ceiling"
        )
    try:
        return BudgetPolicy(
            global_daily_usd=global_limit,
            use_case_daily_usd=use_case_limits,
            use_case_fallback_usd=fallback_limits,
            session_limit_usd=session_limit,
            reserve_in_flight=reserve_in_flight,
        )
    except ValueError as exc:
        raise ConfigError(f"invalid budget config ({source}): {exc}") from exc
