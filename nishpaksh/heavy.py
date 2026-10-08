"""Heavy article columns (text, embedding, minhash) kept in a local cache between runs (Oct 8 2026).

Supabase's free plan allows 5 GB of egress a month. Each hourly run re-read the full text, the
embedding and the minhash of every recent article (about 5,500 rows, ~8 KB each, in several steps),
which was ~85% of the 12.5 GB the project used in its first week. These columns rarely change once
written, so a query asks the database for a 32-character md5 of each heavy column instead of the
column itself, and the value comes from the local cache when the cache holds that exact md5; only
new or changed values are fetched. Results are identical by construction.

The cache is one SQLite file (path in NISHPAKSH_HEAVY_CACHE), kept between GitHub Actions runs by
actions/cache (hourly.yml). Without the variable, or on a database other than Postgres (tests), the
columns are selected directly as before.

    cols = heavy.columns(store, "text", "embedding")      # use in select(...) in place of the columns
    rows = heavy.fill(store, store.rows(select(articles.c.id, *cols, ...)), "text", "embedding")
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import time

from sqlalchemy import Text, cast, func

from .db import Store, articles, select

log = logging.getLogger(__name__)

COLS = {"text": articles.c.text, "embedding": articles.c.embedding, "minhash": articles.c.minhash}
KEEP_DAYS = 6          # entries not used for this long are pruned (the pipeline looks back 3 days)
CHUNK = 400
_conn: sqlite3.Connection | None = None
stats = {"hit": 0, "fetched": 0}


def _path() -> str:
    return os.environ.get("NISHPAKSH_HEAVY_CACHE", "")


def active(store: Store) -> bool:
    return bool(_path()) and store.engine.dialect.name == "postgresql"


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        p = pathlib.Path(_path())
        p.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(p))
        _conn.execute("create table if not exists heavy (col text, id integer, md5 text, val text, used real, "
                      "primary key (col, id))")
        _conn.execute("delete from heavy where used < ?", (time.time() - KEEP_DAYS * 86400,))
        _conn.commit()
    return _conn


def _digest(name: str):
    return func.md5(cast(COLS[name], Text)).label(f"{name}__md5")


def columns(store: Store, *names: str) -> list:
    """The select columns for these heavy columns: their md5 when the cache is active, else themselves."""
    return [_digest(n) if active(store) else COLS[n] for n in names]


def fill(store: Store, rows: list[dict], *names: str) -> list[dict]:
    """Put the heavy values back into rows selected with `columns`: from the cache when its md5 matches,
    else fetched from the database (and cached)."""
    if not active(store) or not rows:
        return rows
    db = _db()
    now = time.time()
    for name in names:
        key = f"{name}__md5"
        want = {r["id"]: r.pop(key) for r in rows}
        have: dict[int, object] = {}
        ids = [i for i, m in want.items() if m is not None]
        for start in range(0, len(ids), 900):
            part = ids[start:start + 900]
            q = f"select id, md5, val from heavy where col = ? and id in ({','.join('?' * len(part))})"
            for i, m, val in db.execute(q, [name, *part]):
                if want.get(i) == m:
                    have[i] = json.loads(val)
        missing = [i for i in ids if i not in have]
        for start in range(0, len(missing), CHUNK):
            part = missing[start:start + CHUNK]
            for r in store.rows(select(articles.c.id, _digest(name), COLS[name]).where(articles.c.id.in_(part))):
                have[r["id"]] = r[name]
                db.execute("insert or replace into heavy values (?, ?, ?, ?, ?)",
                           (name, r["id"], r[key], json.dumps(r[name], ensure_ascii=False), now))
        hit = [i for i in ids if i not in missing]
        for start in range(0, len(hit), 900):
            part = hit[start:start + 900]
            db.execute(f"update heavy set used = ? where col = ? and id in ({','.join('?' * len(part))})", [now, name, *part])
        db.commit()
        stats["hit"] += len(ids) - len(missing)
        stats["fetched"] += len(missing)
        for r in rows:
            r[name] = have.get(r["id"]) if want.get(r["id"]) is not None else None
    return rows
