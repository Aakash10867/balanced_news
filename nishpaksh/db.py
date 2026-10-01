"""Storage. One schema for SQLite (local/tests) and Postgres (Supabase)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, Integer, MetaData, String, Table, Text,
    create_engine, delete, insert, select, update,
)

md = MetaData()

feeds = Table(
    "feeds", md,
    Column("id", Integer, primary_key=True),
    Column("name", String(200), nullable=False),
    Column("url", String(1000), unique=True, nullable=False),
    Column("lang", String(8), nullable=False),
    Column("role", String(20), default="news"),
    Column("fail_count", Integer, default=0),
    Column("disabled", Boolean, default=False),
    Column("last_ok", DateTime),
)

articles = Table(
    "articles", md,
    Column("id", Integer, primary_key=True),
    Column("url", String(1500), unique=True, nullable=False),
    Column("feed_id", Integer),
    Column("outlet", String(200)),
    Column("lang", String(8)),
    Column("role", String(20), default="news"),
    Column("title", Text),
    Column("author", String(300)),
    Column("published_at", DateTime, index=True),
    Column("fetched_at", DateTime),
    Column("text", Text),
    Column("text_source", String(10)),          # full | summary
    Column("agency", String(40)),                # PTI, ANI, Bhasha ... if wire copy
    Column("minhash", JSON),
    Column("wire_group", Integer, index=True),
    Column("extraction", JSON),
    Column("extracted_at", DateTime),
    Column("extract_failures", Integer, default=0),
    Column("signature", Text),                   # neutral English one-liner
    Column("embedding", JSON),
    Column("story_id", Integer, index=True),
)

stories = Table(
    "stories", md,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime),
    Column("updated_at", DateTime, index=True),
    Column("signature", Text),
    Column("dirty", Boolean, default=True),
    Column("qualifies", Boolean, default=False),
    Column("analysis", JSON),                    # perspectives, groups, split diagnostics
)

claims = Table(
    "claims", md,
    Column("id", Integer, primary_key=True),
    Column("story_id", Integer, index=True),
    Column("article_id", Integer, index=True),
    Column("local_id", String(20)),
    Column("kind", String(10)),                  # event | claim | relation
    Column("text", Text),
    Column("stance", String(12)),                # asserts | attributes | denies
    Column("attributed_to", String(200)),
    Column("evidence", String(30)),
    Column("loaded_words", JSON),
    Column("time", JSON),                        # {when_text, start, end, precision}
    Column("rel", JSON),                         # {from, to, type} (local ids)
    Column("canonical_id", Integer, index=True),
)

canonical = Table(
    "canonical", md,
    Column("id", Integer, primary_key=True),
    Column("story_id", Integer, index=True),
    Column("kind", String(10)),
    Column("text", Text),
    Column("rel", JSON),                         # for relations: {from, to, type} canonical ids
    Column("conflicts", JSON, default=list),     # canonical ids judged contradictory
    Column("verdict", String(14), default="pending"),
    Column("detail", JSON),                      # verification record
    Column("checked_members", Integer, default=0),
)

story_pairs = Table(  # per-story agreement between two sources; global affinity = average
    "story_pairs", md,
    Column("story_id", Integer, primary_key=True),
    Column("a", String(300), primary_key=True),
    Column("b", String(300), primary_key=True),
    Column("value", Float),
)

source_clusters = Table(
    "source_clusters", md,
    Column("source", String(300), primary_key=True),
    Column("cluster", Integer),
    Column("updated_at", DateTime),
)

published = Table(
    "published", md,
    Column("story_id", Integer, primary_key=True),
    Column("version", Integer),
    Column("updated_at", DateTime, index=True),
    Column("headline_en", Text),
    Column("headline_hi", Text),
    Column("payload_en", JSON),
    Column("payload_hi", JSON),
)

translations = Table(
    "translations", md,
    Column("key", String(64), primary_key=True),  # sha256(lang + text)
    Column("text", Text),
)

quota_usage = Table(
    "quota_usage", md,
    Column("model", String(100), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("requests", Integer, default=0),
    Column("tokens", Integer, default=0),
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Store:
    def __init__(self, url: str):
        kw = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(url, **kw)

    def init(self) -> None:
        md.create_all(self.engine)

    # generic helpers ---------------------------------------------------------
    def rows(self, stmt) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(stmt)]

    def one(self, stmt) -> dict | None:
        r = self.rows(stmt)
        return r[0] if r else None

    def exec(self, stmt):
        with self.engine.begin() as c:
            return c.execute(stmt)

    def insert_returning_id(self, table: Table, values: dict) -> int:
        with self.engine.begin() as c:
            res = c.execute(insert(table).values(**values))
            return int(res.inserted_primary_key[0])

    # quota -------------------------------------------------------------------
    def quota_load(self, day: str) -> dict[str, tuple[int, int]]:
        rs = self.rows(select(quota_usage).where(quota_usage.c.day == day))
        return {r["model"]: (r["requests"], r["tokens"]) for r in rs}

    def quota_save(self, model: str, day: str, requests: int, tokens: int) -> None:
        with self.engine.begin() as c:
            n = c.execute(
                update(quota_usage)
                .where(quota_usage.c.model == model, quota_usage.c.day == day)
                .values(requests=requests, tokens=tokens)
            ).rowcount
            if not n:
                c.execute(insert(quota_usage).values(model=model, day=day, requests=requests, tokens=tokens))

    # translations cache --------------------------------------------------------
    def translation_get(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        rs = self.rows(select(translations).where(translations.c.key.in_(keys)))
        return {r["key"]: r["text"] for r in rs}

    def translation_put(self, items: dict[str, str]) -> None:
        if not items:
            return
        with self.engine.begin() as c:
            existing = {r[0] for r in c.execute(select(translations.c.key).where(translations.c.key.in_(list(items))))}
            new = [{"key": k, "text": v} for k, v in items.items() if k not in existing]
            if new:
                c.execute(insert(translations), new)

    # retention -----------------------------------------------------------------
    def purge_old_text(self, days: int) -> int:
        cutoff = utcnow() - dt.timedelta(days=days)
        res = self.exec(
            update(articles)
            .where(articles.c.published_at < cutoff, articles.c.text.is_not(None))
            .values(text=None, minhash=None, embedding=None)
        )
        return res.rowcount or 0


__all__ = [
    "Store", "utcnow", "feeds", "articles", "stories", "claims", "canonical", "story_pairs",
    "source_clusters", "published", "translations", "quota_usage", "select", "insert", "update", "delete",
]
