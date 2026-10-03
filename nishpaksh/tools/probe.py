"""Measure the real world before changing the pipeline. Read-only on pipeline tables; the
report goes into the `diagnostics` table.

    python -m nishpaksh.tools.probe --parts grouping,fetch,search

grouping  how well embedding similarity separates "same event" from "different event",
          using a model to label sampled real article pairs in each similarity band
fetch     which blocked or summary-only outlets our fetcher and Tavily can actually read
search    whether Google News / Bing News searches find other outlets' coverage, and
          whether their links resolve to the publisher's URL
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import re
import time
from urllib.parse import parse_qs, quote_plus, urlsplit

import numpy as np
import requests
from sqlalchemy import text as sql

from ..config import database_url, gemini_api_key, load_yaml
from ..db import Store
from ..router import GeminiBackend, Router

log = logging.getLogger("probe")
BANDS = [(0.95, 1.01), (0.90, 0.95), (0.86, 0.90), (0.83, 0.86), (0.80, 0.83), (0.77, 0.80),
         (0.74, 0.77), (0.70, 0.74), (0.65, 0.70)]

PAIR_PROMPT = """Each numbered line shows two news items, A and B (headline and opening words).
For each line decide:
  "same"      - both report the same specific event, incident, announcement or statement
                (same who, what, and when; follow-ups on that exact incident count as same)
  "related"   - same broader topic or ongoing saga, but a different specific event
  "different" - unrelated
Language does not matter: a Hindi and an English item can be "same".

{pairs}

Reply with JSON only: {{"results": [{{"n": 1, "label": "same"}}, ...]}}"""


def _q(store: Store, stmt):
    """Run a read query and close the connection (an open transaction blocks schema changes)."""
    with store.engine.connect() as c:
        return list(c.execute(stmt))


def _lead(a: dict, n: int = 160) -> str:
    return re.sub(r"\s+", " ", (a.get("text") or "")[:n])


def _label_pairs(router: Router, pairs: list[tuple[dict, dict]]) -> list[str | None]:
    out: list[str | None] = []
    for i in range(0, len(pairs), 12):
        chunk = pairs[i:i + 12]
        lines = "\n".join(f'{k + 1}. A: "{a["title"]} — {_lead(a)}" | B: "{b["title"]} — {_lead(b)}"'
                          for k, (a, b) in enumerate(chunk))
        labels: list[str | None] = [None] * len(chunk)
        for tier in ("light", "second"):
            try:
                res = router.call(tier, PAIR_PROMPT.format(pairs=lines), json_out=True, max_output_tokens=1200)
                for r in (res.data or {}).get("results", []):
                    n = int(r.get("n", 0)) - 1
                    if 0 <= n < len(chunk) and r.get("label") in ("same", "related", "different"):
                        labels[n] = r["label"]
                break
            except Exception as e:  # noqa: BLE001
                log.warning("pair labelling via %s failed: %s", tier, e)
        out.extend(labels)
    return out


def probe_grouping(store: Store, router: Router, per_band: int = 24) -> dict:
    rows = [dict(r._mapping) for r in _q(store, sql(
        "select id, title, text, lang, outlet, story_id, embedding, text_source, published_at "
        "from articles where embedding is not null and published_at > now() - interval '96 hours'"))]
    rows = [r for r in rows if r["embedding"] and len(r["embedding"]) == 256]
    X = np.array([r["embedding"] for r in rows], dtype=np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    S = X @ X.T
    n = len(rows)
    iu = np.triu_indices(n, 1)
    allv = S[iu]
    rng = random.Random(7)
    report: dict = {"n_articles": n,
                    "pair_cosine_percentiles": {str(p): round(float(np.percentile(allv, p)), 3)
                                                for p in (50, 90, 99, 99.9)}}
    np.fill_diagonal(S, -1)
    nn = S.max(axis=1)
    report["nearest_neighbour_percentiles"] = {str(p): round(float(np.percentile(nn, p)), 3)
                                               for p in (10, 25, 50, 75, 90)}
    # sample pairs per band, half of them cross-language where possible
    sampled: list[tuple[int, int, float]] = []
    for lo, hi in BANDS:
        mask = (allv >= lo) & (allv < hi)
        idx = np.nonzero(mask)[0]
        if not len(idx):
            continue
        cand = [(int(iu[0][k]), int(iu[1][k])) for k in rng.sample(list(idx), min(len(idx), 4000))]
        cross = [p for p in cand if rows[p[0]]["lang"] != rows[p[1]]["lang"]]
        same_lang = [p for p in cand if rows[p[0]]["lang"] == rows[p[1]]["lang"]]
        pick = cross[: per_band // 3] + same_lang[: per_band - min(len(cross), per_band // 3)]
        sampled += [(i, j, float(S[i, j])) for i, j in pick]
    labels = _label_pairs(router, [(rows[i], rows[j]) for i, j, _ in sampled])
    bands = []
    detail = []
    for lo, hi in BANDS:
        got = [(i, j, c, l) for (i, j, c), l in zip(sampled, labels) if lo <= c < hi and l]
        if not got:
            continue
        cnt = {k: sum(1 for *_, l in got if l == k) for k in ("same", "related", "different")}
        xl = [g for g in got if rows[g[0]]["lang"] != rows[g[1]]["lang"]]
        bands.append({"band": f"{lo:.2f}-{hi:.2f}", "n": len(got), **cnt,
                      "precision_same": round(cnt["same"] / len(got), 2),
                      "cross_lang_n": len(xl),
                      "cross_lang_same": sum(1 for *_, l in xl if l == "same")})
        for i, j, c, l in got[:6]:
            detail.append({"cos": round(c, 3), "label": l, "a": rows[i]["title"][:90], "b": rows[j]["title"][:90],
                           "a_src": rows[i]["text_source"], "b_src": rows[j]["text_source"]})
    report["bands"] = bands
    report["examples"] = detail

    # does a title-only vector separate better than title + lead? same labelled pairs, re-embedded
    lab = [(i, j, l) for (i, j, _), l in zip(sampled, labels) if l]
    ids = sorted({i for i, _, _ in lab} | {j for _, j, _ in lab})
    alt: dict[str, dict[int, np.ndarray]] = {}
    for name, fn in (("title_only", lambda a: a["title"] or ""),
                     ("title_lead400", lambda a: f"{a['title'] or ''}. {_lead(a, 400)}")):
        vecs = router.embed([fn(rows[k]) for k in ids])
        if vecs:
            V = np.array(vecs, dtype=np.float32)
            V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
            alt[name] = dict(zip(ids, V))
    def auc(scores_same, scores_other):
        if not scores_same or not scores_other:
            return None
        wins = sum((s > o) + 0.5 * (s == o) for s in scores_same for o in scores_other)
        return round(wins / (len(scores_same) * len(scores_other)), 3)
    sep = {}
    for name, vv in alt.items():
        same = [float(vv[i] @ vv[j]) for i, j, l in lab if l == "same"]
        other = [float(vv[i] @ vv[j]) for i, j, l in lab if l != "same"]
        sep[name] = {"auc_same_vs_rest": auc(same, other),
                     "same_p10": round(float(np.percentile(same, 10)), 3) if same else None,
                     "other_p90": round(float(np.percentile(other, 90)), 3) if other else None}
    stored_same = [float(S[i, j]) for i, j, l in lab if l == "same"]
    stored_other = [float(S[i, j]) for i, j, l in lab if l != "same"]
    sep["stored"] = {"auc_same_vs_rest": auc(stored_same, stored_other)}
    report["representation"] = sep

    # summary-only texts: are they the ones in the giant clusters?
    report["summary_only_share"] = round(sum(r["text_source"] == "summary" for r in rows) / max(1, n), 3)
    return report


# fetch -------------------------------------------------------------------------------------
def _tavily(path: str, body: dict) -> dict:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return {"error": "no TAVILY_API_KEY"}
    r = requests.post(f"https://api.tavily.com/{path}", json=body, timeout=90,
                      headers={"Authorization": f"Bearer {key}"})
    try:
        data = r.json()
    except ValueError:
        data = {"error": r.text[:300]}
    data["_status"] = r.status_code
    return data


def probe_fetch(store: Store, extra_urls: list[str]) -> dict:
    from ..ingest import fetch_article
    rows = [dict(r._mapping) for r in _q(store, sql(
        "select distinct on (outlet) outlet, url from articles where text_source = 'summary' "
        "and published_at > now() - interval '48 hours' order by outlet, published_at desc"))]
    more = [dict(r._mapping) for r in _q(store, sql(
        "select outlet, url from (select outlet, url, row_number() over (partition by outlet order by published_at desc) k "
        "from articles where text_source = 'summary' and published_at > now() - interval '48 hours') t where k = 2"))]
    targets = [(r["outlet"], r["url"]) for r in rows + more] + [("search:" + urlsplit(u).netloc, u) for u in extra_urls]
    targets = targets[:20]
    out = []
    for outlet, url in targets:
        page = fetch_article(url)
        out.append({"outlet": outlet, "url": url, "ours_chars": len((page or {}).get("text") or "")})
    ext = _tavily("extract", {"urls": [u for _, u in targets], "extract_depth": "basic", "format": "text",
                              "include_usage": True})
    got = {r.get("url"): len(r.get("raw_content") or "") for r in ext.get("results", [])}
    failed = {f.get("url"): str(f.get("error"))[:120] for f in ext.get("failed_results", [])}
    for o in out:
        o["tavily_chars"] = got.get(o["url"], 0)
        if o["url"] in failed:
            o["tavily_error"] = failed[o["url"]]
    sample = next((r for r in ext.get("results", []) if (r.get("raw_content") or "")), None)
    return {"targets": out, "tavily_status": ext.get("_status"), "tavily_usage": ext.get("usage"),
            "tavily_error": ext.get("error") or ext.get("detail"),
            "sample_text": (sample or {}).get("raw_content", "")[:600]}


# search ------------------------------------------------------------------------------------
def _gnews(query: str, lang: str) -> list[dict]:
    import feedparser
    hl, ceid = ("hi", "IN:hi") if lang == "hi" else ("en-IN", "IN:en")
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}+when:3d&hl={hl}&gl=IN&ceid={ceid}"
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    f = feedparser.parse(r.content)
    return [{"title": e.get("title"), "link": e.get("link"),
             "source": (e.get("source") or {}).get("title"), "source_url": (e.get("source") or {}).get("href")}
            for e in f.entries]


def _bing(query: str) -> list[dict]:
    import feedparser
    url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss&cc=IN&setlang=en-IN"
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    f = feedparser.parse(r.content)
    out = []
    for e in f.entries:
        link = e.get("link") or ""
        real = parse_qs(urlsplit(link).query).get("url", [link])[0]
        out.append({"title": e.get("title"), "url": real, "source": e.get("news_source") or e.get("source")})
    return out


def probe_search(store: Store) -> tuple[dict, list[str]]:
    """Exercise the production search code (discover.py) on real stories, without storing anything."""
    from ..discover import bing, gnews, query_from, resolve
    from ..ownership import canonical_outlet, owner_of
    stories = [dict(r._mapping) for r in _q(store, sql(
        "select s.id, s.signature, count(distinct a.outlet) o from stories s join articles a on a.story_id = s.id "
        "where a.published_at > now() - interval '36 hours' and a.extracted_at is not null "
        "group by s.id, s.signature having count(distinct a.outlet) between 1 and 5 order by random() limit 6"))]
    report = {"queries": []}
    decoded_ok = decoded_n = 0
    to_read: list[str] = []
    for s in stories:
        q = query_from(s["signature"] or "")
        entry = {"story": s["id"], "signature": (s["signature"] or "")[:140], "query": q, "have_outlets": s["o"]}
        try:
            g = gnews(q)
            entry["gnews_n"] = len(g)
            entry["gnews_outlets"] = sorted({canonical_outlet(x["outlet"], x["site"]) for x in g})[:20]
            entry["gnews_owners_new"] = len({owner_of(x["outlet"], x["site"]) for x in g})
            sample = []
            for x in g[:4]:
                decoded_n += 1
                u = resolve(x)
                if u:
                    decoded_ok += 1
                sample.append({"title": x["title"][:90], "outlet": x["outlet"], "url": u})
                time.sleep(1)
            entry["gnews_sample"] = sample
            to_read += [x["url"] for x in sample if x["url"]][:1]
        except Exception as e:  # noqa: BLE001
            entry["gnews_error"] = repr(e)[:300]
        try:
            b = bing(q)
            entry["bing"] = [{"title": x["title"][:80], "url": resolve(x)} for x in b[:5]]
        except Exception as e:  # noqa: BLE001
            entry["bing_error"] = repr(e)[:300]
        report["queries"].append(entry)
        time.sleep(2)
    report["gnews_decode"] = {"ok": decoded_ok, "tried": decoded_n}
    return report, to_read[:6]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--parts", default="search,fetch,grouping")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(database_url())
    # the diagnostics table is created by a migration (the app role cannot create tables)
    from ..config import gemini_api_keys
    router = Router(load_yaml("models.yaml")["tiers"], [GeminiBackend(k, timeout_s=60) for k in gemini_api_keys()], store)
    router.resolve()
    parts = a.parts.split(",")
    extra: list[str] = []
    def save(kind, rep):
        with store.engine.begin() as c:
            c.execute(sql("insert into diagnostics (kind, report) values (:k, :r)"),
                      {"k": kind, "r": json.dumps(rep, default=str)})
        log.info("%s: %s", kind, json.dumps(rep, default=str)[:3000])
    for part in parts:
        t0 = time.time()
        try:
            if part == "grouping":
                rep = probe_grouping(store, router)
            elif part == "search":
                rep, extra = probe_search(store)
            elif part == "fetch":
                rep = probe_fetch(store, extra)
            else:
                continue
        except Exception as e:  # noqa: BLE001
            log.exception("probe %s failed", part)
            rep = {"error": repr(e)[:1000]}
        rep["seconds"] = round(time.time() - t0)
        save(part, rep)


if __name__ == "__main__":
    main()
