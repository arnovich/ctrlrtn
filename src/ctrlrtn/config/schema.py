"""Scalar configuration schema shared by defaults, YAML, and environment."""

from __future__ import annotations

from ctrlrtn.config.models import (
    _DEFAULT_DB,
    _DEFAULT_HOST,
    _DEFAULT_PORT,
    _DEFAULT_TIMEOUT,
    ConfigError,
)
from ctrlrtn.policy.budget import BudgetPolicy
from ctrlrtn.routing import ANTHROPIC_BASE_URL, OPENAI_BASE_URL

_TRUTHY = {"1", "true", "yes", "on"}


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _as_int(value: object) -> int:
    return int(value)


def _as_float(value: object) -> float:
    return float(value)


def _as_str(value: object) -> str:
    return str(value)


def _as_hosts(value: object) -> tuple:
    """Extra trusted control-plane Host names: a YAML list or a
    comma-separated string."""
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = str(value).split(",")
    return tuple(s.strip() for s in items if s.strip())


def _coerce(coerce, value: object, key: str, source: str) -> object:
    # _as_hosts is the one list-shaped setting; everything else is a scalar.
    if isinstance(value, dict) or (
        isinstance(value, list) and coerce is not _as_hosts
    ):
        raise ConfigError(
            f"config {key} ({source}) must be a scalar, got {value!r}"
        )
    try:
        return coerce(value)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"config {key}={value!r} ({source}): {exc}") from None


# One schema drives the defaults, the env parsing, and the YAML parsing so the
# three can't drift. Columns: config key, YAML key, env var, coercer, default.
_SCHEMA: tuple[tuple[str, str, str, object, object], ...] = (
    ("db_path", "db_path", "CTRLRTN_DB", _as_str, _DEFAULT_DB),
    ("host", "host", "CTRLRTN_HOST", _as_str, _DEFAULT_HOST),
    ("port", "port", "CTRLRTN_PORT", _as_int, _DEFAULT_PORT),
    (
        "log_requests",
        "log_requests",
        "CTRLRTN_LOG_REQUESTS",
        _as_bool,
        False,
    ),
    ("log_level", "log_level", "CTRLRTN_LOG_LEVEL", _as_str, "info"),
    ("upstream_base_url", "upstream", "CTRLRTN_UPSTREAM", _as_str, None),
    (
        "anthropic_upstream",
        "anthropic_upstream",
        "CTRLRTN_ANTHROPIC_UPSTREAM",
        _as_str,
        ANTHROPIC_BASE_URL,
    ),
    (
        "openai_upstream",
        "openai_upstream",
        "CTRLRTN_OPENAI_UPSTREAM",
        _as_str,
        OPENAI_BASE_URL,
    ),
    ("timeout", "timeout", "CTRLRTN_TIMEOUT", _as_float, _DEFAULT_TIMEOUT),
    (
        "inject_cache",
        "inject_cache",
        "CTRLRTN_INJECT_CACHE",
        _as_bool,
        False,
    ),
    ("kill_switch", "kill_switch", "CTRLRTN_KILL_SWITCH", _as_bool, False),
    (
        "retention_days",
        "retention_days",
        "CTRLRTN_RETENTION_DAYS",
        _as_int,
        None,
    ),
    (
        "control_hosts",
        "control_hosts",
        "CTRLRTN_CONTROL_HOSTS",
        _as_hosts,
        (),
    ),
)

_DEFAULTS = {key: default for key, _, _, _, default in _SCHEMA}
_DEFAULTS["providers"] = ()
_DEFAULTS["budget_policy"] = BudgetPolicy()
_BY_YAML = {yk: (key, coerce) for key, yk, _, coerce, _ in _SCHEMA}
_BY_ENV = {env: (key, coerce) for key, _, env, coerce, _ in _SCHEMA}
