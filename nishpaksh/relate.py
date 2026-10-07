"""Are two statements the same fact? One place decides it, for the whole pipeline (owner, Oct 7 2026).

It replaces a chain of judges that could each veto the others (Oct 5-7: real_difference,
typed_difference, frames, may_be_same, same_facts), where a weak model's labels outvoted the words:
"11 of the 12 injured crew members are Indian nationals", word for word from two outlets, stayed two
statements because the two readings labelled it "identify / nationality" and "be / Indian nationals".

The words outrank the labels. Judges, in this order:

  1. code, on the words themselves: the numbers (as numbers: "eleven" = 11, "1.2 lakh" = 120000),
     the names (capitalised words inside the sentence) and the root words (stemmed, small words out)
       same        identical wording; or the same numbers, the same names and almost the same words
       covers      one says everything the other says (every number, name and nearly every word) and more:
                   the detailed line takes in the short one ("12 crew members, including 11 Indians, were
                   injured" covers "12 crew members were injured")
       different   different numbers or names, one negated and the other not, or little in common
       ask         the same numbers and names, but worded differently enough that code cannot tell
  2. the model, only for "ask": one plain question (match.same_facts), asked twice with A and B swapped;
     only two "same" answers count
  3. labels (statement frames) only point to pairs worth looking at; they never block anything

A covered line is not merged into the detailed one (that would lend its outlets to details they never
reported, and could turn them green): it is kept, marked covered, and not written separately.
"""
from __future__ import annotations

import re

from .frames import STOP as _STOP, _stem, numbers as _numbers, words as _roots

WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
            "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
            "hundred": 100, "dozen": 12}
NEGATION = re.compile(r"(?i)\b(not|no|never|nobody|none|neither|nor|without|denied|denies|deny|refused|"
                      r"refuses|rejected|rejects)\b|n't\b")
# words that say how a line was reported, not what happened: never a difference between two lines
FILLER = {_stem(w) for w in """said say says stated told added according report reports reported article also
officials official sources source claimed claims noted informed confirmed will would shall can could may might
must also new current currently""".split()}

# the same meaning in other words, as Indian news writes it (owner, Oct 7 2026): each group is read as
# ONE word. Kept short and hand-checked: words that differ in meaning are never grouped ("injured" and
# "killed" stay apart; so do "arrested" and "questioned").
SYNONYM_GROUPS = [
    "injured hurt wounded", "killed dead died death deaths die dies lost", "arrested detained nabbed apprehended",
    "attack attacked assault assaulted", "vessel ship boat", "blast explosion", "fire blaze",
    "police cops", "rescued saved", "missing untraceable", "village hamlet", "residents locals",
    "part section portion", "collapsed caved",
]
SYNONYM = {}
for _g in SYNONYM_GROUPS:
    _ws = [_stem(w) for w in _g.split()]
    for _w in _ws:
        SYNONYM[_w] = _ws[0]

SAME_WORDS = 0.8        # share of root words for "same" (with the same numbers and names)
COVER_WORDS = 0.85      # share of the short line's root words the detailed line must contain
ASK_WORDS = 0.4         # below this, two lines with the same numbers and names are still different


# words that can turn the detailed line into a different event ("arrested him in another case")
CONTRAST = re.compile(r"(?i)\b(another|other|earlier|previous|previously|former|formerly|last year|last month|"
                      r"separate|different|second|again|also|before|after)\b")


class Profile:
    __slots__ = ("norm", "nums", "names", "roots", "neg", "contrast")

    def __init__(self, text: str):
        t = text or ""
        self.norm = re.sub(r"\W+", " ", t.lower()).strip()
        nums = {round(x, 3) for x in _numbers(t)}
        nums |= {float(n) for n in re.findall(r"\b(\d+)(?:st|nd|rd|th)\b", t)}       # "29th"
        nums |= {float(WORD_NUM[w]) for w in re.findall(r"[a-z]+", t.lower()) if w in WORD_NUM}
        self.nums = frozenset(nums)
        # names: capitalised words inside the sentence (the first word is capitalised anyway)
        toks = re.findall(r"[A-Za-z][\w'-]*", t)
        names = {_stem(w.lower()) for w in toks[1:] if w[0].isupper() and len(w) > 1}
        # the first word is a name when it starts a run of capitalised words ("Air Marshal ...")
        if len(toks) > 1 and toks[0][0].isupper() and toks[1][0].isupper() and toks[0].lower() not in _STOP:
            names.add(_stem(toks[0].lower()))
        self.names = frozenset(names)
        self.roots = frozenset(SYNONYM.get(r, r) for r in _roots(t) if r not in FILLER and not r.isdigit())
        self.neg = bool(NEGATION.search(t))
        self.contrast = bool(CONTRAST.search(t))


def _share(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def _within(small: frozenset, big: frozenset) -> float:
    return len(small & big) / len(small) if small else 1.0


def _times_apart(x: dict | None, y: dict | None) -> bool:
    from .frames import _times_apart as apart
    return apart(x, y)


def relate(a: str, b: str, time_a: dict | None = None, time_b: dict | None = None,
           pa: Profile | None = None, pb: Profile | None = None) -> str:
    """'same', 'a_covers_b', 'b_covers_a', 'ask' or 'different', by code from the words."""
    pa, pb = pa or Profile(a), pb or Profile(b)
    if pa.neg != pb.neg or _times_apart(time_a, time_b):
        return "different"
    if pa.norm == pb.norm:
        return "same"
    # the names both lines share (the subject, "Air Marshal Ashutosh Dixit") are compared as names and
    # left out of the word overlap: in a story about one person every line shares them, and they made
    # unrelated lines look alike (Oct 7 2026, story 13809)
    common = pa.names & pb.names
    ra, rb = pa.roots - common, pb.roots - common
    same_facts = pa.nums == pb.nums and pa.names == pb.names
    if same_facts and _share(ra, rb) >= SAME_WORDS:
        return "same"
    for big, small, rbig, rsmall, label in ((pa, pb, ra, rb, "a_covers_b"), (pb, pa, rb, ra, "b_covers_a")):
        if (len(rsmall) >= 3 and len(rbig) > len(rsmall) and small.nums <= big.nums
                and small.names <= big.names and _within(rsmall, rbig) >= COVER_WORDS):
            # code alone only when the detailed line adds no number and no word that could make it a
            # different event; otherwise the model is asked
            if big.nums == small.nums and not (big.contrast and not small.contrast):
                return label
            return "ask_" + label
    if same_facts and _share(ra, rb) >= ASK_WORDS and (pa.nums or pa.names or len(ra) >= 3):
        return "ask"
    # nearly covered: every number and name, all but one or two words ("injured in the incident" /
    # "injured in the attack"); code cannot tell "incident" = "attack" from "injured" vs "killed",
    # so the model is asked whether the detailed line says everything the short one says
    for big, small, rbig, rsmall, label in ((pa, pb, ra, rb, "ask_a_covers_b"), (pb, pa, rb, ra, "ask_b_covers_a")):
        if (len(rsmall) >= 2 and len(rbig) > len(rsmall) and small.nums <= big.nums and small.names <= big.names
                and len(rsmall - rbig) <= 1 and _within(rsmall, rbig) >= 0.6):
            return label
    return "different"


def group(texts: dict[int, str], times: dict[int, dict | None] | None = None, asked: dict | None = None
          ) -> tuple[list[tuple[int, int]], dict[int, int], list[tuple[int, int]], list[tuple[int, int]]]:
    """For a story's statements {id: text}: (pairs that are the same, {covered id: covering id}, pairs
    to ask "same?", pairs (detailed, short) to ask "covers?"). Code only; the caller asks the model."""
    times = times or {}
    prof = {i: Profile(t) for i, t in texts.items()}
    ids = sorted(texts)
    same, covered, ask, ask_cover = [], {}, [], []
    for k, a in enumerate(ids):
        for b in ids[k + 1:]:
            r = relate(texts[a], texts[b], times.get(a), times.get(b), prof[a], prof[b])
            if r == "same":
                same.append((a, b))
            elif r == "a_covers_b":
                covered.setdefault(b, a)
            elif r == "b_covers_a":
                covered.setdefault(a, b)
            elif r == "ask":
                ask.append((a, b))
            elif r == "ask_a_covers_b":
                ask_cover.append((a, b))
            elif r == "ask_b_covers_a":
                ask_cover.append((b, a))
    return same, covered, ask, ask_cover


def resolve_covered(covered: dict[int, int]) -> dict[int, int]:
    """Each covered line points at the most detailed line that covers it (no chains, no loops)."""
    out = dict(covered)
    for x in list(out):
        seen = {x}
        while out.get(out[x]) is not None and out[out[x]] not in seen:
            seen.add(out[x])
            out[x] = out[out[x]]
        if out[x] == x:
            del out[x]
    return out


def numbers_close(pa: Profile, pb: Profile, tol: float = 0.05) -> bool:
    """Every number of the line with fewer numbers has one within `tol` in the other (rounding:
    "125.27 million" / "about 125 million"); True when either line has none."""
    small, big = sorted((pa.nums, pb.nums), key=len)
    if not small:
        return True
    return all(any(abs(x - y) <= tol * max(abs(x), abs(y), 1e-9) for y in big) for x in small)
