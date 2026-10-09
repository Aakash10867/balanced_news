"""The reading site's ready-made files (nishpaksh_version_1.0, owner Oct 8 2026).

The site used to ask Supabase (us-east-1) for every page view, so a reader in India waited for a
round trip to Virginia before anything appeared. After every desk run and pipeline run the live
articles are written as small static files to the repository's `feed` branch, served from GitHub's
CDN (raw.githubusercontent.com). The branch is one commit, force-pushed each time: it never grows.

Layout of the branch:
  en.json, hi.json     the home page: one card per live article (headline, the opening sentences
                       with their colours, the colour bar's counts, publication time, thread)
  story/<id>.json      one article, English and Hindi, as the site renders it (the fields it reads)

    python -m nishpaksh.feed --dir feed      # write the files into a directory

The site falls back to Supabase when a file is missing or older than the page it wants, and to the
archive branch for articles older than three days, so a failed push only costs speed.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import logging
import pathlib

from .config import database_url
from sqlalchemy import Text, cast, func

from .categories import labels, normalize
from .db import Store, articles, claims, published, select, utcnow

log = logging.getLogger(__name__)

CARD_SENTENCES = 9          # the card fades out after about this much text
BAR = {"established": "e", "corroborated": "e", "confirmed": "e", "single": "o", "disputed": "d",
       "false": "r", "unverified": "u", "pending": "u", "developing": "u"}


def _sentence_classes(paragraphs: list) -> list[str]:
    """One class per coloured piece of text: a sentence, or each part of a sentence in parts."""
    out: list[str] = []
    for para in paragraphs or []:
        for s in para or []:
            parts = s.get("parts") or []
            if len(parts) > 1:
                out += [p.get("class") or "unverified" for p in parts]
            else:
                out.append(s.get("class") or "unverified")
    return out


def bar_counts(paragraphs: list) -> dict[str, int]:
    """How many pieces of the article are green / purple / amber / red / grey (the card's bar)."""
    c = {"e": 0, "o": 0, "d": 0, "r": 0, "u": 0}
    for k in _sentence_classes(paragraphs):
        c[BAR.get(k, "u")] += 1
    return c


def _card_text(paragraphs: list, limit: int = CARD_SENTENCES) -> list[list[dict]]:
    """The opening of the article for the card: whole paragraphs until `limit` sentences."""
    out: list[list[dict]] = []
    n = 0
    for para in paragraphs or []:
        if n >= limit:
            break
        keep = []
        for s in para or []:
            if n >= limit:
                break
            item = {"t": s.get("text") or "", "c": s.get("class") or "unverified"}
            parts = s.get("parts") or []
            if len(parts) > 1:
                item["p"] = [{"t": p.get("text") or "", "c": p.get("class") or "unverified"} for p in parts]
            keep.append(item)
            n += 1
        if keep:
            out.append(keep)
    return out


def _item(i: dict) -> dict:
    """A statement for the "who reported it" list: only what the page shows."""
    return {"text": i.get("text"), "verdict": i.get("verdict"), "n_sources": i.get("n_sources"),
            "sources": [{"outlet": s.get("outlet"), "url": s.get("url"), "stance": s.get("stance")}
                        for s in i.get("sources") or []]}


PAGE_KEYS = ("story_id", "headline", "counts", "has_established", "qualified_by", "perspective_mode",
             "perspectives", "framing", "suicide", "thread", "parents", "children", "written_at", "category")


def slim_payload(p: dict | None, who: list | None = None) -> dict | None:
    """The fields of a published payload the site reads (the rest stays in the database). `who`: the speaker of each
    "What they say" paragraph, for its label on the site (display only; the article itself is unchanged)."""
    if not p:
        return p
    out = {k: p.get(k) for k in PAGE_KEYS if k in p}
    nar = p.get("narrative") or {}
    out["narrative"] = {"paragraphs": nar.get("paragraphs") or [], "section_keys": nar.get("section_keys") or [],
                        "sources": nar.get("sources") or []}
    if who and any(who):
        out["narrative"]["who"] = who
    out["timeline"] = [[_item(i) for i in day] for day in p.get("timeline") or []]
    for k in ("undated", "established", "contested", "context"):
        out[k] = [_item(i) for i in p.get(k) or []]
    return out


def card(row: dict, lang: str) -> dict | None:
    pe = row.get("payload_en") or {}
    p = (row.get("payload_hi") if lang == "hi" else None) or pe
    paras = (p.get("narrative") or {}).get("paragraphs") or []
    if not ((pe.get("narrative") or {}).get("paragraphs")):
        return None                       # a page still waiting for its first essay is not shown
    head = (row.get("headline_hi") if lang == "hi" else None) or row.get("headline_en") or p.get("headline")
    return {"id": row["story_id"], "at": row["updated_at"].isoformat(timespec="seconds"),
            "thread": pe.get("thread") or row["story_id"], "ongoing": bool(pe.get("parents")),
            "h": head, "paras": _card_text(paras),
            "bar": bar_counts((pe.get("narrative") or {}).get("paragraphs")),
            "n": (pe.get("counts") or {}).get("independent_sources") or 0,
            "cat": normalize(pe.get("category"))}   # the site's sections, keys (labels in en.json / hi.json)


MANIFEST = "manifest.json"
FEED_VERSION = 2          # part of every article's stamp: raising it rewrites every article file once
NOT_SPEAKERS = {"", "article", "-", "none", "unknown", "reporter", "the article"}


def _speakers(store: Store, payload: dict | None) -> list | None:
    """Who speaks in each "What they say" paragraph: the name the reports give most often for its statements
    (claims.attributed_to, the outlets' own words), the shortest on a tie; None where nobody is named."""
    nar = (payload or {}).get("narrative") or {}
    paras, keys = nar.get("paragraphs") or [], nar.get("section_keys") or []
    want = {k: [i for s in para for i in (s.get("ids") or [])] for k, para in enumerate(paras)
            if k < len(keys) and keys[k] == "say"}
    ids = sorted({i for v in want.values() for i in v})
    if not ids:
        return None
    by: dict[int, list[str]] = collections.defaultdict(list)
    for r in store.rows(select(claims.c.canonical_id, claims.c.attributed_to).where(claims.c.canonical_id.in_(ids))):
        name = (r["attributed_to"] or "").strip()
        if name.lower() not in NOT_SPEAKERS:
            by[r["canonical_id"]].append(name)
    out: list = [None] * len(paras)
    for k, pids in want.items():
        c = collections.Counter(n for i in pids for n in by.get(i, []))
        if c:
            out[k] = sorted(c.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]
    return out


def _page(r: dict, who: list | None = None) -> dict:
    return {"story_id": r["story_id"], "updated_at": r["updated_at"].isoformat(timespec="seconds"),
            "headline_en": r["headline_en"], "headline_hi": r["headline_hi"],
            "payload_en": slim_payload(r["payload_en"], who), "payload_hi": slim_payload(r["payload_hi"])}


def _lead_urls(payload: dict | None) -> list[str]:
    """The story's reports in the order a picture is taken from: those behind the lead first, then the rest."""
    nar = (payload or {}).get("narrative") or {}
    by_n = {s.get("n"): s.get("url") for s in nar.get("sources") or []}
    paras = nar.get("paragraphs") or []
    first = [n for s in (paras[0] if paras else []) for n in (s.get("sources") or [])]
    order = first + sorted(n for n in by_n if n not in first)
    return [by_n[n] for n in dict.fromkeys(order) if by_n.get(n)]


def pictures(store: Store, manifest: dict) -> dict[str, dict]:
    """One picture per live article (owner, Oct 9 2026): the share picture of the report behind the lead, else
    the next report's; never an outlet's default picture (the same link on 3+ different stories). Linked, never
    copied; the credit is the outlet's."""
    ids = [int(k) for k in manifest]
    if not ids:
        return {}
    since = utcnow() - dt.timedelta(days=6)
    logos = {r["image"] for r in store.rows(
        select(articles.c.image).where(articles.c.image.like("http%"), articles.c.fetched_at >= since)
        .group_by(articles.c.image).having(func.count(func.distinct(articles.c.story_id)) >= 3))}
    have: dict[int, dict[str, dict]] = collections.defaultdict(dict)
    for r in store.rows(select(articles.c.story_id, articles.c.url, articles.c.outlet, articles.c.image)
                        .where(articles.c.story_id.in_(ids), articles.c.image.like("http%"))):
        if r["image"] not in logos:
            have[r["story_id"]][r["url"]] = r
    out: dict[str, dict] = {}
    for sid, entry in manifest.items():
        got = have.get(int(sid)) or {}
        pick = next((got[u] for u in entry.get("lead") or [] if u in got), None)
        if pick is None and got:
            pick = got[sorted(got)[0]]
        if pick:
            out[sid] = {"src": pick["image"], "by": pick["outlet"], "href": pick["url"]}
    return out


def export(store: Store, root: str | pathlib.Path) -> int:
    """Write the home-page files and one file per live article into `root`, a checkout of the feed
    branch. Only articles whose content changed since the last export are read from the database
    (egress, Oct 8 2026): the branch keeps a manifest of each article's md5 and its two cards."""
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        old = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    pg = store.engine.dialect.name == "postgresql"
    meta = [published.c.story_id, published.c.updated_at, published.c.headline_en, published.c.headline_hi]
    if pg:
        meta += [func.md5(cast(published.c.payload_en, Text)).label("m_en"),
                 func.md5(cast(published.c.payload_hi, Text)).label("m_hi")]
    else:
        meta += [published.c.payload_en, published.c.payload_hi]
    rows = store.rows(select(*meta).order_by(published.c.updated_at.desc()))
    if not pg:
        for r in rows:
            r["m_en"] = hashlib.md5(json.dumps(r["payload_en"], sort_keys=True, default=str).encode()).hexdigest()
            r["m_hi"] = hashlib.md5(json.dumps(r["payload_hi"], sort_keys=True, default=str).encode()).hexdigest()
    (root / "story").mkdir(exist_ok=True)
    stamp = lambda r: [r["m_en"], r["m_hi"], r["updated_at"].isoformat(timespec="seconds"), r["headline_en"], r["headline_hi"],
                       FEED_VERSION]
    keep = {str(r["story_id"]) for r in rows
            if (old.get(str(r["story_id"])) or {}).get("m") == stamp(r) and (root / "story" / f"{r['story_id']}.json").exists()}
    todo = [r["story_id"] for r in rows if str(r["story_id"]) not in keep]
    full: dict[int, dict] = {}
    for start in range(0, len(todo), 50):
        for r in store.rows(select(published).where(published.c.story_id.in_(todo[start:start + 50]))):
            full[r["story_id"]] = r
    manifest: dict[str, dict] = {}
    for r in rows:
        sid = str(r["story_id"])
        if sid in keep:
            manifest[sid] = old[sid]
            continue
        f = full.get(r["story_id"])
        if not f:
            continue
        entry = {"m": stamp(r), "en": card(f, "en"), "hi": card(f, "hi"), "lead": _lead_urls(f["payload_en"])}
        if not entry["en"]:
            continue                      # a page still waiting for its first essay is not shown
        (root / "story" / f"{sid}.json").write_text(json.dumps(_page(f, _speakers(store, f["payload_en"])), ensure_ascii=False,
                                                              separators=(",", ":"), default=str), encoding="utf-8")
        manifest[sid] = entry
    for p in (root / "story").glob("*.json"):
        if p.stem not in manifest:
            p.unlink()
    now = utcnow().isoformat(timespec="seconds")
    order = [str(r["story_id"]) for r in rows if str(r["story_id"]) in manifest]
    pics = pictures(store, manifest)      # every export: a picture found later still reaches its card
    for lang in ("en", "hi"):
        cards = [dict(manifest[sid][lang], **({"img": pics[sid]} if sid in pics else {}))
                 for sid in order if manifest[sid].get(lang)]
        (root / f"{lang}.json").write_text(json.dumps({"generated_at": now, "sections": labels(lang), "stories": cards},
                                                      ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (root / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (root / "README.md").write_text(
        "# Nishpaksh feed\n\nThe live articles as ready-made files for the reading site, rewritten every hour "
        "(one commit, force-pushed). See nishpaksh/feed.py on main.\n", encoding="utf-8")
    log.info("feed: %d articles (%d read from the database) written to %s", len(manifest), len(full), root)
    return len(manifest)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    print(export(Store(database_url()), a.dir))


if __name__ == "__main__":
    main()
