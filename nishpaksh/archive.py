"""Daily archive: everything we learned on a given day, minus article bodies.

    python -m nishpaksh.archive --day 2026-10-01 --out archive/

Writes nishpaksh-YYYY-MM-DD.jsonl.gz, one JSON object per line, each tagged with "type":
  article     metadata only (url, outlet, title, author, time, story, wire group). Never the body:
              the repository is public and article text is the publishers' copyright.
  story       grouping, signature and the perspective analysis
  claim       what each article said, in neutral words, with stance, source and loaded words
  canonical   the matched facts with verdicts and the evidence check
  published   the article as shown on the site, English and Hindi
  source_clusters  the learned perspective membership at export time

A day D covers articles published on D and stories last updated on D (UTC). A story that
changes again later is exported again on that later day; the newest copy wins.
The daily workflow uploads the file to the GitHub release "archive-YYYY-MM".
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import pathlib

from .config import database_url
from .db import Store, articles, canonical, claims, published, select, source_clusters, stories

ARTICLE_FIELDS = ("id", "url", "outlet", "lang", "role", "title", "author", "published_at", "agency",
                  "wire_group", "story_id", "signature", "extracted_at", "text_source")


def _json_default(o):
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    return str(o)


def export_day(store: Store, day: dt.date, out_dir: str | pathlib.Path) -> pathlib.Path:
    start = dt.datetime.combine(day, dt.time.min)
    end = start + dt.timedelta(days=1)
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"nishpaksh-{day.isoformat()}.jsonl.gz"

    cols = [getattr(articles.c, f) for f in ARTICLE_FIELDS]
    arts = store.rows(select(*cols).where(articles.c.published_at >= start, articles.c.published_at < end))
    sts = store.rows(select(stories).where(stories.c.updated_at >= start, stories.c.updated_at < end))
    sids = [s["id"] for s in sts]

    def by_story(table, column):
        rows = []
        for i in range(0, len(sids), 500):
            rows += store.rows(select(table).where(column.in_(sids[i:i + 500])))
        return rows

    counts = {}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        def write(kind, rows):
            counts[kind] = len(rows)
            for r in rows:
                f.write(json.dumps({"type": kind, **r}, ensure_ascii=False, default=_json_default) + "\n")

        write("article", arts)
        write("story", sts)
        write("claim", by_story(claims, claims.c.story_id))
        write("canonical", by_story(canonical, canonical.c.story_id))
        write("published", by_story(published, published.c.story_id))
        clusters = store.rows(select(source_clusters))
        f.write(json.dumps({"type": "source_clusters", "data": clusters}, ensure_ascii=False,
                           default=_json_default) + "\n")
    print(f"{path.name}: " + ", ".join(f"{v} {k}s" for k, v in counts.items()))
    return path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--day", required=True, help="YYYY-MM-DD (UTC)")
    p.add_argument("--out", default="archive")
    a = p.parse_args()
    export_day(Store(database_url()), dt.date.fromisoformat(a.day), a.out)


if __name__ == "__main__":
    main()
