"""Copy the live articles from the newsroom database (the job's own Postgres) to Supabase, for the site's
fallback and the audio job (Oct 10 2026).

Writing into Supabase costs no egress; only reading out of it does. So the copy reads one short fingerprint
per article from Supabase (~100 rows x 40 bytes), writes the articles whose fingerprint differs, and deletes
the ones no longer live (moved to the archive branch). The newsroom is the source of truth: Supabase's
`published` is never read back by the newsroom job.

    python -m nishpaksh.mirror            # in the hourly job, after the desk
"""
from __future__ import annotations

import argparse
import logging

from sqlalchemy import Text, cast, func, literal
from sqlalchemy import text as sql_text

from .db import Store, delete, published, select

log = logging.getLogger(__name__)


def _fingerprint():
    """One md5 over every column, computed the same way by both Postgres databases."""
    parts = [cast(published.c.version, Text), cast(published.c.updated_at, Text), published.c.headline_en,
             published.c.headline_hi, cast(published.c.payload_en, Text), cast(published.c.payload_hi, Text)]
    joined = parts[0]
    for p in parts[1:]:
        joined = joined.op("||")(literal("|")).op("||")(func.coalesce(p, literal("")))
    return func.md5(func.coalesce(joined, literal(""))).label("m")


def published_to_readers(news: Store, readers: Store) -> dict:
    """Make Supabase's `published` equal to the newsroom's. Returns counts."""
    if readers is news:
        return {"skipped": "one database"}
    mine = {r["story_id"]: r["m"] for r in news.rows(select(published.c.story_id, _fingerprint()))}
    theirs = {r["story_id"]: r["m"] for r in readers.rows(select(published.c.story_id, _fingerprint()))}
    changed = sorted(sid for sid, m in mine.items() if theirs.get(sid) != m)
    gone = sorted(set(theirs) - set(mine))
    # the JSON is copied as its exact text, so both fingerprints agree afterwards (re-serialising it in Python
    # changed spacing on some rows, and they were written again every run)
    put = sql_text("insert into published (story_id, version, updated_at, headline_en, headline_hi, payload_en, "
                   "payload_hi) values (:story_id, :version, :updated_at, :headline_en, :headline_hi, "
                   "cast(:payload_en as json), cast(:payload_hi as json))")
    for start in range(0, len(changed), 20):
        part = changed[start:start + 20]
        rows = news.rows(select(published.c.story_id, published.c.version, published.c.updated_at,
                                published.c.headline_en, published.c.headline_hi,
                                cast(published.c.payload_en, Text).label("payload_en"),
                                cast(published.c.payload_hi, Text).label("payload_hi"))
                         .where(published.c.story_id.in_(part)))
        with readers.engine.begin() as c:           # one transaction: the site never sees a half-copied article
            c.execute(delete(published).where(published.c.story_id.in_(part)))
            c.execute(put, rows)
    for start in range(0, len(gone), 200):
        readers.exec(delete(published).where(published.c.story_id.in_(gone[start:start + 200])))
    out = {"live": len(mine), "written": len(changed), "deleted": len(gone)}
    log.info("published copied to Supabase: %s", out)
    return out


def main() -> None:
    from .config import database_url
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argparse.ArgumentParser().parse_args()
    news = Store(database_url())
    print(published_to_readers(news, news.readers))


if __name__ == "__main__":
    main()
