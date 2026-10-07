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
import datetime as dt
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

HEADLINE_PROMPT = """Write the headline for this news story: at most 12 words, plain English, crisp.
- Hit the single most consequential fact, with an active verb and one concrete detail (a number, a
  place, a role). The reader should want to read on: lead with the tension or the unusual element that
  the statements themselves contain. No question headlines, no clickbait, no judging adjectives.
- Introduce any person who is not nationally famous by who they are ("gym trainer who defended a
  shopkeeper", "IIT Bombay student"), never by a bare name the reader cannot know.
- Use ONLY the statements given. ESTABLISHED ones may be stated as fact. For the others, attribute
  ("family alleges ...") or put "alleged"/"reportedly" next to the uncertain part, never at the end.
- Lead with the NEWEST development. Background from earlier weeks or months is not the hook; leave it
  out rather than tie it on.
- Do not link two events by cause ("due to", "because", "over", "amid") unless a statement does.
  "After" may join only two steps of the same incident (a death, then the probe into it); never use it
  to tie the newest event to an older, separate one.
- Describe what people did with the statements' own verbs ("called himself", not "posed as").
- Never start with "Reports", never write "reports say/emerge/detail".
{thread}
Statements, NEWEST FIRST, each with the date it happened (headline the newest development that matters):
{facts}

Reply with JSON only: {{"headline": "..."}}"""

LAZY_HEDGES = re.compile(r"(?i)\breports? (say|says|emerge|emerges|detail|details|indicate|indicates|follow|on)\b|^reports\b")
LINKS = ("due to", "amid", "because", "as a result", "triggered", "led to")

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


def _headline(router: Router | None, facts: list[str], banned: set[str], fallback: str,
              unsettled: list[str] | None = None, thread: str = "", lead: str = "") -> str:
    """A specific headline. `facts` are established statements, `unsettled` the best-supported
    others. Checked: no loaded word, no lazy 'reports say', no cause/sequence link the statements
    do not make, no number the statements do not have."""
    lines = [f"- ESTABLISHED: {f}" for f in facts[:6]] + [f"- REPORTED: {f}" for f in (unsettled or [])[:8]]
    if router is None or not lines:
        return fallback
    source = " ".join(facts + (unsettled or [])).lower()
    ctx = f"\nThis is a new development in an ongoing story: {thread}. Headline the NEW development.\n" if thread else ""
    if lead:
        # written after the article, from its opening: the writer has decided what the news is
        ctx += f"\nThe article opens with the news; headline THIS: {lead}\n"
    prompt = HEADLINE_PROMPT.format(facts="\n".join(lines), thread=ctx)
    for _ in range(3):   # two retries, each told what was wrong
        try:
            res = router.call("page", prompt, json_out=True, max_output_tokens=200)
            h = str((res.data or {}).get("headline") or "").strip().strip('"').rstrip(".")
        except (QuotaExhausted, Exception) as e:  # noqa: BLE001
            log.info("headline fallback: %s", e)
            return fallback
        problem = _headline_problem(h, facts, source, banned)
        if not problem:
            return h
        prompt = HEADLINE_PROMPT.format(facts="\n".join(lines), thread=ctx) + (
            f"\n\nYour previous headline \"{h}\" was rejected: {problem}. Write a new one.")
    return fallback


def _newest_first(items: list[dict]) -> list[str]:
    """'(3 October) text' lines, newest first; undated ones after, best-supported first."""
    seen, dated, undated = set(), [], []
    for i in items:
        if i["id"] in seen:
            continue
        seen.add(i["id"])
        start = (i.get("time") or {}).get("start")
        try:
            d = dt.datetime.fromisoformat(start) if start else None
        except (TypeError, ValueError):
            d = None
        (dated if d else undated).append((d, i))
    dated.sort(key=lambda x: (x[0].replace(tzinfo=None), x[1]["n_sources"]), reverse=True)
    undated.sort(key=lambda x: (-x[1]["n_sources"], -x[1]["n_articles"]))
    return ([f"({d.strftime('%-d %B')}) {i['text']}" for d, i in dated]
            + [f"(undated) {i['text']}" for _, i in undated])


def _headline_problem(h: str, facts: list[str], source: str, banned: set[str]) -> str | None:
    low = h.lower()
    if not h or len(h.split()) > 13:
        return "it must be at most 12 words"
    if re.search(r"(?i),?\s*(reportedly|allegedly|reports say|reports said)\s*$", h):
        return ("do not tack 'reportedly' on at the end; put it before the verb, e.g. 'Delhi Police reportedly "
                "deny permission for Jantar Mantar protest', or attribute it to whoever says it")
    m = re.match(r"([A-Z][a-z]+)\s+(was|is|has|had|gets|got|were)\b", h)
    if m and m.group(1).lower() not in ("police", "court", "government", "centre", "parliament", "army"):
        return f"it starts with a bare name ('{m.group(1)}'); introduce the person by who they are"
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
    return _actor_problem(h, source)


HEADLINE_STOP = {"the", "and", "for", "with", "from", "over", "into", "after", "amid", "its", "his", "her", "their"}


def _actor_problem(h: str, source: str) -> str | None:
    """Who did it must match the statements (Oct 2026: "FSSAI recalls Everest cumin powder" when the
    statements say FSSAI ORDERED the recall; Everest recalls). Checked for the headline's first verb in
    the present tense ("recalls", "arrests"): where the statements use that verb, someone named before
    it in the headline must be its actor there, not "the recall" ordered by them. A verb the
    statements never use (a paraphrase) is not judged here."""
    words = re.findall(r"[A-Za-z'&.-]+", h)
    for k, w in enumerate(words):
        if k == 0 or not re.fullmatch(r"[a-z]{3,}s", w) or w in ("was", "has", "is", "its", "his", "says"):
            continue
        stem = w[:-2] if w.endswith(("ches", "shes", "sses", "xes")) else w[:-1]
        subject = {x.lower().strip("'s.") for x in words[:k] if len(x) >= 3} - HEADLINE_STOP
        forms = list(re.finditer(rf"\b{re.escape(stem.lower())}(?:s|es|ed|d|ing)?\b", source))
        if not forms or not subject:
            return None
        for m in forms:
            before = re.findall(r"[a-z'&.-]+", source[max(0, m.start() - 60):m.start()])[-4:]
            if before and before[-1] in ("the", "a", "an", "its", "his", "her", "their"):
                continue   # "ordered the recall": a noun, someone else's action on it
            if subject & {b.strip("'s.") for b in before}:
                return None
        m = forms[0]
        snippet = source[max(0, m.start() - 50):m.end() + 30].strip()
        return f"it says '{' '.join(words[:k + 1])}', but the statements say \"...{snippet}...\": keep who did what"
    return None


VERB_START = re.compile(r"\b(was|were|is|are|has|have|had|does|did|do|will|gave|made|held|took|paid|met|got|"
                        r"\w+ed)\b")


def _hedged(t: str) -> str:
    """'Delhi Police denied permission' -> 'Delhi Police reportedly denied permission': the hedge goes
    before the first verb, never tacked on at the end."""
    m = VERB_START.search(t, 1)
    return t[:m.start()] + "reportedly " + t[m.start():] if m else t


def _fallback_headline(facts: list[str], unsettled: list[str], signature: str) -> str:
    """No usable model headline: a short best-supported statement itself (never cut mid-sentence),
    hedged before its verb if it is not established. Never a raw non-English title."""
    def fits(t):
        return len(t.rstrip(".").split()) <= 13
    for t in facts[:8]:
        if fits(t):
            return t.rstrip(".")
    for t in unsettled[:8]:
        if fits(t):
            return _hedged(t.rstrip("."))
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
    # headline material: the best-supported facts first, not the first in time (that one is
    # often a vague scene-setter)
    est_all = [items[n] for tier in tl["tiers"] for n in tier] + [items[n] for n in tl["undated"]] + established
    facts = [i["text"] for i in sorted(est_all, key=lambda i: (-i["n_sources"], -i["n_articles"]))]
    unsettled = [i["text"] for i in contested if not i["minor"]][:8] or [i["text"] for i in contested][:8]
    fallback = _fallback_headline(facts, unsettled, story["signature"] or "")
    # threads first: a development of an earlier story is headlined as the new development
    from . import importance as imp, threads
    main_facts = (facts + unsettled)[:8]
    from .editions import link_candidate, parent_pages
    link_candidate(store, story_id)      # later reports of a published story develop it
    parent_ids = threads.find_parents(store, router, story_id, story["signature"] or "", " ".join(main_facts[:4]))
    live = parent_pages(store, parent_ids)   # live, or already on the archive branch
    thread_ctx = "; ".join(live[p]["headline_en"] for p in parent_ids if p in live)
    # The headline model sees each statement's date, newest first. Sorted by support alone, old
    # background that every report retells (a January protest) outranked this week's arrest, and the
    # model tied the two together with "after".
    contested_top = [i for i in contested if not i["minor"]][:8] or contested[:8]
    headline_input = dict(facts=_newest_first(est_all), banned=sorted(banned), fallback=fallback,
                          unsettled=_newest_first(contested_top), thread=thread_ctx)
    headline = _headline(router, headline_input["facts"], banned, fallback, headline_input["unsettled"], thread_ctx)

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

    # importance: filler is never published; the rest is ranked for the front page
    rated = imp.assess(router, headline, main_facts, analysis.get("importance"))
    if rated != analysis.get("importance"):
        an = dict(story["analysis"] or {})
        an["importance"] = rated
        store.exec(update(stories).where(stories.c.id == story_id).values(analysis=an))
    if rated.get("filler"):
        log.info("story %s is filler (%s): not published", story_id, rated.get("reason"))
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
        "importance": {"score": rated["score"], "reason": rated.get("reason")},
        "rank": imp.rank(rated["score"], n_indep, langs),
        "thread": threads.root_of(store, story_id) if parents else story_id,
        "parents": parents,
        "children": children,
        "background": background,
        "headline": headline,
        "headline_is_fallback": headline == fallback,
        "_headline_input": headline_input,    # used once more after the article is written; not stored
        "has_established": bool(facts),
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
    hi["translation_complete"] = all(_key(s) in cache for s in strings)
    return hi


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
    from .narrative import essay_ok, input_hash, sections_from_payload, write_narrative
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
    unify_payload(payload)                  # one spelling per name, before the writer sees the statements
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
        hl = payload.pop("_headline_input", None)
        lead = " ".join(x["text"] for x in (nar.get("paragraphs") or [[]])[0])
        if hl and lead and router is not None:
            hd = _headline(router, hl["facts"], set(hl["banned"]), hl["fallback"], hl["unsettled"], hl["thread"], lead=lead)
            if hd != hl["fallback"]:
                payload["headline"] = hd
                payload["headline_is_fallback"] = False
    headline_missing = essay_good and payload.get("headline_is_fallback")
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
    payload.pop("_headline_input", None)    # the headline was written from the article's lead above
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
