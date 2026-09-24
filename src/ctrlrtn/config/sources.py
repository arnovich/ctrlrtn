"""YAML and environment configuration sources."""

from __future__ import annotations

import os

import yaml

from ctrlrtn.config.budgets import _parse_budgets
from ctrlrtn.config.models import ConfigError
from ctrlrtn.config.providers import _parse_providers
from ctrlrtn.config.schema import _BY_ENV, _BY_YAML, _coerce

_CONFIG_ENV = "CTRLRTN_CONFIG"
_DEFAULT_CONFIG_FILE = "ctrlrtn.yaml"


def _config_path() -> str | None:
    """The YAML config path: explicit ``CTRLRTN_CONFIG``, else the default
    file if it exists, else none."""
    explicit = os.environ.get(_CONFIG_ENV, "").strip()
    if explicit:
        # An explicit-but-missing path is an error (see _from_yaml).
        return os.path.expanduser(explicit)
    if os.path.exists(_DEFAULT_CONFIG_FILE):
        return _DEFAULT_CONFIG_FILE
    return None


def _from_yaml() -> dict:
    path = _config_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError:
        # Only reachable for an explicit CTRLRTN_CONFIG (the default file
        # is checked for existence first) — a typo'd path should fail loudly.
        raise ConfigError(
            f"config file not found: {path} (from {_CONFIG_ENV})"
        ) from None
    if data is None:  # empty file
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must be a YAML mapping")
    unknown = set(data) - set(_BY_YAML) - {"providers", "budgets"}
    if unknown:
        raise ConfigError(
            f"unknown config key(s) in {path}: {', '.join(sorted(unknown))}"
        )
    # A null value (`port:` with nothing) is "unset", not the literal None —
    # fall through to env/default rather than coercing None into "None"/a crash.
    values = {
        key: _coerce(coerce, data[yk], key, path)
        for yk, (key, coerce) in _BY_YAML.items()
        if data.get(yk) is not None
    }
    if data.get("providers") is not None:
        values["providers"] = _parse_providers(data["providers"], path)
    if data.get("budgets") is not None:
        values["budget_policy"] = _parse_budgets(data["budgets"], path)
    return values


def _from_env() -> dict:
    # An empty/whitespace env value means "unset" (falls through to file /
    # default) for every key. NOTE: this means an env var cannot *clear* a
    # YAML-set `upstream` back to per-path routing — set it to a real URL, or
    # unset it in the file.
    values: dict = {}
    for env, (key, coerce) in _BY_ENV.items():
        raw = os.environ.get(env)
        if raw is not None and raw.strip() != "":
            values[key] = _coerce(coerce, raw, key, env)
    return values
