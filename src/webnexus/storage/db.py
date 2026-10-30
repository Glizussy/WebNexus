"""Engine/session helpers. Kept free of global state so tests can inject a URL."""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "postgresql+psycopg://webnexus:webnexus@localhost:5433/webnexus"


def database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)


def create_db_engine(url: str | None = None, **kwargs: object) -> Engine:
    return create_engine(
        url or database_url(),
        pool_pre_ping=True,
        future=True,
        **kwargs,
    )


def create_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine or create_db_engine(),
        class_=Session,
        expire_on_commit=False,
    )
