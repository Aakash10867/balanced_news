"""Which stories are worth reading in depth (owner, Oct 6 2026).

The writer publishes one or two articles an hour, so about 24-48 a day; the pipeline used to read and
analyse every story with three sources (Oct 6: 458 articles in 95 stories in eight hours, ~160
Flash-Lite calls per published article). Now each story is ranked from its HEADLINES as soon as it
has three independent sources: one cheap call rates about 20 stories at once (the same 1-5 rubric and
filler test as importance.py). Only the top of that ranking, the preparation queue, is read,
analysed and searched for more outlets; the rest wait as headlines (embeddings only) and enter the
queue the moment they rank higher: a big late story jumps the queue, coverage growth earns a re-rank.

    rank_new(store, router)   rate stories not yet rated, or whose coverage grew by 2+ sources
    queue(store)              the story ids to prepare now, best first
"""
from __future__ import annotations

import datetime as dt
import logging

from .config import SETTINGS
from .db import Store, articles, select, stories, update, utcnow
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

PROMPT = """Below are news stories, each shown by the headlines different outlets gave it. For each story, rate how
important it is for an ordinary Indian reader, and whether it is filler.

{stories}

Score 1-5:
5  national significance: many people affected, or central government, Parliament, Supreme Court,
   national security, the economy, major disasters, rights of large groups
4  major state-level or national-interest story: state government action, a serious crime or accident
   with wide attention, a significant court ruling, a large protest
3  notable but limited: a local incident with wider interest, a regional political development
2  minor: a routine procedural step in a smaller case, a local event, a ceremonial occasion
1  trivial

filler = true for content that is not news reporting: horoscopes, lottery results, product launches or
reviews, celebrity gossip, recipes, explainers, quizzes, listicles, opinion or editorial pieces,
live-blog shells, sponsored content.

Reply with JSON only: {{"results": [{{"n": 1, "score": 4, "filler": false}}, ...]}}"""

BATCH = 20


def _candidates(store: Store, now: dt.datetime) -> dict[int, dict]:
    """Unpublished stories with 3+ independent sources whose newest report is fresh enough to write."""
    from .editions import frozen_ids
    from .wire import independence_groups
    since = now - dt.timedelta(hours=SETTINGS.stale_after_hours)
    rows = store.rows(select(articles.c.id, articles.c.story_id, articles.c.outlet, articles.c.url, articles.c.agency,
                             articles.c.wire_group, articles.c.title, articles.c.lang, articles.c.published_at,
                             articles.c.fetched_at)
                      .where(articles.c.story_id.is_not(None)))
    closed = frozen_ids(store)
    by: dict[int, list[dict]] = {}
    for r in rows:
        if r["story_id"] not in closed:
            by.setdefault(r["story_id"], []).append(r)
    out = {}
    for sid, arts in by.items():
        newest = max((a["fetched_at"] or a["published_at"] or now) for a in arts)
        if newest < since:
            continue
        groups = len(set(independence_groups(arts).values()))
        if groups >= SETTINGS.min_sources_to_read:
            out[sid] = {"groups": groups, "langs": len({a["lang"] for a in arts}),
                        "titles": list(dict.fromkeys(a["title"] for a in arts if a["title"]))[:3]}
    return out


def rank_new(store: Store, router: Router | None, now: dt.datetime | None = None) -> int:
    """Rate the candidates that have no rating, or whose coverage grew by 2+ sources since."""
    now = now or utcnow()
    cands = _candidates(store, now)
    if not cands or router is None:
        return 0
    an = {r["id"]: dict(r["analysis"] or {}) for r in store.rows(
        select(stories.c.id, stories.c.analysis).where(stories.c.id.in_(sorted(cands))))}
    todo = [sid for sid, c in cands.items()
            if not (an.get(sid) or {}).get("priority")
            or c["groups"] >= (an[sid]["priority"].get("groups") or 0) + 2]
    todo.sort(key=lambda sid: -cands[sid]["groups"])
    rated = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        body = "\n".join(f"{n + 1}. " + " | ".join(cands[sid]["titles"]) for n, sid in enumerate(chunk))
        try:
            res = router.call("page", PROMPT.format(stories=body), json_out=True, max_output_tokens=1200)
        except QuotaExhausted:
            break
        except Exception as e:  # noqa: BLE001
            log.warning("priority: rating failed: %s", str(e)[:200])
            continue
        for item in (res.data or {}).get("results", []) if isinstance(res.data, dict) else []:
            try:
                n, score = int(item["n"]) - 1, int(item.get("score", 3))
            except (KeyError, TypeError, ValueError):
                continue
            if not 0 <= n < len(chunk):
                continue
            sid = chunk[n]
            a = an.get(sid) or {}
            a["priority"] = {"score": min(5, max(1, score)), "filler": item.get("filler") is True,
                             "groups": cands[sid]["groups"], "at": now.isoformat(timespec="minutes")}
            store.exec(update(stories).where(stories.c.id == sid).values(analysis=a))
            rated += 1
    log.info("priority: %d stories rated", rated)
    return rated


def value(p: dict | None, groups: int, langs: int) -> float:
    """Rating plus coverage (importance.rank): many outlets choosing to cover a story is itself a signal."""
    from .importance import rank
    return rank((p or {}).get("score", 0), groups, langs)


def queue(store: Store, now: dt.datetime | None = None, size: int | None = None) -> list[int]:
    """The stories to read, analyse and search for now: the best-rated non-filler candidates."""
    now = now or utcnow()
    size = SETTINGS.prep_queue if size is None else size
    cands = _candidates(store, now)
    an = {r["id"]: (r["analysis"] or {}) for r in store.rows(
        select(stories.c.id, stories.c.analysis).where(stories.c.id.in_(sorted(cands) or [-1])))}
    ranked = [(value(an.get(sid, {}).get("priority"), c["groups"], c["langs"]), c["groups"], sid)
              for sid, c in cands.items()
              if (an.get(sid, {}).get("priority") or {}).get("score") and not an[sid]["priority"].get("filler")]
    ranked.sort(reverse=True)
    return [sid for _, _, sid in ranked[:size]]
