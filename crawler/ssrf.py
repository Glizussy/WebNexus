"""SSRF (Server-Side Request Forgery) protection and destination validation."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp.abc import AbstractResolver


class SSRFSecurityError(Exception):
    """Raised when a URL destination targets a prohibited or internal IP address or host."""
    pass


# Specific cloud metadata IP addresses and hostnames
PROHIBITED_METADATA_IPS = {
    "169.254.169.254",  # AWS, GCP, Azure, OpenStack instance metadata
    "fd00:ec2::254",     # AWS IPv6 metadata
}

PROHIBITED_HOSTNAMES = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "metadata.google.internal",
    "instance-data",
}


def is_ip_prohibited(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address belongs to private, loopback, link-local, multicast, or non-global ranges."""
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped  # Unwrap IPv4-mapped IPv6 address (e.g. ::ffff:127.0.0.1)
    if str(ip) in PROHIBITED_METADATA_IPS:
        return True
    return bool(
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_destination_sync(
    url: str,
    allow_private_ips: bool = False,
    blocked_hostnames: tuple[str, ...] = tuple(PROHIBITED_HOSTNAMES),
) -> None:
    """Synchronously validate that a URL targets a safe, non-internal destination.

    Raises SSRFSecurityError if prohibited.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise SSRFSecurityError(f"Unsupported scheme '{parsed.scheme}': only http and https are permitted")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFSecurityError(f"Invalid URL '{url}': missing hostname")

    hostname_clean = hostname.lower().strip("[]")

    if not allow_private_ips:
        if hostname_clean in blocked_hostnames:
            raise SSRFSecurityError(f"Access to prohibited host '{hostname}' is blocked")

    # Check if hostname is an explicit IP literal
    try:
        ip = ipaddress.ip_address(hostname_clean)
        if not allow_private_ips and is_ip_prohibited(ip):
            raise SSRFSecurityError(f"Access to prohibited IP literal '{ip}' is blocked")
        return
    except ValueError:
        # Not an IP literal, it is a DNS name
        pass

    if allow_private_ips:
        return

    # Resolve hostname to check for DNS rebinding or private IP mapping
    try:
        addrinfo = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, socket.herror, socket.timeout, TimeoutError, OSError) as e:
        raise SSRFSecurityError(f"DNS resolution failed or timed out for '{hostname}': {e}") from e

    if not addrinfo:
        raise SSRFSecurityError(f"No IP addresses found for hostname '{hostname}'")

    for entry in addrinfo:
        sockaddr = entry[4]
        ip_str = sockaddr[0]
        try:
            resolved_ip = ipaddress.ip_address(ip_str)
            if is_ip_prohibited(resolved_ip):
                raise SSRFSecurityError(
                    f"Host '{hostname}' resolved to prohibited IP '{ip_str}'"
                )
        except ValueError:
            raise SSRFSecurityError(f"Invalid IP address returned for host: '{ip_str}'")


async def validate_destination_async(
    url: str,
    allow_private_ips: bool = False,
    blocked_hostnames: tuple[str, ...] = tuple(PROHIBITED_HOSTNAMES),
) -> None:
    """Asynchronously validate that a URL targets a safe destination using non-blocking DNS resolution."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        validate_destination_sync,
        url,
        allow_private_ips,
        blocked_hostnames,
    )


class SafeSSRFResolver(AbstractResolver):
    """Custom aiohttp DNS resolver that validates destination IP addresses at connection time.

    Mitigates DNS rebinding and TOCTOU (Time-of-Check / Time-of-Use) attacks by
    ensuring that every address returned by DNS is inspected before establishing a connection.
    Fails closed on any prohibited address, DNS resolution error, or mixed answers
    containing prohibited destinations.
    """

    def __init__(
        self,
        allow_private_ips: bool = False,
        blocked_hostnames: tuple[str, ...] = tuple(PROHIBITED_HOSTNAMES),
        underlying_resolver: AbstractResolver | None = None,
    ) -> None:
        self.allow_private_ips = allow_private_ips
        self.blocked_hostnames = {h.lower() for h in blocked_hostnames}
        self._resolver = underlying_resolver or aiohttp.resolver.DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_UNSPEC,
    ) -> list[dict[str, Any]]:
        host_clean = host.lower().strip("[]")

        # 1. Prohibited hostname check
        if not self.allow_private_ips and host_clean in self.blocked_hostnames:
            raise SSRFSecurityError(f"Access to prohibited hostname '{host}' is blocked")

        # 2. Check if host is an explicit IP literal
        try:
            ip_lit = ipaddress.ip_address(host_clean)
            if not self.allow_private_ips and is_ip_prohibited(ip_lit):
                raise SSRFSecurityError(f"Access to prohibited IP literal '{ip_lit}' is blocked")
        except ValueError:
            pass

        # 3. Perform DNS resolution using underlying resolver
        try:
            results = await self._resolver.resolve(host, port, family)
        except SSRFSecurityError:
            raise
        except Exception as e:
            raise SSRFSecurityError(f"DNS resolution failed or timed out for '{host}': {e}") from e

        if not results:
            raise SSRFSecurityError(f"No IP addresses resolved for hostname '{host}'")

        if self.allow_private_ips:
            return results

        # 4. Strict multi-answer validation
        # If ANY DNS answer points to a prohibited IP, reject the entire host (fail closed)
        for r in results:
            ip_str = r.get("host")
            if not ip_str:
                raise SSRFSecurityError(f"Missing IP address in DNS answer for '{host}'")
            try:
                resolved_ip = ipaddress.ip_address(ip_str)
            except ValueError:
                raise SSRFSecurityError(f"Malformed IP address in DNS answer for '{host}': '{ip_str}'")

            if is_ip_prohibited(resolved_ip):
                raise SSRFSecurityError(
                    f"Host '{host}' resolved to prohibited IP '{ip_str}' (connection blocked)"
                )

        return results

    async def close(self) -> None:
        await self._resolver.close()
