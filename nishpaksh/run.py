"""Hourly pipeline run.

    python -m nishpaksh.run                 # full run
    python -m nishpaksh.run --no-ingest     # reprocess what is stored
"""
from __future__ import annotations

import argparse
import logging
import os
import time

from .config import SETTINGS, database_url, gemini_api_keys, load_yaml
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
        verify_budget: dict | None = None, search_news: bool | None = None) -> dict:
    from . import compose, extract, ingest, match, perspectives, stories as story_mod, verify, wire
    search_news = ingest_news if search_news is None else search_news

    t0 = time.time()
    deadline = t0 + time_budget_min * 60
    store = store or Store(database_url())
    store.init()
    if backend is None:
        keys = gemini_api_keys()
        if not keys:
            raise SystemExit("GEMINI_API_KEY is not set")
        backend = [GeminiBackend(k) for k in keys]
        log.info("using %d Gemini API key(s)", len(keys))
    from .db import insert as _insert, runs as _runs, utcnow as _now
    run_id = store.insert_returning_id(_runs, dict(started_at=_now(), trigger=os.environ.get("RUN_TRIGGER", "manual")))
    router = Router(load_yaml("models.yaml")["tiers"], backend, store)
    router.resolve()
    stats: dict = {}

    from . import discover
    from .tavily import Tavily
    tavily = Tavily(store, daily_cap=SETTINGS.tavily_daily_cap) if search_news else None

    def step(name, fn):
        """A failing optional step is recorded and skipped, never the end of the run."""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log.exception("step %s failed", name)
            stats.setdefault("errors", []).append(f"{name}: {str(e)[:200]}")
            return None

    if ingest_news:
        ingest.sync_feeds(store)
        stats["ingested"] = ingest.ingest(store)
    if search_news:
        # who else covered the stories we know? found articles are grouped like any other
        stats["search"] = step("search", lambda: discover.discover(store, tavily, until=t0 + 8 * 60))
    stats["retracted_headline_only"] = extract.retract_unreadable(store)
    if tavily is not None:
        # pages we could not read, in stories worth reading (grouped in earlier runs)
        stats["tavily_pages_read"] = step("tavily", lambda: extract.read_blocked_pages(
            store, tavily, SETTINGS.tavily_extract_pages_per_run))
    stats["wire_assigned"] = wire.assign_wire_groups(store)
    stats["grouped"] = step("grouping", lambda: story_mod.group_stories(store, router))   # a failure must not stop reading and writing
    # time plan: reading stops 20 minutes before the deadline; the story stage gets the rest
    stats["extracted"] = extract.extract_pending(store, router, deadline - 20 * 60)

    # pages published before the readable story existed get rewritten once
    from .db import articles as _articles, published as _published
    for row in store.rows(select(_published.c.story_id, _published.c.payload_en)):
        pe = row["payload_en"] or {}
        # missing or old format; "qualified_by" marks pages built under the origins rules
        from .narrative import WRITER_VERSION
        if (not (pe.get("narrative") or {}).get("paragraphs") or "qualified_by" not in pe
                or (pe.get("narrative") or {}).get("writer") != WRITER_VERSION):
            store.exec(update(stories).where(stories.c.id == row["story_id"]).values(dirty=True))
    dirty = [s["id"] for s in store.rows(select(stories.c.id).where(stories.c.dirty.is_(True)))]
    # most-covered stories first, so the stories readers most likely want are never the ones cut
    with store.engine.connect() as c:
        size = dict(c.execute(select(_articles.c.story_id, func.count())
                              .where(_articles.c.extracted_at.is_not(None)).group_by(_articles.c.story_id)).all())
    dirty.sort(key=lambda sid: -size.get(sid, 0))
    workers = 1 if str(store.engine.url).startswith("sqlite") else 4

    from . import origins

    from .consolidate import consolidate_story
    live_now = {r["story_id"] for r in store.rows(select(_published.c.story_id))}

    def analyse(sid):
        match.match_story(store, router, sid)
        # merge duplicate statements, mark contradictions, one spelling per name: only for stories
        # that can be published (it costs a model call)
        if sid in live_now or origins.independent_read_outlets(store, sid) >= SETTINGS.qualify_min_outlets:
            consolidate_story(store, router, sid)
        perspectives.analyze_story(store, sid)
        if origins.assess_story(store, router, sid):
            # interim rule while perspectives are unknown: 3+ independent outlets, 2+ origins
            perspectives.mark_qualified(store, sid, "interim")
    analysed = _parallel(dirty, analyse, deadline - 12 * 60, workers)
    stats["global_clusters"] = perspectives.recompute_global(store)
    if stats["global_clusters"]:
        for sid in analysed:  # labels may have changed
            perspectives.analyze_story(store, sid)
            if origins.assess_story(store, None, sid):
                perspectives.mark_qualified(store, sid, "interim")

    # a story whose read articles were grouped on another embedding model's vectors cannot be
    # trusted to be one event (real data: one such "story" mixed GST, a temple and a phone launch);
    # it is not published until those articles are re-embedded and regrouped
    model = story_mod.embed_model(router)
    if model:
        untrusted = {r["story_id"] for r in store.rows(
            select(_articles.c.story_id).where(_articles.c.story_id.is_not(None), _articles.c.extracted_at.is_not(None),
                                               (_articles.c.embed_model.is_(None)) | (_articles.c.embed_model != model))
            .distinct())}
        if untrusted:
            ids = sorted(untrusted)
            for i in range(0, len(ids), 500):
                store.exec(update(stories).where(stories.c.id.in_(ids[i:i + 500]), stories.c.qualifies.is_(True))
                           .values(qualifies=False))
        stats["stories_awaiting_regroup"] = len(untrusted)
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
    # pages not touched this run still age: a "developing" statement becomes established once it has
    # stood 6 hours, and the clock is code-only (no model calls), so every live page is re-checked
    for sid in sorted(live - set(to_publish)):
        if sid not in qualifying:
            to_publish.append(sid)          # no longer qualifies: publish_story takes the page down
        elif verify.base_verdicts(store, sid):
            to_publish.append(sid)
    published = _parallel(to_publish, lambda sid: compose.publish_story(store, router, sid), deadline, workers)
    stats.update(stories_dirty=len(dirty), analysed=len(analysed), qualifying=len(to_publish),
                 claims_checked=checked, published=len(published),
                 left_for_next_run=len(dirty) - len(analysed) + len(to_publish) - len(published))
    from . import retention
    stats.update(storage=retention.enforce(store), seconds=round(time.time() - t0))
    stats["quota_left"] = {t: router.remaining_today(t) for t in router.tiers}
    stats["quota_now"] = {t: router.remaining_now(t) for t in router.tiers}   # under the pacing curve
    if tavily is not None:
        stats["tavily"] = {"spent_this_run": tavily.spent_this_run, "left_today": tavily.allowance_today()}
    stats["health"] = health(store, stats)
    store.exec(update(_runs).where(_runs.c.id == run_id).values(finished_at=_now(), stats=stats))
    log.info("run complete: %s", stats)
    return stats


def stalled(store: Store, stats: dict) -> dict:
    """Is the pipeline still taking in news? Counts from this run and the last few finished runs:
    articles came in but none were grouped for STALL_RUNS runs in a row. (Zero articles read is
    not a stall signal: healthy runs often read none, as only multi-outlet stories are read.)"""
    from .db import runs as R
    n = SETTINGS.health_stall_runs
    past = [r["stats"] or {} for r in store.rows(select(R.c.stats).where(R.c.finished_at.is_not(None))
                                                  .order_by(R.c.id.desc()).limit(n - 1))]
    window = [stats] + past
    out: dict = {"problems": []}
    if len(window) < n:
        return {}
    came_in = sum((s.get("ingested") or 0) + ((s.get("search") or {}).get("new_articles") or 0) for s in window)
    if came_in and not any(s.get("grouped") for s in window):
        out["problems"].append(f"{came_in} new articles in the last {n} runs but none grouped")
    return {"stall": out["problems"]} if out["problems"] else {}


def health(store: Store, stats: dict) -> dict:
    """Invariants checked on every real run. A failure is written into the run record (and the
    log) so problems in the live data are seen without waiting for someone to notice the site."""
    from .db import articles as A, canonical as C
    from .stories import story_health
    out: dict = {"problems": []}
    try:
        out.update(story_health(store))
        biggest = out["largest_stories"][0][1] if out["largest_stories"] else 0
        if biggest > SETTINGS.health_max_story_articles:
            out["problems"].append(f"a story holds {biggest} articles: grouping may be merging events")
        import datetime as _dt
        from .db import utcnow as _now

        def _bad(o):
            o = o or {}
            try:
                stood = (_now() - _dt.datetime.fromisoformat(o["met_at"])).total_seconds() / 3600
            except (KeyError, TypeError, ValueError):
                stood = -1
            return (o.get("n_origins", 0) < SETTINGS.established_min_origins
                    or o.get("outlets", 0) < SETTINGS.established_min_outlets
                    or stood < SETTINGS.established_after_hours)
        bad = [c["id"] for c in store.rows(select(C.c.id, C.c.origins).where(C.c.verdict == "corroborated"))
               if _bad(c["origins"])]
        if bad:
            out["problems"].append(f"{len(bad)} established statements lack 2 origins / 3 outlets / 6 hours: {bad[:10]}")
        summ = store.rows(select(A.c.id).where(A.c.text_source == "summary", A.c.extracted_at.is_not(None)).limit(5))
        if summ:
            out["problems"].append(f"headline-only articles were read for facts: {[r['id'] for r in summ]}")
        # a tier is dead for the day when what is left cannot pay for one call (one grouping batch
        # for embeddings). Oct 2026: 20 embeddings and 0 Flash-Lite left read as "no problems"
        # while nothing new was read or grouped for nine hours.
        need = {"embed": SETTINGS.health_embed_batch, "light": 1, "page": 1, "writer": 1}
        left = stats.get("quota_left", {})
        dead = [t for t, n in need.items() if t in left and left[t] < n]
        if dead:
            out["problems"].append(f"daily quota used up for: {', '.join(dead)} (resets 00:00 Pacific)")
        out.update(stalled(store, stats))
        if stats.get("errors"):
            out["problems"].append(f"{len(stats['errors'])} steps failed")
    except Exception as e:  # noqa: BLE001
        out["problems"].append(f"health check failed: {e}")
    out["problems"] += out.pop("stall", [])
    for p in out["problems"]:
        log.warning("HEALTH: %s", p)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--time-budget", type=float, default=40, help="minutes")
    p.add_argument("--no-ingest", action="store_true")
    p.add_argument("--no-search", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        run(time_budget_min=a.time_budget, ingest_news=not a.no_ingest, search_news=not (a.no_search or a.no_ingest))
    except BaseException as e:  # noqa: BLE001
        # GitHub job logs are not readable from where the pipeline is maintained: keep the traceback
        if not isinstance(e, SystemExit) or e.code not in (0, None):
            _record_crash()
        raise


def _record_crash() -> None:
    import json
    import traceback
    try:
        from sqlalchemy import text as sql
        with Store(database_url()).engine.begin() as c:
            c.execute(sql("insert into diagnostics (kind, report) values ('crash', :r)"),
                      {"r": json.dumps({"trigger": os.environ.get("RUN_TRIGGER", "manual"),
                                        "traceback": traceback.format_exc()[-6000:]})})
    except Exception:  # noqa: BLE001
        log.exception("could not record the crash")


if __name__ == "__main__":
    main()
