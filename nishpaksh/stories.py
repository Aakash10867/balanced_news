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

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from .config import SETTINGS
from .db import Store, articles, claims, insert, select, stories, update, utcnow
from .router import Router

log = logging.getLogger(__name__)


def _story_text(a: dict) -> str:
    lead = re.sub(r"\s+", " ", (a["text"] or "")[:600])
    return f"{a['title'] or ''}. {lead}"


def group_stories(store: Store, router: Router | None) -> int:
    since = utcnow() - dt.timedelta(hours=SETTINGS.story_window_hours)
    arts = store.rows(
        select(articles.c.id, articles.c.title, articles.c.text, articles.c.embedding, articles.c.story_id,
               articles.c.published_at)
        .where(articles.c.text.is_not(None), articles.c.published_at >= since)
        .order_by(articles.c.published_at)
    )
    if not any(a["story_id"] is None for a in arts):
        return 0
    texts = [_story_text(a) for a in arts]

    # embeddings where possible
    missing = [i for i, a in enumerate(arts) if not a["embedding"]]
    if missing and router is not None:
        vecs = router.embed([texts[i] for i in missing])
        if vecs:
            for i, v in zip(missing, vecs):
                arts[i]["embedding"] = v
                store.exec(update(articles).where(articles.c.id == arts[i]["id"]).values(embedding=v))
    if all(a["embedding"] for a in arts):
        mode = "embed"
        X = np.array([a["embedding"] for a in arts], dtype=np.float32)
        X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
        threshold = SETTINGS.story_join_cosine_embed
        row = lambda i: X[i]  # noqa: E731
        dim = X.shape[1]
    else:
        mode = "tfidf"
        vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1)
        X = vec.fit_transform(texts)  # rows are L2-normalised
        threshold = SETTINGS.story_join_cosine_tfidf
        row = lambda i: X[i].toarray().ravel()  # noqa: E731
        dim = X.shape[1]

    # centroids of existing open stories
    sums: dict[int, np.ndarray] = {}
    for i, a in enumerate(arts):
        if a["story_id"] is not None:
            sums.setdefault(a["story_id"], np.zeros(dim, dtype=np.float32))
            sums[a["story_id"]] += row(i)

    assigned = 0
    touched: set[int] = set()
    for i, a in enumerate(arts):
        if a["story_id"] is not None:
            continue
        x = row(i)
        best, best_sim = None, -1.0
        for sid, s in sums.items():
            sim = float(x @ s) / (float(np.linalg.norm(s)) + 1e-9)
            if sim > best_sim:
                best, best_sim = sid, sim
        if best is not None and best_sim >= threshold:
            sid = best
        else:
            sid = store.insert_returning_id(stories, dict(
                created_at=utcnow(), updated_at=utcnow(), signature=a["title"], dirty=False, qualifies=False))
            sums[sid] = np.zeros(dim, dtype=np.float32)
        sums[sid] += x
        a["story_id"] = sid
        touched.add(sid)
        store.exec(update(articles).where(articles.c.id == a["id"]).values(story_id=sid))
        store.exec(update(claims).where(claims.c.article_id == a["id"]).values(story_id=sid))
        assigned += 1
    for sid in touched:
        store.exec(update(stories).where(stories.c.id == sid).values(updated_at=utcnow()))
    # stories become "dirty" (need re-analysis) only when an article in them is read
    log.info("stories: %d articles grouped (%s mode), %d stories touched", assigned, mode, len(touched))
    return assigned
