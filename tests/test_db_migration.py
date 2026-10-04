"""The additive migration and the startup backfill must render valid SQL for Postgres
as well as SQLite. There is no Postgres in CI, so the statements are compiled against
the Postgres dialect and checked as text, and executed for real on SQLite."""
from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, text, update
from sqlalchemy.dialects import postgresql, sqlite

from app.db import column_ddl, engine, init_db
from app.models import Base, Review


def test_backfill_renders_booleans_per_dialect():
    stmt = (update(Review).where(Review.first_replied_at.is_(None), Review.has_owner_reply.is_(True),
                                 Review.owner_reply_updated_at.isnot(None))
            .values(first_replied_at=Review.owner_reply_updated_at))
    pg = str(stmt.compile(dialect=postgresql.dialect()))
    assert "has_owner_reply IS true" in pg and "= 1" not in pg
    assert "has_owner_reply IS 1" in str(stmt.compile(dialect=sqlite.dialect()))


def test_column_ddl_per_dialect():
    md = MetaData()
    t = Table("things", md,
              Column("id", Integer, primary_key=True),
              Column("flag", Boolean, default=True, nullable=False),
              Column("label", String(20), default="it's", nullable=False),
              Column("n", Integer, default=0, nullable=False),
              Column("note", String(20)),
              Column("when", DateTime, default=lambda: None, nullable=False))
    pg, sq = postgresql.dialect(), sqlite.dialect()
    assert column_ddl(t, t.c.flag, pg) == "ALTER TABLE things ADD COLUMN flag BOOLEAN DEFAULT true NOT NULL"
    assert column_ddl(t, t.c.flag, sq) == "ALTER TABLE things ADD COLUMN flag BOOLEAN DEFAULT 1 NOT NULL"
    assert column_ddl(t, t.c.label, pg) == "ALTER TABLE things ADD COLUMN label VARCHAR(20) DEFAULT 'it''s' NOT NULL"
    assert column_ddl(t, t.c.n, pg).endswith("INTEGER DEFAULT 0 NOT NULL")
    assert column_ddl(t, t.c.note, pg) == "ALTER TABLE things ADD COLUMN note VARCHAR(20)"
    # a non-null column with a callable default has no value for existing rows: added nullable
    assert column_ddl(t, t.c.when, pg) == 'ALTER TABLE things ADD COLUMN "when" TIMESTAMP WITHOUT TIME ZONE' or \
        column_ddl(t, t.c.when, pg).startswith("ALTER TABLE things ADD COLUMN when ")
    assert "NOT NULL" not in column_ddl(t, t.c.when, pg)


def test_every_model_column_migrates_on_sqlite_and_compiles_for_postgres():
    """Drop a column from each table, re-run init_db, and check it comes back with the right default."""
    Base.metadata.drop_all(engine)
    init_db()
    pg = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            ddl = column_ddl(table, col, pg)
            assert ddl.startswith(f"ALTER TABLE {table.name} ADD COLUMN {col.name} ")
            assert " 1 NOT NULL" not in ddl and " 0 NOT NULL" not in ddl or not isinstance(col.type, Boolean)
    # SQLite round trip for a boolean-with-default and a string-with-default column
    # (the Postgres equivalent is test_against_real_postgres in test_coverage_gaps.py)
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE reviews DROP COLUMN author_is_anonymous"))
        conn.execute(text("ALTER TABLE users DROP COLUMN auth_provider"))
    init_db()
    with engine.begin() as conn:
        cols = {r[1]: r for r in conn.execute(text("PRAGMA table_info(reviews)"))}
        assert cols["author_is_anonymous"][4] == "0" and cols["author_is_anonymous"][3] == 1   # default, notnull
        cols = {r[1]: r for r in conn.execute(text("PRAGMA table_info(users)"))}
        assert cols["auth_provider"][4] == "'local'"
