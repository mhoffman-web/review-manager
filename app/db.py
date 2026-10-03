"""SQLAlchemy engine and session factory."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)

from .config import settings

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(settings.database_url, future=True, connect_args=connect_args)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables and add any columns the models gained since the
    database was created. Safe to run repeatedly. Additive only: it never
    drops or renames, so a proper migration tool can take over later."""
    from . import models  # noqa: F401  (registers models on Base)
    models.Base.metadata.create_all(engine)
    _add_missing_columns(models.Base)
    _backfill()


def _backfill() -> None:
    """Idempotent data fixes that go with schema additions."""
    with engine.begin() as conn:
        # first_replied_at was added after replies existed; the platform reply time is the best estimate.
        conn.execute(text("UPDATE reviews SET first_replied_at = owner_reply_updated_at "
                          "WHERE first_replied_at IS NULL AND has_owner_reply = 1 AND owner_reply_updated_at IS NOT NULL"))


def _add_missing_columns(base) -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}'
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    lit = "1" if val is True else "0" if val is False else repr(val) if isinstance(val, str) else str(val)
                    ddl += f" DEFAULT {lit}"
                conn.execute(text(ddl))
                log.info("schema: added %s.%s", table.name, col.name)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
