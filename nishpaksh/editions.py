"""Editions: when an article is written, and when later reporting earns a follow-up (owner, Oct 5 2026).

Like a newspaper:
  * An article is written ONCE, when its coverage has settled: the publishing rule has been met and no
    new independent outlet has turned up for `settle_quiet_hours` (or `settle_max_hours` have passed
    since the rule was first met, so a story that keeps growing is still written the same day).
    Measured on Oct 1-5 data: a 3-hour quiet wait sees 86% of the outlets a story ever gets, about
    4 hours after the rule is met.
  * A published article is closed. Its text, headline and statements never change; no model call is
    spent on it again; no new report joins it. Only its colours mature, by code (`mature`): the
    6-hour clock turns "developing" into "established" from the evidence it was written with.
  * Reports about a published event that arrive later gather in a fresh candidate story (grouping
    redirects them, stories.py). The candidate is read like any story, and is published as a
    follow-up only if, against its parent, it carries a lot of new information or a major
    development (`follow_up_ok`). On the parent's own date only a major development carried by
    `followup_same_day_outlets` independent outlets will do: one article per topic per day.
  * A follow-up says what the parent already disputed or got wrong itself; the parent is never touched.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
from zoneinfo import ZoneInfo

from .config import SETTINGS
from .db import Store, articles, published, select, stories, story_links, insert, update, utcnow
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

MAJOR_KINDS = {
    "arrest": "an arrest or detention",
    "charges": "an FIR registered or charges filed",
    "court": "a court order, verdict or bail decision",
    "deaths": "deaths, or a sharp rise in the number of dead or injured",
    "resignation": "a resignation, sacking or suspension",
    "official_decision": "an official decision or order (government, regulator, police, election body)",
    "result": "a result: an election or vote result, an inquiry or probe report",
}

FOLLOWUP_PROMPT = """A news site published an article about an event. Later reports about the same event have come in.
Decide, for each LATER statement, whether the EARLIER article already says it.

EARLIER ARTICLE (published {date}): {headline}
{earlier}

LATER STATEMENTS:
{later}

For each later statement:
- "new": false if the earlier article already says the same thing, even in other words or with a small
  change in a detail; true only if it adds information the earlier article does not have.
- "major": for a NEW statement only, the kind of development it reports, if it is one of these:
{kinds}
  otherwise "none". A statement that only repeats, explains, reacts to or comments on the event is "none".

Reply with JSON only: {{"items": [{{"n": 1, "new": true, "major": "arrest"}}, {{"n": 2, "new": false, "major": "none"}}]}}"""


def _parse(ts) -> dt.datetime | None:
    if isinstance(ts, dt.datetime):
        return ts
    try:
        return dt.datetime.fromisoformat(str(ts)) if ts else None
    except ValueError:
        return None


def ist_date(ts: dt.datetime) -> dt.date:
    """The Indian calendar date of a naive UTC time."""
    return ts.replace(tzinfo=dt.timezone.utc).astimezone(IST).date()


def frozen_ids(store: Store) -> set[int]:
    """Published stories: closed for good."""
    return {r["story_id"] for r in store.rows(select(published.c.story_id))}


def _edition(store: Store, sid: int) -> tuple[dict, dict]:
    row = store.one(select(stories.c.analysis).where(stories.c.id == sid)) or {}
    an = dict(row.get("analysis") or {})
    return an, dict(an.get("edition") or {})


def _save(store: Store, sid: int, an: dict, ed: dict) -> None:
    an["edition"] = ed
    store.exec(update(stories).where(stories.c.id == sid).values(analysis=an))


# ---------------------------------------------------------------------------- settling

def note_rule(store: Store, qualifying: set[int], candidates: list[int], now: dt.datetime | None = None) -> None:
    """Record when each story first met the publishing rule (the 8-hour cap counts from it); a story
    that stops meeting it starts again."""
    now = now or utcnow()
    for sid in candidates:
        an, ed = _edition(store, sid)
        if sid in qualifying and not ed.get("met_at"):
            ed["met_at"] = now.isoformat(timespec="minutes")
        elif sid not in qualifying and ed.get("met_at"):
            ed.pop("met_at")
        else:
            continue
        _save(store, sid, an, ed)


def last_new_source(store: Store, sid: int) -> dt.datetime | None:
    """When the most recent independent source first turned up (when we fetched it: an outlet found
    late by search counts as new coverage for us)."""
    from .wire import independence_groups
    arts = store.rows(select(articles.c.id, articles.c.outlet, articles.c.url, articles.c.agency,
                             articles.c.wire_group, articles.c.fetched_at, articles.c.published_at)
                      .where(articles.c.story_id == sid))
    if not arts:
        return None
    groups = independence_groups(arts)
    first: dict = {}
    for a in arts:
        t = a["fetched_at"] or a["published_at"]
        if t is None:
            continue
        g = groups[a["id"]]
        first[g] = min(first.get(g, t), t)
    return max(first.values()) if first else None


def settled(store: Store, sid: int, now: dt.datetime | None = None) -> bool:
    """Ready to be written: coverage has gone quiet, or the cap has passed. Old news is not written at
    all (a story whose newest source is older than `stale_after_hours`: a paper does not print
    Tuesday's story on Friday)."""
    now = now or utcnow()
    last = last_new_source(store, sid)
    if last is None or (now - last).total_seconds() >= SETTINGS.stale_after_hours * 3600:
        return False
    _, ed = _edition(store, sid)
    met = _parse(ed.get("met_at"))
    if met and (now - met).total_seconds() >= SETTINGS.settle_max_hours * 3600:
        return True
    return (now - last).total_seconds() >= SETTINGS.settle_quiet_hours * 3600


# ---------------------------------------------------------------------------- follow-ups

def parent_pages(store: Store, ids: list[int]) -> dict[int, dict]:
    """{story_id: {"headline_en", "payload_en", "written_at"}} for live parents, and for parents already
    moved to the archive branch."""
    from . import pagearchive
    out = {r["story_id"]: {"headline_en": r["headline_en"], "payload_en": r["payload_en"] or {},
                           "written_at": (r["payload_en"] or {}).get("written_at") or r["updated_at"].isoformat()}
           for r in store.rows(select(published.c.story_id, published.c.headline_en, published.c.payload_en,
                                      published.c.updated_at).where(published.c.story_id.in_(ids or [-1])))}
    for sid in ids:
        if sid not in out:
            page = pagearchive.fetch_page(sid)
            if page:
                out[sid] = {"headline_en": page.get("headline_en"), "payload_en": page.get("payload_en") or {},
                            "written_at": page.get("written_at")}
    return out


def link_candidate(store: Store, sid: int) -> None:
    """A candidate made of later reports about a published story is a development of it."""
    _, ed = _edition(store, sid)
    parent = ed.get("follows")
    if not parent:
        return
    have = store.one(select(story_links.c.parent_id).where(story_links.c.parent_id == parent,
                                                           story_links.c.child_id == sid))
    if not have:
        store.exec(insert(story_links).values(parent_id=parent, child_id=sid, created_at=utcnow(),
                                              reason=json.dumps(["later reports of the same event"])))


def _all_items(p: dict) -> list[dict]:
    return ([i for tier in p.get("timeline") or [] for i in tier] + list(p.get("undated") or [])
            + list(p.get("established") or []) + list(p.get("contested") or []) + list(p.get("context") or []))


def _core_major(p: dict) -> list[dict]:
    """Statements of the story's own event that are not minor (no relations: they restate others)."""
    return [i for i in _all_items(p) if not i.get("minor") and (i.get("role") or "core") == "core"
            and i.get("kind") != "relation"]


def _classify(router: Router, parent: dict, items: list[dict]) -> dict[int, dict] | None:
    pp = parent["payload_en"]
    earlier = "\n".join(f"- {i['text']}" for i in _all_items(pp) if i.get("kind") != "relation")[:6000]
    later = "\n".join(f"{n + 1}. {i['text']}" for n, i in enumerate(items))
    kinds = "\n".join(f"  {k}: {v}" for k, v in MAJOR_KINDS.items())
    written = _parse(parent.get("written_at"))
    prompt = FOLLOWUP_PROMPT.format(date=ist_date(written).isoformat() if written else "earlier",
                                    headline=parent.get("headline_en") or pp.get("headline") or "",
                                    earlier=earlier, later=later, kinds=kinds)
    try:
        res = router.call("page", prompt, json_out=True, max_output_tokens=1500)
    except QuotaExhausted:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("follow-up check failed: %s", str(e)[:200])
        return None
    rows = (res.data or {}).get("items") if isinstance(res.data, dict) else None
    if not isinstance(rows, list):
        return None
    out: dict[int, dict] = {}
    for r in rows:
        try:
            n = int(r.get("n")) - 1
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= n < len(items):
            new = r.get("new") is True
            major = r.get("major") if new and r.get("major") in MAJOR_KINDS else None
            out[items[n]["id"]] = {"new": new, "major": major}
    # an unanswered statement counts as not new: when unsure, no follow-up
    return {i["id"]: out.get(i["id"], {"new": False, "major": None}) for i in items}


def follow_up_ok(store: Store, router: Router | None, sid: int, payload: dict, parent_ids: list[int],
                 now: dt.datetime | None = None) -> bool:
    """Is this story worth publishing as a follow-up of its (latest) parent? Either a lot of new
    information (`followup_min_new` new non-minor statements, or `followup_new_share` of the parent's,
    whichever is more) carried by `followup_min_outlets` independent outlets, or a major development
    carried by that many. On the parent's own IST date: a major development only, carried by
    `followup_same_day_outlets`. The decision is kept per statement set, so it costs one cheap model
    call per change in the story, not one per run."""
    from .verify import _story_context, support_summary
    now = now or utcnow()
    parents = parent_pages(store, parent_ids)
    if not parents:
        return True     # the parents are gone from everywhere: judged as a story of its own
    pid, parent = max(parents.items(), key=lambda kv: str(kv[1].get("written_at") or ""))
    items = _core_major(payload)
    an, ed = _edition(store, sid)
    key = hashlib.sha256(json.dumps([pid, sorted(i["id"] for i in items)]).encode()).hexdigest()[:16]
    rec = ed.get("followup") or {}
    if rec.get("key") != key:
        if router is None or not items:
            return False
        judged = _classify(router, parent, items)
        if judged is None:
            return False                     # no answer: wait, ask again next run
        rec = {"key": key, "parent": pid, "judged": {str(k): v for k, v in judged.items()}}
    judged = {int(k): v for k, v in (rec.get("judged") or {}).items()}

    _, agroup, gpersp, _, _, members = _story_context(store, sid)
    groups = {i["id"]: set(support_summary(i["id"], members, agroup, gpersp)["support_groups"]) for i in items}
    new = [i["id"] for i in items if (judged.get(i["id"]) or {}).get("new")]
    carried = set().union(*(groups[x] for x in new)) if new else set()
    major_reach = max((len(groups[x]) for x in new if (judged.get(x) or {}).get("major")), default=0)
    need_new = max(SETTINGS.followup_min_new,
                   math.ceil(SETTINGS.followup_new_share * len(_core_major(parent["payload_en"]))))
    written = _parse(parent.get("written_at"))
    same_day = written is not None and ist_date(written) == ist_date(now)
    if same_day:
        ok = major_reach >= SETTINGS.followup_same_day_outlets
    else:
        ok = ((len(new) >= need_new and len(carried) >= SETTINGS.followup_min_outlets)
              or major_reach >= SETTINGS.followup_min_outlets)
    rec.update(new=len(new), need_new=need_new, outlets=len(carried), major_outlets=major_reach,
               same_day=same_day, ok=ok, at=now.isoformat(timespec="minutes"))
    _save(store, sid, an, dict(ed, followup=rec))
    log.info("story %s as a follow-up of %s: %s (%d new of %d needed, %d outlets, major in %d, same day %s)",
             sid, pid, "yes" if ok else "not yet", len(new), need_new, len(carried), major_reach, same_day)
    return ok


# ---------------------------------------------------------------------------- published pages

def mature(store: Store, sid: int) -> bool:
    """Colours of a published article catch up with the clock, by code: verdicts are recomputed from
    the evidence the story already holds, and each sentence and statement takes its current colour.
    Text, headline, order and sections never change. Returns True if a colour changed."""
    from .db import canonical
    from .narrative import CLASS, RANK
    from .verify import base_verdicts
    base_verdicts(store, sid)
    verdict = {r["id"]: r["verdict"] for r in store.rows(select(canonical.c.id, canonical.c.verdict)
                                                         .where(canonical.c.story_id == sid))}
    row = store.one(select(published.c.payload_en, published.c.payload_hi).where(published.c.story_id == sid))
    if not row:
        return False
    changed = False
    out = {}
    for col in ("payload_en", "payload_hi"):
        p = json.loads(json.dumps(row[col])) if row[col] else None
        if not p:
            continue
        by_id = {}
        for i in _all_items(p):
            v = verdict.get(i.get("id"))
            if v and i.get("verdict") != v:
                i["verdict"] = v
                changed = True
            by_id[i.get("id")] = i
        for para in (p.get("narrative") or {}).get("paragraphs") or []:
            for s in para:
                ranks = [RANK.get((by_id.get(x) or {}).get("verdict"), 2) for x in s.get("ids") or [] if x in by_id]
                if ranks and s.get("class") != CLASS[max(ranks)]:
                    s["class"] = CLASS[max(ranks)]
                    changed = True
        est = any(i.get("verdict") in ("corroborated", "confirmed") for i in by_id.values())
        if est and not p.get("has_established"):
            p["has_established"] = True      # the "nothing confirmed yet" note goes
            changed = True
        out[col] = p
    if changed:
        store.exec(update(published).where(published.c.story_id == sid).values(**out))
    return changed
