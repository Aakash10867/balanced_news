"""Statements across a story's reports: on arrival, a new report's statement joins one whose words are
plainly the same (relate.py) or becomes a new statement. The model questions the story review asks are
here: same fact? (same_facts), says it all? (covers_facts), can both be true? (check_conflicts). Each is
one plain question, asked in small batches; the callers ask twice and decide (relate.py, disputes.py).
"""
from __future__ import annotations

import logging
import re

from .config import SETTINGS
from .db import Store, canonical, claims, delete, select, update
from .relate import relate
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

CONFLICT_CHECK_PROMPT = """Each numbered line has two statements, A and B, from news reports about the same story.
For each line, decide whether A and B can both be true at the same time.

"both_true" - they can both be true. This is the answer when they answer DIFFERENT questions (two different
              steps, dates, events, people, places, measures or parts of the story: when something was
              signed and when it took effect, a target and a pledge, the number injured and the number dead),
              when one only adds a detail or a number the other leaves out, when they are the same fact told
              from two sides or by two parties, or when one is a party's response to the other.
"cannot_both_be_true" - they give DIFFERENT ANSWERS TO THE SAME QUESTION about the same thing: the same event,
              the same moment, the same measure (one says 40 died in the collapse, the other 50; one says
              he was arrested, the other that he was not; one says Friday, the other Saturday, for the same
              event).
"unsure"    - you cannot tell from the statements.

{pairs}

For each line give "question": the one question both statements answer, if there is one, and "answer".
Reply with JSON only: {{"results": [{{"n": 1, "question": "How many people died in the collapse?", "answer": "cannot_both_be_true"}}, {{"n": 2, "question": "", "answer": "both_true"}}]}}"""

CHECK_ANSWERS = {"both_true", "cannot_both_be_true", "unsure"}


def check_conflicts(router: Router | None, pairs: list[tuple[str, str]]) -> list[str]:
    """The test that defines a contradiction, asked on its own: can both statements be true? Every
    proposed contradiction passes through it, whoever proposed it (a model naming two values, or code
    seeing different numbers). Two values that differ are not enough: Oct 2026, "signed in March 2024"
    and "entered into force last October", and a target announced by one side and the same figures
    pledged by the other, were shown as disputes. Anything but a clear "cannot both be true" is not a
    contradiction; "unsure" is returned so the caller can keep such statements from being established."""
    out = ["unsure"] * len(pairs)
    if router is None or not pairs:
        return out
    for start in range(0, len(pairs), SETTINGS.match_batch_size):
        chunk = pairs[start:start + SETTINGS.match_batch_size]
        body = "\n".join(f'{i + 1}. A: "{a}" | B: "{b}"' for i, (a, b) in enumerate(chunk))
        try:
            res = router.call("light", CONFLICT_CHECK_PROMPT.format(pairs=body), json_out=True, max_output_tokens=1500)
        except QuotaExhausted:
            break
        except Exception as e:  # noqa: BLE001
            log.warning("conflict check failed: %s", str(e)[:200])
            continue
        for item in (res.data or {}).get("results", []) if isinstance(res.data, dict) else []:
            try:
                n = int(item["n"]) - 1
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= n < len(chunk) and item.get("answer") in CHECK_ANSWERS:
                out[start + n] = item["answer"]
    return out


SAME_CHECK_PROMPT = """Each numbered line has two sentences, A and B, from different news reports.
Question: do A and B report the SAME single fact, only in different words?

"same"      - yes: one fact, worded differently. Example: "He will take charge as Chief of Air Staff" and
              "He will take over as the air chief" -> same. "Police arrested the engineer" and "The site
              engineer was arrested by police" -> same.
"different" - anything else: two different facts, two steps of one thing, two different times or posts,
              or one says much more than the other. Example: "He was appointed chief" and "He will take
              charge as chief" -> different (two steps). "He took charge as vice chief in July" and "He will
              take charge as chief in October" -> different. "He is Deputy Chief of the Air Staff" and "He will be
              Chief of the Air Staff" -> different (two posts). "He got the Param Vishisht Seva Medal" and "He
              got the Vishisht Seva Medal" -> different (two medals). "Police arrested him" and "Police did not
              arrest him" -> different (one says it happened, the other that it did not).
A "no" or "not" can be the same fact in other words: "Rate cuts are off the table" and "There is no option
for rate cuts" -> same. Judge what each sentence MEANS, not whether one has "no" in it.

If you are not sure, answer "different".

{pairs}

Reply with JSON only: {{"results": [{{"n": 1, "answer": "same"}}, {{"n": 2, "answer": "different"}}]}}"""

SAME_BATCH = 6     # small batches: the models answer short lists far better than long ones


def same_facts(router: Router | None, pairs: list[tuple[str, str]]) -> list[bool]:
    """Are these the same fact in other words? (Oct 7 2026: four outlets saying he takes charge on 31
    October, in four wordings, stood as four one-outlet lines.) Asked twice, A/B swapped in the second
    asking, in small batches with a plain yes/no choice: the models are simple, and a wrong "same" would
    add outlets to a statement and could make it green. Only "same" both times counts."""
    out = [False] * len(pairs)
    if router is None or not pairs:
        return out

    def ask(ps: list[tuple[str, str]]) -> list[bool]:
        got = [False] * len(ps)
        for start in range(0, len(ps), SAME_BATCH):
            chunk = ps[start:start + SAME_BATCH]
            body = "\n".join(f'{i + 1}. A: "{a}" | B: "{b}"' for i, (a, b) in enumerate(chunk))
            try:
                res = router.call("light", SAME_CHECK_PROMPT.format(pairs=body), json_out=True, max_output_tokens=400)
            except QuotaExhausted:
                break
            except Exception as e:  # noqa: BLE001
                log.warning("same-fact check failed: %s", str(e)[:200])
                continue
            for item in (res.data or {}).get("results", []) if isinstance(res.data, dict) else []:
                try:
                    n = int(item["n"]) - 1
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= n < len(chunk):
                    got[start + n] = str(item.get("answer") or "").strip().lower() == "same"
        return got

    first = ask(pairs)
    again = [i for i, ok in enumerate(first) if ok]
    for i, ok in zip(again, ask([(pairs[i][1], pairs[i][0]) for i in again])):
        out[i] = ok
    return out


COVER_PROMPT = """Each numbered line has two sentences from news reports: A (longer) and B (shorter).
Question: does A say everything that B says? A may say more.

"yes" - every fact in B is also in A, perhaps in other words. Example: A "12 crew members, including 11
        Indians, were injured in the attack" and B "12 crew members were injured in the incident" -> yes.
"no"  - B says something A does not, or says it differently. Example: A "12 crew members were injured in
        the attack" and B "12 crew members were killed" -> no (injured is not killed). A "Police arrested
        him in 2019 in another case" and B "Police arrested him" -> no (a different arrest).

If you are not sure, answer "no".

{pairs}

Reply with JSON only: {{"results": [{{"n": 1, "answer": "yes"}}, {{"n": 2, "answer": "no"}}]}}"""


def covers_facts(router: Router | None, pairs: list[tuple[str, str]]) -> list[bool]:
    """(detailed, short) pairs: does the detailed line say everything the short one says? Asked twice,
    the second time in reverse order of lines; only two "yes" answers count (simple models answer
    by position and length as much as by meaning)."""
    out = [False] * len(pairs)
    if router is None or not pairs:
        return out

    def ask(idx: list[int]) -> dict[int, bool]:
        got: dict[int, bool] = {}
        for start in range(0, len(idx), SAME_BATCH):
            chunk = idx[start:start + SAME_BATCH]
            body = "\n".join(f'{k + 1}. A: "{pairs[i][0]}" | B: "{pairs[i][1]}"' for k, i in enumerate(chunk))
            try:
                res = router.call("light", COVER_PROMPT.format(pairs=body), json_out=True, max_output_tokens=400)
            except QuotaExhausted:
                break
            except Exception as e:  # noqa: BLE001
                log.warning("cover check failed: %s", str(e)[:200])
                continue
            for item in (res.data or {}).get("results", []) if isinstance(res.data, dict) else []:
                try:
                    n = int(item["n"]) - 1
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= n < len(chunk):
                    got[chunk[n]] = str(item.get("answer") or "").strip().lower() == "yes"
        return got

    first = ask(list(range(len(pairs))))
    again = ask([i for i in reversed(range(len(pairs))) if first.get(i)])
    for i, ok in again.items():
        out[i] = ok and first.get(i, False)
    return out


def _create_canonical(store: Store, story_id: int, kind: str, text: str, frame: dict | None = None,
                      time: dict | None = None) -> int:
    # a statement's frame (frames.py) is kept with it: who / action / what / value, and its time
    rel = {"frame": frame, "time": time} if frame else None
    return store.insert_returning_id(canonical, dict(story_id=story_id, kind=kind, text=text, conflicts=[],
                                                     verdict="pending", checked_members=0, rel=rel))


def frame_of(c: dict) -> tuple[dict | None, dict | None]:
    """(frame, time) of a statement (canonical row), or (None, None) if it was read before frames."""
    rel = c.get("rel") or {}
    return (rel.get("frame"), rel.get("time")) if c.get("kind") != "relation" else (None, None)


def _merge(store: Store, src: int, dst: int) -> None:
    store.exec(update(claims).where(claims.c.canonical_id == src).values(canonical_id=dst))
    src_row = store.one(select(canonical).where(canonical.c.id == src))
    dst_row = store.one(select(canonical).where(canonical.c.id == dst))
    conf = sorted(set((dst_row["conflicts"] or []) + (src_row["conflicts"] or [])) - {src, dst})
    kind = "event" if "event" in (src_row["kind"], dst_row["kind"]) else dst_row["kind"]
    store.exec(update(canonical).where(canonical.c.id == dst).values(conflicts=conf, kind=kind))
    store.exec(delete(canonical).where(canonical.c.id == src))
    for r in store.rows(select(canonical).where(canonical.c.story_id == dst_row["story_id"])):
        if src in (r["conflicts"] or []):
            new = sorted(set(c if c != src else dst for c in r["conflicts"]) - {r["id"]})
            store.exec(update(canonical).where(canonical.c.id == r["id"]).values(conflicts=new))


def _add_conflict(store: Store, a: int, b: int) -> None:
    for x, y in ((a, b), (b, a)):
        r = store.one(select(canonical).where(canonical.c.id == x))
        if r and y not in (r["conflicts"] or []):
            store.exec(update(canonical).where(canonical.c.id == x).values(conflicts=sorted((r["conflicts"] or []) + [y])))


def _remove_conflict(store: Store, a: int, b: int) -> None:
    for x, y in ((a, b), (b, a)):
        r = store.one(select(canonical).where(canonical.c.id == x))
        if r and y in (r["conflicts"] or []):
            store.exec(update(canonical).where(canonical.c.id == x).values(conflicts=[c for c in r["conflicts"] if c != y]))


def prune_orphans(store: Store, story_id: int) -> int:
    """Statements no report supports any more (their articles were retracted or moved to another
    story) are removed, along with references to them."""
    used = {r["canonical_id"] for r in store.rows(select(claims.c.canonical_id).where(
        claims.c.story_id == story_id, claims.c.canonical_id.is_not(None)))}
    canon = store.rows(select(canonical.c.id, canonical.c.conflicts, canonical.c.rel).where(canonical.c.story_id == story_id))
    dead = [c["id"] for c in canon if c["id"] not in used]
    if not dead:
        return 0
    store.exec(delete(canonical).where(canonical.c.id.in_(dead)))
    dead_set = set(dead)
    for c in canon:
        if c["id"] in dead_set:
            continue
        conf = [x for x in (c["conflicts"] or []) if x not in dead_set]
        if conf != (c["conflicts"] or []):
            store.exec(update(canonical).where(canonical.c.id == c["id"]).values(conflicts=conf))
    return len(dead)


def match_story(store: Store, router: Router | None, story_id: int) -> None:
    """A new report's statements, on arrival: each joins a statement whose words are plainly the same
    (relate.py, code only), or becomes a new statement. Everything that needs judgement (same fact in
    other words, a line covering another, disputes) is decided once, for the whole story, in the story
    review (consolidate.py), by one structure each (owner, Oct 7 2026)."""
    prune_orphans(store, story_id)
    rows = store.rows(select(claims).where(claims.c.story_id == story_id).order_by(claims.c.id))
    new = [r for r in rows if r["kind"] in ("event", "claim") and r["canonical_id"] is None]
    if new:
        from .relate import Profile
        canon = store.rows(select(canonical).where(canonical.c.story_id == story_id, canonical.c.kind != "relation"))
        known = [(c["id"], c["text"], Profile(c["text"]), frame_of(c)[1]) for c in canon]
        for r in new:
            fr = ((r["rel"] or {}).get("frame"))
            pr = Profile(r["text"])
            cid = next((cid_ for cid_, t_, p_, time_ in known
                        if relate(r["text"], t_, r["time"], time_, pr, p_) == "same"), None)
            if cid is None:
                cid = _create_canonical(store, story_id, r["kind"], r["text"], fr, r["time"])
                known.append((cid, r["text"], pr, r["time"]))
            store.exec(update(claims).where(claims.c.id == r["id"]).values(canonical_id=cid))
            if r["kind"] == "event":
                store.exec(update(canonical).where(canonical.c.id == cid).values(kind="event"))
    _match_relations(store, story_id)


def _match_relations(store: Store, story_id: int) -> None:
    rows = store.rows(select(claims).where(claims.c.story_id == story_id))
    local = {(r["article_id"], r["local_id"]): r["canonical_id"] for r in rows if r["kind"] != "relation"}
    existing = {}
    for c in store.rows(select(canonical).where(canonical.c.story_id == story_id, canonical.c.kind == "relation")):
        rel = c["rel"] or {}
        existing[(rel.get("from"), rel.get("to"), rel.get("type"))] = c["id"]
    for r in rows:
        if r["kind"] != "relation" or not r["rel"]:
            continue
        a = local.get((r["article_id"], r["rel"]["from"]))
        b = local.get((r["article_id"], r["rel"]["to"]))
        if a is None or b is None or a == b:
            continue
        key = (a, b, r["rel"]["type"])
        if key not in existing:
            existing[key] = store.insert_returning_id(canonical, dict(
                story_id=story_id, kind="relation", text="", rel={"from": a, "to": b, "type": key[2]},
                conflicts=[], verdict="pending", checked_members=0))
        if r["canonical_id"] != existing[key]:
            store.exec(update(claims).where(claims.c.id == r["id"]).values(canonical_id=existing[key]))
    # relations whose endpoints were merged away are stale; re-point them
    valid = {c["id"] for c in store.rows(select(canonical.c.id).where(canonical.c.story_id == story_id))}
    for key, cid in existing.items():
        if key[0] not in valid or key[1] not in valid:
            store.exec(update(claims).where(claims.c.canonical_id == cid).values(canonical_id=None))
            store.exec(delete(canonical).where(canonical.c.id == cid))
