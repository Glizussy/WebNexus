"""Unit tests for SSRF protection and destination validation."""

import pytest
from crawler.ssrf import SSRFSecurityError, validate_destination_sync


def test_ssrf_blocks_localhost():
    with pytest.raises(SSRFSecurityError, match="prohibited host"):
        validate_destination_sync("http://localhost/path", allow_private_ips=False)


def test_ssrf_blocks_127_0_0_1():
    with pytest.raises(SSRFSecurityError, match="prohibited host|prohibited IP"):
        validate_destination_sync("http://127.0.0.1/admin", allow_private_ips=False)


def test_ssrf_blocks_metadata_ip():
    with pytest.raises(SSRFSecurityError, match="prohibited host|prohibited IP"):
        validate_destination_sync("http://169.254.169.254/latest/meta-data/", allow_private_ips=False)


def test_ssrf_blocks_private_subnet_ip():
    with pytest.raises(SSRFSecurityError, match="prohibited IP"):
        validate_destination_sync("http://10.0.0.1/internal", allow_private_ips=False)
    with pytest.raises(SSRFSecurityError, match="prohibited IP"):
        validate_destination_sync("http://192.168.1.1/router", allow_private_ips=False)


def test_ssrf_rejects_unsupported_schemes():
    with pytest.raises(SSRFSecurityError, match="Unsupported scheme"):
        validate_destination_sync("ftp://example.com/file", allow_private_ips=False)
    with pytest.raises(SSRFSecurityError, match="Unsupported scheme"):
        validate_destination_sync("file:///etc/passwd", allow_private_ips=False)


def test_ssrf_blocks_cgnat_shared_address_space():
    with pytest.raises(SSRFSecurityError, match="prohibited IP"):
        validate_destination_sync("http://100.64.0.1/internal", allow_private_ips=False)


def test_ssrf_fails_safely_on_dns_failure(monkeypatch):
    import socket

    def mock_getaddrinfo(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    with pytest.raises(SSRFSecurityError, match="DNS resolution failed or timed out"):
        validate_destination_sync("http://unresolvable-domain-xyz999.org/", allow_private_ips=False)


def test_ssrf_allows_private_ip_when_configured():
    # When explicitly allowed (for local testing/controlled mock server)
    validate_destination_sync("http://127.0.0.1:8080/test", allow_private_ips=True)
    validate_destination_sync("http://localhost:8080/test", allow_private_ips=True)
