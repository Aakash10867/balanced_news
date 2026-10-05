"""Do outlets really take positions? The daily test that decides whether perspectives may be shown.

Evidence (owner-approved, Oct 2026), compared only like with like (same story, same fact):
  omission   which genuinely reported facts each outlet left out (verified against its text; the
             main signal: each side leaves out what is inconvenient to it)
  stance     asserting vs denying the same fact
  framing    which loaded words (as English concepts) are used for the same fact
  voices     which named sources an outlet carries in a story where others are quoted
Nothing about the outlets themselves (owner, reputation, assumed leaning) is used.

Model: one-dimensional ideal points, as political scientists use for sparse roll-call votes:
P(outlet o gives answer 1 on item i) = sigmoid(a_i + b_i * theta_o). The model is not told which
facts favour which side; it finds facts the same outlets keep leaving out, story after story.
Random omissions (a short brief) form no pattern and move no one.

The test: positions must be stable when the stories are resampled, clearly separate outlets, and
beat the same data with outlet names shuffled within each story. Only then do perspectives appear.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
from collections import Counter, defaultdict

import numpy as np

from .config import SETTINGS
from .db import Store, articles, claims, diagnostics, insert, select, stories, utcnow

log = logging.getLogger(__name__)


def build_items(store: Store) -> dict[int, list[tuple[str, dict[str, int]]]]:
    """Items per story: (type, {unit: 0/1})."""
    from . import concepts
    from .perspectives import source_key
    arts = {a["id"]: a for a in store.rows(select(articles.c.id, articles.c.outlet, articles.c.author,
                                                  articles.c.agency, articles.c.story_id)
                                           .where(articles.c.extracted_at.is_not(None)))}
    analyses = {r["id"]: (r["analysis"] or {}) for r in store.rows(select(stories.c.id, stories.c.analysis))}
    per_story: dict[int, list] = defaultdict(list)
    # omission (from each story's verified coverage)
    for sid, an in analyses.items():
        cov = an.get("coverage") or {}
        facts = {c for v in cov.values() for c in v.get("omitted", [])}
        for c in facts:
            d = {u: 1 for u, v in cov.items() if c in v.get("reported", [])}
            d.update({u: 0 for u, v in cov.items() if c in v.get("omitted", [])})
            if sum(d.values()) >= 1 and len(d) >= 3:
                per_story[sid].append(("omission", d))
    # stance, framing, voices (from the statements)
    rows = store.rows(select(claims.c.story_id, claims.c.article_id, claims.c.canonical_id, claims.c.stance,
                             claims.c.attributed_to, claims.c.loaded_words).where(claims.c.canonical_id.is_not(None)))
    words = {w for r in rows for w in (r["loaded_words"] or [])}
    concept = concepts.lookup(store, words)
    by_story: dict[int, list] = defaultdict(list)
    for r in rows:
        if r["article_id"] in arts:
            by_story[r["story_id"]].append(r)
    for sid, rs in by_story.items():
        unit = {r["article_id"]: source_key(arts[r["article_id"]]) for r in rs}
        if len(set(unit.values())) < 3:
            continue
        amap = (analyses.get(sid) or {}).get("attribution_map") or {}
        stance: dict[int, dict[str, int]] = defaultdict(dict)
        reporters: dict[int, set[str]] = defaultdict(set)
        framed: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        voice: dict[str, set[str]] = defaultdict(set)
        for r in rs:
            u = unit[r["article_id"]]
            stance[r["canonical_id"]][u] = 0 if r["stance"] == "denies" else 1
            reporters[r["canonical_id"]].add(u)
            for w in r["loaded_words"] or []:
                if w in concept:
                    framed[r["canonical_id"]][concept[w]].add(u)
            m = amap.get(r["attributed_to"] or "") or {}
            if m.get("key") and m.get("kind") not in ("own", "anonymous", "unresolved", None):
                voice[m["key"]].add(u)
        for c, d in stance.items():
            if len(d) >= 2 and len(set(d.values())) > 1:
                per_story[sid].append(("stance", d))
        for c, ws in framed.items():
            pool = reporters[c]
            for w, us in ws.items():
                if len(pool) >= 3 and 1 <= len(us) < len(pool):
                    per_story[sid].append(("framing", {u: int(u in us) for u in pool}))
        quoting = {u for us in voice.values() for u in us}
        if len(quoting) >= 3:
            for k, us in voice.items():
                if len(us) < len(quoting):
                    per_story[sid].append(("voices", {u: int(u in us) for u in quoting}))
    return dict(per_story)


def fit(items: list[tuple[str, dict[str, int]]], units: list[str], iters: int = 400, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    idx = {u: k for k, u in enumerate(units)}
    obs = [(i, idx[u], y) for i, (_, d) in enumerate(items) for u, y in d.items() if u in idx]
    if not obs:
        return np.zeros(len(units))
    ii = np.array([o[0] for o in obs], int)
    oo = np.array([o[1] for o in obs], int)
    yy = np.array([o[2] for o in obs], float)
    n_i = len(items)
    th, a, b = rng.normal(0, .1, len(units)), np.zeros(n_i), rng.normal(0, .1, n_i)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(a[ii] + b[ii] * th[oo])))
        r = yy - p
        a += .05 * (np.bincount(ii, r, n_i) - .1 * a)
        b += .05 * (np.bincount(ii, r * th[oo], n_i) - b)
        th += .05 * (np.bincount(oo, r * b[ii], len(units)) - th)
        th = (th - th.mean()) / (th.std() + 1e-9)
    return th


def _measure(per_story: dict[int, list], units: list[str], rounds: int, seed: int) -> dict:
    items = [it for v in per_story.values() for it in v]
    th = fit(items, units)
    sids = sorted(per_story)
    rng = random.Random(seed)
    boots = []
    for k in range(rounds):
        pick = [rng.choice(sids) for _ in sids]
        tb = fit([it for s in pick for it in per_story[s]], units, seed=k)
        if tb.std() == 0 or th.std() == 0:
            continue
        boots.append(tb if np.corrcoef(tb, th)[0, 1] >= 0 else -tb)
    if not boots:
        return {"r": 0.0, "separated": 0.0, "theta": th, "lo": th, "hi": th}
    B = np.array(boots)
    lo, hi = np.percentile(B, 5, 0), np.percentile(B, 95, 0)
    n = len(units)
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    sep = float(np.mean([hi[i] < lo[j] or hi[j] < lo[i] for i, j in pairs])) if pairs else 0.0
    r = float(np.mean([np.corrcoef(x, th)[0, 1] for x in B]))
    return {"r": r, "separated": sep, "theta": th, "lo": lo, "hi": hi}


def _shuffle(per_story: dict[int, list], rng: random.Random) -> dict[int, list]:
    out = {}
    for sid, items in per_story.items():
        us = sorted({u for _, d in items for u in d})
        perm = us[:]
        rng.shuffle(perm)
        m = dict(zip(us, perm))
        out[sid] = [(t, {m[u]: y for u, y in d.items()}) for t, d in items]
    return out


def test_positions(store: Store, rounds: int = 20, shuffles: int = 3) -> dict:
    per_story = build_items(store)
    counts = Counter(u for v in per_story.values() for _, d in v for u in d)
    units = sorted(u for u, c in counts.items() if c >= SETTINGS.positions_min_answers)
    kinds = Counter(t for v in per_story.values() for t, _ in v)
    report: dict = {"items": dict(kinds), "stories": len(per_story), "units": len(units), "signal": False}
    if len(units) < 4:
        report["why"] = "too few outlets with enough evidence"
        return report
    real = _measure(per_story, units, rounds, 1)
    rng = random.Random(7)
    null = [_measure(_shuffle(per_story, rng), units, max(5, rounds // 3), 2 + k) for k in range(shuffles)]
    null_r = float(np.mean([x["r"] for x in null]))
    null_sep = float(np.mean([x["separated"] for x in null]))
    report.update(r=round(real["r"], 3), separated=round(real["separated"], 3),
                  null_r=round(null_r, 3), null_separated=round(null_sep, 3))
    report["signal"] = bool(real["r"] >= SETTINGS.positions_min_r and real["separated"] >= SETTINGS.positions_min_separated
                            and real["r"] >= null_r + SETTINGS.positions_beat_null_r
                            and real["separated"] >= 2 * null_sep)
    order = np.argsort(real["theta"])
    report["positions"] = [{"unit": units[k], "theta": round(float(real["theta"][k]), 3),
                            "lo": round(float(real["lo"][k]), 3), "hi": round(float(real["hi"][k]), 3),
                            "answers": counts[units[k]]} for k in order]
    return report


def latest(store: Store) -> dict | None:
    row = store.one(select(diagnostics.c.report, diagnostics.c.created_at).where(diagnostics.c.kind == "positions")
                    .order_by(diagnostics.c.id.desc()).limit(1))
    if not row:
        return None
    return dict(row["report"] or {}, at=row["created_at"].isoformat(timespec="minutes") if row["created_at"] else None)


def daily(store: Store, until: float | None = None) -> dict | None:
    """Run the test once a day (it reads every stored story; a few seconds of numpy)."""
    import time
    last = store.one(select(diagnostics.c.created_at).where(diagnostics.c.kind == "positions")
                     .order_by(diagnostics.c.id.desc()).limit(1))
    if last and last["created_at"] and utcnow() - last["created_at"] < dt.timedelta(hours=SETTINGS.positions_every_hours):
        return None
    if until is not None and time.time() > until:
        return None
    report = test_positions(store)
    store.exec(insert(diagnostics).values(kind="positions", report=report, created_at=utcnow()))
    log.info("positions test: signal=%s r=%s separated=%s (shuffled r=%s, separated=%s)", report.get("signal"),
             report.get("r"), report.get("separated"), report.get("null_r"), report.get("null_separated"))
    return report
