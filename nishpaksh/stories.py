"""Stage 3: group articles about the same event into stories, BEFORE reading them with an LLM.

Uses multilingual Gemini embeddings of title + opening paragraph, so Hindi and English articles
about one event land together.

The asymmetry that shapes everything here: splitting one event into two stories only delays it,
but merging two events into one story mixes their facts, and one outlet's claim about event A
then "contradicts" another's about event B. That is a false positive on the page. So when in
doubt, keep apart.

What went wrong before, and the rule that replaces it:
  A story used to be represented by the average of its articles. Every article it absorbed made
  that average more generic ("Indian politics this week"), so it attracted still more: one
  "story" grew to 151 articles about GST, a temple priest and Pakistani cosmetics.
  Now a new article is judged against the story's actual articles:
    join   its similarity to the story's closest members (mean of the top two) is high, AND it is
           close to the story's core article (the medoid), so a story cannot drift by chaining
    ask    a middle band goes to a cheap model in batches: "same specific event?", the article
           against the story's core article (dates, headlines, opening words), asked twice with
           the two swapped; only two "same" join. A borderline article published 12 h or more
           before the story's first report is not asked: it is an earlier event
    else   it starts a new story
  Every run, stories are re-checked: one whose articles fall into separate groups is split.

Embedding quota: each article is embedded once, at most a fixed share of the day's quota per
run. If the quota is gone, articles wait for the next run; there is no word-overlap fallback
(it cannot match Hindi with English and it built the giant stories).
"""
from __future__ import annotations

import datetime as dt
import logging
import re

import numpy as np
from sqlalchemy import bindparam, func

from .config import SETTINGS
from .db import Store, articles, canonical, claims, delete, published, select, stories, update, utcnow
from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

SAME_EVENT_PROMPT = """Each numbered line shows two news reports, A and B, each with its publication date (IST),
headline and opening words. Do A and B report the SAME specific event: the same incident, announcement,
decision or statement (a later report on that exact incident counts)?

Same kind of event is not the same event. Look at WHO, WHERE and WHEN:
- "Former sarpanch shot dead in Ludhiana" (Oct 4) / "AAP sarpanch shot dead by bike-borne men in Tarn
  Taran" (Oct 6): two killings, two people, two places -> "different"
- "Bus falls into gorge in Kullu, 12 dead" (Oct 3) / "Kullu bus accident: toll rises to 15" (Oct 4):
  one accident, a later report -> "same"
- "Farmers protest in Mumbai over prices" / "Farmers protest in Chennai over prices": two protests -> "different"
- "Minister X resigns" / "मंत्री X का इस्तीफा": one event in Hindi and English -> "same"
If the reports do not let you tell, or you are unsure, answer "different".

{pairs}

Reply with JSON only: {{"results": [{{"n": 1, "answer": "same"}}, {{"n": 2, "answer": "different"}}, ...]}}"""


def _story_text(a: dict) -> str:
    lead = re.sub(r"\s+", " ", (a["text"] or "")[:400])
    return f"{a['title'] or ''}. {lead}"


def _heal_copied_vectors(store: Store, since: dt.datetime, limit: int = 1000) -> int:
    """Self-repair: different articles must not share an identical vector. If they do (an
    embedding batch was misread), detach them and re-embed them, at most `limit` per run."""
    rows = store.rows(select(articles.c.id, articles.c.title, articles.c.embedding, articles.c.story_id)
                      .where(articles.c.embedding.is_not(None), articles.c.published_at >= since))
    by_vec: dict[tuple, list[dict]] = {}
    for r in rows:
        if not r["embedding"]:  # a cleared vector can be stored as JSON null, which passes IS NOT NULL
            continue
        by_vec.setdefault(tuple(r["embedding"]), []).append(r)
    bad = [r for group in by_vec.values() if len({g["title"] for g in group}) > 1 for r in group][:limit]
    if not bad:
        return 0
    _detach(store, [r["id"] for r in bad], clear_embedding=True)
    log.warning("stories: %d articles shared a copied vector; detached and queued for re-embedding", len(bad))
    return len(bad)


def _reset_story(store: Store, story_id: int) -> None:
    """A story whose membership changed under already-read articles is re-matched from scratch:
    its statements are rebuilt from the claims of the articles it now holds."""
    store.exec(update(claims).where(claims.c.story_id == story_id).values(canonical_id=None))
    store.exec(delete(canonical).where(canonical.c.story_id == story_id))
    store.exec(update(stories).where(stories.c.id == story_id).values(dirty=True, updated_at=utcnow()))


def _detach(store: Store, ids: list[int], clear_embedding: bool = False) -> None:
    """Take articles out of their stories. A story re-matches its statements only if an article
    that had already been read (and so contributed statements) left it."""
    rows = store.rows(select(articles.c.id, articles.c.story_id, articles.c.extracted_at).where(articles.c.id.in_(ids or [-1])))
    # a published story is closed (editions.py): its articles and statements stay as they are
    pub = {r["story_id"] for r in store.rows(select(published.c.story_id))}
    rows = [r for r in rows if r["story_id"] is None or r["story_id"] not in pub]
    ids = [r["id"] for r in rows]
    olds = sorted({r["story_id"] for r in rows if r["story_id"] is not None and r["extracted_at"]})
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        vals = {"story_id": None}
        if clear_embedding:
            vals["embedding"] = None
        store.exec(update(articles).where(articles.c.id.in_(chunk)).values(**vals))
        store.exec(update(claims).where(claims.c.article_id.in_(chunk)).values(story_id=None, canonical_id=None))
    for sid in olds:
        _reset_story(store, sid)


def embed_model(router: Router | None) -> str | None:
    """The one embedding model in use. Vectors from two different models live in different spaces:
    comparing them gives meaningless similarities (real data showed unrelated Hindi and English
    articles at 0.97). So the embed tier must list exactly one model, and every stored vector is
    tagged with the model that made it."""
    if router is None:
        return None
    ids = {s.id for s in router.tiers.get("embed", []) if not s.disabled}
    if len(ids) > 1:
        raise ValueError(f"the embed tier must use one model, found {sorted(ids)}")
    return next(iter(ids), None)


def _embed_missing(store: Store, router: Router | None, arts: list[dict]) -> int:
    model = embed_model(router)
    for a in arts:
        if a["embedding"] and model and a.get("embed_model") != model:
            a["embedding"] = None  # made by another model (or before tagging): not comparable, re-embed
    missing = [a for a in arts if not a["embedding"]]
    if not missing or router is None or not model:
        return 0
    # articles already read come first (their stories cannot be published until regrouped), then
    # newest first: what is arriving now matters more than the backlog. The budget is in texts
    # (Google counts each text in a batch), spread over the day's remaining runs.
    # Budget: everything left today except what the remaining runs need for new arrivals, so a
    # backlog clears early in the day instead of being spread thin (real runs: only 75 of 1,510
    # new articles got a vector in a day, so almost nothing could be grouped or read).
    import datetime as _dt
    from .router import PACIFIC
    left = router.remaining_today("embed")
    runs_left = max(1, 24 - _dt.datetime.now(PACIFIC).hour)
    budget = min(left, max(SETTINGS.embed_min_texts_per_run, left - SETTINGS.embed_reserve_per_run * (runs_left - 1)))
    # never more than the pacing curve allows now (the router would refuse the rest mid-way)
    budget = min(budget, router.remaining_now("embed"))
    # Half for new arrivals (newest first: that is what forms today's stories), half for read
    # articles still on an old model's vector (their stories cannot be published until regrouped).
    new = sorted([a for a in missing if a["extracted_at"] is None], key=lambda a: a["published_at"], reverse=True)
    old = sorted([a for a in missing if a["extracted_at"] is not None], key=lambda a: a["published_at"], reverse=True)
    half = budget // 2
    take_old = old[:max(half, budget - len(new))]
    missing = new[:budget - len(take_old)] + take_old
    done = 0
    for start in range(0, len(missing), 25):
        chunk = missing[start:start + 25][:max(0, budget)]
        if not chunk:
            break
        vecs = router.embed([_story_text(a) for a in chunk], batch=25, max_requests=3)
        budget -= len(chunk)
        if not vecs:
            continue
        for a, v in zip(chunk, vecs):
            a["embedding"] = v
            a["embed_model"] = model
        with store.engine.begin() as c:
            c.execute(update(articles).where(articles.c.id == bindparam("aid"))
                      .values(embedding=bindparam("emb"), embed_model=bindparam("m")),
                      [{"aid": a["id"], "emb": a["embedding"], "m": model} for a in chunk])
        done += len(chunk)
    if len(missing) > done:
        log.info("stories: %d articles wait for embedding (next run)", len(missing) - done)
    return done


def _ist(t) -> str:
    return (t + dt.timedelta(hours=5, minutes=30)).strftime("%b %d") if t else "date unknown"


def _item(a: dict) -> str:
    return f'{_ist(a.get("published_at"))} | {a["title"]} — {_lead(a)}'.replace('"', "'")


def _ask_same(router: Router, pairs: list[tuple[dict, dict]]) -> list[bool | None]:
    """One pass of the question over (A, B) pairs: True / False, None when not answered."""
    out: list[bool | None] = [None] * len(pairs)
    for start in range(0, len(pairs), 20):
        chunk = pairs[start:start + 20]
        body = "\n".join(f'{k + 1}. A: "{_item(a)}" | B: "{_item(b)}"' for k, (a, b) in enumerate(chunk))
        try:
            res = router.call("light", SAME_EVENT_PROMPT.format(pairs=body), json_out=True, max_output_tokens=900)
        except QuotaExhausted:
            log.info("stories: light tier exhausted; %d borderline articles start their own stories", len(pairs) - start)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("same-event check failed: %s", str(e)[:200])
            continue
        for it in (res.data or {}).get("results", []) if isinstance(res.data, dict) else []:
            try:
                n = int(it["n"]) - 1
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= n < len(chunk):
                ans = str(it.get("answer", "")).strip().lower()
                out[start + n] = True if ans == "same" else False if ans == "different" else None
    return out


def _same_event(router: Router | None, asks: list[tuple[dict, dict]]) -> list[bool]:
    """(new article, story core article) -> same specific event? Asked twice, A and B swapped; only
    two "same" answers join (Oct 8 2026, story 13968: one question on two headlines, "former sarpanch
    murder mystery" / "AAP sarpanch shot dead in Punjab", merged two killings a day and 150 km apart)."""
    if router is None or not asks:
        return [False] * len(asks)
    first = _ask_same(router, [(a, b) for a, b in asks])
    again = [k for k, x in enumerate(first) if x is True]
    second = _ask_same(router, [(asks[k][1], asks[k][0]) for k in again]) if again else []
    out = [False] * len(asks)
    for k, x in zip(again, second):
        out[k] = x is True
    return out


def _before_story(a: dict, members: list[dict]) -> bool:
    """A report published well before a story's first report is about an earlier event: a borderline
    one is never asked into it (story 13968: a Ludhiana killing of Oct 4-5 joined a Tarn Taran killing
    of Oct 6). Only the strict code rule can still place it there."""
    first = min((m["published_at"] for m in members if m.get("published_at")), default=None)
    return bool(first and a.get("published_at") and first - a["published_at"] >= dt.timedelta(
        hours=SETTINGS.story_before_hours))


def _lead(a: dict, n: int = 140) -> str:
    return re.sub(r"\s+", " ", (a.get("text") or "")[:n])


class _Index:
    """Stories as lists of member rows (earliest first), each with a fixed core: the most central
    of its first three articles. The core is what the story is about; it never moves, so a story
    cannot drift step by step to a different event (a moving centre, even a medoid, can).

    Scoring uses one precomputed row of similarities per candidate article (to every article in the
    window), so a candidate is compared with its nearest articles only, never story by story."""

    def __init__(self, X: np.ndarray):
        self.X = X
        self.members: dict[int, list[int]] = {}
        self.medoid: dict[int, int] = {}
        self.sid_of: dict[int, int] = {}

    def add(self, sid: int, i: int) -> None:
        m = self.members.setdefault(sid, [])
        m.append(i)
        self.sid_of[i] = sid
        if len(m) <= 2:
            self.medoid[sid] = m[0]
        elif len(m) == 3:
            S = self.X[m] @ self.X[m].T
            self.medoid[sid] = m[int(S.sum(axis=1).argmax())]

    def remove_story(self, sid: int) -> None:
        for i in self.members.pop(sid, []):
            self.sid_of.pop(i, None)
        self.medoid.pop(sid, None)

    def score(self, i: int, row: np.ndarray, exclude: set[int] = frozenset(), top: int = 40):
        """(member score, core similarity, story id, nearest member) for the best stories, best first.
        `row` is article i's similarity to every article; the best story must own one of i's
        nearest articles, so only those stories are scored."""
        order = np.argsort(-row)
        cands: list[int] = []
        for j in order[: top + 1]:
            j = int(j)
            if j == i or j not in self.sid_of:
                continue
            sid = self.sid_of[j]
            if sid not in exclude and sid not in cands:
                cands.append(sid)
        out = []
        for sid in cands:
            m = self.members[sid]
            sims = row[m]
            k = min(2, len(m))
            topk = np.sort(sims)[-k:]
            out.append((float(topk.mean()), float(row[self.medoid[sid]]), sid, m[int(sims.argmax())]))
        out.sort(reverse=True)
        return out


def _split_story(store: Store, sid: int, idx: list[int], arts: list[dict], X: np.ndarray) -> int:
    """Split a story whose articles form separate groups (average linkage). The largest group keeps
    the story; other groups of 2+ become new stories; lone leftovers are released to regroup."""
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import pdist
    if len(idx) < SETTINGS.story_split_min_size:
        return 0
    D = pdist(X[idx], metric="cosine")
    labels = fcluster(linkage(D, method="average"), t=1 - SETTINGS.story_split_cosine, criterion="distance")
    groups: dict[int, list[int]] = {}
    for i, l in zip(idx, labels):
        groups.setdefault(int(l), []).append(i)
    if len(groups) == 1:
        return 0
    ordered = sorted(groups.values(), key=len, reverse=True)
    keep = ordered[0]
    # only groups of 2+ leave: a lone outlier got in by the join rule and would only rejoin next
    # run, wiping the story's statements each time (split and join must not disagree in a loop)
    rest = [g for g in ordered[1:] if len(g) >= 2]
    if not rest:
        return 0
    moved = [arts[i]["id"] for g in rest for i in g]
    _detach(store, moved)
    for g in rest:
        new_sid = store.insert_returning_id(stories, dict(created_at=utcnow(), updated_at=utcnow(),
                                                          signature=arts[g[0]]["title"], dirty=False, qualifies=False))
        ids = [arts[i]["id"] for i in g]
        store.exec(update(articles).where(articles.c.id.in_(ids)).values(story_id=new_sid))
        store.exec(update(claims).where(claims.c.article_id.in_(ids)).values(story_id=new_sid, canonical_id=None))
        if any(arts[i]["extracted_at"] for i in g):
            store.exec(update(stories).where(stories.c.id == new_sid).values(dirty=True))
        for i in g:
            arts[i]["story_id"] = new_sid
    log.info("stories: split story %s (%d articles) into %s", sid, len(idx), [len(keep)] + [len(g) for g in rest])
    return len(moved)


def group_stories(store: Store, router: Router | None, embed_seconds: float = 360) -> int:
    since = utcnow() - dt.timedelta(hours=SETTINGS.story_window_hours)
    healed = _heal_copied_vectors(store, since, limit=SETTINGS.heal_per_run)
    arts = store.rows(
        select(articles.c.id, articles.c.title, articles.c.text, articles.c.embedding, articles.c.embed_model,
               articles.c.story_id, articles.c.published_at, articles.c.extracted_at)
        .where(articles.c.text.is_not(None), articles.c.published_at >= since)
        .order_by(articles.c.published_at)
    )
    model = embed_model(router)
    # published stories are closed (editions.py): their articles are never moved out or regrouped
    pub = {r["story_id"] for r in store.rows(select(published.c.story_id))}
    arts = [dict(a, frozen=a["story_id"] is not None and a["story_id"] in pub) for a in arts]
    stale = {a["id"] for a in arts if a["embedding"] and model and a.get("embed_model") != model and not a.get("frozen")}
    # an unread article whose vector came from another model sits in a story chosen on meaningless
    # similarities: take it out now (cheap: it has no statements) and let it wait for a new vector
    loose = [a["id"] for a in arts if model and a["story_id"] is not None and not a["extracted_at"]
             and a.get("embed_model") != model and not a.get("frozen")]
    if loose:
        _detach(store, loose)
        for a in arts:
            if not a["extracted_at"] and a.get("embed_model") != model and not a.get("frozen"):
                a["story_id"] = None
        _drop_empty_stories(store)
    embedded = _embed_missing(store, router, arts)
    # an article whose story was decided on another model's vector is grouped again from scratch
    regroup = [a["id"] for a in arts if a["id"] in stale and a.get("embed_model") == model and a["story_id"] is not None]
    if regroup:
        _detach(store, regroup)
        for a in arts:
            if a["id"] in set(regroup):
                a["story_id"] = None
        _drop_empty_stories(store)
    arts = [a for a in arts if a["embedding"] and (model is None or a.get("embed_model") == model)]
    if not arts:
        return 0
    # one vector size only (a model change can leave a few of another size; they re-embed later)
    from collections import Counter
    dims = Counter(len(a["embedding"]) for a in arts).most_common(1)[0][0]
    arts = [a for a in arts if len(a["embedding"]) == dims]
    X = np.array([a["embedding"] for a in arts], dtype=np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9

    # re-check existing stories first: split any that hold separate events
    by_story: dict[int, list[int]] = {}
    for i, a in enumerate(arts):
        if a["story_id"] is not None:
            by_story.setdefault(a["story_id"], []).append(i)
    split_moved = 0
    for sid, idx in list(by_story.items()):
        if sid not in pub:          # published stories are closed: never split, never joined
            split_moved += _split_story(store, sid, idx, arts, X)

    index = _Index(X)
    for i, a in enumerate(arts):
        if a["story_id"] is not None:
            index.add(a["story_id"], i)
    # articles that need a story: new ones, and the lone unread article of a one-article story
    # (it may have arrived before its siblings); the latter keep their story unless they join one
    lone = {sid: m[0] for sid, m in index.members.items()
            if len(m) == 1 and not arts[m[0]]["extracted_at"] and sid not in pub}
    todo = [i for i, a in enumerate(arts) if a["story_id"] is None] + sorted(lone.values())
    if not todo:
        log.info("stories: nothing to group; %d embedded, %d moved out of split stories", embedded, split_moved)
        return 0
    rows = X[todo] @ X.T
    row_of = {i: r for r, i in enumerate(todo)}
    analyses = {r["id"]: (r["analysis"] or {}) for r in store.rows(
        select(stories.c.id, stories.c.analysis).where(stories.c.id.in_(sorted(index.members) or [-1])))}
    asked_before = {sid: set(an.get("not_same") or []) for sid, an in analyses.items()}

    base_sids = set(index.members)                 # stories that existed before this pass
    assign: list[dict] = []
    asks: list[tuple[int, int, int]] = []          # (article index, story id, nearest member index)
    touched: set[int] = set()
    emptied: list[int] = []

    def new_story(i: int) -> int:
        return store.insert_returning_id(stories, dict(created_at=utcnow(), updated_at=utcnow(),
                                                       signature=arts[i]["title"], dirty=False, qualifies=False))

    candidate_of: dict[int, int] = {}

    def follow_up_story(parent: int, i: int) -> int:
        """Later reports about a published story gather in one candidate story of their own: it is
        published only if it earns a follow-up (editions.follow_up_ok)."""
        if parent not in candidate_of:
            an = analyses.get(parent)
            if an is None:
                an = (store.one(select(stories.c.analysis).where(stories.c.id == parent)) or {}).get("analysis") or {}
            an = dict(an)
            ed = dict(an.get("edition") or {})
            cand = ed.get("candidate")
            alive = cand is not None and cand not in pub and store.one(select(stories.c.id).where(stories.c.id == cand))
            if not alive:
                cand = store.insert_returning_id(stories, dict(
                    created_at=utcnow(), updated_at=utcnow(), signature=arts[i]["title"], dirty=False,
                    qualifies=False, analysis={"edition": {"follows": parent}}))
                ed["candidate"] = cand
                an["edition"] = ed
                analyses[parent] = an
                store.exec(update(stories).where(stories.c.id == parent).values(analysis=an))
            candidate_of[parent] = cand
        return candidate_of[parent]

    def place(i: int, sid: int | None) -> None:
        """Put article i into story sid (None: a new story). A lone article keeps its own story
        when it joins nothing. An article that belongs with a published story goes to its candidate."""
        if sid is not None and sid in pub:
            sid = follow_up_story(sid, i)
        own = arts[i]["story_id"]
        if sid is None:
            if own is not None:
                return
            sid = new_story(i)
        if own is not None and own != sid:
            index.remove_story(own)
            emptied.append(own)
        arts[i]["story_id"] = sid
        index.add(sid, i)
        touched.add(sid)
        assign.append({"aid": arts[i]["id"], "sid": sid})

    for i in todo:
        own = arts[i]["story_id"]
        cands = index.score(i, rows[row_of[i]], exclude={own} if own is not None else set())[:1]
        if cands and cands[0][0] >= SETTINGS.story_join_cosine and cands[0][1] >= SETTINGS.story_core_cosine:
            place(i, cands[0][2])
        elif (cands and cands[0][0] >= SETTINGS.story_ask_cosine and cands[0][1] >= SETTINGS.story_ask_cosine - 0.05
              and arts[i]["id"] not in asked_before.get(cands[0][2], set())
              and not _before_story(arts[i], [arts[j] for j in index.members[cands[0][2]]])):
            asks.append((i, cands[0][2], cands[0][3]))
        else:
            place(i, None)

    # A story asked about can be emptied later in this pass (its lone article joined another story).
    # Those articles are not guessed into anything: they get their own story, and as lone stories
    # they are looked at again next run.
    gone = [(i, sid, near) for i, sid, near in asks if sid not in index.medoid]
    asks = [a for a in asks if a[1] in index.medoid]
    for i, _, _ in gone:
        place(i, None)
    if gone:
        log.info("stories: %d questions dropped, their story was merged away in this pass", len(gone))
    if asks:
        verdicts = _same_event(router, [(arts[i], arts[index.medoid[sid]]) for i, sid, _ in asks])
        rejected = []
        for (i, sid, _), same in zip(asks, verdicts):
            if same and sid not in index.medoid:    # emptied by an earlier answer in this loop
                place(i, None)
            elif same:
                place(i, sid)
            else:
                rejected.append(i)
                asked_before.setdefault(sid, set()).add(arts[i]["id"])
        # remember the answer, so the same question is not asked every run
        for sid in {sid for i, sid, _ in asks}:
            an = dict(analyses.get(sid) or {})
            an["not_same"] = sorted(asked_before.get(sid, set()))[-200:]
            store.exec(update(stories).where(stories.c.id == sid).values(analysis=an))
        # articles the model kept out of a story may still belong together (two reports of a second
        # protest, say): group them among themselves by the strict rule, never back into that story
        for i in rejected:
            own = arts[i]["story_id"]
            cands = index.score(i, rows[row_of[i]], exclude=base_sids | ({own} if own else set()))[:1]
            if cands and cands[0][0] >= SETTINGS.story_join_cosine and cands[0][1] >= SETTINGS.story_core_cosine:
                place(i, cands[0][2])
            else:
                place(i, None)

    if assign:
        with store.engine.begin() as c:
            c.execute(update(articles).where(articles.c.id == bindparam("aid")).values(story_id=bindparam("sid")),
                      assign)
            c.execute(update(claims).where(claims.c.article_id == bindparam("aid")).values(story_id=bindparam("sid")),
                      assign)
            c.execute(update(stories).where(stories.c.id.in_(sorted(touched))).values(updated_at=utcnow()))
            moved = {y["aid"] for y in assign}
            read = sorted({a["story_id"] for a in arts if a.get("extracted_at") and a["id"] in moved})
            if read:
                c.execute(update(stories).where(stories.c.id.in_(read)).values(dirty=True))
    if emptied:
        _drop_empty_stories(store, emptied)
    log.info("stories: %d articles placed (%d after a same-event check), %d embedded, %d regrouped after a "
             "model change, %d stories touched, %d moved out of split stories, %d copied vectors repaired",
             len(assign), len(asks), embedded, len(regroup), len(touched), split_moved, healed)
    return len(assign)


def _drop_empty_stories(store: Store, sids: list[int] | None = None) -> int:
    """Stories left without any article lose their statements and their page."""
    q = select(stories.c.id)
    if sids is not None:
        q = q.where(stories.c.id.in_(sids or [-1]))
    with_articles = select(articles.c.story_id).where(articles.c.story_id.is_not(None)).distinct()
    empty = [r["id"] for r in store.rows(q.where(stories.c.id.not_in(with_articles)))]
    for k in range(0, len(empty), 500):
        chunk = empty[k:k + 500]
        store.exec(update(claims).where(claims.c.story_id.in_(chunk)).values(story_id=None, canonical_id=None))
        store.exec(delete(canonical).where(canonical.c.story_id.in_(chunk)))
        store.exec(delete(published).where(published.c.story_id.in_(chunk)))
        store.exec(delete(stories).where(stories.c.id.in_(chunk)))
        from .db import story_links
        store.exec(delete(story_links).where(story_links.c.parent_id.in_(chunk) | story_links.c.child_id.in_(chunk)))
    return len(empty)


def story_health(store: Store) -> dict:
    """For the run record: is grouping behaving? Largest stories and their spread."""
    since = utcnow() - dt.timedelta(hours=SETTINGS.story_window_hours)
    rows = store.rows(select(articles.c.story_id, func.count().label("n"))
                      .where(articles.c.story_id.is_not(None), articles.c.published_at >= since)
                      .group_by(articles.c.story_id).order_by(func.count().desc()).limit(5))
    return {"largest_stories": [[r["story_id"], r["n"]] for r in rows]}
