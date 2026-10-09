"""People and organisations an article is about, by code (owner, Oct 9 2026: readers follow them and are notified).

No model call: the names the statements already carry (each statement's speaker and the capitalised names in its
text, the same reading `narrative._people` gives the writer), kept only when the written article names them, ranked
by how often it does. Titles are taken off ("Chief Minister Siddaramaiah" -> "Siddaramaiah", "PM Narendra Modi" ->
"Narendra Modi"); one entry per person (the fullest form). A name can colour nothing and merge nothing.

Stored as `payload.people` = ["Narendra Modi", "Supreme Court", ...] (at most MAX_PEOPLE), the same on the Hindi page.
Following matches by words: a reader following "Modi" is told about an article naming "Narendra Modi".
"""
from __future__ import annotations

import collections
import re

MAX_PEOPLE = 8
TITLES = {
    "pm", "prime", "minister", "chief", "cm", "union", "home", "finance", "external", "affairs", "defence", "deputy",
    "president", "vice", "governor", "lieutenant", "lt", "speaker", "justice", "judge", "cji", "mr", "mrs", "ms", "dr",
    "shri", "smt", "sri", "former", "late", "leader", "opposition", "mp", "mla", "senior", "superintendent", "sp",
    "ssp", "dgp", "igp", "inspector", "sub-inspector", "officer", "secretary", "general", "spokesperson", "actor",
    "actress", "director", "ceo", "chairman", "chairperson", "founder", "captain", "coach", "minister's", "state",
    "congress", "bjp", "aap", "tmc", "sp's", "bsp", "dmk", "aiadmk", "cpi(m)", "cpi", "jd(u)", "rjd", "ncp", "shiv",
    "sena", "national", "party", "attorney", "solicitor", "additional", "district", "collector", "commissioner",
    "police", "station", "house", "sho", "deputy commissioner", "monsignor", "pope", "king", "queen", "prince",
    "princess", "justices", "amicus", "curiae", "patient", "spokesman", "sheikh", "emir", "chancellor", "foreign", "army", "air", "navy", "marshal", "admiral", "lieutenant-general",
}
NOT_NAMES = {
    "the", "a", "an", "in", "on", "at", "after", "before", "while", "meanwhile", "however", "but", "and", "or", "of",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october", "november", "december", "india", "indian",
    "he", "she", "they", "it", "this", "that", "these", "those", "his", "her", "their", "its", "according", "earlier",
    "later", "today", "yesterday", "also", "rs", "crore", "lakh", "per", "cent",
}
GENERIC = {"centre", "government", "court", "police", "state", "the centre", "the government", "the court",
           "the police", "the state", "reports", "media"}
INSTITUTION = {"court", "commission", "ministry", "party", "council", "police", "government", "department", "board",
               "bank", "university", "assembly", "sabha", "corporation", "authority", "bureau", "agency", "office",
               "committee", "force", "army", "navy", "tribunal", "institute", "college", "hospital", "company", "group",
               "limited", "ltd", "india", "station", "district", "pradesh", "nadu", "bengal"}
NAME = re.compile(r"(?:[A-Z][\w.&'()-]*\s+){0,4}[A-Z][\w'-]{2,}")


def _clean(phrase: str) -> str:
    """A name without its title, role or descriptor: "Election Commissioner Gyanesh Kumar" -> "Gyanesh Kumar",
    "Yemen's Houthi" -> "Houthi", "Iran-backed Houthi" -> "Houthi"."""
    words = [w.strip(".,;:\"'‘’“”") for w in phrase.replace("’", "'").split()]
    words = [w for w in words if w]
    # a possessive or a hyphenated descriptor in front ("Delhi's", "Liberian-flagged") is not part of the name
    while len(words) > 1 and (words[0].endswith("'s") or re.search(r"-[a-z]", words[0])):
        words = words[1:]
    # everything up to the last title word goes, when a name of 2+ words is left after it
    last_title = max((k for k, w in enumerate(words) if w.lower() in TITLES), default=-1)
    if 0 <= last_title and len(words) - last_title - 1 >= 2:
        words = words[last_title + 1:]
    while words and (words[0].lower() in TITLES or words[0].lower() in NOT_NAMES):
        words = words[1:]
    while words and words[-1].lower() in NOT_NAMES:
        words = words[:-1]
    if words and words[-1].endswith("'s"):
        words[-1] = words[-1][:-2]
    return " ".join(words)


def _candidates(items: list[dict]) -> list[str]:
    out: list[str] = []
    for i in items:
        sp = _clean(str(i.get("speaker") or ""))
        if sp:
            out.append(sp)
        for m in NAME.finditer(str(i.get("text") or "")):
            c = _clean(m.group(0))
            if c:
                out.append(c)
    return out


def _ok(name: str) -> bool:
    words = name.split()
    if not words or name.lower() in GENERIC or len(name) < 4:
        return False
    if all(w.lower() in NOT_NAMES or w.lower() in TITLES for w in words):
        return False
    from .places import BY_NAME, _key
    if _key(name) in BY_NAME:
        return False                       # a place, not a person or a body (places.py)
    if len(words) == 1 and (words[0].isupper() and len(words[0]) <= 3):
        return False                       # "BJP" alone stays (4+ letters or mixed case); "SP", "UP" do not
    return not re.search(r"\d", name)


def _items(p: dict) -> list[dict]:
    out = [i for tier in p.get("timeline") or [] for i in tier or []]
    for k in ("undated", "contested", "established", "context"):
        out += p.get(k) or []
    return [i for i in out if isinstance(i, dict)]


def _single_ok(word: str, text: str) -> bool:
    """A one-word name: an acronym (FCRA, MPSC), or a word the article capitalises in the middle of a sentence and
    never writes in lower case ("Authorities", "Some", "Local" open sentences and are not names)."""
    if re.search(r"-[a-z]", word):
        return False                       # "Liberian-flagged", "AI-generated": a description
    if re.fullmatch(r"[A-Z]{3,}", word):
        return True
    if re.search(rf"(?<![\w-]){re.escape(word.lower())}(?![\w-])", text):
        return False
    return re.search(rf"[a-z,]\s+{re.escape(word)}(?![\w-])", text) is not None


def from_payload(payload: dict) -> list[str]:
    """The people and bodies the article names, most named first. The fullest form of one name wins ("Narendra
    Modi" over "Modi"), counted together."""
    from .places import article_text
    text = article_text(payload)
    if not text.strip():
        return []
    cands = [c for c in _candidates(_items(payload)) if _ok(c) and (len(c.split()) > 1 or _single_ok(c, text))]
    forms: list[str] = []                  # the fullest forms; a shorter name inside one is the same entry
    for c in sorted(set(cands), key=lambda x: (-len(x.split()), -len(x))):
        if not any(set(c.lower().split()) <= set(f.lower().split()) for f in forms):
            forms.append(c)
    counts = collections.Counter()
    for form in forms:
        n = len(re.findall(rf"(?<![\w-]){re.escape(form)}(?![\w-])", text))
        last = form.split()[-1]
        if len(form.split()) > 1 and last.lower() not in INSTITUTION:
            # a person is named in full once, then by surname (house style): count the surname
            n = max(n, len(re.findall(rf"(?<![\w-]){re.escape(last)}(?![\w-])", text)))
        if n:
            counts[form] = n
    return [n for n, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_PEOPLE]]


def matches(follow_key: str, names: list[str]) -> bool:
    """A followed name matches an article when every word of it is a word of one of the article's names
    ("modi" -> "Narendra Modi"; "supreme court" -> "Supreme Court")."""
    want = [w for w in re.split(r"\s+", (follow_key or "").lower().strip()) if w]
    if not want:
        return False
    for n in names or []:
        words = set(n.lower().split())
        if all(w in words for w in want):
            return True
    return False
