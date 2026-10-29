"""Configuration management and validation for the acquisition module."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CrawlerConfig:
    """Acquisition module configuration with strict validation and safe defaults."""

    # Identity (RFC 9309 recommendation: identify crawler and contact)
    user_agent: str = (
        "AcademicCrawler/1.0 (+https://academic-research.org/bot; crawler-contact@academic-research.org)"
    )
    contact_email: str = "crawler-contact@academic-research.org"

    # Concurrency and Rate Limiting
    max_concurrency: int = 10
    max_connections_per_host: int = 2
    per_host_delay_sec: float = 1.0
    respect_crawl_delay: bool = True
    max_crawl_delay_sec: float = 30.0

    # HTTP Timeouts and Retries
    connect_timeout_sec: float = 5.0
    request_timeout_sec: float = 15.0
    max_retries: int = 3
    backoff_factor: float = 1.5
    backoff_jitter: bool = True
    max_backoff_sec: float = 30.0
    max_retry_after_sec: float = 60.0

    # Safe Data and Stream Limits
    max_response_size_bytes: int = 10 * 1024 * 1024  # 10 MB limit to avoid decompression bombs
    max_redirects: int = 5
    allowed_schemes: tuple[str, ...] = ("http", "https")
    allowed_content_types: tuple[str, ...] = (
        "text/html",
        "application/xhtml+xml",
        "text/xml",
        "application/xml",
        "application/rss+xml",
        "application/atom+xml",
        "text/plain",
    )

    # Security & SSRF Protection
    allow_private_ips: bool = False  # MUST be False in production; only True for local mock tests
    blocked_hostnames: tuple[str, ...] = (
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "metadata.google.internal",
        "169.254.169.254",  # AWS/GCP/Azure link-local metadata service
    )

    # Robots.txt Policies
    respect_robots_txt: bool = True
    robots_cache_ttl_sec: float = 86400.0  # 24 hours
    robots_timeout_sec: float = 5.0
    max_robots_size_bytes: int = 512 * 1024  # 512 KB response limit for robots.txt

    # Scope and Limits
    allowed_domains: set[str] = field(default_factory=set)
    max_depth: int = 5
    max_pages: int = 1000
    default_recrawl_interval_sec: float = 86400.0  # 24 hours for incremental recrawl

    # Prioritization
    keyword_weights: dict[str, float] = field(
        default_factory=lambda: {
            "research": 2.0,
            "publication": 2.0,
            "article": 1.5,
            "news": 1.0,
            "report": 1.5,
            "paper": 2.0,
        }
    )
    source_weights: dict[str, float] = field(default_factory=dict)

    # Persistent Storage (SQLite default)
    state_db_path: str = "crawl_state.db"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate all parameters to prevent misconfiguration or dangerous settings."""
        if not self.user_agent or not self.user_agent.strip():
            raise ValueError("user_agent must not be empty")
        if "@" not in self.contact_email:
            raise ValueError("contact_email must contain a valid email address")
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if self.max_connections_per_host <= 0:
            raise ValueError("max_connections_per_host must be positive")
        if self.per_host_delay_sec < 0:
            raise ValueError("per_host_delay_sec cannot be negative")
        if self.connect_timeout_sec <= 0:
            raise ValueError("connect_timeout_sec must be positive")
        if self.request_timeout_sec <= 0:
            raise ValueError("request_timeout_sec must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.max_response_size_bytes <= 0:
            raise ValueError("max_response_size_bytes must be positive")
        if self.max_robots_size_bytes <= 0:
            raise ValueError("max_robots_size_bytes must be positive")
        if self.max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if self.max_depth < 0:
            raise ValueError("max_depth cannot be negative")
        if self.max_pages <= 0:
            raise ValueError("max_pages must be positive")

    @classmethod
    def from_env(cls, **overrides: Any) -> CrawlerConfig:
        """Create configuration loading environment variables when set."""
        env = os.environ
        data: dict[str, Any] = {}

        if "CRAWLER_USER_AGENT" in env:
            data["user_agent"] = env["CRAWLER_USER_AGENT"]
        if "CRAWLER_CONTACT_EMAIL" in env:
            data["contact_email"] = env["CRAWLER_CONTACT_EMAIL"]
        if "CRAWLER_MAX_CONCURRENCY" in env:
            data["max_concurrency"] = int(env["CRAWLER_MAX_CONCURRENCY"])
        if "CRAWLER_PER_HOST_DELAY_SEC" in env:
            data["per_host_delay_sec"] = float(env["CRAWLER_PER_HOST_DELAY_SEC"])
        if "CRAWLER_REQUEST_TIMEOUT_SEC" in env:
            data["request_timeout_sec"] = float(env["CRAWLER_REQUEST_TIMEOUT_SEC"])
        if "CRAWLER_CONNECT_TIMEOUT_SEC" in env:
            data["connect_timeout_sec"] = float(env["CRAWLER_CONNECT_TIMEOUT_SEC"])
        if "CRAWLER_MAX_RETRIES" in env:
            data["max_retries"] = int(env["CRAWLER_MAX_RETRIES"])
        if "CRAWLER_BACKOFF_FACTOR" in env:
            data["backoff_factor"] = float(env["CRAWLER_BACKOFF_FACTOR"])
        if "CRAWLER_MAX_RESPONSE_SIZE_BYTES" in env:
            data["max_response_size_bytes"] = int(env["CRAWLER_MAX_RESPONSE_SIZE_BYTES"])
        if "CRAWLER_MAX_ROBOTS_SIZE_BYTES" in env:
            data["max_robots_size_bytes"] = int(env["CRAWLER_MAX_ROBOTS_SIZE_BYTES"])
        if "CRAWLER_MAX_REDIRECTS" in env:
            data["max_redirects"] = int(env["CRAWLER_MAX_REDIRECTS"])
        if "CRAWLER_STATE_DB_PATH" in env:
            data["state_db_path"] = env["CRAWLER_STATE_DB_PATH"]
        if "CRAWLER_ALLOW_PRIVATE_IPS" in env:
            data["allow_private_ips"] = env["CRAWLER_ALLOW_PRIVATE_IPS"].strip().lower() in ("true", "1", "yes")

        data.update(overrides)
        return cls(**data)
