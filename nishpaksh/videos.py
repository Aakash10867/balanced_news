"""YouTube videos for an article (owner, Oct 9 2026).

Fetched once, when the article is new (the desk, after publishing), so the site's videos button opens at once and
costs nothing per click. YouTube's own order is kept (owner: "it is fine if videos are ranked as per YouTube"); what
Nishpaksh adds is balance and two guards:
  * two searches, the Hindi headline and the English headline, taken in turn, Hindi first (owner), so both
    languages are there (when one runs out, the other fills the list);
  * at most 2 videos from one channel, so no one outlet fills the list (until perspectives are proven, this is how
    "all sides" is kept; then every perspective cluster gets a place);
  * only videos uploaded after the story's first report (an old clip passed off as new is the commonest video lie);
  * a "Primary footage" badge for official channels (Parliament, courts, ministries, parties) - a label, not a rank.
The site says videos are not checked by Nishpaksh. Nothing here colours or merges anything.

Needs YOUTUBE_API_KEY (YouTube Data API v3, free: 10,000 units a day; a search costs 100). Without it, nothing runs.
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
PER_SEARCH = 15
KEEP = 10
PER_CHANNEL = 2
DAY_UNITS = 9000             # of 10,000: room for mistakes
NEW_HOURS = 36               # only articles this new get videos
PRIMARY = ("sansad tv", "pib india", "supreme court of india", "narendra modi", "mygovindia", "ministry of",
           "bharatiya janata party", "indian national congress", "aam aadmi party", "rahul gandhi", "amit shah",
           "rajnath singh", "president of india", "election commission", "dd national", "rashtrapati bhavan",
           "prime minister's office", "pmo india", "ministry of external affairs", "meaindia", "lok sabha",
           "rajya sabha", "government of", "chief minister", "high court")


def is_primary(channel: str) -> bool:
    c = (channel or "").lower()
    return any(p in c for p in PRIMARY)


STOP = set("""the a an and or of to in on at for with from by as is are was were be been has have had will would after before
over under into about than this that these those its his her their new says said news live video latest update updates
today full what why how who when where watch big breaking report reports की के का में से पर और है हैं को ने भी एक यह
वह लिए साथ बाद तक कर किया गया गई गए हुआ हुई रहा रही रहे ख़बर खबर""".split())


def _roots(text: str) -> set[str]:
    """Root words of a title: lower case, Latin words cut to 5 letters ("margins" = "margin" = "margi"), Devanagari
    words whole; small words and news words left out."""
    out = set()
    for w in re.findall(r"[A-Za-z]+|[\u0900-\u097f]+", text or ""):
        w = w.lower()
        if w in STOP or (w.isascii() and len(w) < 3):
            continue
        out.add(w[:5] if w.isascii() else w)
    return out


def relevant(title: str, headlines: list[str]) -> bool:
    """A video is about the story only if its title shares two root words with a headline (English or Hindi):
    YouTube returns whatever it has when nothing matches (Oct 10 2026: an audiobook and a Chinese drama for a US
    sanctions story)."""
    t = _roots(title)
    return any(len(t & _roots(h)) >= 2 for h in headlines if h)


def pick(lists: list[list[dict]], keep: int = KEEP, per_channel: int = PER_CHANNEL) -> list[dict]:
    """Take the lists in turn, each in YouTube's order; a channel at most `per_channel` times; one entry per video."""
    out, seen, per = [], set(), {}
    k = 0
    while len(out) < keep and any(k < len(lst) for lst in lists):
        for lst in lists:
            if k < len(lst) and len(out) < keep:
                v = lst[k]
                ch = v.get("channelId") or v.get("channel")
                if v["id"] in seen or per.get(ch, 0) >= per_channel:
                    continue
                seen.add(v["id"])
                per[ch] = per.get(ch, 0) + 1
                out.append(v)
        k += 1
    return out


def _search(key: str, q: str, after: dt.datetime, lang: str, get=None) -> list[dict]:
    import requests
    get = get or requests.get
    r = get(API, params={"part": "snippet", "type": "video", "q": q[:200], "maxResults": PER_SEARCH, "key": key,
                         "regionCode": "IN", "relevanceLanguage": lang, "safeSearch": "moderate",
                         "publishedAfter": after.replace(microsecond=0).isoformat() + "Z"}, timeout=20)
    r.raise_for_status()
    out = []
    for it in r.json().get("items") or []:
        vid = (it.get("id") or {}).get("videoId")
        sn = it.get("snippet") or {}
        if not vid:
            continue
        thumbs = sn.get("thumbnails") or {}
        thumb = (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url")
        out.append({"id": vid, "title": html.unescape(sn.get("title") or ""), "channel": html.unescape(sn.get("channelTitle") or ""),
                    "channelId": sn.get("channelId"), "at": sn.get("publishedAt"), "thumb": thumb, "lang": lang,
                    "primary": is_primary(sn.get("channelTitle") or "")})
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
        for q, lang in ((r["headline_hi"], "hi"), (r["headline_en"], "en")):   # Hindi first (owner, Oct 9 2026)
            if not q:
                continue
            try:
                lists.append(_search(key, re.sub(r"\s+", " ", q), after, lang, get))
            except Exception as e:  # noqa: BLE001
                log.info("youtube search failed for %s: %s", r["story_id"], e)
                lists.append(None)
            store.quota_add("youtube-search", quota_day(), 100, 0)
        if any(x is None for x in lists):
            continue                            # tried again next run
        heads = [r["headline_en"], r["headline_hi"]]
        lists = [[v for v in lst if relevant(v["title"], heads)] for lst in lists]
        store.exec(insert(videos).values(story_id=r["story_id"], fetched_at=utcnow(), items=pick(lists)))
        done.append(r["story_id"])
    return done
