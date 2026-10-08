"""Published articles move to the repository's `archive` branch once they are `archive_after_days` old,
then leave the database (owner, Oct 5 2026: nobody reads an article older than three days; the free
database stays small, and the article is kept forever).

Layout of the branch:
  pages/<story_id>.json.gz    the article exactly as published, English and Hindi, never changed
  index/<YYYY-MM>.jsonl       one line per article: id, date, headline, thread, opening sentences
                              (follow-ups look for their parent here once it has left the database)

    python -m nishpaksh.pagearchive export --dir arch    # write due pages into a checkout of the branch
    python -m nishpaksh.pagearchive delete --dir arch    # after a successful push: delete them from the db

A story leaves the database only after its file is committed on the branch and pushed (the workflow
runs `delete` only when the push succeeded), so a failed push loses nothing: it is retried next hour.
The site and follow-ups read archived pages from NISHPAKSH_ARCHIVE_URL (default: the raw branch on
GitHub; empty: no archive, as in tests; a local directory works too).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import logging
import os
import pathlib
import subprocess

from .config import SETTINGS, database_url
from .db import Store, published, select, utcnow

log = logging.getLogger(__name__)
# the repository the workflow runs in (GITHUB_REPOSITORY), so a moved or renamed repository needs no change
DEFAULT_URL = f"https://raw.githubusercontent.com/{os.environ.get('GITHUB_REPOSITORY') or 'nishpaksh/nishpaksh.github.io'}/archive"
PENDING = ".pending.json"


def base_url() -> str:
    return os.environ.get("NISHPAKSH_ARCHIVE_URL", DEFAULT_URL).rstrip("/")


def _read(rel: str) -> bytes | None:
    base = base_url()
    if not base:
        return None
    if not base.startswith(("http://", "https://")):
        p = pathlib.Path(base) / rel
        return p.read_bytes() if p.exists() else None
    import requests
    try:
        r = requests.get(f"{base}/{rel}", timeout=15)
    except requests.RequestException as e:
        log.warning("archive: %s not fetched: %s", rel, e)
        return None
    return r.content if r.status_code == 200 else None


_page_cache: dict[int, dict | None] = {}
_index_cache: dict[str, list[dict]] = {}


def fetch_page(story_id: int) -> dict | None:
    if story_id not in _page_cache:
        raw = _read(f"pages/{int(story_id)}.json.gz")
        try:
            _page_cache[story_id] = json.loads(gzip.decompress(raw)) if raw else None
        except (OSError, ValueError):
            _page_cache[story_id] = None
    return _page_cache[story_id]


def recent_index(days: int = 60, now: dt.datetime | None = None) -> list[dict]:
    """Index lines of archived articles from the last `days` (the months they fall in)."""
    now = now or utcnow()
    months = sorted({(now - dt.timedelta(days=d)).strftime("%Y-%m") for d in range(0, days + 1, 15)} | {now.strftime("%Y-%m")})
    out: list[dict] = []
    for m in months:
        if m not in _index_cache:
            raw = _read(f"index/{m}.jsonl")
            lines = raw.decode("utf-8").splitlines() if raw else []
            _index_cache[m] = [json.loads(x) for x in lines if x.strip()]
        out += _index_cache[m]
    since = (now - dt.timedelta(days=days)).isoformat()
    return [r for r in out if str(r.get("written_at") or "") >= since]


def _written_at(row: dict) -> str:
    return (row["payload_en"] or {}).get("written_at") or row["updated_at"].isoformat(timespec="seconds")


def _summary(p: dict) -> str:
    paras = (p.get("narrative") or {}).get("paragraphs") or []
    first = " ".join(x["text"] for x in paras[0][:2]) if paras and paras[0] else ""
    return first[:400]


def export_due(store: Store, root: str | pathlib.Path, now: dt.datetime | None = None) -> list[int]:
    """Write every published article older than `archive_after_days` into the branch checkout at
    `root`; returns their ids (also left in root/.pending.json for `delete`)."""
    now = now or utcnow()
    root = pathlib.Path(root)
    cutoff = now - dt.timedelta(days=SETTINGS.archive_after_days)
    rows = store.rows(select(published).where(published.c.updated_at < cutoff).order_by(published.c.story_id))
    done: list[int] = []
    lines: dict[str, list[str]] = {}
    for r in rows:
        written = _written_at(r)
        page = {"story_id": r["story_id"], "written_at": written, "headline_en": r["headline_en"],
                "headline_hi": r["headline_hi"], "payload_en": r["payload_en"], "payload_hi": r["payload_hi"]}
        path = root / "pages" / f"{r['story_id']}.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        idx = root / "index" / f"{written[:7]}.jsonl"
        listed = _listed(idx)
        if r["story_id"] not in listed:   # an archived article is never rewritten
            path.write_bytes(gzip.compress(json.dumps(page, ensure_ascii=False, default=str).encode("utf-8"), mtime=0))
            pe = r["payload_en"] or {}
            lines.setdefault(written[:7], []).append(json.dumps(
                {"story_id": r["story_id"], "written_at": written, "headline_en": r["headline_en"],
                 "headline_hi": r["headline_hi"], "thread": pe.get("thread") or r["story_id"],
                 "summary": _summary(pe)}, ensure_ascii=False))
        done.append(r["story_id"])
    for month, ls in lines.items():
        idx = root / "index" / f"{month}.jsonl"
        idx.parent.mkdir(parents=True, exist_ok=True)
        with idx.open("a", encoding="utf-8") as f:
            f.write("\n".join(ls) + "\n")
    (root / PENDING).write_text(json.dumps(done))
    log.info("archive: %d articles written to %s", len(done), root)
    return done


_listed_cache: dict[pathlib.Path, set[int]] = {}


def _listed(idx: pathlib.Path) -> set[int]:
    """Story ids already in a month's index (the index is checked out; the pages are not)."""
    if idx not in _listed_cache:
        lines = idx.read_text(encoding="utf-8").splitlines() if idx.exists() else []
        _listed_cache[idx] = {json.loads(x)["story_id"] for x in lines if x.strip()}
    return _listed_cache[idx]


def committed(root: str | pathlib.Path, story_id: int) -> bool:
    """The page is in the branch's last commit (not merely written to disk)."""
    r = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"HEAD:pages/{int(story_id)}.json.gz"],
                       capture_output=True)
    return r.returncode == 0


def delete_archived(store: Store, root: str | pathlib.Path) -> int:
    """Delete from the database the stories `export_due` wrote, once their pages are committed."""
    from .retention import delete_stories
    root = pathlib.Path(root)
    p = root / PENDING
    ids = json.loads(p.read_text()) if p.exists() else []
    safe = [sid for sid in ids if committed(root, sid)]
    if len(safe) < len(ids):
        log.warning("archive: %d pages not committed; kept in the database", len(ids) - len(safe))
    delete_stories(store, safe, keep_links=True)
    log.info("archive: %d archived stories deleted from the database", len(safe))
    return len(safe)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["export", "delete"])
    p.add_argument("--dir", required=True, help="checkout of the archive branch")
    a = p.parse_args()
    store = Store(database_url())
    if a.action == "export":
        print(len(export_due(store, a.dir)))
    else:
        print(delete_archived(store, a.dir))


if __name__ == "__main__":
    main()
