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

Do four things:
1. "same": groups of ids that state the same fact, even if worded differently, in another language,
   or with names spelled differently. Do NOT group statements that differ in any number, date, place
   or person, or where one adds an important new fact.
2. "conflicts": pairs of ids that cannot both be true (for example different numbers, times or places
   for the same thing, or one says something happened and the other says it did not).
3. "names": spelling variants of the same person or place mapped to ONE spelling (use the most common
   one), e.g. {{"Dulla": "Doolla", "Dula": "Doolla"}}. Only real variants of the same name.
4. "speaker": for each statement that is an allegation, accusation, claim, demand, denial or
   opinion made by a person or body, who makes it, in plain English ("Sahil's parents", "Professor
   Doolla", "Mumbai Police"). Leave out statements that are simply reported events.

Reply with JSON only:
{{"same": [[1, 4]], "conflicts": [[2, 7]], "names": {{"Dulla": "Doolla"}}, "speaker": {{"3": "Sahil's parents"}}}}"""

NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _hash(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(sorted((r["id"], r["text"]) for r in rows)).encode()).hexdigest()[:16]


def _apply_names(text: str, names: dict[str, str]) -> str:
    for variant, canon in names.items():
        if len(variant) >= 3 and variant != canon:
            text = re.sub(rf"(?<!\w){re.escape(variant)}(?!\w)", canon, text)
    return text


def consolidate_story(store: Store, router: Router | None, story_id: int, max_statements: int = 90) -> dict:
    story = store.one(select(stories.c.id, stories.c.analysis).where(stories.c.id == story_id))
    if not story or router is None:
        return {}
    analysis = dict(story["analysis"] or {})
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

    conflicts = {tuple(sorted(p)) for p in (tuple(ids(p)) for p in data.get("conflicts") or []) if len(p) == 2 and p[0] != p[1]}
    for r in rows:  # contradictions already known
        for o in r["conflicts"] or []:
            conflicts.add(tuple(sorted((r["id"], o))))
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
             if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()}
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
    after = [r for r in store.rows(select(canonical.c.id, canonical.c.text, canonical.c.kind)
                                   .where(canonical.c.story_id == story_id)) if r["kind"] != "relation" and r["text"]]
    analysis.update(consolidated=_hash(after), speakers=speakers,
                    names={**(analysis.get("names") or {}), **names})
    store.exec(update(stories).where(stories.c.id == story_id).values(analysis=analysis))
    log.info("consolidate story %s: %d merged, %d contradictions, %d names", story_id, merged, added, len(names))
    return {"merged": merged, "conflicts": added, "names": len(names)}
