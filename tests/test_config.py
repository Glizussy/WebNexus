"""Unit tests for configuration validation and environment loading."""

import pytest
from crawler.config import CrawlerConfig


def test_default_config_valid():
    cfg = CrawlerConfig()
    assert cfg.max_concurrency == 10
    assert cfg.per_host_delay_sec == 1.0
    assert "@" in cfg.contact_email
    assert cfg.allow_private_ips is False


def test_config_invalid_concurrency():
    with pytest.raises(ValueError, match="max_concurrency must be positive"):
        CrawlerConfig(max_concurrency=0)


def test_config_invalid_email():
    with pytest.raises(ValueError, match="contact_email"):
        CrawlerConfig(contact_email="invalid-email")


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("CRAWLER_MAX_CONCURRENCY", "25")
    monkeypatch.setenv("CRAWLER_PER_HOST_DELAY_SEC", "2.5")
    monkeypatch.setenv("CRAWLER_ALLOW_PRIVATE_IPS", "true")
    cfg = CrawlerConfig.from_env()
    assert cfg.max_concurrency == 25
    assert cfg.per_host_delay_sec == 2.5
    assert cfg.allow_private_ips is True
