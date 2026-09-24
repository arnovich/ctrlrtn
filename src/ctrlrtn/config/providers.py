"""Named upstream-provider and credential configuration parsing."""

from __future__ import annotations

import os

from ctrlrtn.config.models import ConfigError
from ctrlrtn.routing import ProviderCredential, UpstreamRoute

_PROVIDER_KEYS = frozenset({"base_url", "prefix", "api", "free", "credential"})
_CREDENTIAL_KEYS = frozenset({"env", "header", "prefix"})
_PROVIDER_APIS = frozenset({"anthropic", "openai"})


def _parse_credential(
    value: object, provider: str, source: str
) -> ProviderCredential:
    """Parse an environment-owned credential for a named provider."""
    if not isinstance(value, dict):
        raise ConfigError(
            f"provider {provider!r} credential ({source}) must be a mapping"
        )
    unknown = set(value) - _CREDENTIAL_KEYS
    if unknown:
        raise ConfigError(
            f"unknown credential key(s) for {provider!r} ({source}): "
            f"{', '.join(sorted(unknown))}"
        )
    env = value.get("env")
    if not isinstance(env, str) or not env.strip():
        raise ConfigError(
            f"provider {provider!r} credential ({source}) requires a non-empty env"
        )
    if not os.environ.get(env):
        raise ConfigError(
            f"provider {provider!r} credential env {env!r} ({source}) is not set"
        )
    header = value.get("header", "authorization")
    if not isinstance(header, str) or not header.strip():
        raise ConfigError(
            f"provider {provider!r} credential header ({source}) must be non-empty"
        )
    prefix = value.get("prefix", "Bearer ")
    if not isinstance(prefix, str):
        raise ConfigError(
            f"provider {provider!r} credential prefix ({source}) must be a string"
        )
    return ProviderCredential(env=env, header=header.lower(), prefix=prefix)


def _parse_providers(value: object, source: str) -> tuple[UpstreamRoute, ...]:
    """Parse the structured, YAML-only named-provider mapping."""
    if not isinstance(value, dict):
        raise ConfigError(f"config providers ({source}) must be a mapping")
    routes = []
    prefixes: set[str] = set()
    for name, raw in value.items():
        if (
            not isinstance(name, str)
            or not name
            or any(
                char
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for char in name
            )
        ):
            raise ConfigError(
                f"provider name {name!r} ({source}) must use letters, numbers, "
                "_ or -"
            )
        if not isinstance(raw, dict):
            raise ConfigError(f"provider {name!r} ({source}) must be a mapping")
        unknown = set(raw) - _PROVIDER_KEYS
        if unknown:
            raise ConfigError(
                f"unknown provider key(s) for {name!r} ({source}): "
                f"{', '.join(sorted(unknown))}"
            )
        base_url = raw.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ConfigError(
                f"provider {name!r} ({source}) requires a non-empty base_url"
            )
        prefix = raw.get("prefix", f"/{name}")
        if not isinstance(prefix, str) or not prefix.startswith("/"):
            raise ConfigError(
                f"provider {name!r} prefix ({source}) must start with /"
            )
        if prefix == "/" or prefix.endswith("/"):
            raise ConfigError(
                f"provider {name!r} prefix ({source}) must be a non-root path "
                "without a trailing /"
            )
        if prefix == "/ctrlrtn" or prefix.startswith("/ctrlrtn/"):
            raise ConfigError(
                f"provider {name!r} prefix {prefix!r} uses the reserved "
                "/ctrlrtn control-plane namespace"
            )
        if prefix in prefixes:
            raise ConfigError(
                f"duplicate provider prefix {prefix!r} ({source})"
            )
        prefixes.add(prefix)
        api = raw.get("api")
        if api is not None and api not in _PROVIDER_APIS:
            raise ConfigError(
                f"provider {name!r} api ({source}) must be one of: "
                f"{', '.join(sorted(_PROVIDER_APIS))}"
            )
        free = raw.get("free", False)
        if not isinstance(free, bool):
            raise ConfigError(
                f"provider {name!r} free ({source}) must be a boolean"
            )
        credential = (
            _parse_credential(raw["credential"], name, source)
            if raw.get("credential") is not None
            else None
        )
        routes.append(
            UpstreamRoute(
                name=name,
                base_url=base_url,
                path_prefixes=(prefix,),
                strip_prefix=prefix,
                api=api,
                free=free,
                credential=credential,
            )
        )
    return tuple(routes)
