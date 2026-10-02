"""Stage 1: read RSS feeds and article pages. Plain HTTP, no AI, no quota."""
from __future__ import annotations

import calendar
import datetime as dt
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
import trafilatura

from .config import SETTINGS, load_yaml
from .db import Store, articles, feeds, insert, select, update, utcnow
from .wire import minhash

log = logging.getLogger(__name__)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"}
# Some sites block browser-looking requests from cloud servers but allow declared bots,
# others the reverse. Try one, then the other.
BOT_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NishpakshBot/0.2; +https://github.com/Aakash10867/balanced_news)",
               "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8"}


def _get(url: str, timeout: int) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    if r.status_code in (401, 403, 429) or (r.ok and r.text.lstrip()[:15].lower().startswith(("<!doctype html", "<html"))
                                            and url.endswith((".xml", "/feed/", "/feed", "/rss", ".rss", ".cms"))):
        r2 = requests.get(url, headers=BOT_HEADERS, timeout=timeout)
        if r2.ok:
            return r2
    return r
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_|cmp$|ito$)")

AGENCY_PATTERNS = [
    ("PTI", r"\(\s*PTI\s*\)|\bPTI\b\s*$|^PTI\b"),
    ("ANI", r"\(\s*ANI\s*\)|^ANI\b"),
    ("IANS", r"\(\s*IANS\s*\)|^IANS\b"),
    ("UNI", r"\(\s*UNI\s*\)"),
    ("Reuters", r"\(\s*Reuters\s*\)"),
    ("AFP", r"\(\s*AFP\s*\)"),
    ("Bhasha", r"\(\s*भाषा\s*\)|^भाषा\b"),
    ("ANI", r"\(\s*एएनआई\s*\)"),
    ("IANS", r"\(\s*आईएएनएस\s*\)"),
    ("Agency", r"\(\s*एजेंसी\s*\)"),
]


def canonical_url(u: str) -> str:
    p = urlsplit(u.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k.lower())]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, urlencode(q), ""))


def detect_agency(author: str | None, text: str | None) -> str | None:
    probes = [(author or "").strip()]
    if text:
        probes += [text[:300], text[-200:]]
    for name, pat in AGENCY_PATTERNS:
        for p in probes:
            if p and re.search(pat, p, flags=re.MULTILINE):
                return name
    if author and re.fullmatch(r"(?i)\s*(pti|ani|ians|uni|reuters|afp|agencies|agency)\s*", author):
        return author.strip().upper()
    return None


def strip_html(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def entry_time(e) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return dt.datetime.fromtimestamp(calendar.timegm(t), dt.timezone.utc).replace(tzinfo=None)
    return None


def sync_feeds(store: Store) -> None:
    """Make the feeds table match config/feeds.yaml: add new feeds, re-enable edited ones,
    and disable feeds that were removed from the file."""
    cfg = load_yaml("feeds.yaml")["feeds"]
    wanted = {f["url"]: f for f in cfg}
    existing = {r["url"]: r for r in store.rows(select(feeds))}
    for url, f in wanted.items():
        if url not in existing:
            store.exec(insert(feeds).values(name=f["name"], url=url, lang=f.get("lang", "en"),
                                            role=f.get("role", "news"), fail_count=0, disabled=False))
    for url, r in existing.items():
        if url not in wanted and not r["disabled"]:
            store.exec(update(feeds).where(feeds.c.id == r["id"]).values(disabled=True))


def fetch_feed(feed: dict) -> list[dict]:
    r = _get(feed["url"], timeout=20)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.bozo_exception}")
    out = []
    for e in parsed.entries:
        link = e.get("link")
        if not link:
            continue
        out.append({
            "url": canonical_url(link),
            "title": strip_html(e.get("title", "")),
            "summary": strip_html(e.get("summary", "")),
            "author": e.get("author"),
            "published_at": entry_time(e),
        })
    return out


def fetch_article(url: str) -> dict | None:
    try:
        r = _get(url, timeout=25)
        if r.status_code != 200 or not r.text:
            return None
        doc = trafilatura.bare_extraction(r.text, url=url, with_metadata=True, include_comments=False)
    except Exception as e:  # noqa: BLE001
        log.debug("fetch failed %s: %s", url, e)
        return None
    if doc is None:
        return None
    d = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
    return {"text": d.get("text") or "", "author": d.get("author"), "title": d.get("title")}


def ingest(store: Store) -> int:
    now = utcnow()
    oldest = now - dt.timedelta(hours=SETTINGS.max_article_age_hours)
    candidates: list[tuple[dict, dict]] = []
    seen: set[str] = set()
    diag: dict[str, dict] = {}

    for feed in store.rows(select(feeds).where(feeds.c.disabled.is_(False))):
        try:
            entries = fetch_feed(feed)
            store.exec(update(feeds).where(feeds.c.id == feed["id"]).values(fail_count=0, last_ok=now))
        except Exception as e:  # noqa: BLE001
            fails = (feed["fail_count"] or 0) + 1
            disabled = fails >= SETTINGS.feed_disable_after_failures
            store.exec(update(feeds).where(feeds.c.id == feed["id"]).values(fail_count=fails, disabled=disabled))
            log.warning("feed %s failed (%d)%s: %s", feed["name"], fails, " -> DISABLED" if disabled else "", e)
            continue
        diag[feed["name"]] = {"entries": len(entries), "fresh": 0, "added": 0, "too_short": 0}
        for en in entries:
            pub = en["published_at"] or now
            if pub < oldest or en["url"] in seen:
                continue
            seen.add(en["url"])
            candidates.append((feed, en))
            diag[feed["name"]]["fresh"] += 1

    if not candidates:
        log.info("ingest: no fresh links; per feed %s", diag)
        return 0
    known = set()
    urls = [en["url"] for _, en in candidates]
    for i in range(0, len(urls), 500):
        known |= {r["url"] for r in store.rows(select(articles.c.url).where(articles.c.url.in_(urls[i:i + 500])))}
    todo = [(f, en) for f, en in candidates if en["url"] not in known]
    todo.sort(key=lambda fe: fe[1]["published_at"] or now, reverse=True)
    per_feed: dict[int, int] = {}
    fair = []
    for f, en in todo:
        if per_feed.get(f["id"], 0) < SETTINGS.max_new_per_feed:
            per_feed[f["id"]] = per_feed.get(f["id"], 0) + 1
            fair.append((f, en))
    todo = fair[: SETTINGS.max_new_articles_per_run]
    log.info("ingest: %d new links across %d feeds", len(todo), len({f['id'] for f, _ in todo}))

    with ThreadPoolExecutor(max_workers=8) as pool:
        pages = list(pool.map(lambda fe: fetch_article(fe[1]["url"]), todo))

    added = 0
    for (feed, en), page in zip(todo, pages):
        text, source = (page or {}).get("text") or "", "full"
        if len(text) < SETTINGS.min_full_text_chars:
            # page blocked or unreadable: fall back to what the feed itself says
            text, source = ". ".join(x for x in (en["title"], en["summary"]) if x), "summary"
        if len(text) < 60:
            diag[feed["name"]]["too_short"] += 1
            continue
        author = (page or {}).get("author") or en["author"]
        values = dict(
            url=en["url"], feed_id=feed["id"], outlet=feed["name"], lang=feed["lang"], role=feed["role"],
            title=en["title"] or (page or {}).get("title") or "", author=(author or None) and author[:300],
            published_at=en["published_at"] or now, fetched_at=now, text=text, text_source=source,
            agency=detect_agency(author, text), minhash=minhash(text), extract_failures=0,
        )
        try:
            store.exec(insert(articles).values(**values))
            added += 1
            diag[feed["name"]]["added"] += 1
        except Exception as e:  # unique race etc.
            log.debug("insert skipped %s: %s", en["url"], e)
    quiet = {k: v for k, v in diag.items() if v["added"] == 0}
    if quiet:
        log.info("feeds that added nothing this run: %s", quiet)
    return added
