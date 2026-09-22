"""XiaoYou-owned HTTP egress policy for Platform Adapters.

The policy deliberately lives outside Hermes Core.  It is a transport safety
boundary only: it neither interprets messages nor selects a business action.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


def platform_httpx_limits() -> httpx.Limits:
    """Conservative reusable connection limits for XiaoYou platform traffic."""

    return httpx.Limits(max_connections=20, max_keepalive_connections=8, keepalive_expiry=20.0)


def is_safe_platform_url(value: str) -> bool:
    """Allow only public HTTP(S) destinations, resolving hostnames before use."""

    parsed = urlparse(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.rstrip(".")
    try:
        direct = ipaddress.ip_address(host)
        return bool(direct.is_global)
    except ValueError:
        pass
    try:
        rows = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        addresses = {row[4][0] for row in rows}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        # DNS failure must fail closed rather than fall through to an internal
        # resolver/redirect.  The caller keeps the real transport failure.
        return False


async def reject_unsafe_redirect(response: httpx.Response) -> None:
    """Reject a redirect whose next hop is not a public HTTP(S) endpoint."""

    location = response.headers.get("location")
    if not location or response.status_code < 300 or response.status_code >= 400:
        return
    target = urljoin(str(response.request.url), location)
    if not is_safe_platform_url(target):
        raise ValueError("blocked_unsafe_platform_redirect")


def create_safe_async_client(*, timeout: float, follow_redirects: bool, limits: httpx.Limits | None = None, **kwargs: Any) -> httpx.AsyncClient:
    """Create a channel client with XiaoYou's SSRF/redirect policy attached."""

    hooks = dict(kwargs.pop("event_hooks", {}) or {})
    response_hooks = list(hooks.get("response") or [])
    response_hooks.append(reject_unsafe_redirect)
    hooks["response"] = response_hooks
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=follow_redirects,
        limits=limits or platform_httpx_limits(),
        event_hooks=hooks,
        **kwargs,
    )
