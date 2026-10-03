"""Stage 5: decide which statements across articles are the same fact.

Clear matches and clear non-matches are decided by text similarity (code).
Only the ambiguous middle band goes to a Flash-Lite model, which answers
same / contradict / different. If that quota is gone, ambiguous pairs stay
separate: we would rather under-corroborate than wrongly merge.
"""
from __future__ import annotations

import logging

from scipy.sparse import vstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .config import SETTINGS
from .db import Store, canonical, claims, delete, select, update
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

MATCH_PROMPT = """Each numbered line has two statements, A and B, from different news reports about the same story.
For each line decide:
  "same"       - A and B state the same fact (wording or tone may differ),
  "contradict" - A and B cannot both be true,
  "different"  - neither of the above.
Judge only the facts stated, not the tone.

{pairs}

Reply with JSON only: {{"results": [{{"n": 1, "label": "same"}}, ...]}}"""


def _create_canonical(store: Store, story_id: int, kind: str, text: str) -> int:
    return store.insert_returning_id(canonical, dict(story_id=story_id, kind=kind, text=text, conflicts=[],
                                                     verdict="pending", checked_members=0))


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


def _llm_pairs(router: Router | None, pairs: list[tuple[str, str]]) -> list[str]:
    labels = ["different"] * len(pairs)
    if router is None:
        return labels
    for start in range(0, len(pairs), SETTINGS.match_batch_size):
        chunk = pairs[start:start + SETTINGS.match_batch_size]
        body = "\n".join(f'{i + 1}. A: "{a}" | B: "{b}"' for i, (a, b) in enumerate(chunk))
        try:
            res = router.call("light", MATCH_PROMPT.format(pairs=body), json_out=True, max_output_tokens=1500)
        except QuotaExhausted:
            log.info("match: light tier exhausted; %d ambiguous pairs left separate", len(pairs) - start)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("match call failed: %s", e)
            continue
        for item in (res.data or {}).get("results", []) if isinstance(res.data, dict) else []:
            try:
                n = int(item["n"]) - 1
                if 0 <= n < len(chunk) and item.get("label") in ("same", "contradict", "different"):
                    labels[start + n] = item["label"]
            except (KeyError, ValueError, TypeError):
                continue
    return labels


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
    prune_orphans(store, story_id)
    rows = store.rows(select(claims).where(claims.c.story_id == story_id).order_by(claims.c.id))
    facts = [r for r in rows if r["kind"] in ("event", "claim")]
    new = [r for r in facts if r["canonical_id"] is None]

    if new:
        canon = store.rows(select(canonical).where(canonical.c.story_id == story_id,
                                                   canonical.c.kind != "relation"))
        reps = [(c["id"], c["text"]) for c in canon]
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
        vec.fit([t for _, t in reps] + [r["text"] for r in new])
        rep_ids = [cid for cid, _ in reps]
        rep_mat = vec.transform([t for _, t in reps]) if reps else None
        queued: list[tuple[int, int, str, str]] = []  # (new canonical, existing canonical, textA, textB)

        for r in new:
            v = vec.transform([r["text"]])
            best, best_sim = None, 0.0
            if rep_mat is not None and rep_mat.shape[0]:
                sims = cosine_similarity(v, rep_mat).ravel()
                j = int(sims.argmax())
                best, best_sim = rep_ids[j], float(sims[j])
            if best is not None and best_sim >= SETTINGS.claim_same_cosine:
                cid = best
            else:
                cid = _create_canonical(store, story_id, r["kind"], r["text"])
                if best is not None and best_sim >= SETTINGS.claim_candidate_cosine:
                    best_text = reps[rep_ids.index(best)][1]
                    queued.append((cid, best, r["text"], best_text))
                rep_ids.append(cid)
                reps.append((cid, r["text"]))
                rep_mat = v if rep_mat is None or rep_mat.shape[0] == 0 else vstack([rep_mat, v])
            store.exec(update(claims).where(claims.c.id == r["id"]).values(canonical_id=cid))
            if r["kind"] == "event":
                store.exec(update(canonical).where(canonical.c.id == cid).values(kind="event"))

        if queued:
            labels = _llm_pairs(router, [(a, b) for _, _, a, b in queued])
            alias: dict[int, int] = {}

            def find(x):
                while x in alias:
                    x = alias[x]
                return x

            for (cnew, cold, _, _), label in zip(queued, labels):
                a, b = find(cnew), find(cold)
                if a == b:
                    continue
                if label == "same":
                    _merge(store, a, b)
                    alias[a] = b
                elif label == "contradict":
                    _add_conflict(store, a, b)

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
