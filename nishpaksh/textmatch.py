"""Is a statement's substance in an article's text? Used to tell an outlet's omission from our
reader's miss: a fact counts as left out only when its names and numbers are absent from the text.

Statements are stored in English, also for Hindi articles, so names are compared by a rough
consonant skeleton that works across Roman and Devanagari spelling ("Jaishankar" ~ "जयशंकर").
Matching is deliberately lenient: a false "present" only drops one omission from the evidence,
while a false "absent" would invent one.
"""
from __future__ import annotations

import re

DEVA_CONS = {
    "क": "k", "ख": "k", "ग": "g", "घ": "g", "ङ": "n", "च": "c", "छ": "c", "ज": "j", "झ": "j", "ञ": "n",
    "ट": "t", "ठ": "t", "ड": "d", "ढ": "d", "ण": "n", "त": "t", "थ": "t", "द": "d", "ध": "d", "न": "n",
    "प": "p", "फ": "f", "ब": "b", "भ": "b", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "s",
    "ष": "s", "स": "s", "ह": "", "क़": "k", "ख़": "k", "ग़": "g", "ज़": "j", "ड़": "r", "ढ़": "r", "फ़": "f",
    "ं": "n", "ँ": "n",
}
DEVA_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
STOP = {"the", "this", "that", "with", "from", "after", "said", "says", "will", "were", "have", "been", "also",
        "their", "they", "into", "over", "under", "about", "against", "minister", "government", "police",
        "court", "state", "india", "indian", "union", "chief", "president", "prime", "party", "national"}


def skeleton(word: str) -> str:
    """Consonant skeleton of a Roman or Devanagari word."""
    w = word.lower()
    if re.search(r"[ऀ-ॿ]", w):
        w = "".join(DEVA_CONS.get(ch, "") for ch in w)
    else:
        for a, b in (("ph", "f"), ("bh", "b"), ("kh", "k"), ("gh", "g"), ("ch", "c"), ("jh", "j"), ("th", "t"),
                     ("dh", "d"), ("sh", "s"), ("w", "v"), ("z", "j"), ("q", "k"), ("x", "ks")):
            w = w.replace(a, b)
        w = re.sub(r"[^a-z]", "", w)
        w = re.sub(r"[aeiouhy]", "", w)
    w = w.replace("y", "")
    return re.sub(r"(.)\1+", r"\1", w)


def key_tokens(statement: str) -> list[str]:
    """Numbers and proper names: what any report of this fact would have to contain."""
    from . import figures       # a figure is the same whether the statement or the outlet wrote 26 or twenty-six
    nums = [figures.canon(f.value) for f in figures.find(statement, spoken_min=figures.SPOKEN_MIN)]
    words = re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", statement)
    names = [w for i, w in enumerate(words) if w.lower() not in STOP]
    return list(dict.fromkeys(nums + names))


def _close(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < 4 or abs(len(a) - len(b)) > 1:
        return False
    # one edit apart
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    s, l = (a, b) if len(a) < len(b) else (b, a)
    return any(l[:i] + l[i + 1:] == s for i in range(len(l)))


class Text:
    def __init__(self, text: str):
        t = (text or "").translate(DEVA_DIGITS)
        self.lower = t.lower()
        from . import figures
        self.nums = figures.canon_set(t)
        self.skels = {skeleton(w) for w in re.findall(r"[A-Za-zऀ-ॿ]{3,}", t)}
        self.skels.discard("")

    def has(self, token: str) -> bool:
        if re.fullmatch(r"\d+(?:\.\d+)?", token):     # a figure (figures.canon): found by value, as written in the text
            return token in self.nums
        if token.lower() in self.lower:
            return True
        s = skeleton(token)
        return len(s) >= 2 and any(_close(s, k) for k in self.skels)


def verdict(statement: str, text: Text) -> str | None:
    """'present', 'absent', or None when the statement has too little that can be checked."""
    toks = key_tokens(statement)
    if len(toks) < 2:
        return None
    found = sum(1 for t in toks if text.has(t))
    return "absent" if found * 2 < len(toks) else "present"


# ------------------------------------------------------------------ a named person must be in the report
# Oct 9 2026, story 16197: a Hindi report names Chief Justice सूर्यकांत (Surya Kant); reading wrote "Chief Justice
# Sanjiv Khanna (referred to as Chief Justice Suryakant in the text)", a name from the model's own, out-of-date
# memory. A statement naming a titled person the report does not name is not the report's.
PERSON_TITLE = (r"(?:Chief Justice|Justice|Judge|Minister|Mr|Mrs|Ms|Dr|Shri|Smt|Sri|Governor|President|MLA|MP|DGP|CM|"
                r"Inspector|Sub-Inspector|General|Professor|Prof|Advocate|Mayor|Ambassador|Senator|Collector|SP|SSP)")
TITLED = re.compile(rf"\b{PERSON_TITLE}\.?\s+((?:[A-Z][a-zA-Z'’-]+\.?\s+){{0,3}}[A-Z][a-zA-Z'’-]{{2,}})")
NOT_A_NAME = {"Rules", "Rule", "Appointment", "Act", "Bill", "Case", "Order", "Policy", "Scheme", "Report", "Office",
              "House", "Court", "Bench", "Ministry", "Police", "Government", "Department", "Commission", "Council",
              "Board", "Committee", "Party", "Election", "Secretariat", "Residence", "Camp", "Award", "Medal",
              "Secretary", "Director", "Assembly", "Head", "Commissioner", "Spokesperson", "Chairman", "Chairperson", "Officer"}
DEVA_VOWEL = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "e", "ो": "o", "ौ": "o",
              "ं": "n", "ँ": "n", "ः": "", "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ए": "e",
              "ऐ": "e", "ओ": "o", "औ": "o", "ऋ": "ri"}
DEVA_ROMAN = {**DEVA_CONS, "च": "c", "छ": "c", "ड़": "d", "ढ़": "d", "ह": "h", "श": "s", "ष": "s", "व": "v", "य": "y"}


def _roman(word: str) -> str:
    """A name written with its vowels, in one rough Roman form for both scripts ("Khanna" -> "kana",
    "सूर्यकांत" -> "suryakant", "Gowda" / "गौड़ा" -> "goda"); h dropped, w = v, doubled letters single, a final
    a / e dropped."""
    w = word.lower()
    if re.search(r"[ऀ-ॿ]", w):
        out = ""
        for k, ch in enumerate(w):
            if ch in DEVA_VOWEL:
                if ch in "ािीुूृेैोौ" and out.endswith("a"):
                    out = out[:-1]
                out += DEVA_VOWEL[ch]
            elif ch == "्":
                out = out[:-1] if out.endswith("a") else out
            elif ch in DEVA_ROMAN or ch == "़":
                if ch == "़":
                    continue
                out += DEVA_ROMAN[ch] + "a"
        w = out
    else:
        w = re.sub(r"['’]s$", "", w)
        w = re.sub(r"c(?=[eiy])", "s", w)
        for a, b in (("ph", "f"), ("chh", "C"), ("ch", "C"), ("sh", "s"), ("ee", "i"), ("oo", "u"), ("ou", "o"),
                     ("au", "o"), ("ai", "e"), ("z", "j"), ("q", "k"), ("x", "ks"), ("ck", "k"), ("c", "k"),
                     ("C", "c"), ("w", "v"), ("ng", "n")):
            w = w.replace(a, b)
        w = re.sub(r"[^a-z]", "", w)
    w = w.replace("h", "").replace("ng", "n").replace("ri", "r")
    w = re.sub(r"(.)\1+", r"\1", w)
    return re.sub(r"[ae]$", "", w)


def _like(a: str, b: str) -> bool:
    from difflib import SequenceMatcher
    if a == b:
        return True
    ca, cb = re.sub(r"[aeiou]", "", a), re.sub(r"[aeiou]", "", b)
    if len(ca) >= 3 and ca == cb:
        return True                     # the same consonants: vowels differ between scripts ("bagci" / "bagaci")
    if min(len(a), len(b)) < 3 or abs(len(a) - len(b)) > 1:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.75


def absent_people(statement: str, source: str) -> list[str]:
    """Titled people ("Chief Justice Sanjiv Khanna") a statement names whose surname the report does not have,
    in any spelling or script. Lenient: a near spelling counts as present."""
    out = []
    low = (source or "").lower()
    loose = None
    for m in TITLED.finditer(statement or ""):
        words = [re.sub(r"['’]s$", "", w.rstrip(".-")) for w in m.group(1).split()]
        if any(w in NOT_A_NAME for w in words):
            continue
        sur = words[-1]
        if len(sur) < 3 or sur.lower() in low or re.fullmatch(PERSON_TITLE, sur):
            continue
        if loose is None:
            ws = re.findall(r"[A-Za-zऀ-ॿ]{2,}", (source or "").translate(DEVA_DIGITS))
            loose = [_roman(w) for w in ws]
            # a name written as one word in one script and two in the other ("Suryakant" / "सूर्य कांत")
            loose += [loose[k] + loose[k + 1] for k in range(len(loose) - 1)]
            loose = {x for x in loose if x}
        r, whole = _roman(sur), _roman("".join(words))     # "Surya Kant" / "सूर्यकांत"
        if len(r) < 2 or any(_like(r, k) or _like(whole, k) for k in loose):
            continue
        out.append(m.group(0))
    return out
