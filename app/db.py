"""SQLAlchemy engine and session factory."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import logging

from sqlalchemy import create_engine, event, inspect, text, update
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
    """Idempotent data fixes that go with schema additions. Written with Core
    expressions so each dialect renders its own literals (Postgres booleans
    are true/false, not 1/0)."""
    from .models import Review
    with engine.begin() as conn:
        # first_replied_at was added after replies existed; the platform reply time is the best estimate.
        conn.execute(
            update(Review)
            .where(Review.first_replied_at.is_(None), Review.has_owner_reply.is_(True),
                   Review.owner_reply_updated_at.isnot(None))
            .values(first_replied_at=Review.owner_reply_updated_at))


def column_ddl(table, col, dialect) -> str:
    """ALTER TABLE statement that adds `col` the way `dialect` expects it.

    Scalar defaults are rendered through the dialect's literal processor, so a
    Boolean default becomes `true`/`false` on Postgres and `1`/`0` on SQLite.
    A NOT NULL column gets NOT NULL only when a scalar default exists to fill
    existing rows; a non-null column whose default is a Python callable (e.g.
    utcnow) is added nullable and logged, because the rows that already exist
    have no value for it and SQLite cannot tighten a column afterwards."""
    ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect)}"
    default = col.default
    if default is not None and getattr(default, "is_scalar", False):
        val = default.arg
        proc = col.type.literal_processor(dialect)
        if proc is not None:
            lit = proc(val)
        elif isinstance(val, bool):
            lit = "true" if val else "false"
        elif isinstance(val, str):
            lit = "'" + val.replace("'", "''") + "'"
        else:
            lit = str(val)
        ddl += f" DEFAULT {lit}"
        if not col.nullable:
            ddl += " NOT NULL"
    elif not col.nullable and not col.primary_key:
        log.warning("schema: %s.%s is NOT NULL in the model but has no scalar default; "
                    "adding it nullable so existing rows keep loading", table.name, col.name)
    return ddl


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
                conn.execute(text(column_ddl(table, col, engine.dialect)))
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
