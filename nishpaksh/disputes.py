"""Two statements that cannot both be true: one place decides it (owner, Oct 7 2026).

Replaces the disputes made in nine places (frames adding a contradiction on arrival with no check, the
frames deciding pairs during review, a one-shot "contradict" question, the consolidation model's own list
filtered by real_difference / typed_difference, ...). Two of those paths had no check at all: "17 of the
19 crew are Indian" and "11 of the 12 injured crew are Indian" were shown as sources disagreeing.

A dispute is the SAME QUESTION with a DIFFERENT ANSWER. Judges, in this order:

  1. code finds candidates: take the answer out of both lines (numbers, dates, "not"); what remains
     must be nearly the same words (with the synonym list, relate.SYNONYM) and the same names. Code
     clears the known non-disputes itself: rounding, bounds ("at least 40" / "50"), counts of different
     groups ("17 of 19" / "11 of 12"). Proposals from the consolidation model and the frames, and
     disputes marked earlier, are candidates only through this same gate.
  2. figures over time: when every report giving one figure was published clearly after every report
     giving the other, it is an UPDATE, not a dispute ("the toll rose to 50; earlier reports put it at
     40"): each line keeps its own colour
  3. the model answers "can both be true?" twice, A/B swapped: two "cannot" make a dispute (amber, both
     versions and whose), however many outlets are on each side and whether a side is an official or
     an outlet; any "unsure" or the two answers disagreeing: doubtful (never amber, never green)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from .frames import LOWER, UPPER, date_of
from .relate import NEGATION, WORD_NUM, Profile, numbers_close

MONTH_DAY = re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|"
                       r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")
OF_TOTAL = re.compile(r"(?i)\b(\d+)\s+(?:out\s+)?of\s+(?:the\s+)?(?:total\s+|all\s+)?(\d+)\b")
QUESTION_SAME = 0.75     # share of the remaining words: the same question
UPDATE_GAP = dt.timedelta(hours=1)


def _question(p: Profile, text: str) -> tuple[frozenset, frozenset]:
    """The words and names of a line with its answer taken out (numbers, dates, negation)."""
    from .frames import _stem
    drop = {_stem(w.lower()) for w in re.findall(r"[A-Za-z]+", " ".join(m.group(0) for m in MONTH_DAY.finditer(text)))}
    drop |= {_stem(w.lower()) for w in NEGATION.findall(text) if isinstance(w, str)}
    drop |= {_stem(w) for w in re.findall(r"[a-z]+", text.lower()) if w in WORD_NUM}     # "forty"
    return p.roots - drop - p.names, p.names - drop


SPEAKER = re.compile(r"(?i)^.{0,80}?\b(?:said|says|stated|states|claimed|claims|alleged|alleges|told reporters|"
                     r"reported|announced|confirmed)\b(?:\s+that)?,?\s+")
ACCORDING = re.compile(r"(?i),?\s+according to [^,.;]+")


def claim_body(text: str) -> str:
    """The claim without who says it: "Police said 40 died" and "The family said 50 died" ask one
    question (an official against an outlet or a family is a dispute too, owner Oct 7 2026)."""
    t = ACCORDING.sub("", text or "")
    m = SPEAKER.match(t)
    return t[m.end():] if m and len(t) - m.end() > 15 else t


def candidate(a: str, b: str, pa: Profile | None = None, pb: Profile | None = None) -> str | None:
    """'number', 'date' or 'negation' when a and b look like the same question answered differently;
    None when code can tell they are not a dispute."""
    a, b = claim_body(a), claim_body(b)
    pa, pb = Profile(a), Profile(b)
    qa, na = _question(pa, a)
    qb, nb = _question(pb, b)
    if na != nb or not (qa | qb):
        return None
    if len(qa & qb) / len(qa | qb) < QUESTION_SAME:
        return None
    if pa.neg != pb.neg:
        return "negation" if numbers_close(pa, pb) else None
    wa = {m.group(2).lower() for m in MONTH_DAY.finditer(a) if m.group(2)}
    wb = {m.group(2).lower() for m in MONTH_DAY.finditer(b) if m.group(2)}
    if wa and wb and not wa & wb:
        return "date"                     # "on Friday" / "on Saturday"
    da, db = date_of(a), date_of(b)
    if da and db and any(x is not None and y is not None and x != y for x, y in zip(da, db)):
        return "date"
    if pa.nums and pb.nums and not numbers_close(pa, pb):
        # "17 of 19" and "11 of 12": counts of two different groups
        ta, tb = OF_TOTAL.search(a), OF_TOTAL.search(b)
        if ta and tb and ta.group(2) != tb.group(2):
            return None
        # a bound is not a figure: "at least 40" agrees with 50, "up to 50" with 40
        if (LOWER.search(a) and max(pb.nums) >= min(pa.nums)) or (LOWER.search(b) and max(pa.nums) >= min(pb.nums)):
            return None
        if (UPPER.search(a) and min(pb.nums) <= max(pa.nums)) or (UPPER.search(b) and min(pa.nums) <= max(pb.nums)):
            return None
        return "number"
    return None


def is_update(times_a: list[dt.datetime], times_b: list[dt.datetime]) -> int:
    """1 when every report of b came clearly after every report of a (b updates a), -1 the other way,
    0 when the reports overlap in time (then it is a question for the model)."""
    if not times_a or not times_b:
        return 0
    if min(times_b) - max(times_a) >= UPDATE_GAP:
        return 1
    if min(times_a) - max(times_b) >= UPDATE_GAP:
        return -1
    return 0


def _key(a: str, b: str) -> str:
    return hashlib.sha256(json.dumps([a, b]).encode()).hexdigest()[:16]


def judge(router, texts: dict[int, str], times: dict[int, list], proposals: set[tuple[int, int]],
          exclude: set[tuple[int, int]], cache: dict, max_ask: int = 30
          ) -> tuple[set[tuple[int, int]], set[int], dict[int, int]]:
    """For one story's statements: (disputes, doubtful statement ids, {older figure: newer figure}).
    `proposals` (pairs from models, frames, earlier marks) are checked by the same gate as code's own;
    `exclude` holds claim/response pairs (never a dispute). `cache` keeps the model's answers per text pair."""
    from .match import check_conflicts
    prof = {i: Profile(t) for i, t in texts.items()}
    ids = sorted(texts)
    cands: dict[tuple[int, int], str] = {}
    for k, a in enumerate(ids):
        for b in ids[k + 1:]:
            kind = candidate(texts[a], texts[b], prof[a], prof[b])
            if kind:
                cands[(a, b)] = kind
    for a, b in proposals:
        p = (min(a, b), max(a, b))
        if p not in cands and a in texts and b in texts:
            kind = candidate(texts[p[0]], texts[p[1]], prof[p[0]], prof[p[1]])
            if kind:
                cands[p] = kind
    cands = {p: k for p, k in cands.items() if p not in exclude}
    updates: dict[int, int] = {}
    ask = []
    for (a, b), kind in cands.items():
        if kind == "number":
            u = is_update(times.get(a) or [], times.get(b) or [])
            if u:
                old, new = (a, b) if u == 1 else (b, a)
                updates[old] = new
                continue
        ask.append((a, b))
    todo = []
    for a, b in ask:
        for x, y in ((a, b), (b, a)):
            if _key(texts[x], texts[y]) not in cache:
                todo.append((x, y))
    todo = todo[:2 * max_ask]
    for (x, y), ans in zip(todo, check_conflicts(router, [(texts[x], texts[y]) for x, y in todo])):
        cache[_key(texts[x], texts[y])] = ans
    disputes, doubtful = set(), set()
    for a, b in ask:
        first, second = cache.get(_key(texts[a], texts[b])), cache.get(_key(texts[b], texts[a]))
        if first is None or second is None:
            doubtful |= {a, b}            # not asked yet (quota): no amber, and no green either
            continue
        if first == second == "cannot_both_be_true":
            disputes.add((a, b))
        elif "unsure" in (first, second) or first != second:
            doubtful |= {a, b}
    return disputes, doubtful, updates
