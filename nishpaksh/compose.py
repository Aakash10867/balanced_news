"""Stage 8b: assemble the article, then translate it.

Structure is decided by code, not by a model:
  timeline        corroborated/confirmed events, ordered by the partial order
  established     corroborated/confirmed statements that are not events
  contested       everything else, each with its verdict (false ones included, tagged)
  framing         the loaded words each perspective used for the same fact
  sources         every article read, with its perspective
The only generated prose is the headline (built from established facts and
rejected if it contains any loaded word) and the Hindi translation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict

from .db import Store, articles, delete, published, select, stories, update, utcnow, insert
from .router import QuotaExhausted, Router
from .timeline import build_timeline
from .verify import _story_context, relation_text, support_summary

log = logging.getLogger(__name__)
ESTABLISHED = {"corroborated", "confirmed"}

HEADLINE_PROMPT = """Write one news headline of at most 14 words in plain English for the story below.
- Be specific: name the main person, place or body, and say what happened.
- Use ONLY the statements given. ESTABLISHED ones may be stated as fact. For the others, hedge only
  the uncertain part ("alleged", "reportedly"), or attribute it ("family alleges ...").
- Do not link two events by cause or sequence ("following", "after", "due to", "amid", "over") unless a
  statement says so. No judging adjectives, no motives, no blame beyond the statements.
- Never start with "Reports", and never write "reports say", "reports emerge" or "reports detail".

Statements, most important first:
{facts}

Reply with JSON only: {{"headline": "..."}}"""

LAZY_HEDGES = re.compile(r"(?i)\breports? (say|says|emerge|emerges|detail|details|indicate|indicates|follow|on)\b|^reports\b")
LINKS = ("following", "after", "due to", "amid", "because", "as a result", "triggered", "led to")

TRANSLATE_PROMPT = """Translate each value of this JSON object into Hindi (Devanagari script).
Translate literally and neutrally: do not add, soften or strengthen anything. Keep names of people,
places and organisations, and all numbers, as they are. Return a JSON object with exactly the same keys.

{payload}"""


def _interval(rows: list[dict]) -> dict:
    starts = [r["time"]["start"] for r in rows if r.get("time") and r["time"].get("start")]
    ends = [r["time"]["end"] for r in rows if r.get("time") and r["time"].get("end")]
    whens = Counter(r["time"]["when_text"] for r in rows if r.get("time") and r["time"].get("when_text"))
    return {"start": min(starts) if starts else None, "end": max(ends) if ends else None,
            "when_text": whens.most_common(1)[0][0] if whens else ""}


def _headline(router: Router | None, facts: list[str], banned: set[str], fallback: str,
              unsettled: list[str] | None = None) -> str:
    """A specific headline. `facts` are established statements, `unsettled` the best-supported
    others. Checked: no loaded word, no lazy 'reports say', no cause/sequence link the statements
    do not make, no number the statements do not have."""
    lines = [f"- ESTABLISHED: {f}" for f in facts[:6]] + [f"- REPORTED: {f}" for f in (unsettled or [])[:8]]
    if router is None or not lines:
        return fallback
    source = " ".join(facts + (unsettled or [])).lower()
    prompt = HEADLINE_PROMPT.format(facts="\n".join(lines))
    for _ in range(2):   # one retry, told what was wrong
        try:
            res = router.call("light", prompt, json_out=True, max_output_tokens=200)
            h = str((res.data or {}).get("headline") or "").strip().strip('"').rstrip(".")
        except (QuotaExhausted, Exception) as e:  # noqa: BLE001
            log.info("headline fallback: %s", e)
            return fallback
        problem = _headline_problem(h, facts, source, banned)
        if not problem:
            return h
        prompt = HEADLINE_PROMPT.format(facts="\n".join(lines)) + (
            f"\n\nYour previous headline \"{h}\" was rejected: {problem}. Write a new one.")
    return fallback


def _headline_problem(h: str, facts: list[str], source: str, banned: set[str]) -> str | None:
    low = h.lower()
    if not h or len(h.split()) > 16:
        return "it must be at most 14 words"
    if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low) for w in banned):
        return "it uses a loaded word"
    if LAZY_HEDGES.search(h):
        return "do not write 'reports say/emerge/detail'; attribute to a person or use 'reportedly'"
    bad = [l for l in LINKS if re.search(rf"\b{l}\b", low) and l not in source]
    if bad:
        return f"it links events with '{bad[0]}', which no statement does"
    if not set(re.findall(r"\d+", h)) <= set(re.findall(r"\d+", source)):
        return "it has a number that is not in the statements"
    if not facts and not any(m in low for m in ("alleg", "reported", "claim", "accus", "say", "said", "denies",
                                                "deny", "question", "probe", "differ", "seek", "demand")):
        return "nothing is established, so it must attribute or hedge (e.g. 'reportedly', 'alleges')"
    return None


def _fallback_headline(facts: list[str], unsettled: list[str], signature: str) -> str:
    """No usable model headline: a short best-supported statement itself (never cut mid-sentence),
    marked as reported if it is not established. Never a raw non-English title."""
    def fits(t):
        return len(t.rstrip(".").split()) <= 14
    for pool, mark in ((facts, ""), (unsettled, ", reportedly")):
        for t in pool[:8]:
            if fits(t):
                return t.rstrip(".") + mark
    if facts or unsettled:
        t = (facts or unsettled)[0].rstrip(".")
        return " ".join(t.split()[:12]) + "…"
    return signature if signature and not re.search(r"[\u0900-\u097f]", signature) else "Developing story"


def build_payload(store: Store, router: Router | None, story_id: int) -> dict | None:
    story, agroup, gpersp, arts, canon, members = _story_context(store, story_id)
    if not story["qualifies"]:
        return None
    full_arts = {a["id"]: a for a in store.rows(
        select(articles.c.id, articles.c.outlet, articles.c.url, articles.c.title, articles.c.lang,
               articles.c.published_at, articles.c.role, articles.c.extracted_at, articles.c.text_source,
               articles.c.found_by).where(articles.c.story_id == story_id))}
    texts = {cid: c["text"] for cid, c in canon.items()}

    speakers = (story["analysis"] or {}).get("speakers") or {}

    def item(cid: int) -> dict:
        c = canon[cid]
        s = support_summary(cid, members, agroup, gpersp)
        srcs, seen = [], set()
        framing = defaultdict(set)
        for r in members.get(cid, []):
            a = full_arts.get(r["article_id"])
            if not a:
                continue
            p = gpersp.get(agroup.get(a["id"])) or "–"
            for w in r["loaded_words"] or []:
                framing[p].add(w)
            if a["url"] in seen:
                continue
            seen.add(a["url"])
            srcs.append({"outlet": a["outlet"], "url": a["url"], "lang": a["lang"], "perspective": p,
                         "stance": r["stance"], "evidence": r["evidence"], "attributed_to": r["attributed_to"]})
        detail = c["detail"] or {}
        check = None
        if detail.get("checked"):
            check = {"outcome": detail.get("outcome"), "checked_at": detail.get("checked_at"),
                     "reasons": [o.get("reason") for o in detail.get("opinions", []) if o.get("reason")],
                     "evidence_urls": sorted({u for o in detail.get("opinions", []) for u in o.get("evidence_urls", [])}),
                     "web_sources": detail.get("web_sources", [])}
        return {
            "id": cid, "kind": c["kind"],
            "text": relation_text(c["rel"], texts) if c["kind"] == "relation" else c["text"],
            "verdict": c["verdict"], "n_sources": len(s["support_groups"]), "n_articles": s["n_articles"],
            "supported_by": s["support_perspectives"], "denied_by": s["deny_perspectives"],
            "conflicts_with": [x for x in (c["conflicts"] or []) if x in canon],
            "time": _interval(members.get(cid, [])) if c["kind"] == "event" else None,
            "framing": {k: sorted(v) for k, v in sorted(framing.items())},
            "sources": sorted(srcs, key=lambda x: (x["perspective"], x["outlet"])),
            "check": check, "minor": s["n_articles"] <= 1,
            "speaker": speakers.get(str(cid)),
            "origins": (c.get("origins") or {}).get("origins", []),
            "n_origins": (c.get("origins") or {}).get("n_origins", 0),
            "n_outlets": (c.get("origins") or {}).get("outlets", 0),
        }

    items = {cid: item(cid) for cid in canon if members.get(cid)}
    est_events = [i for i in items.values() if i["kind"] == "event" and i["verdict"] in ESTABLISHED]
    est_ids = {i["id"] for i in est_events}
    rel_edges = []
    for i in items.values():
        if i["kind"] == "relation" and i["verdict"] in ESTABLISHED:
            rel = canon[i["id"]]["rel"]
            if rel["from"] in est_ids and rel["to"] in est_ids:
                rel_edges.append((rel["from"], rel["to"]))
    tl = build_timeline([{"id": i["id"], "start": i["time"]["start"], "end": i["time"]["end"],
                          "weight": i["n_sources"]} for i in est_events], rel_edges)
    established = sorted([i for i in items.values() if i["kind"] == "claim" and i["verdict"] in ESTABLISHED],
                         key=lambda i: (-i["n_sources"], i["id"]))
    shown = est_ids | {i["id"] for i in established}
    # a stated "before" that the timeline already shows from the clock adds nothing
    tier_of = {n: k for k, tier in enumerate(tl["tiers"]) for n in tier}
    for i in items.values():
        rel = canon[i["id"]]["rel"] if i["kind"] == "relation" else None
        if (rel and rel["type"] == "before" and rel["from"] in tier_of and rel["to"] in tier_of
                and tier_of[rel["from"]] < tier_of[rel["to"]]):
            shown.add(i["id"])
    contested = sorted([i for i in items.values() if i["id"] not in shown
                        and not (i["kind"] == "relation" and i["verdict"] in ESTABLISHED)],
                       key=lambda i: (-i["n_articles"], i["id"]))
    framing = [{"id": i["id"], "text": i["text"], "words": i["framing"]}
               for i in sorted(items.values(), key=lambda i: -i["n_articles"])
               if len([p for p, w in i["framing"].items() if w]) >= 2]

    banned = {w.lower() for i in items.values() for ws in i["framing"].values() for w in ws if len(w) >= 4}
    # headline material: the best-supported facts first, not the first in time (that one is
    # often a vague scene-setter)
    est_all = [items[n] for tier in tl["tiers"] for n in tier] + [items[n] for n in tl["undated"]] + established
    facts = [i["text"] for i in sorted(est_all, key=lambda i: (-i["n_sources"], -i["n_articles"]))]
    unsettled = [i["text"] for i in contested if not i["minor"]][:8] or [i["text"] for i in contested][:8]
    fallback = _fallback_headline(facts, unsettled, story["signature"] or "")
    headline = _headline(router, facts, banned, fallback, unsettled)

    analysis = story["analysis"] or {}
    persp = defaultdict(set)
    for g, info in (analysis.get("groups") or {}).items():
        persp[info.get("perspective") or "–"].update(info.get("outlets", []))
    sources = sorted([{"outlet": a["outlet"], "title": a["title"], "url": a["url"], "lang": a["lang"],
                       "role": a["role"], "perspective": gpersp.get(agroup.get(a["id"])) or "–",
                       # option B: an article we could not read is listed, never used for facts
                       "read": bool(a["extracted_at"]) and a["text_source"] != "summary",
                       "readable": a["text_source"] != "summary",
                       "found_by": a["found_by"] or "feed",
                       "published_at": a["published_at"].isoformat(timespec="minutes") if a["published_at"] else None}
                      for a in full_arts.values()], key=lambda s: (s["perspective"], s["outlet"]))
    return {
        "story_id": story_id,
        "headline": headline,
        "has_established": bool(facts),
        "perspective_mode": analysis.get("mode"),
        "qualified_by": analysis.get("qualified_by"),
        "perspectives": {k: sorted(v) for k, v in sorted(persp.items())},
        "timeline": [[items[n] for n in tier] for tier in tl["tiers"]],
        "undated": [items[n] for n in tl["undated"]],
        "established": established,
        "contested": contested,
        "framing": framing,
        "sources": sources,
        "loaded_words": sorted(banned),
        "counts": {"articles": len(full_arts), "independent_sources": len(analysis.get("groups") or {}),
                   "outlets": len({a["outlet"] for a in full_arts.values()})},
        # a suicide story carries a helpline note (responsible-reporting guidelines)
        "suicide": any(re.search(r"(?i)suicide|took (his|her|their) own life|आत्महत्या|ख़ुदकुशी|खुदकुशी", i["text"])
                       for i in items.values()),
    }


def _key(text: str) -> str:
    return hashlib.sha256(("hi|" + text).encode("utf-8")).hexdigest()


def _collect_strings(payload: dict) -> list[str]:
    out = [payload["headline"]]
    for sec in ("undated", "established", "contested"):
        for i in payload[sec]:
            out.append(i["text"])
            if i.get("check"):
                out += [r for r in i["check"]["reasons"] if r]
    for tier in payload["timeline"]:
        for i in tier:
            out.append(i["text"])
            if i["time"] and i["time"].get("when_text"):
                out.append(i["time"]["when_text"])
    for f in payload["framing"]:
        out.append(f["text"])
    for para in (payload.get("narrative") or {}).get("paragraphs", []):
        out += [x["text"] for x in para]
    return [s for s in dict.fromkeys(out) if s]


def translate_payload(store: Store, router: Router | None, payload: dict) -> dict:
    strings = _collect_strings(payload)
    cache = store.translation_get([_key(s) for s in strings])
    missing = [s for s in strings if _key(s) not in cache]
    if missing and router is not None:
        for start in range(0, len(missing), 40):
            chunk = missing[start:start + 40]
            body = json.dumps({str(i): s for i, s in enumerate(chunk)}, ensure_ascii=False)
            try:
                res = router.call("light", TRANSLATE_PROMPT.format(payload=body), json_out=True,
                                  max_output_tokens=4000)
            except QuotaExhausted:
                log.info("translation: light tier exhausted; Hindi version partial")
                break
            except Exception as e:  # noqa: BLE001
                log.warning("translation failed: %s", e)
                continue
            got = {}
            for k, v in (res.data or {}).items() if isinstance(res.data, dict) else []:
                if k.isdigit() and int(k) < len(chunk) and isinstance(v, str) and v.strip():
                    got[_key(chunk[int(k)])] = v.strip()
            store.translation_put(got)
            cache.update(got)

    def tr(s):
        return cache.get(_key(s), s) if s else s

    hi = copy.deepcopy(payload)
    hi["headline"] = tr(hi["headline"])
    for sec in ("undated", "established", "contested"):
        for i in hi[sec]:
            i["text"] = tr(i["text"])
            if i.get("check"):
                i["check"]["reasons"] = [tr(r) for r in i["check"]["reasons"]]
    for tier in hi["timeline"]:
        for i in tier:
            i["text"] = tr(i["text"])
            if i["time"] and i["time"].get("when_text"):
                i["time"]["when_text"] = tr(i["time"]["when_text"])
    for f in hi["framing"]:
        f["text"] = tr(f["text"])
    for para in (hi.get("narrative") or {}).get("paragraphs", []):
        for x in para:
            x["text"] = tr(x["text"])
    hi["translation_complete"] = all(_key(s) in cache for s in strings)
    return hi


def publish_story(store: Store, router: Router | None, story_id: int) -> bool:
    payload = build_payload(store, router, story_id)
    store.exec(update(stories).where(stories.c.id == story_id).values(dirty=False))
    if payload is None:
        # no longer meets the bar (e.g. after a rule change): take the page down, not leave it stale
        store.exec(delete(published).where(published.c.story_id == story_id))
        return False
    prev = store.one(select(published).where(published.c.story_id == story_id))
    from .narrative import input_hash, sections_from_payload, write_narrative
    old = ((prev or {}).get("payload_en") or {}).get("narrative")
    if (old and old.get("paragraphs") and old.get("hash") == input_hash(sections_from_payload(payload))
            and not old.get("rejected")):
        payload["narrative"] = old  # same statements, same verdicts: keep the story as written
    else:
        payload["narrative"] = write_narrative(router, payload, set(payload["loaded_words"]))
    hi = translate_payload(store, router, payload)
    version = (prev["version"] if prev else 0) + 1
    payload["version"] = hi["version"] = version
    values = dict(version=version, updated_at=utcnow(), headline_en=payload["headline"],
                  headline_hi=hi["headline"], payload_en=payload, payload_hi=hi)
    if prev:
        store.exec(update(published).where(published.c.story_id == story_id).values(**values))
    else:
        store.exec(insert(published).values(story_id=story_id, **values))
    return True
