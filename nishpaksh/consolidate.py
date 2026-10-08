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

from .db import Store, articles, canonical, claims, select, stories, update
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
SAME_ASK_MAX = 30         # same-fact questions per consolidation, closest pairs first (the rest next time)
CONSOLIDATE_VERSION = 9   # part of the cache key: stories are consolidated again when the task changes
                          # (9, Oct 8 2026: paraphrases proposed by topic, dupes.py)



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


ALIAS = re.compile(r"(?i)\b(?:alias|urf|also known as|a\.?k\.?a\.?|also called|known as|or)\b|[(/]|उर्फ")


def named_together(names: list[str], texts: list[str | None]) -> bool:
    """Two of the names appear in one sentence of a statement or report, not joined as aliases ("X
    alias Y", "X (Y)"): they are two people. Code's check on the model's "reports name different
    people for one fact"."""
    low = [n.lower() for n in names if n and len(n) >= 4]
    for text in texts:
        for sent in re.split(r"(?<=[.!?।])\s+", text or ""):
            s = sent.lower()
            found = sorted((s.find(n), n) for n in low if n in s)
            for (i, a), (j, b) in zip(found, found[1:]):
                if a == b or a in b or b in a:
                    continue
                if not ALIAS.search(sent[i + len(a):j]):
                    return True
    return False


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
    rows = [r for r in store.rows(select(canonical.c.id, canonical.c.text, canonical.c.kind, canonical.c.conflicts,
                                         canonical.c.rel)
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

    from .match import _remove_conflict
    responses = [tuple(p) for p in (ids(p) for p in data.get("responses") or []) if len(p) == 2 and p[0] != p[1]]
    response_pairs = {tuple(sorted(p)) for p in responses}
    # Possible disputes, from every source, are only PROPOSALS: the consolidation model's "conflicts",
    # disputes marked earlier, and the frames. One gate decides them all (disputes.py, owner Oct 7 2026).
    proposed: set[tuple[int, int]] = set()
    for c in data.get("conflicts") or []:
        if isinstance(c, list) and len(c) >= 2:
            pair = ids(c[:2])
            if len(pair) == 2 and pair[0] != pair[1]:
                proposed.add(tuple(sorted(pair)))
    earlier = {tuple(sorted((r["id"], o))) for r in rows for o in r["conflicts"] or [] if o in by_id}
    proposed |= earlier
    same_groups = [[x for x in dict.fromkeys(ids(g))] for g in data.get("same") or []]
    # the frames only propose (same facts and disputes); they decide nothing
    from .frames import compare as frame_compare
    from .match import frame_of
    fr = {r["id"]: frame_of(r) for r in rows}
    with_frames = [r["id"] for r in rows if fr[r["id"]][0]]
    by_frame: dict[tuple[int, int], str] = {}
    for i, a in enumerate(with_frames):
        for b in with_frames[i + 1:]:
            by_frame[(a, b) if a < b else (b, a)] = frame_compare(fr[a][0], fr[b][0], fr[a][1], fr[b][1])
    proposed |= {p for p, v in by_frame.items() if v == "conflict"}
    # ONE structure decides the same fact (relate.py, owner Oct 7 2026): the words first, by code; a
    # model asked twice only about the middle cases; the consolidation model's "same" groups and the
    # frames' "same" are proposals, checked the same way. The frames no longer veto anything here.
    from .match import covers_facts, same_facts
    from .relate import Profile, group, numbers_close, relate, resolve_covered
    key = lambda p: hashlib.sha256(json.dumps([by_id[p[0]]["text"], by_id[p[1]]["text"]]).encode()).hexdigest()[:16]  # noqa: E731
    texts = {i: by_id[i]["text"] for i in by_id}
    times = {i: fr[i][1] for i in by_id}
    prof = {i: Profile(t) for i, t in texts.items()}
    code_same, covered, ask_pairs, ask_cover = group(texts, times)
    proposals = {tuple(sorted((x, y))) for g in same_groups for i, x in enumerate(g) for y in g[i + 1:] if x != y}
    proposals |= {p for p, v in by_frame.items() if v == "same"}
    # the same fact in other words, found topic by topic (dupes.py, Oct 8 2026, story 13970): proposals only,
    # decided below like every other proposal; a "covers" proposal must keep the short line's numbers and its
    # "not" before the twice-asked question
    from .dupes import propose
    dupe_checks = dict(analysis.get("dupe_checks") or {})
    d_same, d_cover = propose(router, texts, dupe_checks)
    proposals |= d_same
    for big, small in d_cover:
        if (prof[big].neg == prof[small].neg and all(any(abs(x - y) <= 0.05 * max(abs(x), abs(y), 1) for y in prof[big].nums)
                                                    for x in prof[small].nums)):
            ask_cover.append((big, small))
    ask_cover = list(dict.fromkeys(ask_cover))
    code_same_set = {tuple(sorted(p)) for p in code_same}
    for a, b in sorted(proposals - code_same_set):
        r = relate(texts[a], texts[b], times[a], times[b], prof[a], prof[b])
        # a model or the frames said "same": checked by the twice-asked question, unless the words plainly
        # differ (one negated, or numbers that do not agree even after rounding)
        if r in ("same", "ask") or (prof[a].neg == prof[b].neg and numbers_close(prof[a], prof[b])):
            ask_pairs.append((a, b))
    ask_pairs = list(dict.fromkeys(tuple(sorted(p)) for p in ask_pairs if tuple(sorted(p)) not in code_same_set))
    same_checks = dict(analysis.get("same_checks") or {})
    todo = [p for p in ask_pairs if key(p) not in same_checks][:SAME_ASK_MAX]
    for p, ok in zip(todo, same_facts(router, [(texts[a], texts[b]) for a, b in todo])):
        same_checks[key(p)] = ok
    ckey = lambda p: "c" + key(p)  # noqa: E731
    todo = [p for p in ask_cover if ckey(p) not in same_checks][:SAME_ASK_MAX]
    for p, ok in zip(todo, covers_facts(router, [(texts[a], texts[b]) for a, b in todo])):
        same_checks[ckey(p)] = ok
    for big, small in ask_cover:
        if same_checks.get(ckey((big, small))):
            covered.setdefault(small, big)
    same_groups = [list(p) for p in code_same_set] + [list(p) for p in ask_pairs if same_checks.get(key(p))]
    # disputes: one gate (code finds the same question with a different answer; figures that changed
    # over time are updates; the model asked "can both be true?" twice decides the rest)
    from .disputes import judge
    rtimes: dict[int, list] = {}
    for r in store.rows(select(claims.c.canonical_id, articles.c.published_at, articles.c.fetched_at)
                        .select_from(claims.join(articles, articles.c.id == claims.c.article_id))
                        .where(claims.c.story_id == story_id, claims.c.canonical_id.is_not(None))):
        t = r["published_at"] or r["fetched_at"]
        if t is not None:
            rtimes.setdefault(r["canonical_id"], []).append(t)
    checks = dict(analysis.get("conflict_checks") or {})     # answers kept per (ordered) pair of texts
    conflicts, doubtful, updates = judge(router, texts, rtimes, proposed, response_pairs, checks)
    for pair in earlier - conflicts:
        _remove_conflict(store, *pair)       # not confirmed by the gate: taken back
    keep_apart = conflicts | {tuple(sorted(p)) for p in updates.items()}
    merged = 0
    gone: set[int] = set()
    merged_into: dict[int, int] = {}
    for g in same_groups:
        g = [x for x in g if x not in gone]
        if len(g) < 2:
            continue
        dst = max(g, key=lambda x: (support.get(x, 0), len(by_id[x]["text"])))
        for src in g:
            if src == dst or tuple(sorted((src, dst))) in keep_apart:
                continue
            _merge(store, src, dst)
            merged_into[src] = dst
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
    # checked by code, every time (earlier entries too): two names that appear together in one statement
    # or one report, not joined as aliases, are two people, not one person named two ways (Oct 7 2026,
    # story 11569: "Ritesh Kumar Singh is accused of assisting Abhishek Kumar Singh", yet the page said
    # "Ritesh Kumar Singh, named in reports as Abhishek Kumar Singh")
    if name_conf:
        texts = [r["text"] for r in store.rows(select(canonical.c.text).where(canonical.c.story_id == story_id))]
        texts += [r["text"] for r in store.rows(select(articles.c.text).where(articles.c.story_id == story_id))]
        name_conf = {k: v for k, v in name_conf.items() if not named_together(v, texts)}
    after = [r for r in store.rows(select(canonical.c.id, canonical.c.text, canonical.c.kind)
                                   .where(canonical.c.story_id == story_id)) if r["kind"] != "relation" and r["text"]]
    analysis.update(consolidated=_hash(after), speakers=speakers,
                    names={**(analysis.get("names") or {}), **names},
                    roles=roles, related_event=related, responses=[list(p) for p in all_resp],
                    name_conflicts=name_conf, conflict_checks=dict(list(checks.items())[-400:]),
                    same_checks=dict(list(same_checks.items())[-400:]),
                    dupe_checks=dict(list(dupe_checks.items())[-60:]),
                    # a line another line says in full, with more: not written on its own (relate.py)
                    covered={str(k): v for k, v in resolve_covered(
                        {merged_into.get(k, k): merged_into.get(v, v) for k, v in covered.items()}).items()
                        if alive(k) and alive(v)},
                    # a possible contradiction the check could not settle: not shown as a dispute, but
                    # neither statement can be established while it stands (verify.base_verdicts)
                    doubtful_conflicts=sorted(x for x in doubtful if alive(x)),
                    # an older figure and the newer one that replaced it (both true when reported)
                    updates={str(merged_into.get(k, k)): merged_into.get(v, v) for k, v in updates.items()
                             if alive(merged_into.get(k, k)) and alive(merged_into.get(v, v))})
    store.exec(update(stories).where(stories.c.id == story_id).values(analysis=analysis))
    log.info("consolidate story %s: %d merged, %d contradictions, %d names", story_id, merged, added, len(names))
    return {"merged": merged, "conflicts": added, "names": len(names)}
