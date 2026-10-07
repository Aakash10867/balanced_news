"""The writing desk: a job of its own, separate from the hourly pipeline (owner, Oct 6 2026).

The pipeline (run.py, at :05 UTC) reads, analyses and prepares stories; this job (:45 UTC, writer.yml)
only writes. Publishing used to come last in a 20-50 minute run and was squeezed out (Oct 6: one run
spent 50 minutes reading and never asked the writer), and reading spent the Flash-Lite the writer
needed as its fallback.

Each run: the settled stories (editions.settled), most important first (priority.value), are
written one by one until this clock hour has `desk_per_hour` articles (owner: at least one an hour,
at most two), `desk_tries` stories were tried, or the time is up. The quality bar is unchanged:
no good essay, no article.

    python -m nishpaksh.desk
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import time

from sqlalchemy import func

from .config import SETTINGS, database_url, gemini_api_keys, load_yaml
from .db import Store, articles, diagnostics, insert, published, select, stories, utcnow
from .router import GeminiBackend, Router

log = logging.getLogger("nishpaksh.desk")
DESK_LOOK = 25      # stories looked at per run at most (each costs a few cheap page calls)


def _hour_start(now: dt.datetime) -> dt.datetime:
    return now.replace(minute=0, second=0, microsecond=0)


def published_this_hour(store: Store, now: dt.datetime | None = None) -> int:
    now = now or utcnow()
    row = store.one(select(func.count().label("n")).select_from(published)
                    .where(published.c.updated_at >= _hour_start(now)))
    return int(row["n"]) if row else 0


def ready(store: Store, now: dt.datetime | None = None) -> list[int]:
    """Settled, qualifying, unpublished stories, most important first."""
    from . import editions, priority
    from .wire import independence_groups
    now = now or utcnow()
    closed = editions.frozen_ids(store)
    rows = store.rows(select(stories.c.id, stories.c.analysis).where(stories.c.qualifies.is_(True)))
    out = []
    for r in rows:
        sid = r["id"]
        if sid in closed or not editions.settled(store, sid, now):
            continue
        # a follow-up refused and no new outlet since: it waits for coverage, not for another look
        fu = ((r["analysis"] or {}).get("edition") or {}).get("followup") or {}
        if fu.get("ok") is False and fu.get("at"):
            last = editions.last_new_source(store, sid)
            try:
                if last is not None and last <= dt.datetime.fromisoformat(fu["at"]):
                    continue
            except ValueError:
                pass
        arts = store.rows(select(articles.c.id, articles.c.outlet, articles.c.url, articles.c.agency,
                                 articles.c.wire_group, articles.c.lang).where(articles.c.story_id == sid))
        groups = len(set(independence_groups(arts).values())) if arts else 0
        p = (r["analysis"] or {}).get("priority")
        if p and p.get("filler"):
            continue
        out.append((priority.value(p, groups, len({a["lang"] for a in arts})), groups, sid))
    out.sort(reverse=True)
    return [sid for _, _, sid in out]


def work(store: Store, router: Router, now: dt.datetime | None = None, until: float | None = None) -> dict:
    from . import compose, verify
    now = now or utcnow()
    stats = {"already_this_hour": published_this_hour(store, now), "tried": 0, "published": []}
    room = SETTINGS.desk_per_hour - stats["already_this_hour"]
    if room <= 0:
        return stats
    queue = ready(store, now)
    stats["ready"] = len(queue)
    stats["skipped"] = {}
    for n, sid in enumerate(queue):
        # a try is a story the writer was asked to write; stories turned away before that (not a
        # follow-up yet, no headline) do not use one up, but at most DESK_LOOK stories are looked at
        if room <= 0 or stats["tried"] >= SETTINGS.desk_tries or n >= DESK_LOOK or (until and time.time() > until):
            break
        verify.base_verdicts(store, sid)            # the 6-hour clock moved since it was analysed
        if compose.publish_story(store, router, sid):
            stats["published"].append(sid)
            stats["tried"] += 1
            room -= 1
            continue
        why = compose.LAST_OUTCOME.get("outcome", "?")
        if why == "written short":
            stats["tried"] += 1
        else:
            stats["skipped"][why] = stats["skipped"].get(why, 0) + 1
    return stats


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=float, default=SETTINGS.desk_minutes)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    t0 = time.time()
    store = Store(database_url())
    store.init()
    keys = gemini_api_keys()
    if not keys:
        raise SystemExit("GEMINI_API_KEY is not set")
    router = Router(load_yaml("models.yaml")["tiers"], [GeminiBackend(k) for k in keys], store)
    router.resolve()
    stats = work(store, router, until=t0 + a.minutes * 60)
    stats.update(seconds=round(time.time() - t0), trigger=os.environ.get("RUN_TRIGGER", "manual"),
                 tier_calls={k: dict(v) for k, v in sorted(router.tier_log.items())})
    log.info("desk: %s", stats)
    # the desk's record goes to diagnostics, not runs: runs is what spaces the pipeline's own runs
    store.exec(insert(diagnostics).values(created_at=utcnow(), kind="desk", report=stats))


if __name__ == "__main__":
    main()
