"""RFC 9309 compliant robots.txt parser, evaluator, and caching manager."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlsplit

from crawler.config import CrawlerConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rule:
    """Represents a single Allow or Disallow rule."""
    path_pattern: str
    is_allow: bool
    regex: re.Pattern[str]
    pattern_len: int


def pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert an RFC 9309 pattern (with '*' wildcards and optional '$' end anchor) to regex."""
    if not pattern:
        # Empty pattern matches nothing or empty string
        return re.compile(r"^$")

    escaped = ""
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            escaped += ".*"
        elif char == "$":
            if i == n - 1:
                escaped += "$"
            else:
                escaped += re.escape(char)
        else:
            escaped += re.escape(char)
        i += 1

    # Prefix matching: if pattern doesn't end with $, it matches any prefix
    if not pattern.endswith("$"):
        escaped += ".*"

    return re.compile(f"^{escaped}")


@dataclass
class ParsedGroup:
    """A collection of rules applying to one or more user-agent tokens."""
    user_agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


class RobotsFile:
    """Parsed in-memory representation of an RFC 9309 robots.txt file."""

    def __init__(self, content: str, default_user_agent: str = "") -> None:
        self.raw_content = content
        self.default_user_agent = default_user_agent
        self.groups: list[ParsedGroup] = []
        self.sitemaps: list[str] = []
        self._parse(content)

    def _parse(self, content: str) -> None:
        """Parse robots.txt content according to RFC 9309 line-by-line rules."""
        lines = content.splitlines()
        current_group: ParsedGroup | None = None
        has_rules_in_group = False

        for raw_line in lines:
            # Strip comments
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue

            if ":" not in line:
                continue

            field, val = line.split(":", 1)
            field = field.strip().lower()
            val = val.strip()

            if field == "user-agent":
                ua = val.lower()
                # If we were already building rules for a previous group, start a new group
                if current_group is not None and has_rules_in_group:
                    self.groups.append(current_group)
                    current_group = ParsedGroup()
                    has_rules_in_group = False
                elif current_group is None:
                    current_group = ParsedGroup()

                current_group.user_agents.append(ua)

            elif field in ("allow", "disallow"):
                if current_group is None:
                    # RFC 9309: Rules before any user-agent directive are ignored
                    continue

                is_allow = (field == "allow")
                # RFC 9309: Empty Disallow means allow all
                if not val and not is_allow:
                    continue

                pattern = val
                # Ensure leading slash for relative path matching
                if pattern and not pattern.startswith("/") and not pattern.startswith("*"):
                    pattern = "/" + pattern

                reg = pattern_to_regex(pattern)
                current_group.rules.append(
                    Rule(
                        path_pattern=pattern,
                        is_allow=is_allow,
                        regex=reg,
                        pattern_len=len(pattern),
                    )
                )
                has_rules_in_group = True

            elif field == "crawl-delay":
                if current_group is not None:
                    try:
                        current_group.crawl_delay = max(0.0, float(val))
                    except ValueError:
                        pass

            elif field == "sitemap":
                if val:
                    self.sitemaps.append(val)

        if current_group is not None and (current_group.user_agents or current_group.rules):
            self.groups.append(current_group)

    def _find_matching_group(self, user_agent: str) -> ParsedGroup | None:
        """Find the most specific group matching user_agent token, falling back to '*'."""
        ua_token = user_agent.split("/")[0].strip().lower()
        ua_token_full = user_agent.lower()

        # 1. Exact or prefix match on specific user-agent tokens
        best_group: ParsedGroup | None = None
        best_match_len = 0

        for group in self.groups:
            for agent in group.user_agents:
                if agent == "*":
                    continue
                if agent in ua_token_full or ua_token in agent:
                    if len(agent) > best_match_len:
                        best_match_len = len(agent)
                        best_group = group

        if best_group is not None:
            return best_group

        # 2. Wildcard group
        for group in self.groups:
            if "*" in group.user_agents:
                return group

        return None

    def is_allowed(self, path: str, user_agent: str) -> bool:
        """Evaluate if path is allowed for user_agent according to RFC 9309 precedence."""
        if not path:
            path = "/"
        elif not path.startswith("/"):
            path = "/" + path

        group = self._find_matching_group(user_agent)
        if group is None or not group.rules:
            # If no group matches or group has no rules, access is allowed
            return True

        # Find all matching rules
        matching_rules: list[Rule] = []
        for rule in group.rules:
            if rule.regex.match(path):
                matching_rules.append(rule)

        if not matching_rules:
            return True

        # Sort by pattern length descending
        # If lengths match: RFC 9309 states Allow wins over Disallow
        def rule_sort_key(r: Rule) -> tuple[int, int]:
            # Length descending, then allow (1) over disallow (0)
            return (r.pattern_len, 1 if r.is_allow else 0)

        best_rule = max(matching_rules, key=rule_sort_key)
        return best_rule.is_allow

    def get_crawl_delay(self, user_agent: str) -> float | None:
        """Retrieve configured crawl-delay for the matching group, if any."""
        group = self._find_matching_group(user_agent)
        if group:
            return group.crawl_delay
        return None


@dataclass
class CacheEntry:
    robots_file: RobotsFile | None
    fetched_at: float
    status_code: int | None
    is_temporary_failure: bool = False


class RobotsManager:
    """Manages downloading, caching, expiry, and querying of robots.txt across origins."""

    def __init__(self, config: CrawlerConfig) -> None:
        self.config = config
        self._cache: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    def _get_origin(self, url: str) -> str:
        """Extract scheme://host[:port] origin from a URL."""
        split = urlsplit(url)
        return f"{split.scheme.lower()}://{split.netloc.lower()}"

    def is_cached(self, url: str) -> bool:
        """Check whether valid cached robots policy exists for origin."""
        origin = self._get_origin(url)
        entry = self._cache.get(origin)
        if entry is None:
            return False
        return (time.time() - entry.fetched_at) < self.config.robots_cache_ttl_sec

    def set_robots_content(self, origin: str, content: str, status_code: int = 200) -> None:
        """Manually populate cache (useful for testing or preloading)."""
        origin = origin.rstrip("/").lower()
        rf = RobotsFile(content, default_user_agent=self.config.user_agent)
        self._cache[origin] = CacheEntry(
            robots_file=rf,
            fetched_at=time.time(),
            status_code=status_code,
            is_temporary_failure=False,
        )

    def set_error_status(self, origin: str, status_code: int) -> None:
        """Record error outcome for origin.

        RFC 9309 §2.3.1.4: 404/410 means robots.txt doesn't exist -> allowed (is_temp=False).
        413 (oversized payload), 429 (rate limited), and 5xx / 599 (server/network errors)
        are temporary failures -> disallows crawling (is_temp=True).
        """
        origin = origin.rstrip("/").lower()
        is_temp = (status_code in (413, 429)) or (500 <= status_code <= 599)
        self._cache[origin] = CacheEntry(
            robots_file=None,
            fetched_at=time.time(),
            status_code=status_code,
            is_temporary_failure=is_temp,
        )

    async def can_fetch(self, url: str, fetcher_callable: Any = None) -> bool:
        """Evaluate if URL can be fetched by the configured User-Agent.

        If not cached, downloads robots.txt using fetcher_callable.
        """
        if not self.config.respect_robots_txt:
            return True

        origin = self._get_origin(url)
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        async with self._lock:
            entry = self._cache.get(origin)
            now = time.time()
            if entry and (now - entry.fetched_at) < self.config.robots_cache_ttl_sec:
                return self._evaluate_entry(entry, path)

        # Needs fetch
        if fetcher_callable is None:
            # If no fetcher provided and not cached, assume allowed by default
            return True

        robots_url = f"{origin}/robots.txt"
        try:
            status_code, content = await fetcher_callable(robots_url)
            async with self._lock:
                if status_code == 200 and content:
                    self.set_robots_content(origin, content, status_code=200)
                elif 400 <= status_code <= 499:
                    # RFC 9309: 4xx client errors mean robots.txt does not exist -> allow all
                    self.set_error_status(origin, status_code)
                elif 500 <= status_code <= 599:
                    # RFC 9309: 5xx server errors mean temporary failure -> disallow
                    self.set_error_status(origin, status_code)
                else:
                    self.set_error_status(origin, status_code)

                entry = self._cache.get(origin)
                if entry:
                    return self._evaluate_entry(entry, path)
                return True
        except Exception as e:
            logger.warning(f"Error fetching robots.txt from {robots_url}: {e}")
            async with self._lock:
                # Network or unreachable failure: treat as temporary failure
                self._cache[origin] = CacheEntry(
                    robots_file=None,
                    fetched_at=time.time(),
                    status_code=None,
                    is_temporary_failure=True,
                )
                entry = self._cache[origin]
                return self._evaluate_entry(entry, path)

    def _evaluate_entry(self, entry: CacheEntry, path: str) -> bool:
        """Helper to evaluate cached robots entry."""
        if entry.is_temporary_failure:
            # Disallowed while site is returning 5xx or network errors
            return False
        if entry.robots_file is None:
            # 4xx or empty: allowed
            return True
        return entry.robots_file.is_allowed(path, self.config.user_agent)

    def get_crawl_delay(self, url: str) -> float | None:
        """Retrieve crawl delay for origin if cached and configured."""
        origin = self._get_origin(url)
        entry = self._cache.get(origin)
        if entry and entry.robots_file:
            return entry.robots_file.get_crawl_delay(self.config.user_agent)
        return None

    def get_sitemaps(self, url: str) -> list[str]:
        """Retrieve discovered Sitemap: URLs from robots.txt."""
        origin = self._get_origin(url)
        entry = self._cache.get(origin)
        if entry and entry.robots_file:
            return list(entry.robots_file.sitemaps)
        return []
