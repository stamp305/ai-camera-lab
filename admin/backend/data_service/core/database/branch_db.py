# data_service/core/database/branch_db.py
"""SQLAlchemy engine/session management for the branches table.

Uses SQLite by default so the feature can be tested locally without a
running MySQL server. Set the AI_CAMERA_ADMIN_BRANCH_DB_URL environment
variable to switch to MySQL (e.g. mysql+pymysql://user:pass@host:3306/ai_camera).
"""
from __future__ import annotations

import sqlite3
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from config import BRANCH_DB_URL

Base = declarative_base()

_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def get_engine() -> Engine:
    """Create (once) and return the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = create_engine(BRANCH_DB_URL, future=True)
        if _engine.dialect.name == "sqlite":
            event.listen(_engine, "connect", _use_wal)
    return _engine


def _use_wal(dbapi_conn, _record) -> None:
    """WAL: reads never wait on a write, and a commit costs one append instead of
    rewriting the journal, so every Branch identifying at once drains faster.
    Writes still go one at a time. The mode is stored in the file (adds
    branches.db-wal and -shm beside it); PRAGMA journal_mode=DELETE undoes it."""
    try:
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as e:
        # The first switch needs the file to itself; another holder only delays it.
        print(f"[branch_db] journal_mode=WAL not set yet: {e}")


def get_session_factory() -> sessionmaker:
    """Create (once) and return the session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), future=True, expire_on_commit=False)
    return _session_factory


def get_session() -> Iterator[Session]:
    """Context-manager style session dependency for FastAPI/SQLAlchemy."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def init_branches_db() -> None:
    """Create the branches table(s) if they do not exist yet."""
    import data_service.core.models.branch  # noqa: F401  (register model on Base)
    engine = get_engine()
    Base.metadata.create_all(bind=engine)

    # Lightweight migration: add columns that were introduced later.
    _add_column_if_missing(engine, "branches", "serial_number", "VARCHAR(64)")
    _add_column_if_missing(engine, "branches", "mac_address", "VARCHAR(32)")
    # Matches deploy/migrations/20241201_add_role_column.sql for databases where
    # that migration has not been run (a fresh SQLite file, for instance).
    _add_column_if_missing(engine, "branches", "role", "VARCHAR(20) NOT NULL DEFAULT 'viewer'")


def _add_column_if_missing(engine: Engine, table: str, column: str, col_type: str) -> None:
    """Add a nullable column to a table when it does not exist yet."""
    from sqlalchemy import inspect, text

    if column in {c["name"] for c in inspect(engine).get_columns(table)}:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type} NULL"))
