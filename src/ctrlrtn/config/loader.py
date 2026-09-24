"""Layer runtime configuration sources into immutable settings."""

from __future__ import annotations

import os

from ctrlrtn.config.models import ConfigError, Settings
from ctrlrtn.config.schema import _DEFAULTS
from ctrlrtn.config.sources import _from_env, _from_yaml
from ctrlrtn.routing import UpstreamRoute, default_routes


def _build(cfg: dict) -> Settings:
    single = cfg["upstream_base_url"]
    named_routes = tuple(cfg["providers"])
    if single and named_routes:
        raise ConfigError(
            "upstream and providers cannot be configured together"
        )
    retention_days = cfg["retention_days"]
    if retention_days is not None and retention_days < 1:
        raise ConfigError("retention_days must be at least 1")
    routes: tuple[UpstreamRoute, ...] = (
        ()
        if single
        else (
            *named_routes,
            *default_routes(
                anthropic_base_url=cfg["anthropic_upstream"],
                openai_base_url=cfg["openai_upstream"],
            ),
        )
    )
    # Absolutize so the resolved path is unambiguous and `~` works. This does
    # NOT by itself make `serve` and the CLI agree when run from different
    # directories with the *relative default* — for that, set an absolute
    # db_path (or a shared CTRLRTN_CONFIG). See README.
    db_path = os.path.abspath(os.path.expanduser(cfg["db_path"]))
    return Settings(
        db_path=db_path,
        host=cfg["host"],
        port=cfg["port"],
        log_requests=cfg["log_requests"],
        log_level=cfg["log_level"],
        upstream_base_url=single,
        routes=routes,
        timeout=cfg["timeout"],
        inject_cache=cfg["inject_cache"],
        kill_switch=cfg["kill_switch"],
        retention_days=retention_days,
        budget_policy=cfg["budget_policy"],
        control_hosts=tuple(cfg["control_hosts"]),
    )


def load_settings(*, overrides: dict | None = None) -> Settings:
    """Build :class:`Settings` from defaults < YAML file < env < overrides.

    ``overrides`` (e.g. CLI flags) wins; ``None`` values in it are ignored so an
    unset flag falls through to env/file/default.
    """
    cfg = dict(_DEFAULTS)
    cfg.update(_from_yaml())
    cfg.update(_from_env())
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})
    return _build(cfg)
