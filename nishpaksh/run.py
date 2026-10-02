"""Hourly pipeline run.

    python -m nishpaksh.run                 # full run
    python -m nishpaksh.run --no-ingest     # reprocess what is stored
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from .config import SETTINGS, database_url, gemini_api_key, load_yaml
from sqlalchemy import func

from .db import Store, select, stories, update
from .router import GeminiBackend, Router

log = logging.getLogger("nishpaksh")


def _parallel(items: list, fn, until: float, workers: int) -> list:
    """Apply fn to items with a few threads, starting nothing after `until`. One story failing
    is logged and skipped; it stays dirty and is retried next run."""
    import threading
    it = iter(items)
    lock = threading.Lock()
    done: list = []

    def worker():
        while time.time() < until:
            with lock:
                item = next(it, None)
            if item is None:
                return
            try:
                fn(item)
                with lock:
                    done.append(item)
            except Exception as e:  # noqa: BLE001
                log.warning("story %s failed: %s", item, str(e)[:300])

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return done


def run(store: Store | None = None, backend=None, time_budget_min: float = 40, ingest_news: bool = True,
        verify_budget: dict | None = None) -> dict:
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
    from .db import insert as _insert, runs as _runs, utcnow as _now
    run_id = store.insert_returning_id(_runs, dict(started_at=_now(), trigger=os.environ.get("RUN_TRIGGER", "manual")))
    router = Router(load_yaml("models.yaml")["tiers"], backend, store)
    router.resolve()
    stats: dict = {}

    if ingest_news:
        ingest.sync_feeds(store)
        stats["ingested"] = ingest.ingest(store)
    stats["wire_assigned"] = wire.assign_wire_groups(store)
    stats["grouped"] = story_mod.group_stories(store, router)
    # time plan: reading stops 20 minutes before the deadline; the story stage gets the rest
    stats["extracted"] = extract.extract_pending(store, router, deadline - 20 * 60)

    # pages published before the readable story existed get rewritten once
    from .db import articles as _articles, published as _published
    for row in store.rows(select(_published.c.story_id, _published.c.payload_en)):
        if not (row["payload_en"] or {}).get("narrative"):
            store.exec(update(stories).where(stories.c.id == row["story_id"]).values(dirty=True))
    dirty = [s["id"] for s in store.rows(select(stories.c.id).where(stories.c.dirty.is_(True)))]
    # most-covered stories first, so the stories readers most likely want are never the ones cut
    with store.engine.connect() as c:
        size = dict(c.execute(select(_articles.c.story_id, func.count())
                              .where(_articles.c.extracted_at.is_not(None)).group_by(_articles.c.story_id)).all())
    dirty.sort(key=lambda sid: -size.get(sid, 0))
    workers = 1 if str(store.engine.url).startswith("sqlite") else 4

    def analyse(sid):
        match.match_story(store, router, sid)
        perspectives.analyze_story(store, sid)
    analysed = _parallel(dirty, analyse, deadline - 12 * 60, workers)
    stats["global_clusters"] = perspectives.recompute_global(store)
    if stats["global_clusters"]:
        for sid in analysed:  # labels may have changed
            perspectives.analyze_story(store, sid)

    qualifying = {r["id"] for r in store.rows(select(stories.c.id).where(stories.c.qualifies.is_(True)))}
    with store.engine.connect() as c:
        live = {r[0] for r in c.execute(select(_published.c.story_id))}
    to_publish = [sid for sid in analysed if sid in qualifying or sid in live]  # live: may need taking down
    # stories that cannot be published yet spend no verdict or writing calls; they are
    # re-examined when another of their articles is read
    idle = [sid for sid in analysed if sid not in qualifying and sid not in live]
    for i in range(0, len(idle), 500):
        store.exec(update(stories).where(stories.c.id.in_(idle[i:i + 500])).values(dirty=False))

    budget = dict(verify_budget) if verify_budget else {
        "grounded": router.per_run_budget("grounded"), "judge": router.per_run_budget("judge")}
    stats["verify_budget"] = dict(budget)
    checked = 0
    for sid in to_publish:
        if sid not in qualifying:
            continue
        verify.base_verdicts(store, sid)
        if time.time() < deadline - 6 * 60 and (budget.get("judge", 0) > 0):
            checked += verify.verify_story(store, router, sid, budget)
    published = _parallel(to_publish, lambda sid: compose.publish_story(store, router, sid), deadline, workers)
    stats.update(stories_dirty=len(dirty), analysed=len(analysed), qualifying=len(to_publish),
                 claims_checked=checked, published=len(published),
                 left_for_next_run=len(dirty) - len(analysed) + len(to_publish) - len(published))
    from . import retention
    stats.update(storage=retention.enforce(store), seconds=round(time.time() - t0))
    stats["quota_left"] = {t: router.remaining_today(t) for t in router.tiers}
    store.exec(update(_runs).where(_runs.c.id == run_id).values(finished_at=_now(), stats=stats))
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
