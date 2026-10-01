"""Stage 3: read each article with Gemma and record what it says, neutrally.

The model does no judging here. It transcribes events, claims, times, stated
relations and the emotive words the article used. Everything else is code.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

from .config import SETTINGS
from .db import Store, articles, claims, delete, insert, select, update, utcnow
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

STANCES = {"asserts", "attributes", "denies"}
EVIDENCE = {"fir", "court_record", "official_data", "video", "official_statement",
            "named_witness", "unnamed_source", "none"}
PRECISION = {"exact", "hour", "part_of_day", "day", "week", "month", "unknown"}
REL_TYPES = {"before", "caused"}

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
3. Times: copy the article's time words into "when_text". Fill start/end ONLY when they follow from the
   text and the publish date (for example "Tuesday night" -> that Tuesday 18:00 to 23:59). Never guess.
   If unclear use null and precision "unknown".
4. stance: "asserts" = the article states it as fact in its own voice; "attributes" = the article reports
   that someone else says it; "denies" = the article says it did not happen or is untrue. For "denies",
   write "text" as the positive statement being denied (text "X insulted deities", stance "denies").
5. attributed_to: who the article gives as the source ("article", "police", "victim's family", "accused",
   "eyewitness", "minister", "unnamed source", ...).
6. evidence: what the article cites: fir | court_record | official_data | video | official_statement |
   named_witness | unnamed_source | none.
7. Record every factual detail, including small ones. One fact per item; do not merge.
8. Reply with the JSON only.
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
    out = {"signature": str(data.get("signature") or "").strip(), "events": [], "claims": [], "relations": []}
    ids = set()
    for kind in ("events", "claims"):
        for i, it in enumerate(data.get(kind) or []):
            if not isinstance(it, dict) or not str(it.get("text") or "").strip():
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
            if kind == "events":
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
    for i, r in enumerate(ex["relations"]):
        rows.append(dict(story_id=story_id, article_id=article_id, local_id=f"r{i + 1}", kind="relation",
                         text="", stance=r["stance"], attributed_to=r["attributed_to"], evidence="none",
                         loaded_words=[], time=None, rel={"from": r["from"], "to": r["to"], "type": r["type"]}))
    if rows:
        with store.engine.begin() as c:
            c.execute(insert(claims), rows)


def extract_pending(store: Store, router: Router, deadline: float) -> int:
    pending = store.rows(
        select(articles.c.id, articles.c.outlet, articles.c.title, articles.c.text, articles.c.published_at,
               articles.c.extract_failures)
        .where(articles.c.extracted_at.is_(None), articles.c.text.is_not(None), articles.c.extract_failures < 3)
        .order_by(articles.c.published_at.desc())
    )
    done = 0
    for a in pending:
        if time.time() > deadline:
            log.info("extract: time budget reached with %d pending", len(pending) - done)
            break
        prompt = EXTRACT_PROMPT.format(outlet=a["outlet"], published=_fmt_ist(a["published_at"]),
                                       title=a["title"] or "", text=(a["text"] or "")[: SETTINGS.max_article_chars])
        try:
            res = router.call("bulk", prompt, json_out=True, max_output_tokens=3000)
            ex = normalize_extraction(res.data)
            if ex is None:
                raise ValueError("empty extraction")
        except QuotaExhausted as e:
            log.info("extract: quota exhausted (%s); %d left for next run", e, len(pending) - done)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("extract failed for article %s: %s", a["id"], e)
            store.exec(update(articles).where(articles.c.id == a["id"])
                       .values(extract_failures=(a["extract_failures"] or 0) + 1))
            continue
        store_extraction(store, a["id"], None, ex)
        store.exec(update(articles).where(articles.c.id == a["id"]).values(
            extraction={"model": res.model}, extracted_at=utcnow(), signature=ex["signature"]))
        done += 1
    return done
