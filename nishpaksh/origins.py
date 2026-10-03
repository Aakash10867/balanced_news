"""Independent origins: who actually knew each fact by their own means.

Ten outlets repeating one police statement have ONE origin, the police. A statement counts as
established only if it traces to at least two origins (see verify.base_verdicts).

For every report of a statement (a claim row: one article supporting one canonical statement):
  attributed to a named source  -> that source, spellings merged by a model once per story;
                                   officials, ministries and police of one government -> one origin
  attributed to nobody / "sources" / "reports"  -> the shared POOL
  in the article's own voice    -> the outlet's owner group, but only if the article shows original
                                   reporting (it reported details no earlier article had); a wire
                                   copy -> the agency; the government press office -> that government;
                                   anything else -> the POOL
The POOL never counts toward the two origins: unattributed repetition is exactly how a rumour
looks like many sources. Every uncertain case resolves toward fewer origins.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict

from .db import Store, articles, canonical, claims, select, stories, update
from .ownership import government_of, owner_of
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)
POOL = "pool"
OWN_WORDS = {"", "article", "the article", "report", "the report", "news report", "reporter", "correspondent",
             "this article", "outlet", "editorial", "our correspondent"}
ANON_WORDS = {"unnamed source", "unnamed sources", "source", "sources", "reports", "media reports", "unknown",
              "none", "people familiar", "insiders", "sources said", "a source", "social media", "viral video",
              "anonymous", "reportedly", "it is said", "local media", "media"}
GOV_KINDS = {"police", "government", "official", "ministry", "agency_gov"}

ATTRIB_PROMPT = """These are the sources that news reports in ONE story attribute statements to, written as the
reports wrote them. Story: {signature}

{items}

For each numbered source, say who it is:
- "name": one canonical name. Use the SAME name for every spelling, title or role that refers to the
  same person or body (e.g. "DCP North", "Delhi Police spokesperson", "police" -> "Delhi Police" if the story
  makes clear it is the same force).
- "kind": person | police | government | court | party | company | witness | document | anonymous | other
  (government = a ministry, department, minister, chief minister, official or government agency;
   document = an FIR, court order, report or data release; anonymous = unnamed, "sources", "officials" with no
   body named, social media)
- "government": the government this source speaks for, if it is a minister, official, ministry, department,
  police force or government agency: "Union" for the central Government of India, the state's name for a
  state government, or the country's name for a foreign government. Otherwise null. A political party's
  spokesperson is not a government. A court is not a government.

Reply with JSON only: {{"items": [{{"n": 1, "name": "...", "kind": "...", "government": null}}]}}"""

CHECKABLE_PROMPT = """For each numbered statement from a news story, decide its type:
- "fact": something that records, documents, video or direct observation could confirm or refute:
  who did what, where and when; numbers; arrests; deaths; official decisions; that a person SAID something.
- "characterisation": a motive, intention, blame, cause asserted without evidence, label, opinion,
  prediction or interpretation (e.g. "it was a planned conspiracy", "the policy failed farmers").

{items}

Reply with JSON only: {{"items": [{{"n": 1, "type": "fact"}}]}}"""

GENERIC_AUTHOR = re.compile(r"(?i)\b(staff|desk|bureau|team|news|web|correspondent|reporter|online|editor|agencies|"
                            r"agency|trending|digital|ht|toi|express|network)\b"
                            r"|संवाददाता|डेस्क|ब्यूरो|एजेंसी|टीम|न्यूज़|न्यूज")


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower().strip(".,:;\"'"))


AGENCIES = {"pti": "pti", "press trust of india": "pti", "ani": "ani", "asian news international": "ani",
            "ians": "ians", "indo-asian news service": "ians", "uni": "uni", "united news of india": "uni",
            "reuters": "reuters", "afp": "afp", "agence france-presse": "afp", "ap": "ap", "associated press": "ap",
            "bhasha": "pti", "भाषा": "pti", "एजेंसी": POOL, "agencies": POOL, "agency": POOL}


def _key_for(name: str, kind: str, gov: str | None) -> str:
    """One key per real-world source, however the reports name it."""
    from .ownership import canonical_outlet, is_known_outlet, owner_of as _owner
    n = _norm(name)
    if kind == "anonymous" or not n:
        return POOL
    if n in AGENCIES:
        return AGENCIES[n] if AGENCIES[n] == POOL else "agency:" + AGENCIES[n]
    if is_known_outlet(name):
        # "according to Times of India": that outlet's reporting; counted like its own voice (see origin_of)
        return "outlet:" + _norm(_owner(canonical_outlet(name)))
    if kind in GOV_KINDS:
        # an official or police force whose government is unknown could be any of the governments
        # already counted: it adds no origin
        return "gov:" + _norm(gov) if gov else POOL
    if gov:
        return "gov:" + _norm(gov)          # a minister or chief minister speaks for the government
    if kind == "court":
        return "court:" + n
    return "src:" + n


def _attribution_map(router: Router | None, signature: str, raws: list[str], known: dict) -> dict:
    """raw attribution -> {"key", "kind", "government", "name"}; cached in the story's analysis."""
    out = dict(known)
    todo = [r for r in raws if r not in out]
    for r in list(todo):
        n = _norm(r)
        if n in OWN_WORDS:
            out[r] = {"key": "OWN", "kind": "own", "government": None}
            todo.remove(r)
        elif n in ANON_WORDS or n.startswith(("unnamed", "anonymous", "sources ")):
            out[r] = {"key": POOL, "kind": "anonymous", "government": None}
            todo.remove(r)
        elif n in AGENCIES:
            out[r] = {"key": _key_for(n, "agency", None), "kind": "agency", "government": None, "name": n}
            todo.remove(r)
    names_so_far = sorted({v["name"] for v in out.values() if v.get("name")})
    for i in range(0, len(todo), 40):
        chunk = todo[i:i + 40]
        result: dict[int, dict] = {}
        if router is not None:
            prompt = ATTRIB_PROMPT.format(signature=signature, items="\n".join(f"{k + 1}. {r}" for k, r in enumerate(chunk)))
            if names_so_far:
                prompt += ("\n\nNames already used for sources in this story (reuse them exactly when a source is "
                           "the same): " + "; ".join(names_so_far[:80]))
            try:
                res = router.call("light", prompt, json_out=True, max_output_tokens=2500)
                for it in (res.data or {}).get("items", []) if isinstance(res.data, dict) else []:
                    try:
                        result[int(it["n"]) - 1] = it
                    except (KeyError, TypeError, ValueError):
                        continue
            except QuotaExhausted:
                pass
            except Exception as e:  # noqa: BLE001
                log.warning("attribution grouping failed: %s", str(e)[:200])
        for k, r in enumerate(chunk):
            it = result.get(k)
            if not it:
                # no model answer: we cannot tell whether "police" and "Delhi Police" are one source,
                # so this report counts toward no origin for now (pooled) and is asked about again
                # next run. Never cached.
                out[r] = {"key": POOL, "kind": "unresolved", "government": None, "unresolved": True}
                continue
            kind = str(it.get("kind") or "other").lower()
            gov = it.get("government")
            gov = str(gov).strip() if gov and str(gov).strip().lower() not in ("null", "none", "") else None
            name = str(it.get("name") or r).strip()
            out[r] = {"key": _key_for(name, kind, gov), "kind": kind, "government": gov, "name": _norm(name)}
            names_so_far = sorted(set(names_so_far) | {_norm(name)})
    return out


# a real dateline: "NEW DELHI:", "IMPHAL, Oct 3:", "Mumbai, October 3 (PTI) -" ... A capitalised place
# name alone is not enough ("Also Read:", "Highlights:" must not pass), so either the place is in
# capitals or it is followed by a date.
DATELINE = re.compile(
    r"^\s*(?:[A-Z][A-Z.\- ]{2,28}(?:,\s*[A-Z][a-z]{2,8}\.?\s*\d{1,2}(?:,\s*\d{4})?)?\s*[:—–(]"
    r"|[A-Z][a-z]+(?:\s[A-Z][a-z]+)?,\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s*\d{1,2}\b)")


def article_originality(arts: dict[int, dict], first_support: dict[int, int], unique: dict[int, int]) -> dict[int, bool]:
    """Original reporting needs a named reporter, and either a dateline from the place or details
    the article reported first that no other article in the story has. A rewritten press note has
    neither. Wire copies are never original here (their origin is the agency)."""
    out = {}
    for aid, a in arts.items():
        if a.get("agency"):
            out[aid] = False
            continue
        author = (a.get("author") or "").strip()
        named = bool(author) and len(author) <= 60 and not GENERIC_AUTHOR.search(author) \
            and _norm(author) != _norm(a.get("outlet"))
        dateline = bool(DATELINE.match(a.get("text") or ""))
        out[aid] = named and (dateline or (first_support.get(aid, 0) >= 2 and unique.get(aid, 0) >= 1))
    return out


def independent_read_outlets(store: Store, story_id: int) -> int:
    """Owner groups (wire copies merged) with at least one fully read article in the story."""
    from .wire import independence_groups
    arts = store.rows(select(articles.c.id, articles.c.outlet, articles.c.url, articles.c.agency,
                             articles.c.wire_group, articles.c.extracted_at, articles.c.text_source)
                      .where(articles.c.story_id == story_id))
    groups = independence_groups(arts)
    return len({groups[a["id"]] for a in arts if a["extracted_at"] and a["text_source"] != "summary"})


def compute_origins(store: Store, router: Router | None, story_id: int) -> dict[int, dict]:
    """Per canonical statement: {"origins": [...], "outlets": n independent read outlets,
    "first_seen": iso}. Stored in canonical.origins; returned for the caller."""
    from .wire import independence_groups
    story = store.one(select(stories).where(stories.c.id == story_id))
    if not story:
        return {}
    arts = {a["id"]: a for a in store.rows(select(
        articles.c.id, articles.c.outlet, articles.c.url, articles.c.author, articles.c.agency,
        articles.c.wire_group, articles.c.text, articles.c.text_source, articles.c.published_at,
        articles.c.extracted_at).where(articles.c.story_id == story_id))}
    rows = [r for r in store.rows(select(claims).where(claims.c.story_id == story_id,
                                                      claims.c.canonical_id.is_not(None)))
            if r["article_id"] in arts]
    groups = independence_groups(list(arts.values()))
    analysis = dict(story["analysis"] or {})
    amap = _attribution_map(router, story["signature"] or "", sorted({r["attributed_to"] or "" for r in rows}),
                            analysis.get("attribution_map") or {})
    analysis["attribution_map"] = {k: v for k, v in amap.items() if not v.get("unresolved")}
    store.exec(update(stories).where(stories.c.id == story_id).values(analysis=analysis))

    # who reported each statement first
    supporters: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if r["stance"] in ("asserts", "attributes"):
            supporters[r["canonical_id"]].append(r)
    first_support: dict[int, int] = defaultdict(int)
    unique: dict[int, int] = defaultdict(int)
    for cid, rs in supporters.items():
        earliest = min(rs, key=lambda r: (arts[r["article_id"]]["published_at"], r["article_id"]))
        first_support[earliest["article_id"]] += 1
        if len({r["article_id"] for r in rs}) == 1:
            unique[rs[0]["article_id"]] += 1
    original = article_originality(arts, first_support, unique)
    original_owners = {owner_of(a["outlet"], a["url"]) for aid, a in arts.items() if original.get(aid)}

    def origin_of(r: dict) -> str:
        info = amap.get(r["attributed_to"] or "") or {"key": "OWN"}
        if info["key"].startswith("outlet:"):
            # "X reported": X's reporting, which counts only if X's own article here is original
            owner_key = info["key"][len("outlet:"):]
            return ("own:" + owner_key) if owner_key in {_norm(o) for o in original_owners} else POOL
        if info["key"] != "OWN":
            return info["key"]
        a = arts[r["article_id"]]
        owner = owner_of(a["outlet"], a["url"])
        gov = government_of(owner)
        if gov:
            return "gov:" + _norm(gov)
        if a.get("agency"):
            return "agency:" + _norm(a["agency"])
        return ("own:" + _norm(owner)) if original.get(r["article_id"]) else POOL

    out = {}
    canon_rows = store.rows(select(canonical.c.id, canonical.c.origins).where(canonical.c.story_id == story_id))
    prev_origins = {c["id"]: c["origins"] for c in canon_rows}
    for c in canon_rows:
        cid = c["id"]
        rs = supporters.get(cid, [])
        origins = sorted({origin_of(r) for r in rs})
        read = [r for r in rs if arts[r["article_id"]]["extracted_at"] and arts[r["article_id"]]["text_source"] != "summary"]
        info = {
            "origins": origins,
            "n_origins": len([o for o in origins if o != POOL]),
            "outlets": len({groups[r["article_id"]] for r in read}),
            "first_seen": min((arts[r["article_id"]]["published_at"] for r in rs), default=None),
        }
        if info["first_seen"] is not None:
            info["first_seen"] = info["first_seen"].isoformat(timespec="minutes")
        prev = prev_origins.get(cid) or {}
        if prev.get("met_at"):
            info["met_at"] = prev["met_at"]   # base_verdicts keeps or clears it
        store.exec(update(canonical).where(canonical.c.id == cid).values(origins=info))
        out[cid] = info
    return out


def classify_checkable(store: Store, router: Router | None, story_id: int, only: set[int] | None = None) -> None:
    """Fact vs characterisation, once per statement. Unclassified statements are treated as
    characterisations (never green), so a missing answer can only make the page more cautious.
    `only` limits the question to statements that could otherwise be established."""
    todo = [c for c in store.rows(select(canonical.c.id, canonical.c.text, canonical.c.kind, canonical.c.checkable)
                                  .where(canonical.c.story_id == story_id))
            if c["checkable"] is None and c["kind"] != "relation" and c["text"]
            and (only is None or c["id"] in only)]
    if router is None:
        return
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        try:
            res = router.call("light", CHECKABLE_PROMPT.format(
                items="\n".join(f'{k + 1}. "{c["text"]}"' for k, c in enumerate(chunk))),
                json_out=True, max_output_tokens=1500)
        except QuotaExhausted:
            return
        except Exception as e:  # noqa: BLE001
            log.warning("checkable classification failed: %s", str(e)[:200])
            continue
        for it in (res.data or {}).get("items", []) if isinstance(res.data, dict) else []:
            try:
                k = int(it["n"]) - 1
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= k < len(chunk) and it.get("type") in ("fact", "characterisation"):
                store.exec(update(canonical).where(canonical.c.id == chunk[k]["id"])
                           .values(checkable=it["type"] == "fact"))


def assess_story(store: Store, router: Router | None, story_id: int) -> bool:
    """Origins and fact/characterisation for one story, spending model calls only where they can
    change something: a story with fewer than 3 independently read outlets can establish nothing.
    Returns the interim publishing rule: 3+ independent read outlets and 2+ independent origins
    across the story."""
    from .config import SETTINGS
    n_out = independent_read_outlets(store, story_id)
    use_model = router if n_out >= SETTINGS.established_min_outlets else None
    info = compute_origins(store, use_model, story_id)
    eligible = {cid for cid, o in info.items()
                if o["outlets"] >= SETTINGS.established_min_outlets and o["n_origins"] >= SETTINGS.established_min_origins}
    if eligible:
        classify_checkable(store, router, story_id, only=eligible)
    story_origins = {o for v in info.values() for o in v["origins"] if o != POOL}
    return n_out >= SETTINGS.qualify_min_outlets and len(story_origins) >= SETTINGS.established_min_origins


__all__ = ["compute_origins", "classify_checkable", "article_originality", "assess_story", "POOL"]
_ = json  # noqa
