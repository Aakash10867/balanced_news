"""Stage 8b: assemble the article, then translate it.

Structure is decided by code, not by a model:
  timeline        corroborated/confirmed events, ordered by the partial order
  established     corroborated/confirmed statements that are not events
  contested       everything else, each with its verdict (false ones included, tagged)
  framing         the loaded words each perspective used for the same fact
  sources         every article read, with its perspective
The news (news.pick_news, code) is chosen here; the article (narrative.py) leads with it and the
headline (news.write_headline) is written from it after the article, then the Hindi translation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict

from .db import Store, articles, claims, published, select, stories, update, utcnow, insert
from .router import QuotaExhausted, Router
from .timeline import build_timeline
from .verify import _story_context, relation_text, support_summary

log = logging.getLogger(__name__)
ESTABLISHED = {"corroborated", "confirmed"}

TRANSLATE_PROMPT = """Translate each value of this JSON object into Hindi (Devanagari script).
Translate literally and neutrally: do not add, soften or strengthen anything. Keep names of people,
places and organisations, and all numbers, as they are. Return a JSON object with exactly the same keys.

{payload}"""


def tidy(text: str) -> str:
    """'LPU (LPU)' -> 'LPU': an abbreviation the extraction repeated in brackets."""
    return re.sub(r"\b([\w.&'-]+(?: [\w.&'-]+){0,4}) \(\1\)", r"\1", text or "")


def _interval(rows: list[dict]) -> dict:
    starts = [r["time"]["start"] for r in rows if r.get("time") and r["time"].get("start")]
    ends = [r["time"]["end"] for r in rows if r.get("time") and r["time"].get("end")]
    whens = Counter(r["time"]["when_text"] for r in rows if r.get("time") and r["time"].get("when_text"))
    return {"start": min(starts) if starts else None, "end": max(ends) if ends else None,
            "when_text": whens.most_common(1)[0][0] if whens else ""}


def build_payload(store: Store, router: Router | None, story_id: int) -> dict | None:
    story, agroup, gpersp, arts, canon, members = _story_context(store, story_id)
    if not story["qualifies"]:
        return None
    full_arts = {a["id"]: a for a in store.rows(
        select(articles.c.id, articles.c.outlet, articles.c.url, articles.c.title, articles.c.lang,
               articles.c.published_at, articles.c.role, articles.c.extracted_at, articles.c.text_source,
               articles.c.found_by).where(articles.c.story_id == story_id))}
    texts = {cid: c["text"] for cid, c in canon.items()}

    analysis_ = story["analysis"] or {}
    speakers = analysis_.get("speakers") or {}
    departures = analysis_.get("departures") or {}
    roles = analysis_.get("roles") or {}
    related_event = analysis_.get("related_event") or {}
    name_conf = analysis_.get("name_conflicts") or {}
    responds_to: dict[int, list[int]] = defaultdict(list)
    responded_by: dict[int, list[int]] = defaultdict(list)
    for a_, b_ in analysis_.get("responses") or []:
        if a_ in canon and b_ in canon:
            responded_by[a_].append(b_)
            responds_to[b_].append(a_)

    def _role(cid: int) -> tuple[str, str]:
        """'core' or a context role, and the related event's name. Consolidation's reading of the
        whole story wins; otherwise the reports' own: context only if every report gave it as context."""
        rows_ = members.get(cid, [])
        ctx = [((r.get("rel") or {}) if r["kind"] != "relation" else {}).get("context") for r in rows_]
        label = related_event.get(str(cid)) or next(
            (((r.get("rel") or {}).get("event") or "") for r in rows_ if (r.get("rel") or {}).get("event")), "")
        if str(cid) in roles:
            return roles[str(cid)], label
        if rows_ and all(ctx):
            return Counter(ctx).most_common(1)[0][0], label
        return "core", ""

    def _persp_of(aid: int) -> str:
        """An article that departs from its outlet's perspective shows its own (marked †)."""
        d = departures.get(str(aid))
        return f"{d['to']}†" if d else (gpersp.get(agroup.get(aid)) or "–")

    def item(cid: int) -> dict:
        c = canon[cid]
        s = support_summary(cid, members, agroup, gpersp)
        srcs, seen = [], set()
        framing = defaultdict(set)
        for r in members.get(cid, []):
            a = full_arts.get(r["article_id"])
            if not a:
                continue
            p = _persp_of(a["id"])
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
            "text": relation_text(c["rel"], texts) if c["kind"] == "relation" else tidy(c["text"]),
            "verdict": c["verdict"], "n_sources": len(s["support_groups"]), "n_articles": s["n_articles"],
            "supported_by": s["support_perspectives"], "denied_by": s["deny_perspectives"],
            "conflicts_with": [x for x in (c["conflicts"] or []) if x in canon],
            "time": _interval(members.get(cid, [])) if c["kind"] == "event" else None,
            "framing": {k: sorted(v) for k, v in sorted(framing.items())},
            "sources": sorted(srcs, key=lambda x: (x["perspective"], x["outlet"])),
            "check": check, "minor": s["n_articles"] <= 1,
            "speaker": speakers.get(str(cid)),
            "role": _role(cid)[0], "related_event": _role(cid)[1] or None,
            "responds_to": [x for x in responds_to.get(cid, []) if members.get(x)],
            "responded_by": [x for x in responded_by.get(cid, []) if members.get(x)],
            "name_conflict": name_conf.get(str(cid)),
            "origins": (c.get("origins") or {}).get("origins", []),
            "n_origins": (c.get("origins") or {}).get("n_origins", 0),
            "n_outlets": (c.get("origins") or {}).get("outlets", 0),
            # its fields (frames.py): the writer's sections place figures by them
            "frame": (c.get("rel") or {}).get("frame") if c["kind"] != "relation" else None,
        }

    all_items = {cid: item(cid) for cid in canon if members.get(cid)}
    # the story's own event drives the timeline, sections and headline; context (background, related
    # events, explanation, reactions, what next) is carried separately and written after it
    items = {cid: i for cid, i in all_items.items() if i["role"] == "core" or i["kind"] == "relation"}
    ROLE_ORDER = {"background": 0, "related": 1, "explanation": 2, "reaction": 3, "next": 4}
    context = sorted([i for i in all_items.values() if i["id"] not in items],
                     key=lambda i: (ROLE_ORDER.get(i["role"], 9), -i["n_articles"], i["id"]))
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
    # the news, chosen once by code (news.py): the lead and the headline are written from it
    from . import importance as imp, news as news_, threads
    news_ids = news_.pick_news(list(items.values()))
    by_support = sorted(items.values(), key=lambda i: (-i["n_sources"], -i["n_articles"], i["id"]))
    summary = [items[n]["text"] for n in news_ids[:1]] + [i["text"] for i in by_support if i["id"] not in news_ids[:1]]
    from .editions import link_candidate, parent_pages
    link_candidate(store, story_id)      # later reports of a published story develop it
    parent_ids = threads.find_parents(store, router, story_id, story["signature"] or "", " ".join(summary[:4]))
    live = parent_pages(store, parent_ids)   # live, or already on the archive branch
    thread_ctx = "; ".join(live[p]["headline_en"] for p in parent_ids if p in live)

    analysis = story["analysis"] or {}
    persp = defaultdict(set)
    for g, info in (analysis.get("groups") or {}).items():
        persp[info.get("perspective") or "–"].update(info.get("outlets", []))
    for aid, d in departures.items():
        a = full_arts.get(int(aid))
        if a:
            persp[d["to"]].add(f"{a['outlet']} († this article differs from the outlet's usual perspective)")
    sources = sorted([{"outlet": a["outlet"], "title": a["title"], "url": a["url"], "lang": a["lang"],
                       "role": a["role"], "perspective": _persp_of(a["id"]),
                       # option B: an article we could not read is listed, never used for facts
                       "read": bool(a["extracted_at"]) and a["text_source"] != "summary",
                       "readable": a["text_source"] != "summary",
                       "found_by": a["found_by"] or "feed",
                       "published_at": a["published_at"].isoformat(timespec="minutes") if a["published_at"] else None}
                      for a in full_arts.values()], key=lambda s: (s["perspective"], s["outlet"]))

    # importance comes from the story's rating (priority.py, from its headlines); filler is never published
    prio = analysis.get("priority") or {}
    if prio.get("filler"):
        log.info("story %s is filler: not published", story_id)
        return None
    n_indep = len(analysis.get("groups") or {})
    langs = len({a["lang"] for a in full_arts.values() if a["extracted_at"]})

    # threads: earlier stories this one develops, and later ones that develop it (published only)
    parents = [{"story_id": p, "headline": live[p]["headline_en"]} for p in parent_ids if p in live]
    children: list[dict] = []    # an article is written once, before anything develops it
    # background for the essay: the parents' established facts, with their sources (at most 4)
    background = []
    for p in parent_ids:
        if p not in live:
            continue
        pe = live[p]["payload_en"] or {}
        est = [i for tier in pe.get("timeline") or [] for i in tier] + (pe.get("established") or [])
        if not est:   # nothing established on the parent: its best-supported reports, still as reports
            est = sorted(pe.get("contested") or [], key=lambda i: -i.get("n_articles", 0))
        for i in est[:4 - len(background)]:
            if i.get("kind") != "relation":
                background.append(dict(i, id=-int(i["id"]), kind="background", parent=p))
    return {
        "story_id": story_id,
        "importance": {"score": prio.get("score")},
        "rank": imp.rank(prio.get("score") or 3, n_indep, langs),
        "thread": threads.root_of(store, story_id) if parents else story_id,
        "parents": parents,
        "children": children,
        "background": background,
        "headline": None,                     # written after the article, from the news (news.py)
        "news": news_ids[:3],                 # the first is THE news: the lead cites it
        "_thread_ctx": thread_ctx,            # for the headline; not stored
        "has_established": bool(est_events or established),
        "perspective_mode": analysis.get("mode"),
        "qualified_by": analysis.get("qualified_by"),
        "perspectives": {k: sorted(v) for k, v in sorted(persp.items())},
        "timeline": [[items[n] for n in tier] for tier in tl["tiers"]],
        "undated": [items[n] for n in tl["undated"]],
        "established": established,
        "contested": contested,
        "framing": framing,
        "context": context,
        "sources": sources,
        "loaded_words": sorted(banned),
        "counts": {"articles": len(full_arts), "independent_sources": len(analysis.get("groups") or {}),
                   "outlets": len({a["outlet"] for a in full_arts.values()})},
        # a suicide story carries a helpline note (responsible-reporting guidelines)
        "suicide": any(re.search(r"(?i)suicide|took (his|her|their) own life|आत्महत्या|ख़ुदकुशी|खुदकुशी", i["text"])
                       for i in all_items.values()),
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
    for i in payload.get("context", []):
        out.append(i["text"])
        if i.get("related_event"):
            out.append(i["related_event"])
    out += [x["headline"] for x in payload.get("parents", []) + payload.get("children", []) if x.get("headline")]
    out += [b["text"] for b in payload.get("background", [])]
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
                res = router.call("page", TRANSLATE_PROMPT.format(payload=body), json_out=True,
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
    for i in hi.get("context", []):
        i["text"] = tr(i["text"])
        if i.get("related_event"):
            i["related_event"] = tr(i["related_event"])
    for x in hi.get("parents", []) + hi.get("children", []):
        x["headline"] = tr(x["headline"])
    for b in hi.get("background", []):
        b["text"] = tr(b["text"])
    for para in (hi.get("narrative") or {}).get("paragraphs", []):
        for x in para:
            x["text"] = tr(x["text"])
            # Hindi word order differs: parts cannot be translated apart, so a Hindi sentence has one
            # colour, the weakest (its "class"), as before parts existed
            x.pop("parts", None)
    hi["translation_complete"] = all(_key(s) in cache for s in strings)
    return hi


def finish_translations(store: Store, router: Router, limit: int = 3) -> list[int]:
    """Hindi pages published half-translated (Oct 8 2026, story 15429: the page models were overloaded
    mid-translation and half the Hindi article stayed English) are finished on later runs. The English
    article is unchanged (it is closed); only its translation is completed. Strings already translated
    come from the cache, so a retry costs only the missing ones."""
    done = []
    incomplete = published.c.payload_hi["translation_complete"].as_boolean().is_(False)   # in SQL: no egress
    for r in store.rows(select(published.c.story_id, published.c.payload_en, published.c.payload_hi)
                        .where(incomplete).order_by(published.c.updated_at.desc()).limit(limit)):
        if not r["payload_en"]:
            continue
        hi = translate_payload(store, router, r["payload_en"])
        hi["version"] = (r["payload_hi"] or {}).get("version", 1)
        store.exec(update(published).where(published.c.story_id == r["story_id"])
                   .values(payload_hi=hi, headline_hi=hi["headline"]))
        done.append(r["story_id"])
        if not hi["translation_complete"]:
            break                           # the page models are still refusing: next run
    return done


WRITER_LITE_OK = ("gemini-3.5-flash-lite",)   # owner, Oct 6 2026: the writer's last resort; older Lites read badly


def _keepable(nar: dict | None) -> bool:
    """An essay we may publish: written by the writer (a Flash model, or 3.5 Flash-Lite when every
    Flash model is refusing), never stitched by code, never by an older Flash-Lite."""
    m = (nar or {}).get("model") or ""
    lite_ok = any(m.startswith(x) for x in WRITER_LITE_OK)
    return bool(m) and ("lite" not in m or lite_ok) and bool((nar or {}).get("paragraphs"))


def _draft_ids(sections) -> set[int]:
    """Statement numbers a kept draft cites: sections are [key, paragraph], a paragraph a list of sentences."""
    return {x for _, para in sections or [] for sent in para or [] if isinstance(sent, dict)
            for x in (sent.get("ids") or []) if isinstance(x, int)}


def _draft_anchors(store: Store, sections) -> dict[str, int]:
    """One report (claim) behind each statement the draft cites: when consolidation later merges that
    statement into another, the report moves with it, so the draft's sentence can follow."""
    ids = sorted(_draft_ids(sections))
    out: dict[str, int] = {}
    for r in store.rows(select(claims.c.id, claims.c.canonical_id).where(claims.c.canonical_id.in_(ids or [-1]))
                        .order_by(claims.c.id)):
        out.setdefault(str(r["canonical_id"]), r["id"])
    return out


def _remap_draft(store: Store, draft: dict | None, payload: dict) -> dict | None:
    """A kept draft cites statement numbers from when it was written; statements merged since then are
    renamed to the statement they joined, so their sentences are kept instead of dropped."""
    if not draft or not draft.get("sections"):
        return draft
    from .narrative import ordered_items
    known = {i["id"] for i in ordered_items(payload) + (payload.get("background") or []) if i.get("id") is not None}
    anchors = draft.get("anchors") or {}
    gone = {x: anchors[str(x)] for x in _draft_ids(draft["sections"]) if x not in known and str(x) in anchors}
    if not gone:
        return draft
    now = {r["id"]: r["canonical_id"] for r in store.rows(
        select(claims.c.id, claims.c.canonical_id).where(claims.c.id.in_(sorted(set(gone.values())))))}
    to = {x: now.get(c) for x, c in gone.items() if now.get(c) in known}
    if not to:
        return draft
    out = copy.deepcopy(draft)
    for _, para in out["sections"]:
        for sent in para or []:
            if isinstance(sent, dict):
                sent["ids"] = list(dict.fromkeys(to.get(x, x) for x in sent.get("ids") or []))
    log.info("draft: %d merged statements renamed (%s)", len(to), to)
    return out


MEDIA_WORDS = re.compile(r"(?i)\b(newspapers?|news ?papers?|channels?|editions?|circulat\w*|readership|widely read|"
                         r"most read|publish\w*|publications?|news agency|broadcaster|daily|founded|established in)\b")


def drop_outlet_self_talk(payload: dict) -> int:
    """Statements a page says about its own publisher ("Hindustan was established in 1936 ... the second
    most widely read Hindi newspaper", Oct 7 2026, story 12687) are not the story: dropped by code when a
    statement names an outlet, speaks of it as a publication, and only that outlet reports it."""
    from .narrative import _known_outlets
    names = {s.get("outlet") for s in payload.get("sources") or [] if s.get("outlet")} | set(_known_outlets())
    names = {n for n in names if n and len(n) >= 4}

    def self_talk(i: dict) -> bool:
        text = i.get("text") or ""
        if not MEDIA_WORDS.search(text):
            return False
        hit = {n for n in names if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text)}
        # "Live Hindustan" reports on "Hindustan": the reporting outlets' names share a word with it
        by = {s.get("outlet") or "" for s in i.get("sources") or []}
        return bool(hit) and all(any(set(h.lower().split()) & set(b.lower().split()) for h in hit) for b in by if b)

    dropped = 0
    for key in ("undated", "established", "contested", "context"):
        keep = [i for i in payload.get(key) or [] if not self_talk(i)]
        dropped += len(payload.get(key) or []) - len(keep)
        payload[key] = keep
    payload["timeline"] = [[i for i in tier if not self_talk(i)] for tier in payload.get("timeline") or []]
    payload["timeline"] = [t for t in payload["timeline"] if t]
    if dropped:
        log.info("dropped %d statements a page made about its own publisher", dropped)
    return dropped


GENERIC = {"india", "indian", "government", "state", "states", "said", "says", "also", "year", "years", "time",
           "people", "officials", "official", "report", "reports", "according", "country", "national", "today",
           "week", "month", "first", "second", "third", "most", "more", "many", "several", "after", "before"}


def drop_unrelated_context(payload: dict) -> int:
    """A context line (background, related event, explanation, what next) must share a specific word or
    name with the story's own event; one that shares none is not this story's context and is dropped by
    code (owner, Oct 7 2026: "Hindustan was founded in 1936 ..." sat in the Background of a story on
    stubble burning). Words are compared by their roots; common words do not count as a link."""
    from .frames import words as roots
    from .narrative import _subject_words, is_core
    core = [i for i in _all_items_of(payload) if is_core(i)]
    if not core:
        return 0
    link = set()
    for i in core:
        link |= roots(" ".join(_subject_words(i.get("text") or "") - GENERIC))
    keep, dropped = [], 0
    for i in payload.get("context") or []:
        mine = roots(" ".join(_subject_words(i.get("text") or "") - GENERIC))
        if mine & link:
            keep.append(i)
        else:
            dropped += 1
            log.info("context line dropped, nothing in common with the story: %s", (i.get("text") or "")[:100])
    payload["context"] = keep
    return dropped


CONTEXT_PROMPT = """A news story, and some lines from the pages that reported it. A news page often carries OTHER
news too (a sidebar, a list of other videos, "also read" links, a live blog). For each numbered line decide:

"connected"  - the line belongs to THIS story: its background (how it came about, earlier events of the
               same people, place or matter), an explanation of something in it, or a separate event that
               is linked to it (the same people, the same dispute, an earlier case of the same kind that the
               report itself compares it with).
"other news" - the line is about something else that only happened to be on the same page.

Examples, for a story "India's cheetah population reached 60 after a cheetah gave birth to five cubs in
Kuno National Park":
  "Cheetahs were brought to Kuno from Namibia in 2022." -> connected (background)
  "Kerala's minister defended a vigilance probe into a road project." -> other news (a different matter)
For a story "Four students died in Vrindavan; the District Magistrate ordered an inquiry":
  "Bhuvneshwar Kumar has returned to the Indian T20 team." -> other news (sport, nothing to do with it)
For a story "A drone struck an oil tanker in the Black Sea":
  "Two days earlier, the cargo ship MV Royad Mammadov was attacked in the Black Sea." -> connected
If you are not sure, answer "other news".

THE STORY:
{story}

LINES:
{lines}

Reply with JSON only: {{"results": [{{"n": 1, "answer": "connected"}}, {{"n": 2, "answer": "other news"}}]}}"""

CONTEXT_CHECKED = ("related", "background", "explanation")   # owner, Oct 8 2026: reactions and "next" not asked
CONTEXT_BATCH = 8


def _ctx_key(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def check_context(store: Store, router: Router | None, story_id: int, payload: dict) -> int:
    """Context lines that are other news from the same page (Oct 8 2026: a Kerala vigilance probe in the
    cheetah story, from a video page's list of other videos; a cricket comeback in a story of students'
    deaths). Shared words cannot tell (measured on the 60 latest articles: a word rule dropped "the project
    is expected to generate employment" in the project's own story, a name rule dropped 38 of 154 related
    lines and kept the cricket one), so a model is asked one plain question: connected / other news. Asked
    twice, the second time with the lines in reverse order; kept only if both say connected. Unsure, or not
    asked (quota), is "other news": unrelated news shown as context is a false link. Answers are kept per
    line in stories.analysis.context_checks, so a retried story is not asked again."""
    from .narrative import ordered_items
    from .news import pick_news
    lines = [i for i in payload.get("context") or [] if i.get("role") in CONTEXT_CHECKED and i.get("text")]
    if not lines:
        return 0
    row = store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}
    an = dict(row.get("analysis") or {})
    cache = dict(an.get("context_checks") or {})
    todo = [i for i in lines if _ctx_key(i["text"]) not in cache]
    if todo and router is not None:
        items = ordered_items(payload)
        news = [x for x in pick_news(items)[:2]]
        by_id = {i["id"]: i for i in items}
        story = " ".join(by_id[x]["text"] for x in news if x in by_id) or " ".join(i["text"] for i in items[:2])

        def ask(texts: list[str]) -> dict[str, bool]:
            got: dict[str, bool] = {}
            for start in range(0, len(texts), CONTEXT_BATCH):
                chunk = texts[start:start + CONTEXT_BATCH]
                body = "\n".join(f'{k + 1}. "{t}"' for k, t in enumerate(chunk))
                try:
                    res = router.call("page", CONTEXT_PROMPT.format(story=story[:600], lines=body),
                                      json_out=True, max_output_tokens=400)
                except QuotaExhausted:
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning("context check failed: %s", str(e)[:200])
                    continue
                for item in (res.data or {}).get("results", []) if isinstance(res.data, dict) else []:
                    try:
                        n = int(item["n"]) - 1
                    except (KeyError, TypeError, ValueError):
                        continue
                    if 0 <= n < len(chunk):
                        got[chunk[n]] = str(item.get("answer") or "").strip().lower() == "connected"
            return got
        texts = list(dict.fromkeys(i["text"] for i in todo))
        first = ask(texts)
        second = ask(list(reversed([t for t in texts if first.get(t)])))
        for t in texts:
            if t in first and (not first[t] or t in second):     # an answer we can keep (both asked, or a no)
                cache[_ctx_key(t)] = "connected" if first[t] and second.get(t) else "other"
        if cache != (an.get("context_checks") or {}):
            an["context_checks"] = cache
            store.exec(update(stories).where(stories.c.id == story_id).values(analysis=an))
    keep, dropped = [], 0
    for i in payload.get("context") or []:
        if i in lines and cache.get(_ctx_key(i["text"])) != "connected":
            dropped += 1
            log.info("context line dropped as other news (story %s): %s", story_id, i["text"][:100])
            continue
        keep.append(i)
    payload["context"] = keep
    return dropped


def link_updates(payload: dict, updates: dict) -> int:
    """An older figure and the newer one that replaced it (disputes.py: every report of the newer one
    came clearly later): both are written, together, the newest first ("the toll rose to 50; earlier
    reports put it at 40"); neither is a dispute, each keeps its own colour (owner, Oct 7 2026)."""
    items = {i["id"]: i for i in _all_items_of(payload) + list(payload.get("context") or [])}
    n = 0
    for old, new in updates.items():
        old, new = int(old), int(new)
        if old in items and new in items:
            items[new]["update_of"] = old
            items[old]["updated_by"] = new
            n += 1
    return n


def fold_covered(payload: dict, covered: dict) -> int:
    """A line another line says in full, with more (relate.py), is not written on its own: its outlets
    are listed as sources of the detailed line, which keeps its own colour (it never borrows their
    support, so its extra details cannot turn green on another outlet's report)."""
    from .narrative import RANK, shade
    items = {i["id"]: i for i in _all_items_of(payload) + list(payload.get("context") or [])}
    gone = set()
    for small, big in covered.items():
        small, big = int(small), int(big)
        if small in items and big in items and small != big and big not in gone:
            # the short line is better supported than the detailed one (four outlets vs one): both are
            # kept and written as one sentence in two parts, so its fact can take its own colour
            # (owner, Oct 7 2026); otherwise there is nothing to gain and the short line folds away
            if RANK.get(shade(items[small]), 2) < RANK.get(shade(items[big]), 2) and not items[big].get("adds_to"):
                items[big]["adds_to"] = small
                continue
            have = {s["url"] for s in items[big].get("sources") or []}
            items[big]["sources"] = list(items[big].get("sources") or []) + [
                s for s in items[small].get("sources") or [] if s["url"] not in have]
            items[big].setdefault("covers_ids", []).append(small)
            gone.add(small)
    if gone:
        for key in ("undated", "established", "contested", "context"):
            payload[key] = [i for i in payload.get(key) or [] if i["id"] not in gone]
        payload["timeline"] = [t for t in ([i for i in tier if i["id"] not in gone]
                                           for tier in payload.get("timeline") or []) if t]
        log.info("%d lines folded into the more detailed lines that say them", len(gone))
    return len(gone)


def _all_items_of(payload: dict) -> list[dict]:
    out, seen = [], set()
    for tier in payload.get("timeline") or []:
        out += tier
    for k in ("undated", "established", "contested"):
        out += payload.get(k) or []
    return [i for i in out if not (id(i) in seen or seen.add(id(i)))]


# why the last publish_story call ended: the desk counts a try only when the writer was asked
# (Oct 7 2026: follow-up candidates refused before writing used up all five tries, two runs in a row)
LAST_OUTCOME: dict[str, str] = {}


def _outcome(story_id: int, what: str) -> bool:
    LAST_OUTCOME.clear()
    LAST_OUTCOME.update(story=str(story_id), outcome=what)
    return what == "published"


def publish_story(store: Store, router: Router | None, story_id: int) -> bool:
    """Write the article, once (editions.py, owner Oct 5 2026). Only the writer produces prose: a story
    is published with a good essay or not at all (it waits; Oct 2026: 85 of 91 live pages were
    code-stitched and read like he-said-she-said). A published article is closed: this never changes
    it again (its colours mature by code, editions.mature). A development of an earlier article is
    published only if it earns a follow-up (editions.follow_up_ok). Returns True when written."""
    from .editions import follow_up_ok
    from .narrative import essay_ok, input_hash, ordered_items, sections_from_payload, write_narrative
    if store.one(select(published.c.story_id).where(published.c.story_id == story_id)):
        return _outcome(story_id, "already published")
    payload = build_payload(store, router, story_id)
    store.exec(update(stories).where(stories.c.id == story_id).values(dirty=False))
    if payload is None:
        log.info("story %s waits: no longer qualifies", story_id)
        return _outcome(story_id, "no payload")
    # a failed headline before writing no longer stops the story (Oct 7 2026: the headline model was
    # overloaded and four stories a run were skipped): the headline is written again from the article's
    # lead below, and if that fails too the finished article waits as a kept draft
    from .spelling import unify_article, unify_payload
    drop_outlet_self_talk(payload)
    drop_unrelated_context(payload)
    check_context(store, router, story_id, payload)     # other news from the same page (a model, asked twice)
    an_ = (store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}).get("analysis") or {}
    fold_covered(payload, an_.get("covered") or {})
    link_updates(payload, an_.get("updates") or {})
    unify_payload(payload)                  # one spelling per name, before the writer sees the statements
    from .news import pick_news
    payload["news"] = pick_news(ordered_items(payload))[:3]   # again: folding can remove a statement
    parents = [x["story_id"] for x in payload.get("parents") or []]
    if parents and not follow_up_ok(store, router, story_id, payload, parents):
        return _outcome(story_id, "not a follow-up yet")
    h = input_hash(sections_from_payload(payload), payload.get("background"))
    banned = set(payload["loaded_words"])
    if router is None or not _writer_attempt_allowed(store, story_id, h):
        return _outcome(story_id, "writer tries used up")
    # an earlier try that fell short of the bar is continued, not started again (owner, Oct 7 2026)
    an = (store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}).get("analysis") or {}
    draft = _remap_draft(store, an.get("writer_draft"), payload)
    nar = write_narrative(router, payload, banned, draft=draft if _keepable({"model": (draft or {}).get("model"),
                                                                             "paragraphs": [1]}) else None)
    drafted = nar.pop("drafted", None)
    essay_good = essay_ok(nar, payload) and _keepable(nar)
    if essay_good:
        from .news import write_headline
        lead = " ".join(x["text"] for x in (nar.get("paragraphs") or [[]])[0])
        items_ = {i["id"]: i for i in ordered_items(payload)}
        news = items_.get((payload.get("news") or [None])[0])
        payload["headline"] = write_headline(router, news, items_, lead, banned, payload.get("_thread_ctx") or "")
    headline_missing = essay_good and not payload.get("headline")
    if not essay_good or headline_missing:
        if not essay_good:
            _note_writer_failure(store, story_id, h, nar)
        if drafted and _keepable({"model": nar.get("model"), "paragraphs": [1]}):   # a writer model's draft
            an = dict((store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}).get("analysis") or {})
            covered = len(set(nar.get("covers") or []))
            an["writer_draft"] = {"sections": drafted, "model": nar.get("model"), "covers": covered,
                                  "anchors": _draft_anchors(store, drafted),
                                  "at": utcnow().isoformat(timespec="minutes")}
            store.exec(update(stories).where(stories.c.id == story_id).values(analysis=an))
        if headline_missing:
            log.info("story %s waits: written, but no headline passed; its draft is kept", story_id)
            return _outcome(story_id, "headline failed")
        log.info("story %s waits: the article carries %d statements, short of the bar; its draft is kept",
                 story_id, len(set(nar.get("covers") or [])))
        return _outcome(story_id, "written short")
    if draft:
        an = dict((store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}).get("analysis") or {})
        an.pop("writer_draft", None)
        store.exec(update(stories).where(stories.c.id == story_id).values(analysis=an))
    payload["narrative"] = nar
    payload.pop("_thread_ctx", None)
    unify_article(payload)                  # and in what the writer and the headline model wrote
    from .style import polish
    polish(payload)                         # surnames after the first mention; varied "he said" (by code)
    now = utcnow()
    payload["written_at"] = now.isoformat(timespec="seconds")
    hi = translate_payload(store, router, payload)
    payload["version"] = hi["version"] = 1
    store.exec(insert(published).values(story_id=story_id, version=1, updated_at=now, headline_en=payload["headline"],
                                        headline_hi=hi["headline"], payload_en=payload, payload_hi=hi))
    return _outcome(story_id, "published")


WRITER_TRIES = 3   # failed writes of the same statements before waiting for new ones


def _writer_attempt_allowed(store: Store, story_id: int, h: str) -> bool:
    a = ((store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}).get("analysis") or {})
    w = a.get("writer_failures") or {}
    return w.get("hash") != h or w.get("n", 0) < WRITER_TRIES


def _note_writer_failure(store: Store, story_id: int, h: str, nar: dict) -> None:
    """Remembered per statement set, with why, so a story the writer cannot do is not retried every
    hour (and the reasons are readable in the database). Quota is not a failure of the story."""
    if nar.get("failure") == "quota":
        return
    row = store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}
    a = dict(row.get("analysis") or {})
    w = a.get("writer_failures") or {}
    prev_n = w.get("n", 0) if w.get("hash") == h else 0
    # an overloaded or rate-limited model is not the story's fault: recorded, but no try is used up
    n = prev_n if str(nar.get("failure") or "").startswith("error") else prev_n + 1
    a["writer_failures"] = {"hash": h, "n": n, "model": nar.get("model"), "failure": nar.get("failure"),
                            "rejected": nar.get("rejected"), "reasons": nar.get("reject_reasons"),
                            "covers": len(nar.get("covers") or []), "at": utcnow().isoformat(timespec="minutes")}
    store.exec(update(stories).where(stories.c.id == story_id).values(analysis=a))
