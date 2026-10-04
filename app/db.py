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
    # The web app and the worker are two processes writing one file: wait for the other's
    # write lock instead of failing at once with "database is locked".
    connect_args["check_same_thread"] = False
    connect_args["timeout"] = 30

engine = create_engine(settings.database_url, future=True, connect_args=connect_args)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA busy_timeout=30000")
        if ":memory:" not in settings.database_url:
            # WAL: readers never wait for the writer, and the writer never waits for readers.
            dbapi_conn.execute("PRAGMA journal_mode=WAL")

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables and add any columns the models gained since the
    database was created. Safe to run repeatedly. Additive only: it never
    drops or renames, so a proper migration tool can take over later."""
    from . import models  # noqa: F401  (registers models on Base)
    models.Base.metadata.create_all(engine)
    _add_missing_columns(models.Base)
    _add_missing_indexes(models.Base)
    _backfill()


def _add_missing_indexes(base) -> None:
    """create_all only builds indexes for tables it creates; add new ones to existing tables."""
    insp = inspect(engine)
    for table in base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {ix["name"] for ix in insp.get_indexes(table.name)}
        for ix in table.indexes:
            if ix.name not in have:
                ix.create(engine)
                log.info("schema: added index %s", ix.name)


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
    _backfill_recipient_editions()


def _backfill_recipient_editions() -> None:
    """Recipient rows from before editions existed got edition 'all' (Corporate) by default;
    their old `brands` column says which brands they actually wanted. New-style rows always
    have brands empty when the edition is 'all', so this only ever touches legacy rows."""
    from sqlalchemy.orm import Session as _S
    from .models import ReportRecipient
    with _S(engine) as s:
        changed = 0
        for r in s.query(ReportRecipient).filter(ReportRecipient.edition == "all", ReportRecipient.brands.isnot(None)).all():
            brands = {b.strip().upper() for b in r.brands.split(";") if b.strip()}
            if not brands:
                continue
            il, tn = "WASHU" in brands, bool(brands & {"ICON", "WA"})
            r.edition = "all" if (il and tn) else "il" if il else "tn" if tn else "all"
            r.brands = None
            changed += 1
        if changed:
            s.commit()
            log.info("recipients: moved %d legacy row(s) from brands to editions", changed)


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
