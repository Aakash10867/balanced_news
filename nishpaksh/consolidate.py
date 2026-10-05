"""Tidy a story's statements before verdicts and writing: one cheap model call per story, only
when its statements changed.

  same       statements that state the same fact (paraphrases, Hindi/English, spelling variants)
             are merged, so the essay says each fact once
  conflicts  statements that cannot both be true (40 vs 50 people questioned) are marked as
             contradicting, so they show as "sources disagree"
  names      one spelling per name (Doolla / Dulla / Dula)
  speaker    who makes each allegation, claim, demand or denial, so the essay pins it on them

Guards, because a wrong merge hides a disagreement: two statements whose numbers differ are never
merged (they are marked as conflicting instead), and nothing is merged with a statement it
conflicts with.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from .db import Store, canonical, claims, select, stories, update
from .match import _add_conflict, _merge
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

PROMPT = """Below are statements extracted from several news reports about ONE story. Each line:
id | statement | who the reports attribute it to (as they wrote it; "article" = the outlet itself).

{lines}

Do seven things:
1. "same": groups of ids that state the same fact, even if worded differently, in another language,
   with names spelled differently, or told from the other side ("X filed a complaint" / "police received
   a complaint from X"). Do NOT group statements that differ in any number, date, place
   or person, or where one adds an important new fact.
2. "conflicts": pairs of ids that cannot both be true AS FACTS (for example different numbers, times or
   places for the same thing, or one says something happened and the other says it did not), each with
   the two incompatible values: [id, id, "40 people vs 50 people"]. If you cannot name two values that
   cannot both be true, it is not a conflict. A named party's answer to an allegation or finding is NOT
   a conflict (see 5). Statements that agree are never a conflict, however differently worded.
3. "names": SPELLING variants of the same name mapped to ONE spelling (use the most common one),
   e.g. {{"Dulla": "Doolla", "Dula": "Doolla"}}. Only different spellings or transliterations of the SAME
   name. Never map an alias, a nickname, a title or a different name of the same person to another
   (e.g. "Deepak Kumar" and "Mohammad Deepak" are two names, not two spellings: leave them).
4. "speaker": for each statement that is an allegation, accusation, claim, demand, denial or
   opinion made by a person or body, who makes it, in plain English ("Sahil's parents", "Professor
   Doolla", "Mumbai Police"). Leave out statements that are simply reported events.
5. "responses": pairs [first, second] where the second is a named person's or body's answer, rebuttal
   or denial of the first (e.g. "an analyst declared the sample unsafe" / "the company says the product
   is safe"). Both can be true as reports: one records a finding, the other a party's position.
6. "role": for each statement that is NOT about the story's main event, what it is: "background"
   (an earlier event that led to this one), "related" (a separate event the reports connect to this
   one), "explanation" (what a rule, term or finding means) or "next" (what happens next). Leave out
   statements about the main event. Also "related_event": for each "related" statement, a short
   name of that separate event with its date if known.
7. "name_conflicts": groups of ids where the reports name a DIFFERENT person or organisation for the
   same role in the same fact (e.g. one says company A's licence was suspended, another company B's),
   with the names: {{"ids": [5, 9], "names": ["Company A", "Company B"]}}. Not spelling variants.

Reply with JSON only:
{{"same": [[1, 4]], "conflicts": [[2, 7, "40 people vs 50 people"]], "names": {{"Dulla": "Doolla"}}, "speaker": {{"3": "Sahil's parents"}},
 "responses": [[8, 9]], "role": {{"11": "related"}}, "related_event": {{"11": "FSSAI finding on Nestle whitener, 4 October"}},
 "name_conflicts": [{{"ids": [5, 9], "names": ["Company A", "Company B"]}}]}}"""

ROLES = {"background", "related", "explanation", "reaction", "next"}
CONSOLIDATE_VERSION = 3   # part of the cache key: stories are consolidated again when the task changes

NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _hash(rows: list[dict]) -> str:
    key = [CONSOLIDATE_VERSION, sorted((r["id"], r["text"]) for r in rows)]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]


def is_spelling_variant(a: str, b: str) -> bool:
    """Doolla / Dulla / Dula, Shireesh / Shirish: yes. Deepak Kumar / Mohammad Deepak (an alias), or
    Kumar / Mohammad Deepak: no. Replacing an alias with another name rewrote "X, also known as Y" into
    "Y, also known as Y" on a real page, so only near-identical strings with the same number of words
    are accepted."""
    from difflib import SequenceMatcher
    a, b = a.strip().lower(), b.strip().lower()
    if a == b or len(a.split()) != len(b.split()):
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.6 and a[0] == b[0]


def _apply_names(text: str, names: dict[str, str]) -> str:
    for variant, canon in names.items():
        if len(variant) >= 3 and variant != canon:
            text = re.sub(rf"(?<!\w){re.escape(variant)}(?!\w)", canon, text)
    return text


def _repair_aliases(store: Store, story_id: int, analysis: dict) -> None:
    """Undo name mappings an earlier version accepted that were aliases, not spellings: restore each
    statement's text from the words of the report it came from, and consolidate again."""
    bad = {k: v for k, v in (analysis.get("names") or {}).items() if not is_spelling_variant(k, v)}
    if not bad:
        return
    for c in store.rows(select(canonical.c.id, canonical.c.text).where(canonical.c.story_id == story_id)):
        if not c["text"] or not any(v in c["text"] for v in bad.values()):
            continue
        first = store.one(select(claims.c.text).where(claims.c.canonical_id == c["id"]).order_by(claims.c.id))
        if first and first["text"] and first["text"] != c["text"]:
            store.exec(update(canonical).where(canonical.c.id == c["id"]).values(text=first["text"]))
    analysis["names"] = {k: v for k, v in (analysis.get("names") or {}).items() if k not in bad}
    analysis.pop("consolidated", None)
    log.info("consolidate story %s: undid %d alias mappings", story_id, len(bad))


def consolidate_story(store: Store, router: Router | None, story_id: int, max_statements: int = 90) -> dict:
    story = store.one(select(stories.c.id, stories.c.analysis).where(stories.c.id == story_id))
    if not story or router is None:
        return {}
    analysis = dict(story["analysis"] or {})
    _repair_aliases(store, story_id, analysis)
    rows = [r for r in store.rows(select(canonical.c.id, canonical.c.text, canonical.c.kind, canonical.c.conflicts)
                                  .where(canonical.c.story_id == story_id)) if r["kind"] != "relation" and r["text"]]
    if len(rows) < 2 or analysis.get("consolidated") == _hash(rows):
        return {}
    attrib: dict[int, set[str]] = {}
    support: dict[int, int] = {}
    for c in store.rows(select(claims.c.canonical_id, claims.c.attributed_to, claims.c.article_id)
                        .where(claims.c.story_id == story_id, claims.c.canonical_id.is_not(None))):
        attrib.setdefault(c["canonical_id"], set()).add(c["attributed_to"] or "article")
        support[c["canonical_id"]] = support.get(c["canonical_id"], 0) + 1
    rows.sort(key=lambda r: -support.get(r["id"], 0))
    rows = rows[:max_statements]
    by_id = {r["id"]: r for r in rows}
    lines = "\n".join(f'{r["id"]} | {r["text"]} | {", ".join(sorted(attrib.get(r["id"], {"article"})))[:120]}'
                      for r in rows)
    try:
        res = router.call("light", PROMPT.format(lines=lines), json_out=True, max_output_tokens=3000)
        data = res.data if isinstance(res.data, dict) else {}
    except QuotaExhausted:
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning("consolidate story %s failed: %s", story_id, str(e)[:200])
        return {}

    def ids(xs):
        out = []
        for x in xs or []:
            try:
                x = int(x)
            except (TypeError, ValueError):
                continue
            if x in by_id:
                out.append(x)
        return out

    from .match import real_difference
    conflicts: set[tuple[int, int]] = set()
    for c in data.get("conflicts") or []:
        if not isinstance(c, list) or len(c) < 2:
            continue
        pair = ids(c[:2])
        if len(pair) == 2 and pair[0] != pair[1] and real_difference(
                by_id[pair[0]]["text"], by_id[pair[1]]["text"], c[2] if len(c) > 2 else None):
            conflicts.add(tuple(sorted(pair)))
    responses = [tuple(p) for p in (ids(p) for p in data.get("responses") or []) if len(p) == 2 and p[0] != p[1]]
    response_pairs = {tuple(sorted(p)) for p in responses}
    # contradictions marked earlier are re-judged here, with the whole story in view: one that is not
    # confirmed (and is not a plain difference in numbers) is taken back (Oct 2026: two statements that
    # agreed stayed "disputed" for good because conflicts could only ever be added)
    from .match import _remove_conflict as _unmark
    for r in rows:
        for o in r["conflicts"] or []:
            pair = tuple(sorted((r["id"], o)))
            if o not in by_id or pair in conflicts:
                continue
            a_, b_ = by_id[pair[0]]["text"], by_id[pair[1]]["text"]
            if NUM.findall(a_) and NUM.findall(b_) and set(NUM.findall(a_)) != set(NUM.findall(b_)):
                conflicts.add(pair)            # different numbers for one thing: kept
            else:
                _unmark(store, *pair)
    # a party's answer to a claim is a response, not a contradiction (Oct 2026: a company's statement
    # that its product is safe turned the analyst's finding amber): take such pairs out of conflicts
    from .match import _remove_conflict
    for a, b in conflicts & response_pairs:
        _remove_conflict(store, a, b)
    conflicts -= response_pairs
    merged = 0
    gone: set[int] = set()
    for group in data.get("same") or []:
        g = [x for x in dict.fromkeys(ids(group)) if x not in gone]
        if len(g) < 2:
            continue
        dst = max(g, key=lambda x: (support.get(x, 0), len(by_id[x]["text"])))
        for src in g:
            if src == dst:
                continue
            a, b = by_id[src]["text"], by_id[dst]["text"]
            if tuple(sorted((src, dst))) in conflicts:
                continue
            if set(NUM.findall(a)) != set(NUM.findall(b)) and NUM.findall(a) and NUM.findall(b):
                conflicts.add(tuple(sorted((src, dst))))   # different numbers: a disagreement, not a duplicate
                continue
            _merge(store, src, dst)
            gone.add(src)
            merged += 1
    added = 0
    for a, b in conflicts:
        if a in gone or b in gone:
            continue
        if b not in (by_id[a]["conflicts"] or []):
            _add_conflict(store, a, b)
            added += 1

    names = {str(k).strip(): str(v).strip() for k, v in (data.get("names") or {}).items()
             if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip() and is_spelling_variant(k, v)}
    if names:
        for r in rows:
            if r["id"] in gone:
                continue
            new = _apply_names(r["text"], names)
            if new != r["text"]:
                store.exec(update(canonical).where(canonical.c.id == r["id"]).values(text=new))
    speakers = dict(analysis.get("speakers") or {})
    for k, v in (data.get("speaker") or {}).items():
        try:
            k = int(k)
        except (TypeError, ValueError):
            continue
        if k in by_id and k not in gone and isinstance(v, str) and v.strip():
            speakers[str(k)] = _apply_names(v.strip(), names)[:80]
    alive = lambda x: x not in gone   # noqa: E731
    roles = {str(k): v for k, v in (analysis.get("roles") or {}).items()}
    for k, v in (data.get("role") or {}).items():
        k = ids([k])
        if k and alive(k[0]) and v in ROLES:
            roles[str(k[0])] = v
    related = dict(analysis.get("related_event") or {})
    for k, v in (data.get("related_event") or {}).items():
        k = ids([k])
        if k and isinstance(v, str) and v.strip():
            related[str(k[0])] = v.strip()[:160]
    prev_resp = {tuple(p) for p in analysis.get("responses") or []}
    all_resp = sorted({p for p in prev_resp | set(responses) if alive(p[0]) and alive(p[1])})
    name_conf = dict(analysis.get("name_conflicts") or {})
    for g in data.get("name_conflicts") or []:
        if not isinstance(g, dict):
            continue
        names_g = [str(n).strip() for n in g.get("names") or [] if str(n).strip()]
        if len(set(n.lower() for n in names_g)) < 2:
            continue
        for x in ids(g.get("ids")):
            if alive(x):
                name_conf[str(x)] = names_g[:4]
    after = [r for r in store.rows(select(canonical.c.id, canonical.c.text, canonical.c.kind)
                                   .where(canonical.c.story_id == story_id)) if r["kind"] != "relation" and r["text"]]
    analysis.update(consolidated=_hash(after), speakers=speakers,
                    names={**(analysis.get("names") or {}), **names},
                    roles=roles, related_event=related, responses=[list(p) for p in all_resp],
                    name_conflicts=name_conf)
    store.exec(update(stories).where(stories.c.id == story_id).values(analysis=analysis))
    log.info("consolidate story %s: %d merged, %d contradictions, %d names", story_id, merged, added, len(names))
    return {"merged": merged, "conflicts": added, "names": len(names)}
