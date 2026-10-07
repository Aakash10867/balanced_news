"""Statement frames: every statement broken into fixed fields when the article is read, so that two
statements are compared by code, field by field, instead of by a model judging two sentences (owner,
Oct 6 2026: each model judgement produced a new kind of error: two steps of one agreement shown as a
dispute, a rounded number shown as a dispute, the same claim worded twice kept twice).

    who      who acts, or what the statement is about        "India and EFTA", "Police"
    action   what is done, as a base verb                     "sign", "take effect", "arrest", "die"
    what     to what or whom                                   "TEPA", "the site engineer"
    value    the answer the statement gives, if any            "125.27 million", "47 per cent", "corrupt"
    approx   the value is approximate ("about", "nearly")
    negated  the statement says it did NOT happen

Words are reduced to their roots (Snowball stemmer: arrested / arrests -> arrest) and small words are
dropped, so wording does not matter; numbers are read as numbers (125.27 million, 1.2 crore).

compare(a, b):
    "same"      same who, action and what, and both give the same value: one fact, whatever the wording
    "compatible" same who, action and what, but a value is missing: never a dispute; the wording decides
    "conflict"  same who, action and what, and incompatible values, or one says it did not happen:
                the same question answered differently. The ONLY way a contradiction is made.
    "unsure"    same who, action and what, but given for different times: maybe two events
    "different" anything else: different questions, so never a dispute
"""
from __future__ import annotations

import re

import snowballstemmer

_stem = snowballstemmer.stemmer("english").stemWord

STOP = set("""a an the of in on at to for from by with and or as is are was were be been being has have had
its his her their this that these those it they he she him them who which s per cent percent about
nearly around approximately roughly some over under more than less least most at almost just
mr mrs ms dr shri smt""".split())
# the same action said in different words (base verbs); kept small and only for true synonyms
ACTION_SYNONYMS = {
    "come into force": "take effect", "enter into force": "take effect", "come into effect": "take effect",
    "become effective": "take effect", "kill": "kill", "slay": "kill",
    "say": "say", "state": "say", "tell": "say",
    "claim": "claim", "allege": "allege", "accuse": "allege",
    "detain": "detain", "arrest": "arrest", "nab": "arrest",
}
APPROX = re.compile(r"(?i)\b(about|around|nearly|approximately|roughly|some|over|more than|less than|at least|"
                    r"almost|close to|up to|estimated|~)\b")
LOWER = re.compile(r"(?i)\b(at least|over|more than|above|upwards of|in excess of)\b")
UPPER = re.compile(r"(?i)\b(up to|at most|less than|under|below|fewer than)\b")
MULT = {"thousand": 1e3, "lakh": 1e5, "lakhs": 1e5, "million": 1e6, "mn": 1e6, "crore": 1e7, "crores": 1e7,
        "billion": 1e9, "bn": 1e9, "trillion": 1e12, "k": 1e3}
NUM = re.compile(r"(\d+(?:[.,]\d+)*)\s*(thousand|lakhs?|million|mn|crores?|billion|bn|trillion|k)?\b", re.I)


def words(text: str | None) -> frozenset[str]:
    """Root words of a field: lower case, small words dropped, each word stemmed."""
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return frozenset(_stem(t) for t in toks if t not in STOP and len(t) > 1)


def action_key(action: str | None) -> str:
    a = re.sub(r"\s+", " ", (action or "").lower().strip())
    a = re.sub(r"^(to|did|has|have|had|was|were|is|are|be|been|will|would|could)\s+", "", a)
    a = ACTION_SYNONYMS.get(a, a)
    return " ".join(sorted(words(a)))


def numbers(text: str | None) -> list[float]:
    out = []
    for n, mult in NUM.findall(text or ""):
        try:
            v = float(n.replace(",", ""))
        except ValueError:
            continue
        out.append(v * MULT.get((mult or "").lower(), 1))
    return out


def normalize(frame: dict | None) -> dict | None:
    """A frame as extracted (any shape) -> the clean frame stored with the statement, or None."""
    if not isinstance(frame, dict):
        return None
    f = {k: str(frame.get(k) or "").strip()[:160] for k in ("who", "action", "what", "value", "where")}
    if not f["action"]:
        return None
    val = f["value"]
    f["approx"] = bool(frame.get("approx")) or bool(APPROX.search(val))
    f["negated"] = bool(frame.get("negated"))
    return f


def _overlap(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def same_slot(a: dict, b: dict) -> bool:
    """Same question: same actor, same action, same object. Names match when one is contained in
    the other ("Modi" / "Prime Minister Narendra Modi")."""
    if action_key(a.get("action")) != action_key(b.get("action")):
        return False
    return (_overlap(words(a.get("who")), words(b.get("who"))) >= 0.5
            and _overlap(words(a.get("what")), words(b.get("what"))) >= 0.5)


MONTHS = {m: n for n, ms in enumerate((("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
                                       ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
                                       ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
                                       ("dec", "december")), 1) for m in ms}


def date_of(text: str | None) -> tuple | None:
    """(year, month, day) with None for a part not given, when the value is a date: "1986-12-06",
    "6 December 1986", "December 6, 1986", "31st October", "July 2026". Oct 7 2026: "6 December 1986"
    and "1986-12-06" were read as the numbers 6 and 1986 and shown as a dispute."""
    t = (text or "").lower()
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b\.?", t)
    if not m:
        return None
    month = MONTHS[m.group(1)]
    day = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*(?:hours|years|crore|lakh|%|per))", t[:m.start()][-8:] + " " + t[m.end():m.end() + 6])
    year = re.search(r"\b(1[89]\d\d|20\d\d)\b", t)
    d = int(day.group(1)) if day and 1 <= int(day.group(1)) <= 31 else None
    return (int(year.group(1)) if year else None), month, d


def _dates_agree(x: tuple, y: tuple) -> bool:
    return all(p is None or q is None or p == q for p, q in zip(x, y))


def values_agree(a: dict, b: dict) -> bool | None:
    """True / False when both give a value; None when one or both give none."""
    va, vb = (a.get("value") or "").strip(), (b.get("value") or "").strip()
    if not va or not vb:
        return None
    da, db = date_of(va), date_of(vb)
    year = lambda v: (int(m.group(1)), None, None) if (m := re.fullmatch(r"\D*\b(1[89]\d\d|20\d\d)\b\D*", v)) else None  # noqa: E731
    if da and not db:
        db = year(vb)
    elif db and not da:
        da = year(va)
    if da and db:
        return _dates_agree(da, db)
    if da or db:
        return None          # a date and something else: not comparable, so not a dispute
    na, nb = numbers(va), numbers(vb)
    if na and nb:
        x, y = na[0], nb[0]
        if x == y:
            return True
        # a bound is not a figure: "at least 40" agrees with 50, "up to 50" with 40
        for v, other, bound in ((va, y, x), (vb, x, y)):
            if LOWER.search(v) and other >= bound or UPPER.search(v) and other <= bound:
                return True
        tol = 0.05 if (a.get("approx") or b.get("approx")) else 0.005
        # a rounded figure: "125 million" vs "125.27 million"
        return abs(x - y) <= tol * max(abs(x), abs(y))
    if na or nb:
        return None          # a number and a word: not comparable, so not a dispute
    wa, wb = words(va), words(vb)
    return _overlap(wa, wb) >= 0.5


def _named_apart(x: str | None, y: str | None) -> bool:
    """One object's name has a capitalised word the other lacks: two named things ("Param Vishisht Seva
    Medal" / "Vishisht Seva Medal"), not one described more fully ("the site engineer" / "the engineer")."""
    wx, wy = words(x), words(y)
    for text, extra in ((x, wx - wy), (y, wy - wx)):
        caps = {_stem(t.lower()) for t in re.findall(r"\b[A-Z][A-Za-z0-9]*", text or "")}
        if extra & caps:
            return True
    return False


def compare(a: dict | None, b: dict | None, when_a: dict | None = None, when_b: dict | None = None) -> str | None:
    """None when either statement has no frame (read before frames existed)."""
    if not a or not b:
        return None
    if not same_slot(a, b):
        return "different"
    agree = values_agree(a, b)
    if a.get("negated") != b.get("negated") or agree is False:
        # a dispute only about exactly the same thing: "Param Vishisht Seva Medal" and "Vishisht Seva
        # Medal" share every word of the shorter name but are two medals (Oct 7 2026)
        if _named_apart(a.get("what"), b.get("what")):
            return "different"
        return "conflict"            # "arrested" vs "not arrested"; same question, two answers
    if _times_apart(when_a, when_b):
        return "unsure"              # maybe two events of the same kind: never a dispute, never merged
    # one fact only when both give the answer and it agrees; without values ("police said something
    # about the accused") two statements may still say different things: compatible, never a dispute,
    # and whether they are one fact is left to their wording
    return "same" if agree else "compatible"


def _times_apart(x: dict | None, y: dict | None) -> bool:
    """Both statements are dated and their time ranges do not overlap."""
    try:
        if not x or not y or not (x.get("start") and y.get("start")):
            return False
        # compared at the coarser precision: "2026-10-31" and "2026-10-31T12:00" are the same day
        # a day-precision time covers the whole day (its "T00:00" is not midnight)
        fine = {"hour", "part_of_day", "minute"}
        n = 16 if x.get("precision") in fine and y.get("precision") in fine else 10
        cut = lambda t: str(t)[:n]  # noqa: E731
        return cut(x.get("end") or x["start"]) < cut(y["start"]) or cut(y.get("end") or y["start"]) < cut(x["start"])
    except TypeError:
        return False
