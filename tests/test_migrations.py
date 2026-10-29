"""End-to-end migration check. Requires a disposable PostgreSQL database.

Run with:

    $env:WEBNEXUS_TEST_DATABASE_URL = "postgresql+psycopg://webnexus:webnexus@localhost:5433/webnexus_test"
    pytest tests/test_migrations.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TEST_URL = os.getenv("WEBNEXUS_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="set WEBNEXUS_TEST_DATABASE_URL to run migration tests"
)


def _alembic(*args: str) -> None:
    env = {**os.environ, "DATABASE_URL": TEST_URL or ""}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_upgrade_downgrade_upgrade() -> None:
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
