"""One fact per statement: compound statements split into their single facts (owner, Oct 8 2026).

Story 13970 (RBI raises the repo rate): "the repo rate is the rate at which the RBI lends to banks" was
written three times, the rate decision four times, the EMI effect six or seven. Outlets write compound
sentences: A = "Repo rate is the rate at which the RBI lends to banks, and an increase raises borrowing
costs", B = "Repo rate is the rate at which the RBI provides loans to banks, which banks use as a basis for
lending". A and B share one fact and each adds another, so neither covers the other: they stayed two
statements and the shared part was written twice. The unit of the article is the single fact.

So, after reading and before matching, a fact row whose text looks compound is split into its single facts
(at most 3, 4 allowed; more and it stays whole). One small question to the model for a batch of such rows
("list the separate facts"), then code checks every piece (`_pieces_ok`): its numbers and names come from
the original, nearly all its words do, the pieces together carry every number and name of the original, a
speaker's statement keeps its speaker in every piece ("X said A and B" -> "X said A", "X said B"), a "not"
is never lost or added. Anything that fails keeps the row whole. The pieces replace the row (same article,
stance, speaker, evidence, time, context role) and go through matching like any fact, so the shared part
becomes one statement with every outlet that reported it, with its colour decided by the usual rules.

A row is asked about once (`rel.split`: "whole" or "piece"). Reading tier, paused with reading on quota.
"""
from __future__ import annotations

import logging
import re

from .db import Store, claims, delete, insert, select, update
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

MAX_PIECES = 4      # owner: up to three is good, four at most
BATCH = 10
MIN_WORDS = 14      # a short sentence is one fact
LIST_OVERLAP = 0.6  # two pieces sharing this share of their root words: a list split apart, not two facts
JOINS = re.compile(r"(?i)(,? and |, which |, while |; | but |, who |, as well as |, adding that | and that )")

PROMPT = """Each numbered sentence comes from a news report. Some say one fact; some join two, three or four
facts in one sentence. Split each sentence into its SEPARATE facts, each written as a full sentence a reader
understands on its own.

Rules:
- Use the sentence's own words. Add nothing, drop nothing, never change a number or a name.
- Repeat the subject instead of "which", "it", "they" ("Repo rate is the rate at which the RBI lends to
  banks, which banks use as a basis for lending" -> "Repo rate is the rate at which the RBI lends to banks." /
  "Banks use the repo rate as a basis for lending.").
- If someone SAID it, every fact keeps who said it ("Police said two men were arrested and a car was
  seized" -> "Police said two men were arrested." / "Police said a car was seized.").
- Keep "allegedly", "not", "no" with the fact they belong to.
- Do NOT split what is one fact: a change ("raised the rate from 5.25% to 5.50%"), a list of things that
  share one action ("arrested Ram, Shyam and Mohan"), a cause the sentence states ("X died after Y").
- At most 4 facts. One fact: return it unchanged, as a list of one.

Examples:
1. "The RBI raised the repo rate by 25 basis points to 5.50 per cent and changed its stance to calibrated
   tightening." -> ["The RBI raised the repo rate by 25 basis points to 5.50 per cent.", "The RBI changed its
   stance to calibrated tightening."]
2. "Governor Sanjay Malhotra said rate cuts were off the table and future actions may be limited to rate
   increases." -> ["Governor Sanjay Malhotra said rate cuts were off the table.", "Governor Sanjay Malhotra
   said future actions may be limited to rate increases."]
3. "The Monetary Policy Committee unanimously increased the repo rate from 5.25% to 5.50%." -> [the same
   sentence] (one fact)

SENTENCES:
{sentences}

Reply with JSON only: {{"results": [{{"n": 1, "facts": ["...", "..."]}}, {{"n": 2, "facts": ["..."]}}]}}"""


def _looks_compound(text: str) -> bool:
    return len(text.split()) >= MIN_WORDS and bool(JOINS.search(text))


def _names(text: str) -> set[str]:
    """Capitalised words, not at a sentence start (a piece starts sentences the original did not)."""
    out = set()
    for m in re.finditer(r"\b[A-Z][\w'.-]*", text):
        before = text[:m.start()].rstrip()
        if before and before[-1] not in ".!?:\"“":
            out.add(m.group(0).lower().rstrip(".'"))
    return out


def _caps(text: str) -> set[str]:
    return {w.lower().rstrip(".'") for w in re.findall(r"\b[A-Z][\w'.-]*", text)}


NEG = re.compile(r"(?i)\b(not|no|never|neither|nor|without|n't)\b")
SPEECH = re.compile(r"(?i)\b(said|says|stated|told|claimed|claims|alleged|alleges|accused|denied|added|noted|"
                    r"announced|according to|warned|asserted|maintained)\b")


def _pieces_ok(original: str, pieces: list[str]) -> bool:
    from .frames import numbers, words as roots
    if not (2 <= len(pieces) <= MAX_PIECES):
        return False
    nums = {round(x, 4) for x in numbers(original)}
    caps = _caps(original)
    orig_roots = set(roots(original))
    got_nums, got_names = set(), set()
    for p in pieces:
        if len(p.split()) < 3:
            return False
        pn = {round(x, 4) for x in numbers(p)}
        if not pn <= nums:
            return False                               # a number the original does not have
        if not _names(p) <= caps:
            return False                               # a name the original does not have
        pr = set(roots(p))
        if pr and len(pr - orig_roots) > max(1, len(pr) // 6):
            return False                               # words the original does not have
        if NEG.search(p) and not NEG.search(original):
            return False                               # a "not" added
        if SPEECH.search(original) and not SPEECH.search(p):
            return False                               # a said thing turned into a fact
        got_nums |= pn
        got_names |= _names(p) | _caps(p)
    if got_nums != nums:
        return False                                   # a number lost
    # a list split into near-identical sentences ("higher EMIs for home loans" / "... for car loans" / "... for
    # personal loans", story 13970) is one fact with a list, not separate facts: pieces of a real compound say
    # different things, so two pieces sharing most of their words means the split is wrong
    rs = [set(roots(p)) for p in pieces]
    for i in range(len(rs)):
        for j in range(i + 1, len(rs)):
            if rs[i] and rs[j] and len(rs[i] & rs[j]) / len(rs[i] | rs[j]) >= LIST_OVERLAP:
                return False
    if not _names(original) <= got_names:
        return False                                   # a name lost
    if NEG.search(original) and not any(NEG.search(p) for p in pieces):
        return False                                   # a "not" lost
    return True


def split_story(store: Store, router: Router | None, story_id: int) -> dict:
    """Compound fact rows of the story not yet looked at are split; {asked, split, pieces}."""
    stats = {"asked": 0, "split": 0, "pieces": 0}
    rows = store.rows(select(claims).where(claims.c.story_id == story_id, claims.c.kind.in_(("event", "claim"))))
    todo = [r for r in rows if not (r["rel"] or {}).get("split")]
    if not todo:
        return stats
    compound = [r for r in todo if _looks_compound(r["text"] or "")]
    simple = [r for r in todo if r not in compound]
    for r in simple:
        _mark(store, r, "whole")
    if router is None:
        return stats
    for start in range(0, len(compound), BATCH):
        chunk = compound[start:start + BATCH]
        body = "\n".join(f'{k + 1}. "{r["text"]}"' for k, r in enumerate(chunk))
        try:
            res = router.call("bulk", PROMPT.format(sentences=body), json_out=True, max_output_tokens=2500)
        except QuotaExhausted:
            break                                      # asked again on a later run
        except Exception as e:  # noqa: BLE001
            log.warning("split failed: %s", str(e)[:200])
            continue
        stats["asked"] += len(chunk)
        answered = {}
        for item in (res.data or {}).get("results", []) if isinstance(res.data, dict) else []:
            try:
                n = int(item["n"]) - 1
            except (KeyError, TypeError, ValueError):
                continue
            facts = [str(f).strip() for f in item.get("facts") or [] if str(f).strip()]
            if 0 <= n < len(chunk) and n not in answered:
                answered[n] = facts
        for n, r in enumerate(chunk):
            facts = answered.get(n)
            if facts is None:
                continue                               # no answer: asked again on a later run
            if len(facts) >= 2 and _pieces_ok(r["text"], facts):
                _replace(store, r, facts)
                stats["split"] += 1
                stats["pieces"] += len(facts)
            else:
                _mark(store, r, "whole")
    if stats["split"]:
        log.info("story %s: %d compound statements split into %d facts", story_id, stats["split"], stats["pieces"])
    return stats


def _mark(store: Store, r: dict, how: str) -> None:
    store.exec(update(claims).where(claims.c.id == r["id"]).values(rel={**(r["rel"] or {}), "split": how}))


def _replace(store: Store, r: dict, facts: list[str]) -> None:
    """The row's pieces take its place: same article, stance, speaker, evidence, time and context role (its
    loaded words, often Hindi, stay with the first piece, counted once); no frame (frames only propose, and the original's frame describes the whole). A relation that named the
    row now names its first piece (the main fact)."""
    rel = {k: v for k, v in (r["rel"] or {}).items() if k not in ("frame", "split")}
    rows = [dict(story_id=r["story_id"], article_id=r["article_id"], local_id=f"{r['local_id']}.{k + 1}"[:20],
                 kind=r["kind"], text=f[:500], stance=r["stance"], attributed_to=r["attributed_to"],
                 evidence=r["evidence"], loaded_words=list(r["loaded_words"] or []) if k == 0 else [],
                 time=r["time"], rel={**rel, "split": "piece"}) for k, f in enumerate(facts)]
    with store.engine.begin() as c:
        c.execute(delete(claims).where(claims.c.id == r["id"]))
        c.execute(insert(claims), rows)
    for x in store.rows(select(claims).where(claims.c.article_id == r["article_id"], claims.c.kind == "relation")):
        xr = dict(x["rel"] or {})
        changed = False
        for end in ("from", "to"):
            if xr.get(end) == r["local_id"]:
                xr[end] = rows[0]["local_id"]
                changed = True
        if changed:
            store.exec(update(claims).where(claims.c.id == x["id"]).values(rel=xr))
