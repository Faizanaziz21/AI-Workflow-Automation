"""Outbound HTTP with an SSRF egress policy.

Workflow authors control URLs (HTTP node, REST connector base URLs), so by default requests to
loopback, private, link-local (incl. cloud metadata 169.254.169.254), multicast and reserved ranges
are refused. Hosts listed in ``FF_EGRESS_ALLOWLIST`` bypass the check (e.g. internal ERP endpoints),
and ``FF_ALLOW_PRIVATE_NETWORK_EGRESS=true`` disables it for development.

The check runs inside the transport for *every* request, including redirects.
"""

from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import socket

import httpx

from app.core.config import get_settings


class EgressDeniedError(httpx.RequestError):
    pass


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


def host_allowlisted(host: str) -> bool:
    return any(fnmatch.fnmatch(host.lower(), pattern.lower()) for pattern in get_settings().egress_allowlist)


async def check_host(host: str, port: int | None) -> None:
    settings = get_settings()
    if settings.allow_private_network_egress or host_allowlisted(host):
        return
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        addresses = [literal]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise EgressDeniedError(f"DNS resolution failed for {host}") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    for addr in addresses:
        if not _is_public(addr):
            raise EgressDeniedError(f"Egress to non-public address {addr} ({host}) is blocked by policy")


class EgressPolicyTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme not in ("http", "https"):
            raise EgressDeniedError(f"Scheme {request.url.scheme!r} not allowed")
        await check_host(request.url.host, request.url.port)
        return await super().handle_async_request(request)


_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Shared pooled client for connector/node traffic (policy-enforcing)."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            transport=EgressPolicyTransport(retries=0),
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            follow_redirects=True,
            max_redirects=5,
            headers={"User-Agent": "FlowForge/1.0 (+https://flowforge.ai)"},
        )
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
