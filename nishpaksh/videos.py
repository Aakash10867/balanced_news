"""YouTube videos for an article.

Fetched once, when the article is new (the desk, after publishing), so the site's videos button opens at once and
costs nothing per click.

Design:
  * Two searches per article (Hindi headline, English headline), each fetching 50 results (100 units each) to
    maximize pool depth without burning extra quota.
  * Curated credible lists for Hindi and English (analytical outlets, UPSC/IAS desks, established national newsrooms).
  * Exact language tagging: channels in the Hindi list are tagged 'hi', channels in the English list are tagged 'en'.
    Channels absent from both lists leave 'lang' as None so the frontend displays no language badge.
  * Credible channels are prioritized to the top of the selection queue.
  * At most 2 videos from one channel, keeping diversity across sources.

Needs YOUTUBE_API_KEY (YouTube Data API v3, free: 10,000 units a day; a search costs 100).
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import os
import re

from sqlalchemy import func

from .db import Store, articles, insert, published, quota_usage, select, utcnow, videos

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3/search"
PER_SEARCH = 50
KEEP = 10
PER_CHANNEL = 2
DAY_UNITS = 9000
NEW_HOURS = 36

# Exhaustive curated credible & analytical channels for Hindi
CREDIBLE_HI = {
    # UPSC / Public affairs / Deep-dive analysis
    "drishti ias",
    "drishti ias : english",
    "studyiq ias",
    "dhyeya ias",
    "next ias hindi",
    "unacademy upsc hindi",
    "khan global studies",
    "sanskriti ias",
    "pw onlyias",
    "parcham classes",
    "world affairs by unacademy",
    # Public broadcasters & legislative desks
    "sansad tv",
    "sansad tv hindi",
    "dd news",
    "dd national",
    "pib india",
    # Editorial & mainstream newsrooms
    "the lallantop",
    "aaj tak",
    "ndtv india",
    "bbc news hindi",
    "abp news",
    "news 24",
    "dainik bhaskar",
    "amar ujala",
    "tv9 bharatvarsh",
    "zee news",
}

# Exhaustive curated credible & analytical channels for English
CREDIBLE_EN = {
    # Public policy / Analytical / UPSC
    "theprint",
    "the wire",
    "scroll.in",
    "newslaundry",
    "vajiram & ravi",
    "vajiram and ravi",
    "vision ias",
    "insights ias",
    "forumias",
    "unacademy upsc",
    "sleepy classes",
    # Public & international coverage from India
    "sansad tv english",
    "wion",
    "rstv",
    # Mainstream editorial desks
    "the hindu",
    "the indian express",
    "hindustan times",
    "ndtv",
    "india today",
    "times now",
    "cnn-news18",
    "business standard",
    "livemint",
    "moneycontrol",
    "cnbc-tv18",
    "et now",
    "reuters",
    "associated press",
}


def match_channel(channel_title: str, channel_set: set[str]) -> bool:
    """Case-insensitive substring match against curated channel sets."""
    c = (channel_title or "").strip().lower()
    return any(target in c for target in channel_set)


def channel_language(channel_title: str) -> str | None:
    """Resolve language strictly by channel membership; returns None if unlisted."""
    if match_channel(channel_title, CREDIBLE_HI):
        return "hi"
    if match_channel(channel_title, CREDIBLE_EN):
        return "en"
    return None


def is_credible(channel_title: str) -> bool:
    """Check if the channel belongs to either curated list."""
    return match_channel(channel_title, CREDIBLE_HI) or match_channel(channel_title, CREDIBLE_EN)


STOP = set("""the a an and or of to in on at for with from by as is are was were be been has have had will would after before
over under into about than this that these those its his her their new says said news live video latest update updates
today full what why how who when where watch big breaking report reports की के का में से पर और है हैं को ने भी एक यह
वह लिए साथ बाद तक कर किया गया गई गए हुआ हुई रहा रही रहे ख़बर खबर""".split())


def _roots(text: str) -> set[str]:
    """Root words of a title: lower case, Latin words trimmed to 5 chars, Devanagari preserved."""
    out = set()
    for w in re.findall(r"[A-Za-z]+|[\u0900-\u097f]+", text or ""):
        w = w.lower()
        if w in STOP or (w.isascii() and len(w) < 3):
            continue
        out.add(w[:5] if w.isascii() else w)
    return out


def relevant(title: str, headlines: list[str]) -> bool:
    """Ensure video shares at least two root terms with one of the article headlines."""
    t = _roots(title)
    return any(len(t & _roots(h)) >= 2 for h in headlines if h)


def pick(lists: list[list[dict]], keep: int = KEEP, per_channel: int = PER_CHANNEL) -> list[dict]:
    """Take results, ranking credible channels first, enforcing the per-channel cap."""
    combined: list[dict] = []
    for lst in lists:
        combined.extend(lst)

    # Sort credible channels ahead of unlisted ones, preserving relative discovery order
    combined.sort(key=lambda v: 0 if v.get("credible") else 1)

    out: list[dict] = []
    seen: set[str] = set()
    per: dict[str, int] = {}

    for v in combined:
        if len(out) >= keep:
            break
        vid = v.get("id")
        ch = v.get("channelId") or v.get("channel")
        if not vid or vid in seen or per.get(ch, 0) >= per_channel:
            continue
        seen.add(vid)
        per[ch] = per.get(ch, 0) + 1
        out.append(v)

    return out


def _search(key: str, q: str, after: dt.datetime, lang: str, get=None) -> list[dict]:
    import requests
    get = get or requests.get
    r = get(
        API,
        params={
            "part": "snippet",
            "type": "video",
            "q": q[:200],
            "maxResults": PER_SEARCH,
            "key": key,
            "regionCode": "IN",
            "relevanceLanguage": lang,
            "safeSearch": "moderate",
            "publishedAfter": after.replace(microsecond=0).isoformat() + "Z",
        },
        timeout=20,
    )
    r.raise_for_status()
    out = []
    for it in r.json().get("items") or []:
        vid = (it.get("id") or {}).get("videoId")
        sn = it.get("snippet") or {}
        if not vid:
            continue
        thumbs = sn.get("thumbnails") or {}
        thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
        ch_title = html.unescape(sn.get("channelTitle") or "")
        out.append({
            "id": vid,
            "title": html.unescape(sn.get("title") or ""),
            "channel": ch_title,
            "channelId": sn.get("channelId"),
            "at": sn.get("publishedAt"),
            "thumb": thumb,
            "lang": channel_language(ch_title),
            "credible": is_credible(ch_title),
        })
    return out


def _units_today(store: Store) -> int:
    from .router import quota_day
    r = store.one(select(quota_usage.c.requests).where(quota_usage.c.model == "youtube-search",
                                                       quota_usage.c.day == quota_day()))
    return (r or {}).get("requests") or 0


def fetch_new(store: Store, limit: int = 6, key: str | None = None, get=None) -> list[int]:
    """Videos for new live articles that have none yet (each article once)."""
    from .router import quota_day
    key = key if key is not None else os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        return []
    since = utcnow() - dt.timedelta(hours=NEW_HOURS)
    done_ids = {r["story_id"] for r in store.rows(select(videos.c.story_id))}
    rows = [r for r in store.rows(select(published.c.story_id, published.c.headline_en, published.c.headline_hi)
                                  .where(published.c.updated_at >= since).order_by(published.c.updated_at.desc()))
            if r["story_id"] not in done_ids]
    done: list[int] = []
    for r in rows[:limit]:
        if _units_today(store) + 200 > DAY_UNITS:
            break
        first = store.one(select(func.min(articles.c.published_at).label("t")).where(articles.c.story_id == r["story_id"]))
        after = ((first or {}).get("t") or since) - dt.timedelta(hours=1)
        lists = []
        heads = [r["headline_en"], r["headline_hi"]]

        for q, lang in ((r["headline_hi"], "hi"), (r["headline_en"], "en")):
            if not q:
                continue
            try:
                results = _search(key, re.sub(r"\s+", " ", q), after, lang, get)
                lists.append([v for v in results if relevant(v["title"], heads)])
            except Exception as e:  # noqa: BLE001
                log.info("youtube search failed for %s: %s", r["story_id"], e)
                lists.append(None)
            store.quota_add("youtube-search", quota_day(), 100, 0)

        if any(x is None for x in lists):
            continue

        store.exec(insert(videos).values(story_id=r["story_id"], fetched_at=utcnow(), items=pick(lists)))
        done.append(r["story_id"])
    return done
