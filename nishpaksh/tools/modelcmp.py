"""Would another model do the `light` / `page` work as well as Flash-Lite? Measured on real stories.

Each chosen story is copied (articles with text, extracted statements, perspective clusters) into a
scratch SQLite file per variant, its statement matching is thrown away, and the analysis is run
from scratch: matching, consolidation, perspectives, origins, fact/characterisation, code verdicts.
Then each variant writes a headline, rates importance and translates the stored page into Hindi
from the same inputs. Variants differ only in which model serves the light and page tiers:

    flash   Flash-Lite as configured (the reference)
    flash2  Flash-Lite again: how much Flash-Lite disagrees with itself (the noise floor)
    gemma   Gemma only (models of the `second` tier)

The comparison that matters is per extracted statement: what colour it would get. A statement
green-track under Gemma but not under Flash-Lite is a potential false positive. The report goes to
`diagnostics` (kind 'modelcmp'); nothing in the real tables changes except the quota counters.

    python -m nishpaksh.tools.modelcmp --stories 10595,10680 [--variants flash,flash2,gemma]
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import tempfile
from collections import Counter

from sqlalchemy import func, insert as sa_insert, text as sql

from ..config import database_url, gemini_api_keys, load_yaml
from ..db import Store, articles, canonical, claims, published, select, source_clusters, stories, update
from ..router import GeminiBackend, Router

log = logging.getLogger("modelcmp")
COLOUR = {"corroborated": "green", "developing": "green", "disputed": "amber", "false": "red",
          "confirmed": "green", "unverified": "grey", "pending": "grey"}


def _copy(src: Store, dst: Store, table, where, skip=("embedding", "minhash")) -> int:
    cols = [c for c in table.c if c.name not in skip]
    rows = src.rows(select(*cols).where(where)) if where is not None else src.rows(select(*cols))
    if rows:
        with dst.engine.begin() as c:
            for i in range(0, len(rows), 500):
                c.execute(sa_insert(table), rows[i:i + 500])
    return len(rows)


def scratch(prod: Store, ids: list[int]) -> Store:
    st = Store(f"sqlite:///{tempfile.mkdtemp()}/cmp.db")
    st.init()
    _copy(prod, st, stories, stories.c.id.in_(ids))
    _copy(prod, st, articles, articles.c.story_id.in_(ids))
    _copy(prod, st, claims, claims.c.story_id.in_(ids))
    _copy(prod, st, source_clusters, None)
    st.exec(update(claims).values(canonical_id=None))       # matching starts from nothing
    st.exec(update(stories).values(analysis={}, qualifies=False))
    return st


class Counting:
    """Router wrapper: counts calls, failures and serving models per tier."""

    def __init__(self, router: Router):
        self.r = router
        self.calls: Counter = Counter()
        self.fails: Counter = Counter()
        self.models: Counter = Counter()

    def call(self, tier, prompt, **kw):
        self.calls[tier] += 1
        try:
            res = self.r.call(tier, prompt, **kw)
        except Exception:
            self.fails[tier] += 1
            raise
        self.models[f"{tier}:{res.model}"] += 1
        return res

    def __getattr__(self, name):
        return getattr(self.r, name)


def make_router(prod: Store, variant: str) -> Counting:
    tiers = copy.deepcopy(load_yaml("models.yaml")["tiers"])
    if variant == "gemma":
        tiers["light"] = copy.deepcopy(tiers["second"])
        tiers["page"] = copy.deepcopy(tiers["second"])
    r = Router(tiers, [GeminiBackend(k, timeout_s=90) for k in gemini_api_keys()], prod)
    r.resolve()
    return Counting(r)


def analyse(st: Store, router, sid: int) -> dict[int, dict]:
    """Run the analysis stages; return per extracted statement its colour and what decided it."""
    from .. import match, origins, perspectives, verify
    from ..consolidate import consolidate_story
    match.match_story(st, router, sid)
    consolidate_story(st, router, sid)
    perspectives.analyze_story(st, sid)
    origins.assess_story(st, router, sid)
    verify.base_verdicts(st, sid)
    canon = {c["id"]: c for c in st.rows(select(canonical).where(canonical.c.story_id == sid))}
    out = {}
    for c in st.rows(select(claims.c.id, claims.c.canonical_id, claims.c.kind)
                     .where(claims.c.story_id == sid, claims.c.kind != "relation")):
        k = canon.get(c["canonical_id"])
        if not k:
            continue
        o = k["origins"] or {}
        out[c["id"]] = {"colour": COLOUR.get(k["verdict"], "grey"), "canon": k["id"], "text": k["text"],
                        "origins": o.get("n_origins"), "outlets": o.get("outlets"), "checkable": k["checkable"],
                        "conflicts": len(k["conflicts"] or [])}
    return out


def page_tasks(prod: Store, router, sid: int, facts: list[str]) -> dict:
    from .. import compose, importance
    st = Store(f"sqlite:///{tempfile.mkdtemp()}/tr.db")
    st.init()
    out: dict = {"headline": compose._headline(router, facts[:6], set(), fallback="(fallback)")}
    out["importance"] = importance.assess(router, out["headline"], facts, None).get("score")
    row = prod.one(select(published.c.payload_en).where(published.c.story_id == sid))
    if row and row["payload_en"]:
        pe = row["payload_en"]
        sample = {"headline": pe.get("headline") or "", "undated": [], "established": [], "contested": [],
                  "timeline": [], "framing": [],
                  "narrative": {"paragraphs": (pe.get("narrative") or {}).get("paragraphs", [])[:2]}}
        strings = compose._collect_strings(sample)
        hi = compose.translate_payload(st, router, sample)
        hs = compose._collect_strings(hi)
        done = sum(1 for a, b in zip(strings, hs) if a != b and re.search(r"[ऀ-ॿ]", b))
        out["translation"] = {"strings": len(strings), "translated": done,
                              "sample": [[a[:160], b[:160]] for a, b in list(zip(strings, hs))[:3]]}
    return out


def compare(a: dict[int, dict], b: dict[int, dict]) -> dict:
    common = sorted(set(a) & set(b))
    conf = Counter(f'{a[i]["colour"]}->{b[i]["colour"]}' for i in common)
    agree = sum(1 for i in common if a[i]["colour"] == b[i]["colour"])
    new_green = [i for i in common if b[i]["colour"] == "green" and a[i]["colour"] != "green"]
    lost_green = [i for i in common if a[i]["colour"] == "green" and b[i]["colour"] != "green"]
    # did the two runs merge the same statements together? (pairs of claims sharing a canonical)
    def pairs(d):
        by = {}
        for i in common:
            by.setdefault(d[i]["canon"], []).append(i)
        return {(x, y) for g in by.values() for k, x in enumerate(g) for y in g[k + 1:]}
    pa, pb = pairs(a), pairs(b)
    return {"statements": len(common), "colour_agreement": round(agree / len(common), 3) if common else None,
            "confusion": dict(conf), "new_green": len(new_green), "lost_green": len(lost_green),
            "merge_pairs": [len(pa), len(pb), len(pa & pb)],
            "checkable_disagree": sum(1 for i in common if a[i]["checkable"] != b[i]["checkable"]),
            "origins_disagree": sum(1 for i in common if (a[i]["origins"] or 0) != (b[i]["origins"] or 0)),
            "new_green_examples": [{"text": b[i]["text"][:200], "ref": a[i]["colour"], "ref_origins": a[i]["origins"],
                                    "origins": b[i]["origins"], "ref_checkable": a[i]["checkable"],
                                    "checkable": b[i]["checkable"]} for i in new_green[:8]],
            "lost_green_examples": [{"text": a[i]["text"][:200], "now": b[i]["colour"], "origins": b[i]["origins"],
                                     "checkable": b[i]["checkable"]} for i in lost_green[:5]]}


def pick_stories(prod: Store, n: int) -> list[int]:
    """Published stories with the most extracted statements, capped so one run stays cheap."""
    with prod.engine.connect() as c:
        rows = c.execute(select(claims.c.story_id, func.count()).where(
            claims.c.story_id.in_(select(published.c.story_id))).group_by(claims.c.story_id)).all()
    rows = [r for r in rows if 15 <= r[1] <= 160]
    rows.sort(key=lambda r: -r[1])
    return [r[0] for r in rows[:n]]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--stories", default="")
    p.add_argument("--n", type=int, default=6)
    p.add_argument("--variants", default="flash,flash2,gemma")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    prod = Store(database_url())
    ids = [int(x) for x in a.stories.split(",") if x.strip()] or pick_stories(prod, a.n)
    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    log.info("stories %s, variants %s", ids, variants)
    results: dict = {v: {} for v in variants}
    pages: dict = {v: {} for v in variants}
    usage: dict = {}
    for v in variants:
        router = make_router(prod, v)
        st = scratch(prod, ids)
        for sid in ids:
            try:
                results[v][sid] = analyse(st, router, sid)
            except Exception as e:  # noqa: BLE001
                log.exception("%s story %s analysis failed", v, sid)
                results[v][sid] = {}
                pages[v][sid] = {"error": repr(e)[:300]}
        usage[v] = {"calls": dict(router.calls), "fails": dict(router.fails), "models": dict(router.models)}
    # page tasks from identical inputs: the reference variant's best-supported statements
    ref = variants[0]
    for sid in ids:
        by_canon = {}
        for c in results[ref].get(sid, {}).values():
            by_canon.setdefault(c["canon"], [c["text"], 0])[1] += 1
        facts = [t for t, _ in sorted(by_canon.values(), key=lambda x: -x[1])][:8]
        for v in variants:
            router = make_router(prod, v)
            try:
                pages[v].setdefault(sid, {}).update(page_tasks(prod, router, sid, facts))
            except Exception as e:  # noqa: BLE001
                log.exception("%s story %s page tasks failed", v, sid)
                pages[v].setdefault(sid, {})["page_error"] = repr(e)[:300]
            usage[v].setdefault("page_calls", Counter()).update(router.calls)
            usage[v].setdefault("page_fails", Counter()).update(router.fails)
    report: dict = {"stories": ids, "variants": variants, "usage": usage, "pages": pages, "comparisons": {}}
    flat = {v: {(sid, cid): x for sid, d in results[v].items() for cid, x in d.items()} for v in variants}
    for v in variants[1:]:
        report["comparisons"][f"{ref}->{v}"] = compare(flat[ref], flat[v])
    report["colours"] = {v: dict(Counter(x["colour"] for x in flat[v].values())) for v in variants}
    log.info("%s", json.dumps({k: report[k] for k in ("colours", "comparisons", "usage")}, default=str)[:6000])
    with prod.engine.begin() as c:
        c.execute(sql("insert into diagnostics (kind, report) values ('modelcmp', :r)"),
                  {"r": json.dumps(report, default=str, ensure_ascii=False)})


if __name__ == "__main__":
    main()
