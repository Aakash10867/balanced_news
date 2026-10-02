"""Stage 3: group articles about the same event into stories, BEFORE reading them with an LLM.

Uses multilingual Gemini embeddings of title + opening paragraph, so Hindi and
English articles about one event land together. Grouping first means the scarce
LLM quota is spent only on stories that several outlets cover: a story one outlet
alone reports can never be published, and adds nothing to the agreement data.
Falls back to TF-IDF (same-language only) when embedding quota is gone; one mode
per run so similarities stay comparable.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sqlalchemy import bindparam, func

from .config import SETTINGS
from .db import Store, articles, claims, delete, published, select, stories, update, utcnow
from .router import Router

log = logging.getLogger(__name__)


def _story_text(a: dict) -> str:
    lead = re.sub(r"\s+", " ", (a["text"] or "")[:400])
    return f"{a['title'] or ''}. {lead}"


def _release_singletons(store: Store, since: dt.datetime) -> int:
    """Unread one-article stories are re-grouped every run, so an article that arrived before
    its sibling (or was grouped in a weaker mode) still gets a chance to join it."""
    rows = store.rows(select(articles.c.story_id, func.count().label("n"), func.max(articles.c.extracted_at).label("x"))
                      .where(articles.c.story_id.is_not(None), articles.c.published_at >= since)
                      .group_by(articles.c.story_id))
    pub = {r["story_id"] for r in store.rows(select(published.c.story_id))}
    lonely = [r["story_id"] for r in rows if r["n"] == 1 and r["x"] is None and r["story_id"] not in pub]
    for i in range(0, len(lonely), 500):
        chunk = lonely[i:i + 500]
        store.exec(update(articles).where(articles.c.story_id.in_(chunk)).values(story_id=None))
        store.exec(update(claims).where(claims.c.story_id.in_(chunk)).values(story_id=None))
        store.exec(delete(stories).where(stories.c.id.in_(chunk)))
    return len(lonely)


def group_stories(store: Store, router: Router | None, embed_seconds: float = 360) -> int:
    since = utcnow() - dt.timedelta(hours=SETTINGS.story_window_hours)
    released = _release_singletons(store, since)
    arts = store.rows(
        select(articles.c.id, articles.c.title, articles.c.text, articles.c.embedding, articles.c.story_id,
               articles.c.published_at)
        .where(articles.c.text.is_not(None), articles.c.published_at >= since)
        .order_by(articles.c.published_at)
    )
    if not any(a["story_id"] is None for a in arts):
        return 0
    from .router import EMBED_DIMS
    for a in arts:
        if a["embedding"] and len(a["embedding"]) != EMBED_DIMS:
            a["embedding"] = None  # stored before the size change: re-embed

    # embed what is missing, within a time cap so grouping cannot starve the reading stage
    missing = [a for a in arts if not a["embedding"]]
    stop_at = time.time() + embed_seconds
    embed_available = router is not None and router.remaining_today("embed") > 0
    for start in range(0, len(missing), 50):
        if not embed_available or time.time() > stop_at:
            break
        chunk = missing[start:start + 50]
        vecs = router.embed([_story_text(a) for a in chunk])
        if not vecs:
            break
        for a, v in zip(chunk, vecs):
            a["embedding"] = v
        with store.engine.begin() as c:
            c.execute(update(articles).where(articles.c.id == bindparam("aid")).values(embedding=bindparam("emb")),
                      [{"aid": a["id"], "emb": a["embedding"]} for a in chunk])

    if any(a["embedding"] for a in arts):
        # embedding mode; articles not embedded yet simply wait for the next run
        mode = "embed"
        arts = [a for a in arts if a["embedding"]]
        X = np.array([a["embedding"] for a in arts], dtype=np.float32)
        threshold = SETTINGS.story_join_cosine_embed
    else:
        # no embedding quota at all today: word overlap (cannot match across languages)
        mode = "tfidf"
        X = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1).fit_transform(
            [_story_text(a) for a in arts])
        if X.shape[1] > 256 and X.shape[0] > 256:
            X = TruncatedSVD(n_components=256, random_state=0).fit_transform(X)
        else:
            X = X.toarray()
        X = X.astype(np.float32)
        threshold = SETTINGS.story_join_cosine_tfidf
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9

    # story centroids as one matrix, so each article is compared with all stories at once
    sids: list[int] = []
    index: dict[int, int] = {}
    sums = np.zeros((len(arts) + 1, X.shape[1]), dtype=np.float32)
    for i, a in enumerate(arts):
        if a["story_id"] is not None:
            if a["story_id"] not in index:
                index[a["story_id"]] = len(sids)
                sids.append(a["story_id"])
            sums[index[a["story_id"]]] += X[i]
    norms = np.linalg.norm(sums, axis=1) + 1e-9

    new_assign: list[dict] = []
    touched: set[int] = set()
    for i, a in enumerate(arts):
        if a["story_id"] is not None:
            continue
        k = len(sids)
        if k:
            sims = (sums[:k] @ X[i]) / norms[:k]
            j = int(sims.argmax())
        if k and sims[j] >= threshold:
            sid = sids[j]
        else:
            sid = store.insert_returning_id(stories, dict(
                created_at=utcnow(), updated_at=utcnow(), signature=a["title"], dirty=False, qualifies=False))
            j = len(sids)
            index[sid] = j
            sids.append(sid)
        sums[j] += X[i]
        norms[j] = np.linalg.norm(sums[j]) + 1e-9
        a["story_id"] = sid
        touched.add(sid)
        new_assign.append({"aid": a["id"], "sid": sid})

    if new_assign:
        with store.engine.begin() as c:
            c.execute(update(articles).where(articles.c.id == bindparam("aid")).values(story_id=bindparam("sid")),
                      new_assign)
            c.execute(update(claims).where(claims.c.article_id == bindparam("aid")).values(story_id=bindparam("sid")),
                      new_assign)
            c.execute(update(stories).where(stories.c.id.in_(sorted(touched))).values(updated_at=utcnow()))
    # stories become "dirty" (need re-analysis) only when an article in them is read
    log.info("stories: %d articles grouped (%s mode), %d stories touched, %d lone articles re-grouped",
             len(new_assign), mode, len(touched), released)
    return len(new_assign)
