"""Proactive search: look for more coverage of stories we already know about.

Feeds bring what outlets choose to push; this asks the question the other way round: for a
story, who else reported it? It never decides that something belongs to a story. A found
article enters as an ordinary article (found_by = "search") and goes through the same grouping
checks as everything else; if it is about a different event it simply does not join.

Which stories: importance x gap, a fixed number per run.
  importance  how many articles and outlets already cover it, how recent it is
  gap         how far it is from publishable (3 independent outlets); a story that already
              qualifies gains little, one outlet short gains most
About 80% of searches go to stories that are not published (breadth), 20% to big published
stories that have gone quiet for a few hours (depth: follow-ups).

How to search without biasing the result: the query is the story's neutral one-line summary
(names, places, the action), never a slanted phrase; diversity comes from WHO we look at, not
from how the question is worded. Searches run in English and, when the story has Hindi
coverage, in Hindi.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
import time
from urllib.parse import parse_qs, quote_plus, urlsplit

import feedparser
import requests

from .config import SETTINGS
from .db import Store, articles, insert, published, select, stories, update, utcnow
from .ingest import HEADERS, canonical_url, detect_agency, fetch_article
from .ownership import canonical_outlet, owner_of
from .wire import independence_groups, minhash

log = logging.getLogger(__name__)
STOP = set("""a an the of in on at to for from by with and or but as is are was were be been has have had
this that these those it its into over after before about after against amid says said will would can
could may might also than then there their they he she his her them who whom which what when where why
how not no yes new news report reports india indian""".split())


def query_from(signature: str, max_words: int = 10) -> str:
    """The neutral one-liner reduced to its content words: names, places, actions."""
    words = re.findall(r"[\wऀ-ॿ'’.-]+", signature or "")
    keep = [w.strip(".'’") for w in words if w.lower().strip(".'’") not in STOP and len(w.strip(".'’")) > 1]
    return " ".join(keep[:max_words])


def gnews(query: str, lang: str = "en", days: int = 3, session=None) -> list[dict]:
    hl, ceid = ("hi", "IN:hi") if lang == "hi" else ("en-IN", "IN:en")
    url = (f"https://news.google.com/rss/search?q={quote_plus(query)}+when:{days}d"
           f"&hl={hl}&gl=IN&ceid={ceid}")
    r = (session or requests).get(url, timeout=20, headers={"User-Agent": HEADERS["User-Agent"]})
    r.raise_for_status()
    out = []
    for e in feedparser.parse(r.content).entries:
        src = e.get("source") or {}
        title = re.sub(r"\s+-\s+[^-]+$", "", e.get("title") or "")  # Google appends " - Outlet"
        pub = None
        if e.get("published_parsed"):
            pub = dt.datetime(*e.published_parsed[:6])
        out.append({"title": title, "link": e.get("link"), "outlet": src.get("title"),
                    "site": src.get("href"), "published_at": pub, "lang": lang, "engine": "gnews"})
    return out


def bing(query: str, session=None) -> list[dict]:
    url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss&cc=IN&setlang=en-IN"
    r = (session or requests).get(url, timeout=20, headers={"User-Agent": HEADERS["User-Agent"]})
    r.raise_for_status()
    out = []
    for e in feedparser.parse(r.content).entries:
        link = e.get("link") or ""
        real = parse_qs(urlsplit(link).query).get("url", [link])[0]
        pub = dt.datetime(*e.published_parsed[:6]) if e.get("published_parsed") else None
        out.append({"title": e.get("title") or "", "link": real, "outlet": e.get("news_source") or None,
                    "site": None, "published_at": pub, "lang": "en", "engine": "bing", "resolved": True})
    return out


def _gnews_decode(link: str, session=None) -> str | None:
    """Google News RSS links are encoded redirects. Decoding takes two requests: the article page
    gives a signature and timestamp, and Google's batchexecute endpoint returns the publisher URL."""
    import json as _json
    http = session or requests
    m = re.search(r"/(?:rss/)?articles/([^?/]+)", link)
    if not m:
        return None
    gid = m.group(1)
    page = http.get(f"https://news.google.com/rss/articles/{gid}", timeout=15,
                    headers={"User-Agent": HEADERS["User-Agent"]})
    sig = re.search(r'data-n-a-sg="([^"]+)"', page.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
    if not (sig and ts):
        return None
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{gid}",{ts.group(1)},"{sig.group(1)}"]')
    body = {"f.req": _json.dumps([[["Fbv4je", inner, None, "generic"]]])}
    r = http.post("https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body, timeout=15,
                  headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                           "User-Agent": HEADERS["User-Agent"]})
    try:
        payload = _json.loads(r.text.split("\n\n", 1)[1])[:-2]
        url = _json.loads(payload[0][2])[1]
    except Exception:  # noqa: BLE001
        return None
    return url if isinstance(url, str) and url.startswith("http") else None


SYNDICATORS = ("msn.com", "news.google.com", "yahoo.com", "dailyhunt.in", "newsbreak", "flipboard.com",
               "inshorts.com", "smartnews")


def resolve(item: dict, session=None) -> str | None:
    """Publisher URL for a search result; None for aggregators that only repost other outlets."""
    link = item.get("link") or ""
    url = link if item.get("resolved") or "news.google.com" not in link else None
    if url is None:
        try:
            url = _gnews_decode(link, session)
        except Exception as e:  # noqa: BLE001
            log.debug("decode failed: %s", e)
            return None
    if not url or any(s in urlsplit(url).netloc for s in SYNDICATORS):
        return None
    return url


def pick_stories(store: Store, n: int) -> list[dict]:
    since = utcnow() - dt.timedelta(hours=48)
    arts = store.rows(select(articles.c.id, articles.c.story_id, articles.c.outlet, articles.c.url, articles.c.agency,
                             articles.c.wire_group, articles.c.lang, articles.c.published_at, articles.c.role,
                             articles.c.title)
                      .where(articles.c.story_id.is_not(None), articles.c.published_at >= since))
    by_story: dict[int, list[dict]] = {}
    for a in arts:
        by_story.setdefault(a["story_id"], []).append(a)
    st = {s["id"]: s for s in store.rows(select(stories.c.id, stories.c.signature, stories.c.last_searched_at,
                                                stories.c.qualifies, stories.c.updated_at)
                                         .where(stories.c.id.in_(list(by_story) or [-1])))}
    live = {r["story_id"] for r in store.rows(select(published.c.story_id))}
    now = utcnow()
    breadth, depth = [], []
    for sid, members in by_story.items():
        s = st.get(sid)
        if not s or not s["signature"]:
            continue
        if s["last_searched_at"] and now - s["last_searched_at"] < dt.timedelta(hours=SETTINGS.search_every_hours):
            continue
        groups = len(set(independence_groups(members).values()))
        newest = max(a["published_at"] for a in members)
        age_h = (now - newest).total_seconds() / 3600
        official = any(a["role"] == "official" for a in members)
        importance = math.log1p(len(members)) + 0.6 * groups + (1.0 if official else 0) + max(0, 2 - age_h / 12)
        if sid in live:
            if groups >= 4 and age_h >= 3:  # a big story that has gone quiet: look for follow-ups
                depth.append((importance, s, members))
            continue
        if groups >= 6:
            continue  # plenty of coverage already; search cannot add much
        if groups == 1 and len(members) < 3 and not official:
            continue  # one outlet, one or two pieces: usually too minor to chase
        gap_bonus = {1: 0.5, 2: 1.5, 3: 1.0, 4: 0.6, 5: 0.3}.get(groups, 0)
        breadth.append((importance + gap_bonus, s, members))
    breadth.sort(key=lambda t: -t[0])
    depth.sort(key=lambda t: -t[0])
    n_depth = max(1, round(n * SETTINGS.search_depth_share)) if depth else 0
    chosen = [(s, m, "depth") for _, s, m in depth[:n_depth]] + [(s, m, "breadth") for _, s, m in breadth[: n - min(n_depth, len(depth))]]
    return [{"story": s, "members": m, "kind": k} for s, m, k in chosen]


def discover(store: Store, tavily=None, n_stories: int | None = None, until: float | None = None,
             engines=(gnews, bing)) -> dict:
    n_stories = SETTINGS.search_stories_per_run if n_stories is None else n_stories
    targets = pick_stories(store, n_stories)
    stats = {"stories": len(targets), "results": 0, "new_articles": 0, "unreadable": 0, "tavily_searches": 0}
    if not targets:
        return stats
    known_urls = set()
    for t in targets:
        known_urls |= {a["url"] for a in t["members"]}
    for t in targets:
        if until and time.time() > until:
            break
        s, members = t["story"], t["members"]
        have_owners = {owner_of(a["outlet"], a["url"]) for a in members}
        q = query_from(s["signature"])
        results: list[dict] = []
        for engine in engines:
            try:
                results += engine(q)
            except Exception as e:  # noqa: BLE001
                log.info("search %s failed: %s", getattr(engine, "__name__", engine), str(e)[:150])
        hindi = [a for a in members if a["lang"] == "hi"]
        if hindi and gnews in engines:
            try:
                results += gnews(query_from(hindi[0]["title"]), "hi")
            except Exception as e:  # noqa: BLE001
                log.info("hindi search failed: %s", str(e)[:150])
        if not results and tavily is not None and tavily.enabled and stats["tavily_searches"] < SETTINGS.tavily_searches_per_run:
            stats["tavily_searches"] += 1
            for r in tavily.search(q, days=3):
                results.append({"title": r["title"], "link": r["url"], "outlet": None, "site": None,
                                "published_at": None, "lang": "en", "engine": "tavily", "resolved": True})
        stats["results"] += len(results)
        # one new piece per owner we do not have yet: that is what adds an independent source
        fresh, seen_owner = [], set()
        for r in results:
            owner = owner_of(r.get("outlet"), r.get("site") or (r["link"] if r.get("resolved") else None))
            if owner in have_owners or owner in seen_owner:
                continue
            if r.get("published_at") and utcnow() - r["published_at"] > dt.timedelta(hours=SETTINGS.max_article_age_hours):
                continue
            seen_owner.add(owner)
            fresh.append(r)
        added = 0
        for r in fresh[: SETTINGS.search_new_per_story]:
            url = resolve(r)
            if not url:
                continue
            url = canonical_url(url)
            if url in known_urls or store.one(select(articles.c.id).where(articles.c.url == url)):
                continue
            known_urls.add(url)
            outlet = canonical_outlet(r.get("outlet"), url)
            page = fetch_article(url)
            text, source = (page or {}).get("text") or "", "full"
            if len(text) < SETTINGS.min_full_text_chars:
                text, source = r["title"], "summary"   # Tavily may read it later; until then coverage only
                stats["unreadable"] += 1
            author = (page or {}).get("author")
            store.exec(insert(articles).values(
                url=url, feed_id=None, outlet=outlet[:200], lang=r.get("lang") or "en", role="news",
                title=r["title"] or (page or {}).get("title") or "", author=(author or None) and author[:300],
                published_at=r.get("published_at") or utcnow(), fetched_at=utcnow(), text=text,
                text_source=source, agency=detect_agency(author, text), minhash=minhash(text),
                extract_failures=0, found_by="search"))
            added += 1
        stats["new_articles"] += added
        store.exec(update(stories).where(stories.c.id == s["id"]).values(last_searched_at=utcnow()))
        log.info("search [%s] story %s %r: %d results, %d new outlets", t["kind"], s["id"], q, len(results), added)
    return stats
