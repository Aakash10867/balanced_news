"""Stage 6: perspectives emerge from who agrees with whom. No hand labels.

Per story: facts every source reports the same way carry no information about
sides, so agreement is measured only over discriminating facts (denied by
someone, part of a contradiction, or omitted by some sources). Each independent
source group gets a stance on those (+1 asserts, +0.5 reports someone saying
it, -1 denies, small penalty for omitting a widely reported fact). The signed
agreement graph's eigenvector proposes two sides; the split is accepted only
if the sides directly contradict each other on at least one fact.

Across stories: per-story agreements accumulate into a source-to-source
affinity matrix (the political-science "roll-call" approach). Spectral
clustering on it gives stable perspectives (A, B, C...) that mean the same
thing in every story.
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict

import numpy as np

from .config import SETTINGS
from .db import Store, articles, canonical, claims, delete, insert, select, source_clusters, stories, story_pairs, update, utcnow
from .wire import independence_groups

log = logging.getLogger(__name__)
STANCE_VAL = {"asserts": 1.0, "attributes": 0.5, "denies": -1.0}
LETTERS = "ABCDEFGH"
GENERIC_AUTHOR = re.compile(r"(?i)\b(staff|desk|bureau|team|news|web|correspondent|reporter|online|editor|agencies)\b")


def source_key(a: dict) -> str:
    author = (a.get("author") or "").strip()
    if author and not a.get("agency") and len(author) <= 60 and not GENERIC_AUTHOR.search(author):
        return f"{a['outlet']}::{author.lower()}"
    return a["outlet"]


def _split(W: np.ndarray) -> tuple[np.ndarray | None, float, float]:
    """Candidate side labels (0/1) from the signed agreement graph, plus inter/intra means."""
    n = W.shape[0]
    if n < 2:
        return None, 0.0, 0.0
    vals, vecs = np.linalg.eigh(W)
    side = None
    for idx in (int(np.argmax(vals)), int(np.argmin(vals))):
        v = vecs[:, idx]
        s = (v > 1e-9).astype(int)
        if 0 < s.sum() < n:
            side = s
            break
    if side is None:
        return None, 0.0, 0.0
    inter = [W[i, j] for i in range(n) for j in range(i + 1, n) if side[i] != side[j]]
    intra = [W[i, j] for i in range(n) for j in range(i + 1, n) if side[i] == side[j]]
    inter_m = float(np.mean(inter)) if inter else 0.0
    intra_m = float(np.mean(intra)) if intra else 1.0
    if inter_m >= intra_m - SETTINGS.split_margin:
        return None, inter_m, intra_m
    return side, inter_m, intra_m


def _direct_conflict(side, groups, M, conflicts) -> bool:
    """A split is real only if the sides contradict each other on at least one fact
    (one asserts, the other denies, or each asserts one of two contradictory facts).
    Omissions alone shape the split but cannot create it."""
    sides = {g: int(side[i]) for i, g in enumerate(groups)}
    for c in {c for g in groups for c in M[g]}:
        pos = {sides[g] for g in groups if M[g].get(c, 0) > 0}
        neg = {sides[g] for g in groups if M[g].get(c, 0) < 0}
        if any(p != q for p in pos for q in neg):
            return True
    for c1, c2 in conflicts:
        s1 = {sides[g] for g in groups if M[g].get(c1, 0) > 0}
        s2 = {sides[g] for g in groups if M[g].get(c2, 0) > 0}
        if any(p != q for p in s1 for q in s2):
            return True
    return False


def analyze_story(store: Store, story_id: int) -> dict:
    arts = store.rows(select(articles.c.id, articles.c.outlet, articles.c.url, articles.c.author, articles.c.agency,
                             articles.c.wire_group, articles.c.role).where(articles.c.story_id == story_id))
    by_id = {a["id"]: a for a in arts}
    gmap = independence_groups(arts)
    groups = sorted(set(gmap.values()))
    rows = store.rows(select(claims.c.article_id, claims.c.canonical_id, claims.c.stance)
                      .where(claims.c.story_id == story_id, claims.c.canonical_id.is_not(None)))

    per_article: dict[int, dict[int, float]] = defaultdict(dict)
    for r in rows:
        v = STANCE_VAL.get(r["stance"], 0.0)
        cur = per_article[r["article_id"]].get(r["canonical_id"])
        if cur is None or abs(v) > abs(cur):
            per_article[r["article_id"]][r["canonical_id"]] = v
    M: dict[str, dict[int, float]] = {}
    for g in groups:
        members = [aid for aid, gg in gmap.items() if gg == g]
        acc: dict[int, list[float]] = defaultdict(list)
        for aid in members:
            for c, v in per_article.get(aid, {}).items():
                acc[c].append(v)
        M[g] = {c: float(np.mean(v)) for c, v in acc.items()}

    support_count = Counter(c for g in groups for c, v in M[g].items() if v > 0)
    notable = {c for c, n in support_count.items() if n >= 2}
    conflicts = set()
    for cr in store.rows(select(canonical.c.id, canonical.c.conflicts).where(canonical.c.story_id == story_id)):
        for o in cr["conflicts"] or []:
            conflicts.add(tuple(sorted((cr["id"], o))))
    # Facts every source reports the same way say nothing about sides. Keep the
    # discriminating ones: denied by someone, in a contradiction, or omitted by some.
    all_c = {c for g in groups for c in M[g]}
    in_conflict = {c for pair in conflicts for c in pair}
    informative = set()
    for c in all_c:
        signs = {np.sign(M[g][c]) for g in groups if M[g].get(c)}
        mentioned = sum(1 for g in groups if M[g].get(c))
        if len(signs) > 1 or c in in_conflict or (c in notable and mentioned < len(groups)):
            informative.add(c)

    n = len(groups)
    W = np.zeros((n, n))
    overlap = np.zeros((n, n), dtype=int)
    for i in range(n):
        for j in range(i + 1, n):
            gi, gj = M[groups[i]], M[groups[j]]
            vals = []
            for c in informative:
                x, y = gi.get(c, 0.0), gj.get(c, 0.0)
                if x and y:
                    vals.append(1.0 if x * y > 0 else -1.0)
                elif c in notable and (x > 0 or y > 0):
                    vals.append(-SETTINGS.omission_penalty)
            for c1, c2 in conflicts:
                if (gi.get(c1, 0) > 0 and gj.get(c2, 0) > 0) or (gi.get(c2, 0) > 0 and gj.get(c1, 0) > 0):
                    vals.append(-1.0)
            W[i, j] = W[j, i] = float(np.mean(vals)) if vals else 0.0
            overlap[i, j] = overlap[j, i] = len(vals)
    side, inter, intra = _split(W)
    if side is not None and not _direct_conflict(side, groups, M, conflicts):
        side = None

    # global clusters (stable letters across stories)
    gclus = {r["source"]: r["cluster"] for r in store.rows(select(source_clusters))}
    group_global: dict[str, int | None] = {}
    for g in groups:
        votes = Counter(gclus[source_key(by_id[aid])] for aid, gg in gmap.items()
                        if gg == g and source_key(by_id[aid]) in gclus)
        group_global[g] = votes.most_common(1)[0][0] if votes else None
    present_global = {v for v in group_global.values() if v is not None}

    labels: dict[str, str | None] = {}
    if len(present_global) >= 2:
        mode = "global"
        for i, g in enumerate(groups):
            if group_global[g] is not None:
                labels[g] = LETTERS[group_global[g] % len(LETTERS)]
            else:
                labels[g] = None
    elif side is not None:
        mode = "story"
        for i, g in enumerate(groups):
            labels[g] = f"{LETTERS[int(side[i])]}*"  # * = split found in this story only
    else:
        mode = "none"
        labels = {g: None for g in groups}

    # A split found inside one story is only trusted with >= 3 independent sources: between two
    # outlets, any discrepancy (a different casualty count, say) would look like two "sides".
    # Stable cross-story perspectives (global mode) carry their own evidence, so 2 is enough there.
    qualifies = n >= 2 and (len(present_global) >= 2 or (side is not None and n >= 3))

    # per-story pairwise agreement between sources, for the global matrix
    store.exec(delete(story_pairs).where(story_pairs.c.story_id == story_id))
    pair_rows = {}
    for i in range(n):
        for j in range(i + 1, n):
            if not overlap[i, j]:
                continue
            ki = {source_key(by_id[a]) for a, g in gmap.items() if g == groups[i]}
            kj = {source_key(by_id[a]) for a, g in gmap.items() if g == groups[j]}
            for a in ki:
                for b in kj:
                    if a != b:
                        x, y = sorted((a, b))
                        pair_rows[(x, y)] = W[i, j]
    if pair_rows:
        with store.engine.begin() as c:
            c.execute(insert(story_pairs), [dict(story_id=story_id, a=a, b=b, value=float(v))
                                            for (a, b), v in pair_rows.items()])

    old = (store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}).get("analysis") or {}
    analysis = {
        "attribution_map": old.get("attribution_map") or {},
        "qualified_by": "perspectives" if qualifies else None,
        "mode": mode,
        "split": side is not None,
        "inter_agreement": round(inter, 3),
        "intra_agreement": round(intra, 3),
        "groups": {
            g: {"articles": sorted(a for a, gg in gmap.items() if gg == g),
                "outlets": sorted({by_id[a]["outlet"] for a, gg in gmap.items() if gg == g}),
                "perspective": labels[g]}
            for g in groups
        },
        "article_group": {str(a): g for a, g in gmap.items()},
    }
    store.exec(update(stories).where(stories.c.id == story_id).values(analysis=analysis, qualifies=qualifies))
    return analysis


def mark_qualified(store: Store, story_id: int, rule: str) -> None:
    story = store.one(select(stories.c.analysis, stories.c.qualifies).where(stories.c.id == story_id))
    if not story or story["qualifies"]:
        return  # already qualifies under the perspectives rule
    analysis = dict((story or {}).get("analysis") or {})
    analysis["qualified_by"] = rule
    store.exec(update(stories).where(stories.c.id == story_id).values(qualifies=True, analysis=analysis))


def recompute_global(store: Store) -> int:
    """Cluster sources by their average agreement across all stories."""
    from scipy.optimize import linear_sum_assignment
    from sklearn.cluster import SpectralClustering
    from sklearn.metrics import silhouette_score

    agg: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in store.rows(select(story_pairs.c.a, story_pairs.c.b, story_pairs.c.value)):
        agg[(r["a"], r["b"])].append(r["value"])
    known = {k: float(np.mean(v)) for k, v in agg.items() if len(v) >= 2}
    deg = Counter()
    for a, b in known:
        deg[a] += 1
        deg[b] += 1
    sources = sorted(s for s, d in deg.items() if d >= 3)
    if len(sources) < SETTINGS.global_min_sources:
        return 0
    idx = {s: i for i, s in enumerate(sources)}
    A = np.full((len(sources), len(sources)), 0.5)
    np.fill_diagonal(A, 1.0)
    for (a, b), v in known.items():
        if a in idx and b in idx:
            A[idx[a], idx[b]] = A[idx[b], idx[a]] = (v + 1) / 2
    D = 1 - A
    best = None
    for k in range(2, min(5, len(sources))):
        try:
            lab = SpectralClustering(n_clusters=k, affinity="precomputed", random_state=0).fit_predict(A)
            if len(set(lab)) < 2:
                continue
            sil = silhouette_score(D, lab, metric="precomputed")
        except Exception as e:  # noqa: BLE001
            log.debug("spectral k=%d failed: %s", k, e)
            continue
        if best is None or sil > best[0]:
            best = (sil, lab)
    if best is None or best[0] < SETTINGS.global_min_silhouette:
        log.info("global perspectives: no clear structure yet (best silhouette %s)",
                 None if best is None else round(best[0], 3))
        store.exec(delete(source_clusters))
        return 0

    labels = best[1]
    # keep letters stable: match new clusters to previous ones by member overlap
    prev = {r["source"]: r["cluster"] for r in store.rows(select(source_clusters))}
    k_new = int(labels.max()) + 1
    prev_ids = sorted(set(prev.values()))
    mapping = {i: i for i in range(k_new)}
    if prev_ids:
        overlap = np.zeros((k_new, max(prev_ids) + 1))
        for s, l in zip(sources, labels):
            if s in prev:
                overlap[l, prev[s]] += 1
        rows_i, cols_i = linear_sum_assignment(-overlap)
        used = set()
        for r, c in zip(rows_i, cols_i):
            if overlap[r, c] > 0:
                mapping[r] = int(c)
                used.add(int(c))
        free = (i for i in range(100) if i not in used)
        for r in range(k_new):
            if r not in rows_i or overlap[r, mapping[r]] == 0:
                mapping[r] = next(free)
    store.exec(delete(source_clusters))
    now = utcnow()
    with store.engine.begin() as c:
        c.execute(insert(source_clusters), [dict(source=s, cluster=mapping[int(l)], updated_at=now)
                                            for s, l in zip(sources, labels)])
    log.info("global perspectives: %d sources in %d clusters (silhouette %.2f)", len(sources), k_new, best[0])
    return k_new
