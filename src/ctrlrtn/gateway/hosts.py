"""The Host-header check that guards local-only surfaces against DNS rebinding."""

from __future__ import annotations

import ipaddress


def trusted_host(host_header: str, extra: tuple[str, ...]) -> bool:
    """True when the Host names this box the way a legitimate client would:
    ``localhost``, an IP literal, or an operator-configured name. A rebound
    attack domain is a DNS name the operator never configured."""
    host = (host_header or "").strip()
    if host.startswith("["):  # bracketed IPv6, e.g. [::1]:4000
        end = host.find("]")
        host = host[1:end] if end != -1 else ""
    else:
        host = host.split(":", 1)[0]
    if not host:
        return False
    lowered = host.lower()
    if lowered == "localhost":
        return True
    if lowered in {name.lower() for name in extra}:
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
