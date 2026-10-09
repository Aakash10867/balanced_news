"""Which stories are worth reading in depth (owner, Oct 6 2026).

The writer publishes one or two articles an hour, so about 24-48 a day; the pipeline used to read and
analyse every story with three sources (Oct 6: 458 articles in 95 stories in eight hours, ~160
Flash-Lite calls per published article). Now each story is ranked from its HEADLINES as soon as it
has three independent sources: one cheap call rates about 20 stories at once (the same 1-5 rubric and
filler test). Only the top of that ranking, the preparation queue, is read,
analysed and searched for more outlets; the rest wait as headlines (embeddings only) and enter the
queue the moment they rank higher: a big late story jumps the queue, coverage growth earns a re-rank.

    rank_new(store, router)   rate stories not yet rated, or whose coverage grew by 2+ sources
    queue(store)              the story ids to prepare now, best first

World outlets (owner, Oct 9 2026): Indian coverage decides relevance. A story with no Indian outlet
(Rule 1) is a candidate only when 3+ independent world outlets carry it AND it affects people beyond
one country (Rule 3): one fixed question, asked twice with the stories in reverse order; two "yes"
needed, "unsure" = no. Kept in `analysis.world`, asked again when coverage grows by 2+.
"""
from __future__ import annotations

import datetime as dt
import logging

from .config import SETTINGS
from sqlalchemy import and_, or_
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

GLOBAL_PROMPT = """Below are news stories from outside India, each shown by the headlines different outlets gave it.
For each story: does it directly affect people OUTSIDE the country where it happened?

"yes": a war or armed conflict between countries; a disaster or disease crossing borders; a decision by a
major power, the UN or another global body that changes things for other countries; oil, trade, markets,
migration or climate affecting many countries; a coup or election result in a major power.
"no": one country's own affairs: its local politics, crimes, accidents, courts, celebrities, sport,
weather, a company's local news.

Examples:
- "US raises tariffs on all steel imports to 50%" -> yes
- "Israel strikes Iranian nuclear site; oil jumps 8%" -> yes
- "WHO declares mpox a global health emergency" -> yes
- "Pakistan's Supreme Court hears petition on Imran Khan's bail" -> no (one country's courts)
- "Three killed in Texas shooting" -> no (a crime in one country, however grave)
- "UK Labour party picks new deputy leader" -> no (one party's affairs)
- "Earthquake kills 40 in Nepal" -> no (a disaster inside one country)
If unsure, answer "unsure".

{stories}

Reply with JSON only: {{"results": [{{"n": 1, "answer": "yes"}}, {{"n": 2, "answer": "no"}}, ...]}}"""


def _candidates(store: Store, now: dt.datetime) -> dict[int, dict]:
    """Unpublished stories with 3+ independent sources whose newest report is fresh enough to write."""
    from .editions import frozen_ids
    from .wire import independence_groups, independent
    since = now - dt.timedelta(hours=SETTINGS.stale_after_hours)
    # only stories with a report inside the window (as `newest < since` below), so old stories are not
    # read every run (egress, Oct 8 2026)
    fresh = (select(articles.c.story_id).where(articles.c.story_id.is_not(None), or_(
        articles.c.fetched_at >= since,
        and_(articles.c.fetched_at.is_(None), or_(articles.c.published_at >= since, articles.c.published_at.is_(None))))))
    from .ownership import region_of
    rows = store.rows(select(articles.c.id, articles.c.story_id, articles.c.outlet, articles.c.url, articles.c.agency,
                             articles.c.wire_group, articles.c.title, articles.c.lang, articles.c.published_at,
                             articles.c.fetched_at)
                      .where(articles.c.story_id.in_(fresh)))
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
        groups = len(independent(independence_groups(arts)))
        if groups >= SETTINGS.min_sources_to_read:
            out[sid] = {"groups": groups, "langs": len({a["lang"] for a in arts}),
                        "titles": list(dict.fromkeys(a["title"] for a in arts if a["title"]))[:3],
                        # Rule 1: an Indian outlet (PIB included) covers it
                        "indian": any(region_of(a["outlet"], a["url"], a["lang"]) == "india" for a in arts)}
    return out


def _ask_global(router: Router, chunk: list[int], cands: dict[int, dict]) -> dict[int, str]:
    body = "\n".join(f"{n + 1}. " + " | ".join(cands[sid]["titles"]) for n, sid in enumerate(chunk))
    try:
        res = router.call("page", GLOBAL_PROMPT.format(stories=body), json_out=True, max_output_tokens=600)
    except QuotaExhausted:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("priority: global-impact question failed: %s", str(e)[:200])
        return {}
    out = {}
    for item in (res.data or {}).get("results", []) if isinstance(res.data, dict) else []:
        try:
            n = int(item["n"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= n < len(chunk):
            out[chunk[n]] = str(item.get("answer", "")).strip().lower()
    return out


def world_check(store: Store, router: Router | None, cands: dict[int, dict], an: dict[int, dict],
                now: dt.datetime) -> int:
    """Rule 3 for the candidates no Indian outlet covers: global impact, asked twice (the second time
    in reverse order), only for those the first answer called "yes". Saved in analysis.world."""
    todo = [sid for sid, c in cands.items() if not c["indian"]
            and (not (an.get(sid) or {}).get("world")
                 or c["groups"] >= (an[sid]["world"].get("groups") or 0) + 2)]
    if not todo or router is None:
        return 0
    asked = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        try:
            first = _ask_global(router, chunk, cands)
            again = [sid for sid in chunk if first.get(sid) == "yes"]
            second = _ask_global(router, list(reversed(again)), cands) if again else {}
        except QuotaExhausted:
            break
        for sid in chunk:
            if sid not in first:
                continue          # not answered: asked again next run
            a = an.setdefault(sid, {})
            a["world"] = {"global": first.get(sid) == "yes" and second.get(sid) == "yes",
                          "answers": [first.get(sid), second.get(sid)], "groups": cands[sid]["groups"],
                          "at": now.isoformat(timespec="minutes")}
            store.exec(update(stories).where(stories.c.id == sid).values(analysis=a))
            asked += 1
    return asked


def _relevant(c: dict, a: dict | None) -> bool:
    """Rule 1, or Rule 3 passed."""
    return c["indian"] or bool(((a or {}).get("world") or {}).get("global"))


def rank_new(store: Store, router: Router | None, now: dt.datetime | None = None) -> int:
    """Rate the candidates that have no rating, or whose coverage grew by 2+ sources since."""
    now = now or utcnow()
    cands = _candidates(store, now)
    if not cands or router is None:
        return 0
    an = {r["id"]: dict(r["analysis"] or {}) for r in store.rows(
        select(stories.c.id, stories.c.analysis).where(stories.c.id.in_(sorted(cands))))}
    world_check(store, router, cands, an, now)
    cands = {sid: c for sid, c in cands.items() if _relevant(c, an.get(sid))}
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
              if (an.get(sid, {}).get("priority") or {}).get("score") and not an[sid]["priority"].get("filler")
              and _relevant(c, an.get(sid))]
    ranked.sort(reverse=True)
    return [sid for _, _, sid in ranked[:size]]
