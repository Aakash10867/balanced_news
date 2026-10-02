"""Stage 2: wire-copy detection.

Twenty outlets running the same PTI story are one source, not twenty.
Near-duplicate text is found with MinHash over 5-word shingles; agency bylines
(PTI, ANI, Bhasha ...) catch translated or rewritten wire copy that text
similarity misses.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import random
import re

import numpy as np

from .config import SETTINGS
from .db import Store, articles, select, update, utcnow

_P = (1 << 61) - 1
_rng = random.Random(1729)
_N = 64
_A = np.array([_rng.randrange(1, _P) for _ in range(_N)], dtype=object)
_B = np.array([_rng.randrange(0, _P) for _ in range(_N)], dtype=object)


def shingles(text: str, k: int = 5) -> set[str]:
    words = re.findall(r"\w+", (text or "").lower())
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


def minhash(text: str) -> list[int] | None:
    sh = shingles(text)
    if not sh:
        return None
    hs = np.array([_h(s) for s in sh], dtype=object)
    sig = []
    for a, b in zip(_A, _B):
        sig.append(int(((a * hs + b) % _P).min()))
    return sig


def jaccard(s1, s2) -> float:
    return sum(x == y for x, y in zip(s1, s2)) / len(s1)


def assign_wire_groups(store: Store) -> int:
    since = utcnow() - dt.timedelta(hours=SETTINGS.wire_window_hours)
    rows = store.rows(
        select(articles.c.id, articles.c.minhash, articles.c.wire_group)
        .where(articles.c.published_at >= since, articles.c.minhash.is_not(None))
        .order_by(articles.c.id)
    )
    rows = [r for r in rows if r["minhash"]]  # cleared fingerprints can be JSON null, which passes IS NOT NULL
    done = [r for r in rows if r["wire_group"] is not None]
    new = [r for r in rows if r["wire_group"] is None]
    if not new:
        return 0
    # uint64 matrix for vectorised comparison
    def as_arr(sig):
        return np.array([x & 0xFFFFFFFFFFFFFFFF for x in sig], dtype=np.uint64)

    mat = np.stack([as_arr(r["minhash"]) for r in done]) if done else np.zeros((0, _N), dtype=np.uint64)
    groups = [r["wire_group"] for r in done]
    for r in new:
        sig = as_arr(r["minhash"])
        group = r["id"]
        if len(groups):
            sims = (mat == sig).mean(axis=1)
            j = int(sims.argmax())
            if sims[j] >= SETTINGS.wire_jaccard:
                group = groups[j]
        store.exec(update(articles).where(articles.c.id == r["id"]).values(wire_group=group))
        mat = np.vstack([mat, sig[None, :]])
        groups.append(group)
    return len(new)


def independence_groups(arts: list[dict]) -> dict[int, str]:
    """Union articles that are not independent of each other: same wire text,
    same agency byline, or same outlet. Returns article id -> group key."""
    parent = {a["id"]: a["id"] for a in arts}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    for key in ("wire_group", "agency", "outlet"):
        first: dict = {}
        for a in arts:
            v = a.get(key)
            if v is None:
                continue
            if v in first:
                union(a["id"], first[v])
            else:
                first[v] = a["id"]
    return {a["id"]: f"g{find(a['id'])}" for a in arts}
