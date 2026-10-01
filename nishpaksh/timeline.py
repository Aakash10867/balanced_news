"""Stage 8a: order events without pretending to know more than the sources say.

Each event has a time interval (exact times are short intervals; "Tuesday night"
is six hours; unknown is no interval). A comes before B only if A's interval ends
before B's begins, or if the order is a stated relation that has itself been
corroborated across perspectives. Everything else is "same period, order not
established". The result is a partial order, laid out in tiers by code.
"""
from __future__ import annotations

import datetime as dt

import networkx as nx


def _t(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def build_timeline(events: list[dict], relation_edges: list[tuple[int, int]]) -> dict:
    """events: [{id, start, end, weight}]; relation_edges: corroborated (earlier, later) pairs."""
    G = nx.DiGraph()
    info = {}
    for e in events:
        s, t = _t(e.get("start")), _t(e.get("end"))
        info[e["id"]] = (s, t or s, e.get("weight", 0))
        G.add_node(e["id"])
    timed = [i for i, (s, t, _) in info.items() if s]
    for a in timed:
        for b in timed:
            if a != b and info[a][1] < info[b][0]:
                G.add_edge(a, b, kind="interval")
    for a, b in relation_edges:
        if a in G and b in G and a != b and not G.has_edge(a, b):
            G.add_edge(a, b, kind="relation")

    dropped = []
    while True:
        try:
            cycle = nx.find_cycle(G)
        except nx.NetworkXNoCycle:
            break
        rel = [(u, v) for u, v, *_ in cycle if G.edges[u, v].get("kind") == "relation"]
        u, v = (rel or [cycle[0][:2]])[0]
        G.remove_edge(u, v)
        dropped.append([u, v])

    undated = [n for n in G if G.degree(n) == 0 and info[n][0] is None]
    H = G.subgraph([n for n in G if n not in undated]).copy()
    tier_of = {}
    for k, gen in enumerate(nx.topological_generations(H)):
        for n in gen:
            tier_of[n] = k

    # timed events with no ordering edge overlap everything: place near their closest-in-time neighbour
    connected = [n for n in H if H.degree(n) > 0]
    if connected:
        def mid(n):
            s, t, _ = info[n]
            return s + (t - s) / 2
        for n in H:
            if H.degree(n) == 0 and info[n][0] is not None:
                nearest = min((c for c in connected if info[c][0] is not None),
                              key=lambda c: abs((mid(c) - mid(n)).total_seconds()), default=None)
                if nearest is not None:
                    tier_of[n] = tier_of[nearest]

    n_tiers = max(tier_of.values()) + 1 if tier_of else 0
    tiers = [[] for _ in range(n_tiers)]
    for n, k in tier_of.items():
        tiers[k].append(n)
    key = lambda n: (-info[n][2], info[n][0] or dt.datetime.max, n)  # noqa: E731
    tiers = [sorted(t, key=key) for t in tiers if t]
    return {"tiers": tiers, "undated": sorted(undated, key=key), "dropped_edges": dropped}
