"""Whole-database backup and restore as one gzipped JSON file.

Works the same on SQLite and Postgres (it reads and writes through SQLAlchemy Core), so a
backup from production restores into a laptop SQLite file and vice versa. Everything the
team creates lives only in this database: replies, notes, events, templates, users, API
key hashes, recipients and the employee roster. Reviews could be re-pulled from Google,
but who replied, when, and why could not.

    python cli.py backup                       # ./backups/review-manager-YYYYMMDD-HHMM.json.gz
    python cli.py restore backups/<file> --yes # into an EMPTY database only
"""
from __future__ import annotations

import gzip
import json
from datetime import date, datetime
from pathlib import Path
from typing import Dict

from sqlalchemy import DateTime, Date, func, select

from .db import engine, init_db
from .models import Base

FORMAT = 1


def _enc(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def dump(path: Path) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    data = {"format": FORMAT, "created_at": datetime.utcnow().isoformat(), "tables": {}}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            rows = [{k: _enc(v) for k, v in r._mapping.items()} for r in conn.execute(select(table))]
            data["tables"][table.name] = rows
            counts[table.name] = len(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    return counts


def restore(path: Path) -> Dict[str, int]:
    """Load a backup into an empty database. Refuses if any table already has rows."""
    init_db(seed=False)          # the backup brings its own tag lists
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("format") != FORMAT:
        raise ValueError(f"unsupported backup format {data.get('format')!r}")
    counts: Dict[str, int] = {}
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if conn.execute(select(func.count()).select_from(table)).scalar():
                raise ValueError(f"table {table.name} is not empty; restore only into a fresh database")
        for table in Base.metadata.sorted_tables:       # parents before children
            rows = data["tables"].get(table.name, [])
            cols = {c.name: c for c in table.columns}
            clean = []
            for r in rows:
                row = {}
                for k, v in r.items():
                    col = cols.get(k)
                    if col is None:
                        continue                         # a column this version no longer has
                    if v is not None and isinstance(col.type, DateTime):
                        v = datetime.fromisoformat(v)
                    elif v is not None and isinstance(col.type, Date):
                        v = date.fromisoformat(v)
                    row[k] = v
                clean.append(row)
            if clean:
                conn.execute(table.insert(), clean)
            counts[table.name] = len(clean)
        if engine.dialect.name == "postgresql":         # move id sequences past the restored rows
            for table in Base.metadata.sorted_tables:
                pk = [c for c in table.primary_key.columns]
                if len(pk) == 1 and pk[0].autoincrement and counts.get(table.name):
                    conn.exec_driver_sql(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                        f"(SELECT COALESCE(MAX({pk[0].name}), 1) FROM {table.name}))")
    return counts
