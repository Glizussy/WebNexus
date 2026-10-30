from __future__ import annotations

import gzip
from datetime import UTC, datetime

from webnexus.pipeline import LocalRawStore


def test_local_raw_store_writes_compressed_body(tmp_path) -> None:
    store = LocalRawStore(tmp_path)
    body = b"<html>hello</html>"
    when = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)

    ref = store.save(crawled_at=when, content_hash="a" * 64, body=body)

    assert ref == f"2026/10/28/{'a' * 64}.html.gz"
    path = tmp_path / ref
    assert path.exists()
    assert gzip.decompress(path.read_bytes()) == body


def test_local_raw_store_is_idempotent(tmp_path) -> None:
    store = LocalRawStore(tmp_path)
    when = datetime(2026, 1, 2, tzinfo=UTC)
    first = store.save(crawled_at=when, content_hash="b" * 64, body=b"one")
    second = store.save(crawled_at=when, content_hash="b" * 64, body=b"two")
    assert first == second
    assert gzip.decompress((tmp_path / first).read_bytes()) == b"one"


def test_local_raw_store_can_skip_compression(tmp_path) -> None:
    store = LocalRawStore(tmp_path, compress=False)
    ref = store.save(
        crawled_at=datetime(2026, 1, 2, tzinfo=UTC),
        content_hash="c" * 64,
        body=b"plain",
    )
    assert ref.endswith(".html")
    assert (tmp_path / ref).read_bytes() == b"plain"
