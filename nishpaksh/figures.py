"""Figures: finding the numbers in a text, whatever way the outlet wrote them (owner, Oct 11 2026).

"26", "twenty-six", "twenty six", "२६" are one figure. Before this module four places read numbers on their
own and none of them read a spelled number as a whole: "twenty-six" became 20 and 6, so a line saying "26
were killed" and one saying "twenty-six were killed" were two different facts, the pair was offered as a
possible dispute, and the writer put all three spellings into one sentence. The writer's own figures were
only checked when written in digits.

THIS MODULE ONLY IDENTIFIES. It never changes a text: an outlet that wrote "twenty-six", "26" or "Section
IV" meant it that way, and the article keeps the writer's words. Two texts are compared by VALUE; each is
printed as it was written.

What is read (`find`, `values`, `value_set`):
  digits       26   1,20,000   1,200.50   25%   Devanagari and Arabic-Indic digits (२६)
  scales       1.2 lakh   5 crore   2 crore   50 cr   12 mn   1.5 lakh crore   25 basis points (= 0.25 per cent)
  ordinals     29th   (digits only: "first", "second" are words of order, not figures)
  words        twenty-six   twenty six   one hundred and five   two lakh fifty thousand   twelve thousand crore
               a dozen   two dozen   hundred   (a bare scale word is its scale: "lakh" = 100000)
  roman        only after a word that is followed by a numeral ("Section IV", "Phase II", "World War II",
               "Class X"), and only I, V and X up to 39: "Group C", "Group D", "Type M" are labels, not 100,
               500 or 1000
Not read: "twenty-sixth", "two-thirds", "one-third" (an order or a fraction, not the count they start with);
"nineteen ninety-nine" and "twenty twenty-six" (spoken years); words of order (first, second); "half", "once".

`spoken_min`: when set, a spelled number below it is not returned ("one of the accused", "no one" and "one
more" are not figures). Used when a WRITTEN sentence is checked against its sources: a sentence may say "one"
without the sources having the figure 1, but it may not say "two" when they say 3.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SPOKEN_MIN = 2      # a WRITTEN sentence's spelled numbers below this are not checked ("no one", "one of")

DIGIT_MAP = str.maketrans("०१२३४५६७८९٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
        "ninety": 90}
SCALES = {"thousand": 1e3, "lakh": 1e5, "lac": 1e5, "million": 1e6, "crore": 1e7, "billion": 1e9,
          "trillion": 1e12}
# after digits only ("5 cr", "12 mn", "3k"); plural forms too ("20 lakhs", "5 crores")
DIGIT_SCALES = {**SCALES, "lakhs": 1e5, "lacs": 1e5, "crores": 1e7, "millions": 1e6, "billions": 1e9,
                "k": 1e3, "mn": 1e6, "mln": 1e6, "bn": 1e9, "cr": 1e7}
# a number followed by one of these is an order or a fraction, not a count: "twenty-sixth", "two-thirds"
ORDER_OR_FRACTION = {
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh",
    "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
    "twentieth", "thirtieth", "fortieth", "fiftieth", "sixtieth", "seventieth", "eightieth", "ninetieth",
    "hundredth", "thousandth", "millionth", "thirds", "fourths", "fifths", "sixths", "sevenths", "eighths",
    "ninths", "tenths", "halves", "half", "quarter", "quarters"}

# words a spelled number can start with ("cr", "mn", "lakhs" only follow digits)
WORD_START = set(UNITS) | set(TEENS) | set(TENS) | set(SCALES) | {"hundred", "dozen"}
# every word that is part of a spelled number: a text's "root words" leave them out so that "twenty-six crore"
# and "26 crore" have the same words as well as the same value
NUMBER_WORDS = (set(UNITS) | set(TEENS) | set(TENS) | set(DIGIT_SCALES) | {"hundred", "dozen"})

_BPS = r"basis\s+points?|bps"
_DIGITS = re.compile(
    r"(\d+(?:[.,]\d+)*)"                                                  # 1,20,000   1.5   26
    r"(?:(?P<ord>st|nd|rd|th)\b"                                          # 29th
    r"|\s*(?P<bps>" + _BPS + r")\b"                                       # 25 basis points
    r"|(?P<scales>(?:\s*(?:" + "|".join(sorted(DIGIT_SCALES, key=len, reverse=True)) + r")\b)+))?",
    re.I)
_ROMAN_TRIGGER = (r"(?i:world\s+war|section|phase|class|part|chapter|article|schedule|stage|type|level|mark|volume|"
                  r"vol|round|grade|group|tier|division|category|zone|unit|rule|form|season|series|generation|war)")
_ROMAN = re.compile(r"\b" + _ROMAN_TRIGGER + r"\s+(?=[IVX])((?:XXX|XX|X)?(?:IX|IV|V?I{0,3}))\b")
_ROMAN_VALUE = {"I": 1, "V": 5, "X": 10}


@dataclass(frozen=True)
class Figure:
    start: int
    end: int
    text: str           # exactly as written
    value: float
    kind: str           # digits | ordinal | words | roman


def _roman(s: str) -> int | None:
    total = 0
    for k, ch in enumerate(s):
        v = _ROMAN_VALUE[ch]
        total += -v if k + 1 < len(s) and _ROMAN_VALUE[s[k + 1]] > v else v
    return total or None


def _digit_figures(t: str) -> list[Figure]:
    out = []
    for m in _DIGITS.finditer(t):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        kind = "digits"
        if m.group("ord"):
            kind = "ordinal"
        elif m.group("bps"):
            v /= 100                      # 25 basis points is 0.25 per cent: one figure
        elif m.group("scales"):
            for w in re.findall(r"[a-z]+", m.group("scales").lower()):
                v *= DIGIT_SCALES[w]
        out.append(Figure(m.start(), m.end(), m.group(0), v, kind))
    return out


def _small(w: str) -> bool:
    return w in UNITS or w in TEENS or w in TENS


def _word_runs(low: str, skip: list[tuple[int, int]] = ()) -> list[tuple[int, int, float]]:
    """(start, end, value) of each spelled number. `low` is lower case; words inside `skip` spans (the scale
    words of a digit figure: "1.2 lakh") belong to that figure and start or join no run."""
    toks = [(m.start(), m.end(), m.group()) for m in re.finditer(r"[a-z]+", low)
            if not any(a <= m.start() < b for a, b in skip)]
    runs: list[tuple[int, int, float]] = []
    i = 0
    while i < len(toks):
        if toks[i][2] not in WORD_START:
            i += 1
            continue
        total, cur, last_scale, k = 0.0, 0, None, i
        while k < len(toks):
            s, _, w = toks[k]
            if k > i:
                gap = low[toks[k - 1][1]:s]
                if w == "and":      # "one hundred and five": only between a hundred / scale and a smaller number
                    nxt = toks[k + 1] if k + 1 < len(toks) else None
                    prev = toks[k - 1][2]
                    if (nxt and _small(nxt[2]) and (prev == "hundred" or prev in SCALES)
                            and re.fullmatch(r"\s+", gap) and re.fullmatch(r"\s+", low[toks[k][1]:nxt[0]])):
                        k += 1
                        continue
                    break
                if not re.fullmatch(r"[\s-]+", gap):
                    break
            if w in UNITS:
                if not (cur % 100 == 0 or (cur % 100 >= 20 and cur % 10 == 0)):
                    break
                cur += UNITS[w]
            elif w in TEENS or w in TENS:
                if cur % 100 != 0:
                    break
                cur += TEENS.get(w) or TENS[w]
            elif w == "hundred":
                if cur == 0 and k == i:
                    cur = 100
                elif 0 < cur < 100:
                    cur *= 100
                else:
                    break
            elif w == "dozen":
                if (cur == 0 and k == i) or 0 < cur < 100:
                    total += (cur or 1) * 12
                    cur = 0
                else:
                    break
            elif w in SCALES:
                p = SCALES[w]
                if cur == 0 and k > i and not (total > 0 and last_scale is not None and p > last_scale):
                    break
                if last_scale is None or p < last_scale:
                    total += (cur or 1) * p
                elif p > last_scale:
                    total = (total + cur) * p       # "twelve thousand crore", "one lakh crore"
                else:
                    break
                cur, last_scale = 0, p
            else:
                break
            k += 1
        if k == i:                      # the first word did not fit by itself (cannot happen, kept for safety)
            i += 1
            continue
        end_tok = toks[k - 1]
        # a trailing "and" is not part of the run (handled above), so the run ends at its last number word
        nxt = toks[k] if k < len(toks) else None
        if nxt and nxt[2] in ORDER_OR_FRACTION and re.fullmatch(r"[\s-]+", low[end_tok[1]:nxt[0]]):
            i = k + 1                   # "twenty-sixth", "two-thirds": an order or a fraction, not a count
            continue
        runs.append((toks[i][0], end_tok[1], total + cur))
        i = k
    # a spoken year ("nineteen ninety-nine", "twenty twenty-six") is two runs side by side: not a figure
    out, n = [], 0
    while n < len(runs):
        if (n + 1 < len(runs) and runs[n][2] in (19, 20) and 10 <= runs[n + 1][2] <= 99
                and re.fullmatch(r"\s+", low[runs[n][1]:runs[n + 1][0]])):
            n += 2
            continue
        out.append(runs[n])
        n += 1
    return out


def find(text: str | None, spoken_min: float | None = None) -> list[Figure]:
    """Every figure in `text`, in order, as written (`Figure.text`) and as a value (`Figure.value`)."""
    t = (text or "").translate(DIGIT_MAP)
    out = _digit_figures(t)
    taken = [(f.start, f.end) for f in out]
    for s, e, v in _word_runs(t.lower(), taken):
        if spoken_min is not None and v < spoken_min:
            continue
        out.append(Figure(s, e, t[s:e], v, "words"))
    for m in _ROMAN.finditer(t):
        if m.group(1) and (v := _roman(m.group(1))):
            out.append(Figure(m.start(1), m.end(1), m.group(1), float(v), "roman"))
    return sorted(out, key=lambda f: f.start)


def values(text: str | None, spoken_min: float | None = None) -> list[float]:
    return [f.value for f in find(text, spoken_min)]


def value_set(text: str | None, spoken_min: float | None = None) -> frozenset[float]:
    return frozenset(round(f.value, 6) for f in find(text, spoken_min))


def canon(value: float) -> str:
    """One string per value ("26", "26.5", "120000"): for matching a figure against text written any way."""
    v = round(float(value), 6)
    return str(int(v)) if v == int(v) else repr(v)


def canon_set(text: str | None, spoken_min: float | None = None) -> set[str]:
    return {canon(v) for v in value_set(text, spoken_min)}
