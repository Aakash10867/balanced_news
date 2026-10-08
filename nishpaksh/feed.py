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
import json
import logging
import pathlib
import shutil

from .config import database_url
from .db import Store, published, select, utcnow

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
             "perspectives", "framing", "suicide", "thread", "parents", "children", "written_at")


def slim_payload(p: dict | None) -> dict | None:
    """The fields of a published payload the site reads (the rest stays in the database)."""
    if not p:
        return p
    out = {k: p.get(k) for k in PAGE_KEYS if k in p}
    nar = p.get("narrative") or {}
    out["narrative"] = {"paragraphs": nar.get("paragraphs") or [], "section_keys": nar.get("section_keys") or [],
                        "sources": nar.get("sources") or []}
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
            "n": (pe.get("counts") or {}).get("independent_sources") or 0}


def export(store: Store, root: str | pathlib.Path) -> int:
    """Write the home-page files and one file per live article into `root` (emptied first)."""
    root = pathlib.Path(root)
    if root.exists():
        for child in root.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    root.mkdir(parents=True, exist_ok=True)
    rows = store.rows(select(published).order_by(published.c.updated_at.desc()))
    now = utcnow().isoformat(timespec="seconds")
    for lang in ("en", "hi"):
        cards = [c for c in (card(r, lang) for r in rows) if c]
        (root / f"{lang}.json").write_text(json.dumps({"generated_at": now, "stories": cards},
                                                      ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (root / "story").mkdir(exist_ok=True)
    n = 0
    for r in rows:
        if not ((r.get("payload_en") or {}).get("narrative") or {}).get("paragraphs"):
            continue
        page = {"story_id": r["story_id"], "updated_at": r["updated_at"].isoformat(timespec="seconds"),
                "headline_en": r["headline_en"], "headline_hi": r["headline_hi"],
                "payload_en": slim_payload(r["payload_en"]), "payload_hi": slim_payload(r["payload_hi"])}
        (root / "story" / f"{r['story_id']}.json").write_text(
            json.dumps(page, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
        n += 1
    (root / "README.md").write_text(
        "# Nishpaksh feed\n\nThe live articles as ready-made files for the reading site, rewritten every hour "
        "(one commit, force-pushed). See nishpaksh/feed.py on main.\n", encoding="utf-8")
    log.info("feed: %d articles written to %s", n, root)
    return n


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    print(export(Store(database_url()), a.dir))


if __name__ == "__main__":
    main()
