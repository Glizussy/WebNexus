"""Regression tests for DNS rebinding protection and SafeSSRFResolver."""

import asyncio
import socket
import pytest
from crawler.config import CrawlerConfig
from crawler.fetcher import AsyncFetcher
from crawler.models import CrawlRequest, FetchStatus
from crawler.ssrf import SafeSSRFResolver, SSRFSecurityError


@pytest.mark.asyncio
async def test_resolver_permits_public_ipv4():
    resolver = SafeSSRFResolver(allow_private_ips=False)

    class MockResolver:
        async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
            return [{
                "hostname": host,
                "host": "93.184.216.34",  # Public IP (example.com)
                "port": port,
                "family": socket.AF_INET,
                "proto": 6,
                "flags": 0,
            }]
        async def close(self): pass

    resolver._resolver = MockResolver()
    results = await resolver.resolve("example.com", 80)
    assert len(results) == 1
    assert results[0]["host"] == "93.184.216.34"


@pytest.mark.asyncio
async def test_resolver_blocks_private_and_loopback_ips():
    resolver = SafeSSRFResolver(allow_private_ips=False)

    for private_ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.169.254", "100.64.0.1"):
        class MockResolver:
            def __init__(self, ip):
                self.ip = ip
            async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
                return [{"hostname": host, "host": self.ip, "port": port, "family": socket.AF_INET, "proto": 6, "flags": 0}]
            async def close(self): pass

        resolver._resolver = MockResolver(private_ip)
        with pytest.raises(SSRFSecurityError, match="prohibited IP"):
            await resolver.resolve("test.internal", 80)


@pytest.mark.asyncio
async def test_resolver_handles_ipv6_addresses():
    resolver = SafeSSRFResolver(allow_private_ips=False)

    # 1. Public IPv6 allowed
    class MockPublicIPv6:
        async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
            return [{"hostname": host, "host": "2606:2800:220:1:248:1893:25c8:1946", "port": port, "family": socket.AF_INET6, "proto": 6, "flags": 0}]
        async def close(self): pass

    resolver._resolver = MockPublicIPv6()
    results = await resolver.resolve("ipv6.example.com", 80)
    assert len(results) == 1

    # 2. Loopback ::1 blocked
    class MockLoopbackIPv6:
        async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
            return [{"hostname": host, "host": "::1", "port": port, "family": socket.AF_INET6, "proto": 6, "flags": 0}]
        async def close(self): pass

    resolver._resolver = MockLoopbackIPv6()
    with pytest.raises(SSRFSecurityError, match="prohibited IP"):
        await resolver.resolve("ipv6-local.example.com", 80)

    # 3. Unique Local Address (ULA) fd00::1 blocked
    class MockULAIPv6:
        async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
            return [{"hostname": host, "host": "fd00::1", "port": port, "family": socket.AF_INET6, "proto": 6, "flags": 0}]
        async def close(self): pass

    resolver._resolver = MockULAIPv6()
    with pytest.raises(SSRFSecurityError, match="prohibited IP"):
        await resolver.resolve("ipv6-ula.example.com", 80)


@pytest.mark.asyncio
async def test_resolver_fails_closed_on_mixed_dns_answers():
    """Verify that if ANY DNS answer points to a prohibited IP, the connection is aborted."""
    resolver = SafeSSRFResolver(allow_private_ips=False)

    class MockMixedResolver:
        async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
            return [
                {"hostname": host, "host": "93.184.216.34", "port": port, "family": socket.AF_INET, "proto": 6, "flags": 0},
                {"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET, "proto": 6, "flags": 0},  # Poisoned/internal address
            ]
        async def close(self): pass

    resolver._resolver = MockMixedResolver()
    with pytest.raises(SSRFSecurityError, match="Host .* resolved to prohibited IP '127.0.0.1'"):
        await resolver.resolve("mixed-dns.example.com", 80)


@pytest.mark.asyncio
async def test_resolver_fails_closed_on_dns_error():
    resolver = SafeSSRFResolver(allow_private_ips=False)

    class MockFailingResolver:
        async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
            raise socket.gaierror(-3, "Temporary failure in name resolution")
        async def close(self): pass

    resolver._resolver = MockFailingResolver()
    with pytest.raises(SSRFSecurityError, match="DNS resolution failed or timed out"):
        await resolver.resolve("failing-dns.example.com", 80)


@pytest.mark.asyncio
async def test_connection_time_dns_rebinding_blocked(monkeypatch):
    """Simulate DNS rebinding where pre-check passes or hostname dynamically points to private IP at connection time."""
    # When allow_private_ips is False, connecting to a rebinding domain that resolves to 127.0.0.1 must be caught
    # at connection time by SafeSSRFResolver.
    orig_getaddrinfo = socket.getaddrinfo

    def rebinding_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host == "rebind.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return orig_getaddrinfo(host, port, family, type, proto, flags)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_getaddrinfo)

    cfg = CrawlerConfig(allow_private_ips=False, max_retries=0)
    async with AsyncFetcher(cfg) as fetcher:
        req = CrawlRequest("http://rebind.example.com/sensitive-data")
        res = await fetcher.fetch(req)
        assert res.status == FetchStatus.SSRF_BLOCKED
        assert res.error is not None
        assert "prohibited" in res.error.message.lower()
