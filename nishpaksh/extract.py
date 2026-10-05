"""Stage 3: read each article with Gemma and record what it says, neutrally.

The model does no judging here. It transcribes events, claims, times, stated
relations and the emotive words the article used. Everything else is code.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from zoneinfo import ZoneInfo

from .config import SETTINGS
from .db import Store, articles, claims, delete, insert, select, stories, update, utcnow
from .router import CallFailed, QuotaExhausted, Router

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

STANCES = {"asserts", "attributes", "denies"}
ROUNDUP = re.compile(r"(?i)\blive\b.*\bupdates?\b|\blive\s*(blog|updates?|news)\b|^live[:|\s]|\btop (news|headlines|stories)\b"
                     r"|\bnews (wrap|roundup|highlights|bulletin)\b|\bmorning brief\b|\bevening brief\b|\bnews live\b"
                     r"|लाइव|ताजा खबर|बड़ी खबरें|टॉप न्यूज|न्यूज़ अपडेट")
EVIDENCE = {"fir", "court_record", "official_data", "video", "official_statement",
            "named_witness", "unnamed_source", "none"}
PRECISION = {"exact", "hour", "part_of_day", "day", "week", "month", "unknown"}
REL_TYPES = {"before", "caused"}
CONTEXT_TYPES = {"background", "related", "explanation", "reaction", "next"}

EXTRACT_PROMPT = """You are a careful annotator. You do not judge truth, take sides, or add facts.
You only record what THIS article says, in neutral language.

ARTICLE
Outlet: {outlet}
Published: {published} (India time)
Title: {title}
Text:
\"\"\"{text}\"\"\"

Return ONE JSON object with exactly these keys:
{{
  "signature": "one neutral English sentence naming the core event: who, what, where",
  "events": [
    {{"id": "e1", "text": "...", "when_text": "...", "start": "YYYY-MM-DDTHH:MM or null",
      "end": "YYYY-MM-DDTHH:MM or null", "precision": "exact|hour|part_of_day|day|week|month|unknown",
      "stance": "asserts|attributes|denies", "attributed_to": "...", "evidence": "...", "loaded_words": ["..."]}}
  ],
  "claims": [
    {{"id": "c1", "text": "...", "stance": "asserts|attributes|denies", "attributed_to": "...",
      "evidence": "...", "loaded_words": ["..."]}}
  ],
  "relations": [
    {{"from": "e1", "to": "e2", "type": "before|caused", "stance": "asserts|attributes|denies", "attributed_to": "..."}}
  ],
  "context": [
    {{"id": "x1", "type": "background|related|explanation|reaction|next", "text": "...", "event": "...",
      "when_text": "...", "start": "YYYY-MM-DDTHH:MM or null", "end": "YYYY-MM-DDTHH:MM or null",
      "precision": "exact|hour|part_of_day|day|week|month|unknown",
      "stance": "asserts|attributes|denies", "attributed_to": "...", "evidence": "...", "loaded_words": ["..."]}}
  ]
}}

Definitions:
- events: things that happened (an action, an arrest, a statement being made, a death).
- claims: statements that are not events themselves: numbers, identities, motives, accusations,
  causes, official positions, background facts.
- relations: ONLY when the article explicitly says one event came before, or caused / was in
  response to, another. "from" is the earlier or causing event.

Rules:
1. Neutral wording. Describe actions with plain verbs (struck, said, arrested, died). Move every
   emotive or judging word (for example: lynched, mob, brutal, so-called, terrorist, provoked, clashed,
   hate, radical, thug, martyr) out of "text" and into "loaded_words", copied exactly as the article
   wrote it, in the article's own language.
2. Write "text" in English even when the article is in Hindi. Keep names as written.
3. Times: write the article's time words in English in "when_text" ("26 September", "Tuesday night"). Fill start/end ONLY when they follow from the
   text and the publish date (for example "Tuesday night" -> that Tuesday 18:00 to 23:59). Never guess.
   If unclear use null and precision "unknown".
4. stance: "asserts" = the article states it as fact in its own voice; "attributes" = the article reports
   that someone else says it; "denies" = the article says it did not happen or is untrue. For "denies",
   write "text" as the positive statement being denied (text "X insulted deities", stance "denies").
   An allegation, accusation, charge or claim that someone makes is ALWAYS "attributes" with that
   person as attributed_to, even when the article words it in its own voice (an article writing
   "the professor harassed the student" while reporting the family's complaint -> stance "attributes",
   attributed_to "the student's family"). Use "asserts" only for what the article itself establishes.
5. attributed_to: who the article gives as the source ("article", "police", "victim's family", "accused",
   "eyewitness", "minister", "unnamed source", ...). Write it in English.
6. evidence: what the article cites: fir | court_record | official_data | video | official_statement |
   named_witness | unnamed_source | none.
7. Record every factual detail about the article's core event (the "signature") in events/claims,
   including small ones. One fact per item; do not merge.
8. Record in "context" what the article tells around the core event, so a reader understands it:
   "background" = earlier events that led to this one; "related" = a SEPARATE event the article itself
   connects to this one (put a short name of that event, with its date if given, in "event", e.g.
   "FSSAI finding on Nestle dairy whitener, 4 October"); "explanation" = what a rule, term, number or
   finding means; "reaction" = a person's or body's response not already recorded as a claim;
   "next" = what happens next (deadlines, hearings, required steps). Same rules for wording,
   stance and attribution. If the article is a roundup or live blog of UNRELATED news, ignore the
   unrelated items entirely: context is only what the article connects to the core event.
9. Reply with the JSON only.
"""


def _fmt_ist(t: dt.datetime | None) -> str:
    if not t:
        return "unknown"
    return t.replace(tzinfo=dt.timezone.utc).astimezone(IST).strftime("%A %d %B %Y, %H:%M")


def _iso_or_none(v) -> str | None:
    if not v or not isinstance(v, str) or v.lower() in {"null", "none", "unknown"}:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(v[:19], fmt).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
    return None


def _words(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(w).strip() for w in v if str(w).strip()][:12]


def normalize_extraction(data: dict) -> dict | None:
    if not isinstance(data, dict):
        return None
    out = {"signature": str(data.get("signature") or "").strip(), "events": [], "claims": [], "relations": [],
           "context": []}
    ids = set()
    for kind in ("events", "claims", "context"):
        for i, it in enumerate(data.get(kind) or []):
            if not isinstance(it, dict) or not str(it.get("text") or "").strip():
                continue
            if kind == "context" and it.get("type") not in CONTEXT_TYPES:
                continue
            local = str(it.get("id") or f"{kind[0]}{i + 1}")
            ids.add(local)
            item = {
                "id": local,
                "text": str(it["text"]).strip()[:500],
                "stance": it.get("stance") if it.get("stance") in STANCES else "asserts",
                "attributed_to": str(it.get("attributed_to") or "article")[:200],
                "evidence": it.get("evidence") if it.get("evidence") in EVIDENCE else "none",
                "loaded_words": _words(it.get("loaded_words")),
            }
            if kind == "context":
                item["context"] = it["type"]
                item["event"] = str(it.get("event") or "").strip()[:160] if it["type"] == "related" else ""
            if kind == "events" or (kind == "context" and it.get("type") in ("background", "related")):
                start, end = _iso_or_none(it.get("start")), _iso_or_none(it.get("end"))
                if start and not end:
                    end = start
                if end and not start:
                    start = end
                if start and end and start > end:
                    start, end = end, start
                prec = it.get("precision") if it.get("precision") in PRECISION else "unknown"
                if not start:
                    prec = "unknown"
                item["time"] = {"when_text": str(it.get("when_text") or "")[:120],
                                "start": start, "end": end, "precision": prec}
            out[kind].append(item)
    for r in data.get("relations") or []:
        if (isinstance(r, dict) and r.get("from") in ids and r.get("to") in ids
                and r.get("from") != r.get("to") and r.get("type") in REL_TYPES):
            out["relations"].append({
                "from": r["from"], "to": r["to"], "type": r["type"],
                "stance": r.get("stance") if r.get("stance") in STANCES else "asserts",
                "attributed_to": str(r.get("attributed_to") or "article")[:200],
            })
    if not out["signature"] and out["events"]:
        out["signature"] = out["events"][0]["text"]
    if not out["signature"]:
        return None
    return out


def store_extraction(store: Store, article_id: int, story_id: int | None, ex: dict) -> None:
    store.exec(delete(claims).where(claims.c.article_id == article_id))
    rows = []
    for kind, key in (("event", "events"), ("claim", "claims")):
        for it in ex[key]:
            rows.append(dict(story_id=story_id, article_id=article_id, local_id=it["id"], kind=kind,
                             text=it["text"], stance=it["stance"], attributed_to=it["attributed_to"],
                             evidence=it["evidence"], loaded_words=it["loaded_words"],
                             time=it.get("time"), rel=None))
    for it in ex.get("context") or []:
        # context is stored like a statement, marked in `rel` (unused for statements): background and
        # related events are events (they have times), the rest are claims
        kind = "event" if it["context"] in ("background", "related") else "claim"
        rows.append(dict(story_id=story_id, article_id=article_id, local_id=it["id"], kind=kind,
                         text=it["text"], stance=it["stance"], attributed_to=it["attributed_to"],
                         evidence=it["evidence"], loaded_words=it["loaded_words"], time=it.get("time"),
                         rel={"context": it["context"], "event": it.get("event") or ""}))
    for i, r in enumerate(ex["relations"]):
        rows.append(dict(story_id=story_id, article_id=article_id, local_id=f"r{i + 1}", kind="relation",
                         text="", stance=r["stance"], attributed_to=r["attributed_to"], evidence="none",
                         loaded_words=[], time=None, rel={"from": r["from"], "to": r["to"], "type": r["type"]}))
    if rows:
        with store.engine.begin() as c:
            c.execute(insert(claims), rows)


def _extract_one(store: Store, router: Router, a: dict) -> str:
    prompt = EXTRACT_PROMPT.format(outlet=a["outlet"], published=_fmt_ist(a["published_at"]),
                                   title=a["title"] or "", text=(a["text"] or "")[: SETTINGS.max_article_chars])
    try:
        res = router.call("bulk", prompt, json_out=True, max_output_tokens=4000)
        ex = normalize_extraction(res.data)
        if ex is None:
            raise ValueError("empty extraction")
    except QuotaExhausted:
        return "quota"
    except CallFailed as e:
        # the model was overloaded: not the article's fault, so it keeps all its tries
        log.info("extract: model unavailable for article %s (%s)", a["id"], str(e)[:120])
        return "failed"
    except Exception as e:  # noqa: BLE001
        log.warning("extract failed for article %s: %s", a["id"], str(e)[:200])
        store.exec(update(articles).where(articles.c.id == a["id"])
                   .values(extract_failures=(a["extract_failures"] or 0) + 1))
        return "failed"
    store_extraction(store, a["id"], a.get("story_id"), ex)
    store.exec(update(articles).where(articles.c.id == a["id"]).values(
        extraction={"model": res.model}, extracted_at=utcnow(), signature=ex["signature"]))
    if a.get("story_id"):
        story = store.one(select(stories.c.signature).where(stories.c.id == a["story_id"]))
        values = {"dirty": True, "updated_at": utcnow()}
        if story and (not story["signature"] or story["signature"] == a["title"]):
            values["signature"] = ex["signature"]  # neutral English one-liner beats a headline
        store.exec(update(stories).where(stories.c.id == a["story_id"]).values(**values))
    return "done"


def select_for_extraction(store: Store) -> list[dict]:
    """Which articles are worth an LLM call. A story only one source covers can never be
    published and teaches the perspective model nothing, so it waits until a second
    independent source appears. Within a story only one article per independent source is
    read (a wire copy repeats its original), up to `max_extract_per_story` sources."""
    from .wire import independence_groups
    rows = store.rows(
        select(articles.c.id, articles.c.outlet, articles.c.url, articles.c.agency, articles.c.wire_group,
               articles.c.story_id, articles.c.title, articles.c.text, articles.c.published_at,
               articles.c.extract_failures, articles.c.extracted_at, articles.c.text_source)
        .where(articles.c.story_id.is_not(None), articles.c.text.is_not(None))
    )
    by_story: dict[int, list[dict]] = {}
    for r in rows:
        if ROUNDUP.search(r["title"] or ""):
            continue   # live blogs and news roundups mix unrelated events into one story
        by_story.setdefault(r["story_id"], []).append(r)
    ranked = []
    for sid, arts in by_story.items():
        groups = independence_groups(arts)
        # read in depth (owner, Oct 2026): a story is read only once 3+ independent sources have a
        # readable page. Under 3 it can never be published, and omissions (the main evidence of a
        # perspective) can only be seen when several outlets' versions of one story are read.
        readable = {groups[a["id"]] for a in arts if a["text_source"] != "summary"}
        if len(readable) < SETTINGS.min_sources_to_read:
            continue
        done = [a for a in arts if a["extracted_at"]]
        room = SETTINGS.max_extract_per_story - len(done)
        if room <= 0:
            continue
        seen_groups = {groups[a["id"]] for a in done}
        # one article per independent source: a wire copy or a second piece from the same outlet
        # would only repeat what that source already said
        # option B: a page we could not read (headline and blurb only) is never read for facts
        todo = [a for a in arts if not a["extracted_at"] and (a["extract_failures"] or 0) < 3
                and groups[a["id"]] not in seen_groups and a["text_source"] != "summary"]
        todo.sort(key=lambda a: (-(a["published_at"].timestamp()), -len(a["text"] or "")))
        picked = []
        for a in todo:
            if len(picked) >= room:
                break
            if groups[a["id"]] in seen_groups:
                continue
            picked.append(a)
            seen_groups.add(groups[a["id"]])
        if picked:
            newest = max(a["published_at"] for a in arts)
            # finish stories already being read before starting new ones, then the most covered
            ranked.append((len(seen_groups - {groups[a["id"]] for a in picked}) > 0, len(readable), newest, picked))
    ranked.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [a for *_, picked in ranked for a in picked]


def retract_unreadable(store: Store) -> int:
    """Option B, applied to the past: articles read from a headline and blurb only (before this rule
    existed) lose their extracted statements, and their stories are re-analysed without them."""
    rows = store.rows(select(articles.c.id, articles.c.story_id)
                      .where(articles.c.text_source == "summary", articles.c.extracted_at.is_not(None)))
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    sids = sorted({r["story_id"] for r in rows if r["story_id"] is not None})
    for i in range(0, len(ids), 500):
        store.exec(delete(claims).where(claims.c.article_id.in_(ids[i:i + 500])))
        store.exec(update(articles).where(articles.c.id.in_(ids[i:i + 500])).values(extracted_at=None, extraction=None))
    for i in range(0, len(sids), 500):
        store.exec(update(stories).where(stories.c.id.in_(sids[i:i + 500])).values(dirty=True))
    log.info("extract: retracted statements from %d unreadable (headline-only) articles in %d stories", len(ids), len(sids))
    return len(ids)


def read_blocked_pages(store: Store, tavily, max_pages: int = 5) -> int:
    """Pages our fetcher could not read, in stories worth reading, fetched through Tavily: at most
    one per independent source per story, stories closest to publishable first."""
    if tavily is None or not tavily.enabled or max_pages <= 0:
        return 0
    from .wire import independence_groups
    rows = store.rows(select(articles.c.id, articles.c.outlet, articles.c.url, articles.c.agency, articles.c.wire_group,
                             articles.c.story_id, articles.c.text_source, articles.c.extracted_at, articles.c.published_at,
                             articles.c.extract_failures, articles.c.author)
                      .where(articles.c.story_id.is_not(None), articles.c.published_at >= utcnow() - dt.timedelta(hours=48)))
    by_story: dict[int, list[dict]] = {}
    for r in rows:
        by_story.setdefault(r["story_id"], []).append(r)
    wanted: list[tuple[int, dt.datetime, dict]] = []
    for sid, arts in by_story.items():
        groups = independence_groups(arts)
        if len(set(groups.values())) < 2:
            continue
        readable = {groups[a["id"]] for a in arts if a["text_source"] != "summary"}
        tried = set()
        for a in sorted(arts, key=lambda a: a["published_at"], reverse=True):
            g = groups[a["id"]]
            # (extract_failures marks a page Tavily also failed on: not retried)
            if a["text_source"] == "summary" and g not in readable and g not in tried and not (a["extract_failures"] or 0):
                tried.add(g)
                wanted.append((len(readable), max(x["published_at"] for x in arts), a))
    # a story that already has readable sources gains most from one more
    wanted.sort(key=lambda t: (t[0], t[1]), reverse=True)
    pick = [a for _, _, a in wanted[:max_pages]]
    if not pick:
        return 0
    got = tavily.extract([a["url"] for a in pick])
    n = 0
    for a in pick:
        text = re.sub(r"\n{3,}", "\n\n", (got.get(a["url"]) or "")).strip()
        if len(text) >= SETTINGS.min_full_text_chars:
            from .ingest import detect_agency
            from .wire import minhash
            text = text[: SETTINGS.max_article_chars * 2]
            # the wire fingerprint and agency were computed from the headline; recompute them from
            # the real text, or a PTI copy read this way would pass as an independent outlet
            store.exec(update(articles).where(articles.c.id == a["id"]).values(
                text=text, text_source="tavily", agency=detect_agency(a.get("author"), text),
                minhash=minhash(text), wire_group=None))
            n += 1
        else:
            store.exec(update(articles).where(articles.c.id == a["id"]).values(extract_failures=1))
    log.info("tavily: %d of %d blocked pages read", n, len(pick))
    return n


def extract_pending(store: Store, router: Router, deadline: float, workers: int = 6) -> int:
    """Read the selected articles, several at a time, while the router keeps every model inside
    its limits."""
    pending = select_for_extraction(store)
    if not pending:
        return 0
    stop = threading.Event()
    counts = {"done": 0, "failed": 0, "quota": 0}
    lock = threading.Lock()
    queue = iter(pending)

    def worker():
        while not stop.is_set():
            if time.time() > deadline:
                stop.set()
                return
            with lock:
                a = next(queue, None)
            if a is None:
                return
            outcome = _extract_one(store, router, a)
            with lock:
                counts[outcome] += 1
            if outcome == "quota":
                stop.set()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    left = len(pending) - counts["done"] - counts["failed"]
    log.info("extract: %d done, %d failed, %d left for next run%s", counts["done"], counts["failed"], left,
             " (quota exhausted)" if counts["quota"] else "")
    return counts["done"]
