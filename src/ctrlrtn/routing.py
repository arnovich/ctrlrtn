"""Resolve which upstream provider a request is forwarded to.

One router endpoint can front several providers; the upstream is chosen per
request by URL path, because the API shape is provider-specific
(``/v1/messages`` is Anthropic, ``/v1/chat/completions`` is OpenAI), so a
router that fronts both providers sends each call to the one that speaks its
shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
OPENAI_BASE_URL = "https://api.openai.com"


@dataclass(frozen=True)
class ProviderCredential:
    """An upstream-owned credential, kept outside recorded request traffic."""

    env: str
    header: str = "authorization"
    prefix: str = "Bearer "

    def headers(self) -> dict[str, str]:
        """Read the secret only when building an upstream request."""
        secret = os.environ.get(self.env, "")
        if not secret:
            raise ValueError(
                f"provider credential environment variable {self.env!r} is not set"
            )
        return {self.header: self.prefix + secret}


@dataclass(frozen=True)
class UpstreamRoute:
    """Forward requests whose path starts with one of ``path_prefixes`` to
    ``base_url``."""

    name: str
    base_url: str
    path_prefixes: tuple[str, ...]
    strip_prefix: str | None = None
    api: str | None = None
    free: bool = False
    credential: ProviderCredential | None = None

    def matches(self, path: str) -> bool:
        return any(
            _matches_prefix(path, prefix) for prefix in self.path_prefixes
        )

    def forward_path(self, path: str) -> str:
        """Return the provider-facing path after removing a client mount."""
        if self.strip_prefix is None:
            return path
        remainder = path[len(self.strip_prefix) :]
        return remainder or "/"


@dataclass(frozen=True)
class ResolvedUpstream:
    """The upstream identity and provider-facing path for one request."""

    name: str | None
    base_url: str
    path: str
    api: str | None = None
    free: bool = False
    credential: ProviderCredential | None = None


def _matches_prefix(path: str, prefix: str) -> bool:
    """Match a complete path segment while allowing descendants."""
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


@dataclass(frozen=True)
class UpstreamRouter:
    """Resolve a request path to an upstream base URL.

    The first matching route wins. An unmatched path falls back to ``default``;
    ``default=None`` means the path is unroutable, so the gateway returns a
    local error rather than forwarding to the wrong provider. A single-upstream
    router is just ``UpstreamRouter(default=base_url)`` with no routes.
    """

    routes: tuple[UpstreamRoute, ...] = ()
    default: str | None = None

    def resolve(self, path: str) -> str | None:
        """Return only the base URL; retained for compatibility callers."""
        target = self.resolve_request(path)
        return target.base_url if target is not None else None

    def resolve_request(self, path: str) -> ResolvedUpstream | None:
        """Resolve both the upstream and the path it should receive."""
        for route in self.routes:
            if route.matches(path):
                return ResolvedUpstream(
                    name=route.name,
                    base_url=route.base_url,
                    path=route.forward_path(path),
                    api=route.api,
                    free=route.free,
                    credential=route.credential,
                )
        if self.default is None:
            return None
        return ResolvedUpstream(None, self.default, path)

    def resolve_provider(
        self, name: str, provider_path: str
    ) -> ResolvedUpstream | None:
        """Resolve an explicit provider while retaining an already-normalized
        provider-facing path. Used only by serving decisions that intentionally
        switch upstreams; client mount matching does not apply."""
        for route in self.routes:
            if route.name == name:
                return ResolvedUpstream(
                    name=route.name,
                    base_url=route.base_url,
                    path=provider_path,
                    api=route.api,
                    free=route.free,
                    credential=route.credential,
                )
        return None


def default_routes(
    *,
    anthropic_base_url: str = ANTHROPIC_BASE_URL,
    openai_base_url: str = OPENAI_BASE_URL,
) -> tuple[UpstreamRoute, ...]:
    """Built-in path -> provider routes for the common providers."""
    return (
        UpstreamRoute(
            "anthropic",
            anthropic_base_url,
            ("/v1/messages", "/v1/complete"),
            api="anthropic",
        ),
        UpstreamRoute(
            "openai",
            openai_base_url,
            (
                "/v1/chat/completions",
                "/v1/responses",
                "/v1/completions",
                "/v1/embeddings",
            ),
            api="openai",
        ),
    )
