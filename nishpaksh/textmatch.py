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
    nums = re.findall(r"\d+(?:\.\d+)?", statement)
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
        self.nums = set(re.findall(r"\d+(?:\.\d+)?", t))
        self.skels = {skeleton(w) for w in re.findall(r"[A-Za-zऀ-ॿ]{3,}", t)}
        self.skels.discard("")

    def has(self, token: str) -> bool:
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
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
