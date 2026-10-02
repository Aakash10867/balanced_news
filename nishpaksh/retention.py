"""Keep the database small enough for the Supabase free tier (500 MB).

What is kept, and for how long (all in config.Settings):
  embeddings + MinHash    ~4 days   only used inside the 72-hour grouping windows
  unread articles         7 days    single-source stories that were never sent to a model
  full text of read ones  14 days   the extracted claims carry what matters
  whole stories           90 days   claims, verdicts, published page, its translations
  source agreements       forever   tiny, and the perspective model learns from all history

If the database still grows past the soft limit, every window is halved for that run.
"""
from __future__ import annotations

import datetime as dt
import logging
import os

from sqlalchemy import text

from .config import SETTINGS
from .db import (Store, articles, canonical, claims, delete, published, select, stories, story_pairs,
                 translations, update, utcnow)

log = logging.getLogger(__name__)


def db_size_mb(store: Store) -> float | None:
    url = str(store.engine.url)
    try:
        if url.startswith("postgresql"):
            with store.engine.connect() as c:
                return c.execute(text("select pg_database_size(current_database())")).scalar() / 1e6
        if url.startswith("sqlite"):
            path = store.engine.url.database
            return os.path.getsize(path) / 1e6 if path and os.path.exists(path) else 0.0
    except Exception as e:  # noqa: BLE001
        log.warning("could not read database size: %s", e)
    return None


def _apply(store: Store, scale: float) -> dict:
    now = utcnow()
    hours = lambda h: now - dt.timedelta(hours=h * scale)  # noqa: E731
    days = lambda d: now - dt.timedelta(days=d * scale)  # noqa: E731
    out = {}

    out["vectors_cleared"] = store.exec(
        update(articles).where(articles.c.published_at < hours(SETTINGS.vectors_retention_hours),
                               (articles.c.embedding.is_not(None)) | (articles.c.minhash.is_not(None)))
        .values(embedding=None, minhash=None)).rowcount or 0

    out["unread_deleted"] = store.exec(
        delete(articles).where(articles.c.published_at < days(SETTINGS.unread_retention_days),
                               articles.c.extracted_at.is_(None))).rowcount or 0

    out["text_cleared"] = store.exec(
        update(articles).where(articles.c.published_at < days(SETTINGS.text_retention_days),
                               articles.c.text.is_not(None))
        .values(text=None)).rowcount or 0

    # whole stories past retention, and empty stories nobody will revisit
    old = [r["id"] for r in store.rows(select(stories.c.id).where(
        stories.c.updated_at < days(SETTINGS.story_retention_days)))]
    with store.engine.connect() as c:
        live = {r[0] for r in c.execute(select(articles.c.story_id).where(articles.c.story_id.is_not(None)).distinct())}
    empty = [r["id"] for r in store.rows(select(stories.c.id).where(
        stories.c.updated_at < days(SETTINGS.unread_retention_days))) if r["id"] not in live]
    doomed = sorted(set(old) | set(empty))
    if doomed:
        from .compose import _collect_strings, _key
        keys = []
        for p in store.rows(select(published.c.payload_en).where(published.c.story_id.in_(doomed))):
            if p["payload_en"]:
                keys += [_key(s) for s in _collect_strings(p["payload_en"])]
        for i in range(0, len(keys), 500):
            store.exec(delete(translations).where(translations.c.key.in_(keys[i:i + 500])))
        for i in range(0, len(doomed), 500):
            chunk = doomed[i:i + 500]
            store.exec(delete(claims).where(claims.c.story_id.in_(chunk)))
            store.exec(delete(canonical).where(canonical.c.story_id.in_(chunk)))
            store.exec(delete(published).where(published.c.story_id.in_(chunk)))
            store.exec(delete(articles).where(articles.c.story_id.in_(chunk)))
            store.exec(delete(stories).where(stories.c.id.in_(chunk)))
    out["stories_deleted"] = len(doomed)
    _ = story_pairs  # kept on purpose: small, and the perspective model learns from all of it
    return out


def enforce(store: Store) -> dict:
    stats = _apply(store, 1.0)
    size = db_size_mb(store)
    if size is not None and size > SETTINGS.storage_soft_limit_mb:
        # Postgres reuses deleted space rather than shrinking the file, so the reported size
        # lags behind deletes. One halving per run, re-checked next hour, avoids over-deleting.
        log.warning("database at %.0f MB (soft limit %d MB): halving retention windows this run",
                    size, SETTINGS.storage_soft_limit_mb)
        _apply(store, 0.5)
        size = db_size_mb(store)
    stats["db_mb"] = None if size is None else round(size, 1)
    return stats
