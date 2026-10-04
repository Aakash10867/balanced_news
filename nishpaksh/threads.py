"""Threads: a later story that is a development of an earlier one (an arrest after a crime, a court
step in a case, a reaction to a decision) is linked to it: parent -> daughter. A story may have
several parents (two probes merging) and several daughters.

How a link is found, once per story (cached until its headline changes): the published stories
that came before it are ranked by word overlap of their headlines and summaries; the top few go
to a cheap model with one question: is the new story a later development of the SAME specific case
or event? A wrong link would put one story's context into another, so the answer must be yes and
the two must share a specific name (person, place, organisation or case).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

from .db import Store, published, select, stories, story_links, update, utcnow, insert, delete
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

PROMPT = """A news site has a NEW story and some EARLIER stories. For each earlier story, decide if the new
story is a later development of the SAME specific case, incident or decision (for example: an arrest,
a court hearing, an investigation step, a reaction or protest about that very incident). A different
event that is only on the same broad topic is NOT a development.

NEW: {new}

EARLIER:
{earlier}

Reply with JSON only: {{"developments": [2]}}  (the numbers of the earlier stories it develops; [] if none)"""

STOP = set("""the a an of in on at to for from by with and or after over amid as is are was were be has have had
its his her their this that new says said police govt government india indian state minister chief
reports report day two three four five protest case court""".split())


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in STOP}


def _names(s: str) -> set[str]:
    """Capitalised words and acronyms: the specific names a real link must share."""
    return {w.lower() for w in re.findall(r"\b(?:[A-Z][a-z]{2,}|[A-Z]{2,})\b", s or "") if w.lower() not in STOP}


def _summary(row: dict) -> str:
    p = row["payload_en"] or {}
    first = ""
    paras = (p.get("narrative") or {}).get("paragraphs") or []
    if paras and paras[0]:
        first = " ".join(x["text"] for x in paras[0][:2])
    return f'{p.get("headline") or row.get("headline_en") or ""}. {first}'[:500]


def find_parents(store: Store, router: Router | None, story_id: int, headline: str, summary: str,
                 max_candidates: int = 6, window_days: int = 60) -> list[int]:
    story = store.one(select(stories.c.analysis, stories.c.created_at).where(stories.c.id == story_id))
    if not story or router is None:
        return [r["parent_id"] for r in store.rows(select(story_links.c.parent_id).where(story_links.c.child_id == story_id))]
    analysis = dict(story["analysis"] or {})
    if analysis.get("thread_checked") == headline:
        return [r["parent_id"] for r in store.rows(select(story_links.c.parent_id).where(story_links.c.child_id == story_id))]
    since = utcnow() - dt.timedelta(days=window_days)
    rows = store.rows(select(published.c.story_id, published.c.headline_en, published.c.payload_en, published.c.updated_at)
                      .where(published.c.story_id != story_id, published.c.updated_at >= since))
    created = {r["id"]: r["created_at"] for r in store.rows(select(stories.c.id, stories.c.created_at)
                                                             .where(stories.c.id.in_([r["story_id"] for r in rows] or [-1])))}
    mine = _words(headline + " " + summary)
    my_names = _names(headline + " " + summary)
    cands = []
    for r in rows:
        if created.get(r["story_id"]) and story["created_at"] and created[r["story_id"]] >= story["created_at"]:
            continue   # a parent came first
        text = _summary(r)
        overlap = len(mine & _words(text))
        shared = my_names & _names(text)
        if overlap >= 3 and shared:
            cands.append((overlap, r["story_id"], text, shared))
    cands.sort(reverse=True)
    cands = cands[:max_candidates]
    found: list[int] = []
    if cands:
        earlier = "\n".join(f"{k + 1}. {t}" for k, (_, _, t, _) in enumerate(cands))
        try:
            res = router.call("light", PROMPT.format(new=f"{headline}. {summary}"[:600], earlier=earlier),
                              json_out=True, max_output_tokens=100)
            for n in (res.data or {}).get("developments", []) if isinstance(res.data, dict) else []:
                try:
                    n = int(n) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= n < len(cands):
                    found.append(cands[n][1])
        except QuotaExhausted:
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("thread check failed for story %s: %s", story_id, str(e)[:200])
            return []
    existing = {r["parent_id"] for r in store.rows(select(story_links.c.parent_id).where(story_links.c.child_id == story_id))}
    for pid in found:
        if pid not in existing:
            store.exec(insert(story_links).values(parent_id=pid, child_id=story_id, created_at=utcnow(),
                                                  reason=json.dumps(sorted(next(c[3] for c in cands if c[1] == pid)))))
            # the parent's page gains a "what happened next" link
            store.exec(update(stories).where(stories.c.id == pid).values(dirty=True))
    analysis["thread_checked"] = headline
    store.exec(update(stories).where(stories.c.id == story_id).values(analysis=analysis))
    if found:
        log.info("threads: story %s develops %s", story_id, found)
    return sorted(existing | set(found))


def relatives(store: Store, story_id: int) -> tuple[list[int], list[int]]:
    parents = [r["parent_id"] for r in store.rows(select(story_links.c.parent_id).where(story_links.c.child_id == story_id))]
    children = [r["child_id"] for r in store.rows(select(story_links.c.child_id).where(story_links.c.parent_id == story_id))]
    return parents, children


def root_of(store: Store, story_id: int, depth: int = 20) -> int:
    """The earliest ancestor (the thread's first story); with several parents, the oldest line."""
    seen = {story_id}
    cur = story_id
    for _ in range(depth):
        ps = sorted(r["parent_id"] for r in store.rows(select(story_links.c.parent_id).where(story_links.c.child_id == cur)))
        ps = [p for p in ps if p not in seen]
        if not ps:
            return cur
        cur = ps[0]
        seen.add(cur)
    return cur


def drop_links_of(store: Store, story_id: int) -> None:
    store.exec(delete(story_links).where((story_links.c.parent_id == story_id) | (story_links.c.child_id == story_id)))
