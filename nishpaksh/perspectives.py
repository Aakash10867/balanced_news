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
from .wire import independence_groups, independent, is_state

log = logging.getLogger(__name__)
STANCE_VAL = {"asserts": 1.0, "attributes": 0.5, "denies": -1.0}
LETTERS = "ABCDEFGH"
GENERIC_AUTHOR = re.compile(r"(?i)\b(staff|desk|bureau|team|news|web|correspondent|reporter|online|editor|agencies)\b")


def author_key(a: dict) -> str | None:
    author = (a.get("author") or "").strip()
    if author and not a.get("agency") and len(author) <= 60 and not GENERIC_AUTHOR.search(author):
        return f"{a['outlet']}::{author.lower()}"
    return None


# authors whose articles keep falling outside their outlet's perspective: their own unit (refresh_units)
SPLIT_AUTHORS: set[str] = set()


def source_key(a: dict) -> str:
    """The unit perspectives are clustered over: the outlet (an editorial line pools all its articles'
    evidence), except an author the data has split off (Oct 2026, owner's decision)."""
    ak = author_key(a)
    return ak if ak and ak in SPLIT_AUTHORS else a["outlet"]


def _unit(key: str) -> str:
    """A stored pair key in today's units: rows written when every author was a unit read as their
    outlet unless that author is split off."""
    return key if "::" not in key or key in SPLIT_AUTHORS else key.split("::", 1)[0]


def outlet_key(a: dict) -> str:
    return a["outlet"]


# ---- evidence of a perspective (owner-approved, Oct 2026), strongest first:
#   stance on contested facts (asserts vs denies, sides of a contradiction)       weight 1
#   whose voices are carried (named sources quoted)                                weight 0.5
#   loaded words used for the same fact                                            weight 0.5
#   omission of a widely reported fact (mostly article length: weak)               weight 0.15
# Never: anything about the outlet itself (owner, reputation, assumed leaning).
VOICE_W, WORDS_W, OMISSION_W = 0.5, 0.5, 0.15


def _profile(stance: dict[int, float], voices: Counter, words: dict[int, set]) -> dict:
    return {"stance": stance, "voices": voices, "words": words}


def agreement(pa: dict, pb: dict, informative: set[int], notable: set[int], conflicts: set) -> tuple[float, float, int]:
    """Signed agreement in [-1, 1] between two profiles in one story, its total weight, and how
    many evidence items it rests on."""
    vals: list[tuple[float, float]] = []
    ga, gb = pa["stance"], pb["stance"]
    for c in informative:
        x, y = ga.get(c, 0.0), gb.get(c, 0.0)
        if x and y:
            vals.append((1.0 if x * y > 0 else -1.0, 1.0))
        elif c in notable and (x > 0 or y > 0):
            # an omission is a weak hint (it mostly reflects article length and what was read), so
            # it carries little weight rather than a small value at full weight: on real data the
            # many omissions drowned the rare real contradictions and agreement centred on zero
            vals.append((-1.0, OMISSION_W))
    for c1, c2 in conflicts:
        if (ga.get(c1, 0) > 0 and gb.get(c2, 0) > 0) or (ga.get(c2, 0) > 0 and gb.get(c1, 0) > 0):
            vals.append((-1.0, 1.0))
    va, vb = pa["voices"], pb["voices"]
    if sum(va.values()) >= 2 and sum(vb.values()) >= 2:
        keys = set(va) | set(vb)
        inter = sum(min(va[k], vb[k]) for k in keys)
        union = sum(max(va[k], vb[k]) for k in keys)
        vals.append((2 * inter / union - 1, VOICE_W))      # same voices +1, disjoint voices -1
    for c in set(pa["words"]) & set(pb["words"]):
        wa, wb = pa["words"][c], pb["words"][c]
        if wa and wb:
            vals.append((1.0 if wa & wb else -1.0, WORDS_W))
        elif wa or wb:
            vals.append((-0.5, WORDS_W))                   # one frames the fact, the other does not
    if not vals:
        return 0.0, 0.0, 0
    tw = sum(w for _, w in vals)
    return sum(v * w for v, w in vals) / tw, tw, len(vals)


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
                             articles.c.wire_group, articles.c.role, articles.c.text, articles.c.text_source,
                             articles.c.extracted_at).where(articles.c.story_id == story_id))
    by_id = {a["id"]: a for a in arts}
    gmap = independence_groups(arts)
    # state media are a government speaking, not an outlet with a perspective: listed, never clustered
    groups = sorted(independent(gmap))
    state_groups = sorted({g for g in gmap.values() if is_state(g)})

    old_analysis = (store.one(select(stories.c.analysis).where(stories.c.id == story_id)) or {}).get("analysis") or {}
    amap = old_analysis.get("attribution_map") or {}
    rows = store.rows(select(claims.c.article_id, claims.c.canonical_id, claims.c.stance, claims.c.attributed_to,
                             claims.c.loaded_words)
                      .where(claims.c.story_id == story_id, claims.c.canonical_id.is_not(None)))
    per_article: dict[int, dict[int, float]] = defaultdict(dict)
    art_voices: dict[int, Counter] = defaultdict(Counter)
    art_words: dict[int, dict[int, set]] = defaultdict(lambda: defaultdict(set))
    for r in rows:
        v = STANCE_VAL.get(r["stance"], 0.0)
        cur = per_article[r["article_id"]].get(r["canonical_id"])
        if cur is None or abs(v) > abs(cur):
            per_article[r["article_id"]][r["canonical_id"]] = v
        m = amap.get(r["attributed_to"] or "") or {}
        if m.get("key") and m.get("kind") not in ("own", "anonymous", "unresolved", None):
            art_voices[r["article_id"]][m["key"]] += 1
        art_words[r["article_id"]][r["canonical_id"]] |= {w for w in (r["loaded_words"] or [])}
    # framing is compared as English concepts; a Hindi word not yet mapped is left out (comparing it
    # raw would only separate Hindi outlets from English ones)
    from . import concepts
    concept = concepts.lookup(store, {w for d in art_words.values() for ws in d.values() for w in ws})
    for d in art_words.values():
        for c in list(d):
            d[c] = {concept[w] for w in d[c] if w in concept}
    M: dict[str, dict[int, float]] = {}
    prof: dict[str, dict] = {}
    for g in groups:
        members = [aid for aid, gg in gmap.items() if gg == g]
        acc: dict[int, list[float]] = defaultdict(list)
        voices: Counter = Counter()
        words: dict[int, set] = defaultdict(set)
        for aid in members:
            for c, v in per_article.get(aid, {}).items():
                acc[c].append(v)
            voices.update(art_voices.get(aid, {}))
            for c, ws in art_words.get(aid, {}).items():
                words[c] |= ws
        M[g] = {c: float(np.mean(v)) for c, v in acc.items()}
        prof[g] = _profile(M[g], voices, dict(words))

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
            w, _, k = agreement(prof[groups[i]], prof[groups[j]], informative, notable, conflicts)
            W[i, j] = W[j, i] = w
            overlap[i, j] = overlap[j, i] = k
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

    # An article may depart from its outlet's perspective (owner, Oct 2026: "1 in X times"). Each
    # article is scored against the other groups of each perspective in this story; it leaves its
    # outlet's perspective only on strong evidence (a clear margin, 3+ evidence items), never on one
    # odd quote. Measured against the OUTLET's perspective always, so a split-off author keeps being
    # tested the same way (refresh_units).
    departures: dict[str, dict] = {}
    assessed: dict[str, dict] = {}
    if mode == "global":
        for aid, g in gmap.items():
            a = by_id[aid]
            own = gclus.get(outlet_key(a))
            if own is None or aid not in per_article:
                continue
            ap = _profile(per_article[aid], art_voices.get(aid, Counter()), dict(art_words.get(aid, {})))
            score: dict[int, list[tuple[float, float]]] = defaultdict(list)
            evidence = 0
            for h in groups:
                if h == g or group_global.get(h) is None:
                    continue
                w, tw, k = agreement(ap, prof[h], informative, notable, conflicts)
                if tw:
                    score[group_global[h]].append((w, tw))
                    evidence += k
            if own not in score or len(score) < 2:
                continue          # nothing of its own outlet's perspective here to compare with
            mean = {c: sum(w * t for w, t in v) / sum(t for _, t in v) for c, v in score.items()}
            best = max(mean, key=mean.get)
            entry = {"outlet": outlet_key(a), "author": author_key(a), "evidence": evidence}
            if evidence >= SETTINGS.departure_min_evidence:
                assessed[str(aid)] = entry
                if (best != own and mean[best] > 0
                        and mean[best] - mean[own] >= SETTINGS.departure_margin):
                    departures[str(aid)] = dict(entry, **{"from": LETTERS[own % len(LETTERS)],
                                                          "to": LETTERS[best % len(LETTERS)]})

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

    analysis = {
        # every other stage's work survives: this step rewrites only its own fields. It used to keep a fixed
        # list of other fields, so whatever was added later was wiped every run while "consolidated" was kept
        # (Oct 9 2026, story 16000): covered lines, updates, doubtful disputes and the review's cached answers
        # never reached the writer, whose own review then saw the story as done. Doubtful disputes block
        # green: wiping them could show a statement as established.
        **old_analysis,
        # article bodies are cleared after a while (retention): keep the evidence already gathered
        "coverage": _coverage(store, story_id, arts, gmap) or old_analysis.get("coverage") or {},
        "departures": departures,
        "assessed": assessed,
        "qualified_by": "perspectives" if qualifies else None,
        "mode": mode,
        "split": side is not None,
        "inter_agreement": round(inter, 3),
        "intra_agreement": round(intra, 3),
        "groups": {
            g: {"articles": sorted(a for a, gg in gmap.items() if gg == g),
                "outlets": sorted({by_id[a]["outlet"] for a, gg in gmap.items() if gg == g}),
                "perspective": labels.get(g)}
            for g in groups + state_groups
        },
        "article_group": {str(a): g for a, g in gmap.items()},
    }
    store.exec(update(stories).where(stories.c.id == story_id).values(analysis=analysis, qualifies=qualifies))
    return analysis


def _coverage(store: Store, story_id: int, arts: list[dict], gmap: dict[int, str]) -> dict:
    """Which genuinely reported facts each unit reported, and which it left out. A fact counts as
    genuinely reported when 2+ independent sources report it; a unit left it out only if none of its
    read articles reports it AND its text does not contain the fact's names and numbers (otherwise
    it is our reader's miss, not the outlet's choice). Omission patterns are the main evidence of a
    perspective (owner, Oct 2026): each side leaves out what is inconvenient to it."""
    from .textmatch import Text, verdict
    read = [a for a in arts if a["extracted_at"] and a["text_source"] != "summary" and a["text"]
            and not is_state(gmap[a["id"]])]    # state media are a government speaking, not a unit
    if len({gmap[a["id"]] for a in read}) < 3:
        return {}
    canon = {c["id"]: c["text"] for c in store.rows(select(canonical.c.id, canonical.c.text, canonical.c.kind)
                                                    .where(canonical.c.story_id == story_id)) if c["kind"] != "relation"}
    by_art: dict[int, set[int]] = defaultdict(set)
    for r in store.rows(select(claims.c.article_id, claims.c.canonical_id)
                        .where(claims.c.story_id == story_id, claims.c.canonical_id.is_not(None))):
        by_art[r["article_id"]].add(r["canonical_id"])
    groups_reporting: dict[int, set[str]] = defaultdict(set)
    for a in read:
        for c in by_art.get(a["id"], ()):
            groups_reporting[c].add(gmap[a["id"]])
    facts = [c for c, g in groups_reporting.items() if len(g) >= 2 and c in canon]
    units: dict[str, list[dict]] = defaultdict(list)
    for a in read:
        units[source_key(a)].append(a)
    texts = {a["id"]: Text(a["text"]) for a in read}
    out = {}
    for u, ua in units.items():
        reported = sorted({c for a in ua for c in by_art.get(a["id"], ()) if c in facts})
        omitted = []
        for c in facts:
            if c in reported:
                continue
            if all(verdict(canon[c], texts[a["id"]]) == "absent" for a in ua):
                omitted.append(c)
        out[u] = {"reported": reported, "omitted": sorted(omitted)}
    return out


def mark_qualified(store: Store, story_id: int, rule: str) -> None:
    story = store.one(select(stories.c.analysis, stories.c.qualifies).where(stories.c.id == story_id))
    if not story or story["qualifies"]:
        return  # already qualifies under the perspectives rule
    analysis = dict((story or {}).get("analysis") or {})
    analysis["qualified_by"] = rule
    store.exec(update(stories).where(stories.c.id == story_id).values(qualifies=True, analysis=analysis))


def _affinity_clusters(rows: list[tuple[str, str, float]], sources: list[str], k: int):
    from sklearn.cluster import SpectralClustering
    agg: dict[tuple[str, str], list[float]] = defaultdict(list)
    for a, b, v in rows:
        agg[(a, b)].append(v)
    idx = {s: i for i, s in enumerate(sources)}
    A = np.full((len(sources), len(sources)), 0.5)
    np.fill_diagonal(A, 1.0)
    for (a, b), v in agg.items():
        if len(v) >= 2 and a in idx and b in idx:
            A[idx[a], idx[b]] = A[idx[b], idx[a]] = (float(np.mean(v)) + 1) / 2
    return SpectralClustering(n_clusters=k, affinity="precomputed", random_state=0).fit_predict(A)


def _stability(store: Store, sources: list[str], labels, rounds: int = 10, share: float = 0.8) -> float:
    """Mean adjusted Rand index between the clustering and clusterings of random 80% subsets of the
    stories: near 1 when the perspectives are real, near 0 when they are noise."""
    import random
    from sklearn.metrics import adjusted_rand_score
    by_story: dict[int, list] = defaultdict(list)
    for r in store.rows(select(story_pairs.c.story_id, story_pairs.c.a, story_pairs.c.b, story_pairs.c.value)):
        a, b = sorted((_unit(r["a"]), _unit(r["b"])))
        if a != b:
            by_story[r["story_id"]].append((a, b, r["value"]))
    ids = sorted(by_story)
    rng = random.Random(0)
    k = int(max(labels)) + 1
    scores = []
    for _ in range(rounds):
        pick = rng.sample(ids, max(1, int(len(ids) * share)))
        try:
            lab = _affinity_clusters([x for sid in pick for x in by_story[sid]], sources, k)
        except Exception:  # noqa: BLE001
            scores.append(0.0)
            continue
        scores.append(adjusted_rand_score(list(labels), list(lab)))
    return float(np.mean(scores)) if scores else 0.0


def recompute_global(store: Store) -> int:
    """Cluster sources by their average agreement across all stories."""
    from scipy.optimize import linear_sum_assignment
    from sklearn.cluster import SpectralClustering
    from sklearn.metrics import silhouette_score

    agg: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in store.rows(select(story_pairs.c.a, story_pairs.c.b, story_pairs.c.value)):
        a, b = sorted((_unit(r["a"]), _unit(r["b"])))
        if a != b:
            agg[(a, b)].append(r["value"])
    known = {k: float(np.mean(v)) for k, v in agg.items() if len(v) >= 2}
    deg = Counter()
    for a, b in known:
        deg[a] += 1
        deg[b] += 1
    sources = sorted(s for s, d in deg.items() if d >= 3)
    if len(sources) < SETTINGS.global_min_sources:
        return 0
    # the daily positions test must have found real positions first (owner, Oct 2026: show sides
    # only when the data proves they exist; until then no perspectives at all)
    from . import positions
    pos = positions.latest(store)
    if SETTINGS.require_positions_signal and not (pos and pos.get("signal")):
        store.exec(delete(source_clusters))
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
    stab = None
    if best is not None and best[0] >= SETTINGS.global_min_silhouette:
        stab = _stability(store, sources, best[1])
    if best is None or best[0] < SETTINGS.global_min_silhouette or stab < SETTINGS.global_min_stability:
        # Oct 2026: silhouette 0.04-0.07 and resampling agreement (ARI) 0.02-0.68 on real data: the
        # clusters were noise crossing the threshold now and then, so perspectives flipped between
        # runs. Shown only when they also survive resampling of the stories.
        log.info("global perspectives: no stable structure yet (silhouette %s, stability %s)",
                 None if best is None else round(best[0], 3), None if stab is None else round(stab, 2))
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


def refresh_units(store: Store) -> dict:
    """Decide which authors are their own unit, from every stored story's per-article assessments:
    an author whose last 5 assessed articles left the outlet's perspective 3+ times is split off.
    Also measures, per outlet, how often its articles depart: if that is not rare, the outlet is not
    one perspective (or the perspectives are wrong) and health says so."""
    per_author: dict[str, list[tuple[int, bool]]] = defaultdict(list)
    per_outlet: dict[str, list[bool]] = defaultdict(list)
    for r in store.rows(select(stories.c.analysis).where(stories.c.analysis.is_not(None))):
        a = r["analysis"] or {}
        dep = a.get("departures") or {}
        for aid, e in (a.get("assessed") or {}).items():
            gone = aid in dep
            per_outlet[e["outlet"]].append(gone)
            if e.get("author"):
                per_author[e["author"]].append((int(aid), gone))
    split = set()
    for au, xs in per_author.items():
        last = [g for _, g in sorted(xs)[-SETTINGS.split_author_window:]]
        if sum(last) >= SETTINGS.split_author_departures:
            split.add(au)
    SPLIT_AUTHORS.clear()
    SPLIT_AUTHORS.update(split)
    often = {o: round(sum(v) / len(v), 2) for o, v in per_outlet.items()
             if len(v) >= SETTINGS.departure_watch_min and sum(v) / len(v) >= SETTINGS.departure_watch_rate}
    return {"split_authors": sorted(split), "outlets_departing_often": often,
            "assessed_articles": sum(len(v) for v in per_outlet.values()),
            "departures": sum(sum(v) for v in per_outlet.values())}
