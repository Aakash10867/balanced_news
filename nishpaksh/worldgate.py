"""Which world-outlet articles are worth an embedding (owner, Oct 9 2026). Code only, no model.

Outlets outside India are read for two things: India's stories seen from outside, and world stories
of global impact. Indian coverage decides relevance: a world outlet's article joins stories Indian
outlets cover (Rule 1); a story no Indian outlet covers is written only if three independent world
outlets carry it and it affects people beyond one country (Rule 3, priority.py).

Embeddings are the tightest quota (3,000 texts a day; ~2,200-3,000 were used before world outlets),
so a world-feed article is embedded only when it can join such a story. Its headline and summary:
  (a) mention India (India, Indian, Delhi, Modi ...), or
  (b) share 2+ names with an Indian outlet's headline of the last 48 h (Roman or Devanagari), or
  (c) share 2+ names with the headlines of 2+ other independent world outlets (a Rule 3 candidate).
Anything else waits as a headline and is checked again every run while it is fresh: coverage often
comes later. A miss only costs a story one source; it never makes anything established.
Articles found by search for a story, and Indian outlets' articles, are never held.
"""
from __future__ import annotations

import datetime as dt
import re

from .ownership import owner_of, region_of
from .textmatch import skeleton

INDIA = re.compile(
    r"\b(India|Indians?|New Delhi|Delhi|Mumbai|Bengaluru|Bangalore|Kolkata|Chennai|Hyderabad|Kashmir|Ladakh|"
    r"Kerala|Gujarat|Assam|Manipur|Modi|Jaishankar|Rajnath|Rahul Gandhi|BJP|Congress party|"
    r"Adani|Ambani|Reliance|Tata|Infosys|rupee|Sensex|Nifty|IndiGo|Air India|ISRO|RBI|Bollywood)\b")
# capitalised words that are not names (sentence openers, months, days, common headline words)
NOT_NAMES = {
    "the", "a", "an", "in", "on", "at", "of", "for", "to", "and", "or", "but", "as", "by", "with", "from", "after",
    "before", "over", "under", "into", "amid", "says", "said", "say", "new", "how", "why", "what", "who", "when",
    "where", "will", "is", "are", "was", "were", "be", "has", "have", "his", "her", "its", "their", "this", "that",
    "these", "those", "it", "he", "she", "they", "we", "us", "our", "you", "your", "no", "not", "more", "most",
    "first", "last", "two", "three", "one", "live", "watch", "video", "news", "update", "updates", "report",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "president", "minister", "prime", "government", "police", "court", "people", "world", "state", "city",
    "official", "officials", "leader", "leaders", "talks", "deal", "war", "attack", "election", "elections",
}
MIN_SHARED = 2
WINDOW_HOURS = 48


def names(text: str) -> set[str]:
    """Name skeletons of a headline: capitalised Roman words that are not common words, and every
    Devanagari word (Hindi has no capitals; a rough skeleton is enough to see a shared name)."""
    out = set()
    for w in re.findall(r"[A-Za-z][A-Za-z'\-]+|[ऀ-ॿ]+", text or ""):
        if re.match(r"[A-Za-z]", w):
            w = re.sub(r"['\u2019]s$", "", w).strip("'-")      # "Macron's" is Macron
            if not w[0].isupper() or w.lower() in NOT_NAMES or len(w) < 3:
                continue
        sk = skeleton(w)
        if len(sk) >= 3:
            out.add(sk)
    return out


def _text(a: dict) -> str:
    return f"{a.get('title') or ''}. {(a.get('text') or '')[:300]}"


def hold(arts: list[dict], now: dt.datetime) -> set[int]:
    """Ids of world-feed articles without a vector that cannot join a story yet: not embedded this run."""
    since = now - dt.timedelta(hours=WINDOW_HOURS)
    fresh = [a for a in arts if (a.get("published_at") or now) >= since]
    region = {a["id"]: region_of(a.get("outlet"), a.get("url"), a.get("lang")) for a in arts}
    todo = [a for a in arts if not a.get("embedding") and a.get("feed_id") is not None
            and region[a["id"]] == "world"]
    if not todo:
        return set()
    # inverted indexes, so each article is compared only with headlines sharing one of its names
    indian: dict[str, set[int]] = {}
    for k, a in enumerate(x for x in fresh if region[x["id"]] == "india"):
        for n in names(a.get("title") or ""):
            indian.setdefault(n, set()).add(k)
    world_names: dict[tuple[str, int], set[str]] = {}
    for a in fresh:
        if region[a["id"]] == "world":
            o = owner_of(a.get("outlet"), a.get("url"))
            world_names[(o, a["id"])] = names(_text(a))
    by_name: dict[str, set[tuple[str, int]]] = {}
    for key, ns in world_names.items():
        for n in ns:
            by_name.setdefault(n, set()).add(key)
    held = set()
    for a in todo:
        t = _text(a)
        if INDIA.search(t):
            continue
        mine = names(t)
        if len(mine) < MIN_SHARED:
            held.add(a["id"])
            continue
        hits: dict[int, int] = {}
        for n in mine:
            for k in indian.get(n, ()):
                hits[k] = hits.get(k, 0) + 1
        if any(v >= MIN_SHARED for v in hits.values()):
            continue
        own = owner_of(a.get("outlet"), a.get("url"))
        counts: dict[tuple[str, int], int] = {}
        for n in mine:
            for key in by_name.get(n, ()):
                if key[0] != own:
                    counts[key] = counts.get(key, 0) + 1
        if len({key[0] for key, v in counts.items() if v >= MIN_SHARED}) >= 2:
            continue
        held.add(a["id"])
    return held
