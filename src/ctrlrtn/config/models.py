"""Public runtime-configuration values and errors."""

from __future__ import annotations

from dataclasses import dataclass, field

from ctrlrtn.policy.budget import BudgetPolicy
from ctrlrtn.routing import UpstreamRoute, UpstreamRouter, default_routes

_DEFAULT_TIMEOUT = 600.0
_DEFAULT_DB = "ctrlrtn.db"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 4000


class ConfigError(ValueError):
    """A malformed configuration value (bad key, type, or coercion). Subclasses
    ValueError so callers can catch either; the CLI turns it into a clean
    ``_fail`` instead of a traceback."""


@dataclass(frozen=True, kw_only=True)
class Settings:
    """Immutable gateway configuration.

    ``upstream_base_url`` (when set) forces a single upstream for every path.
    Otherwise ``routes`` selects the upstream per request; an empty ``routes``
    falls back to the built-in provider routes. Keyword-only so an extra field
    can never silently land in the wrong slot.
    """

    db_path: str = _DEFAULT_DB
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    log_requests: bool = False
    log_level: str = "info"
    upstream_base_url: str | None = None
    routes: tuple[UpstreamRoute, ...] = field(default_factory=tuple)
    timeout: float = _DEFAULT_TIMEOUT
    inject_cache: bool = False
    kill_switch: bool = False
    retention_days: int | None = None
    budget_policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    # Extra Host names the /ctrlrtn/ control plane trusts (the DNS-rebinding
    # guard in app.py); localhost and IP literals are always trusted.
    control_hosts: tuple[str, ...] = ()

    def resolver(self) -> UpstreamRouter:
        if self.upstream_base_url:
            return UpstreamRouter(default=self.upstream_base_url)
        return UpstreamRouter(routes=self.routes or default_routes())
