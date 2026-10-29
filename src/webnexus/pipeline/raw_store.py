"""Optional raw-body storage so parsed documents keep a recoverable source."""

from __future__ import annotations

import gzip
from datetime import datetime
from pathlib import Path
from typing import Protocol


class RawStore(Protocol):
    def save(self, *, crawled_at: datetime, content_hash: str, body: bytes) -> str: ...


class LocalRawStore:
    def __init__(self, base_dir: str | Path, *, compress: bool = True) -> None:
        self._base = Path(base_dir)
        self._compress = compress

    def save(self, *, crawled_at: datetime, content_hash: str, body: bytes) -> str:
        suffix = ".html.gz" if self._compress else ".html"
        relative = Path(crawled_at.strftime("%Y/%m/%d")) / f"{content_hash}{suffix}"
        target = self._base / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if self._compress:
                with gzip.open(target, "wb") as handle:
                    handle.write(body)
            else:
                target.write_bytes(body)
        return relative.as_posix()
