"""Hourly pipeline run.

    python -m nishpaksh.run                 # full run
    python -m nishpaksh.run --no-ingest     # reprocess what is stored
"""
from __future__ import annotations

import argparse
import logging
import time

from .config import SETTINGS, database_url, gemini_api_key, load_yaml
from .db import Store, select, stories
from .router import GeminiBackend, Router

log = logging.getLogger("nishpaksh")


def run(store: Store | None = None, backend=None, time_budget_min: float = 40, ingest_news: bool = True) -> dict:
    from . import compose, extract, ingest, match, perspectives, stories as story_mod, verify, wire

    t0 = time.time()
    deadline = t0 + time_budget_min * 60
    store = store or Store(database_url())
    store.init()
    if backend is None:
        key = gemini_api_key()
        if not key:
            raise SystemExit("GEMINI_API_KEY is not set")
        backend = GeminiBackend(key)
    router = Router(load_yaml("models.yaml")["tiers"], backend, store)
    router.resolve()
    stats: dict = {}

    if ingest_news:
        ingest.sync_feeds(store)
        stats["ingested"] = ingest.ingest(store)
    stats["wire_assigned"] = wire.assign_wire_groups(store)
    stats["grouped"] = story_mod.group_stories(store, router)
    # leave ~10 minutes of the budget for the analysis stages
    stats["extracted"] = extract.extract_pending(store, router, deadline - 10 * 60)

    dirty = [s["id"] for s in store.rows(select(stories.c.id).where(stories.c.dirty.is_(True))
                                         .order_by(stories.c.updated_at.desc()))]
    for sid in dirty:
        match.match_story(store, router, sid)
        perspectives.analyze_story(store, sid)
    stats["global_clusters"] = perspectives.recompute_global(store)
    if stats["global_clusters"]:
        for sid in dirty:  # labels may have changed
            perspectives.analyze_story(store, sid)

    budget = {"grounded": router.per_run_budget("grounded"), "judge": router.per_run_budget("judge")}
    stats["verify_budget"] = dict(budget)
    checked = published = 0
    for sid in dirty:
        verify.base_verdicts(store, sid)
        if time.time() < deadline:
            checked += verify.verify_story(store, router, sid, budget)
        if compose.publish_story(store, router, sid):
            published += 1
    from . import retention
    stats.update(stories_processed=len(dirty), claims_checked=checked, published=published,
                 storage=retention.enforce(store), seconds=round(time.time() - t0))
    stats["quota_left"] = {t: router.remaining_today(t) for t in router.tiers}
    log.info("run complete: %s", stats)
    return stats


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--time-budget", type=float, default=40, help="minutes")
    p.add_argument("--no-ingest", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(time_budget_min=a.time_budget, ingest_news=not a.no_ingest)


if __name__ == "__main__":
    main()
