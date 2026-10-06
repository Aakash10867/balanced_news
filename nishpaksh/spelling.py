"""One spelling per name in an article, by code (Oct 7 2026: one page spelt a pilot's surname
Machhar, Machar, Matchar and Machchhar, and a co-pilot was Hamam and Hammam al-Hammami; the
consolidation model, asked for spelling variants, missed them).

Two capitalised words are one name spelt two ways when they reduce to the same key: lower case,
"tch" / "chh" / "chchh" read as "ch", "ee" as "i", "oo" as "u", doubled letters as one. The spelling
most reports use wins. Only words of four letters or more, and only exact keys: "Kumar" and
"Kumari" stay two names.
"""
from __future__ import annotations

import re
from collections import defaultdict

WORD = re.compile(r"(?<![\w-])[A-Z][a-z]{3,}(?![\w])|(?<=-)[A-Z][a-z]{3,}(?![\w])")


def key(word: str) -> str:
    w = word.lower()
    w = re.sub(r"(?:t?c+h+)+", "ch", w)
    w = w.replace("ee", "i").replace("oo", "u")
    return re.sub(r"(.)\1+", r"\1", w)


def name_map(texts: list[tuple[str, float]]) -> dict[str, str]:
    """{variant: spelling to use} from (text, weight) pairs; weight = how many reports carry the text."""
    weight: dict[str, float] = defaultdict(float)
    for text, w in texts:
        for m in WORD.findall(text or ""):
            weight[m] += w
    groups: dict[str, list[str]] = defaultdict(list)
    for word in weight:
        groups[key(word)].append(word)
    out = {}
    for words in groups.values():
        if len(words) < 2:
            continue
        best = max(words, key=lambda x: (weight[x], len(x), x))
        out.update({x: best for x in words if x != best})
    return out


def apply(text: str | None, names: dict[str, str]) -> str | None:
    if not text or not names:
        return text
    return re.sub(r"(?<![A-Za-z])(" + "|".join(map(re.escape, sorted(names, key=len, reverse=True))) + r")(?![a-z])",
                  lambda m: names[m.group(1)], text)


ITEM_LISTS = ("undated", "established", "contested", "context", "background")


def _items(payload: dict):
    for tier in payload.get("timeline") or []:
        yield from tier
    for k in ITEM_LISTS:
        yield from payload.get(k) or []


def _statement_texts(payload: dict) -> list[tuple[str, float]]:
    seen, texts = set(), []
    for i in _items(payload):
        if id(i) in seen:
            continue
        seen.add(id(i))
        w = float(i.get("n_articles") or 1)
        texts += [(i.get("text") or "", w), (i.get("speaker") or "", w)]
    return texts


def unify_payload(payload: dict) -> dict[str, str]:
    """Rewrite every statement (and speaker) of a story to one spelling per name."""
    names = name_map(_statement_texts(payload))
    if names:
        for i in {id(i): i for i in _items(payload)}.values():
            for f in ("text", "speaker"):
                if i.get(f):
                    i[f] = apply(i[f], names)
    return names


def unify_article(payload: dict) -> None:
    """The same for the article and headline once written: a spelling the writer made up yields to the
    statements' spelling (it weighs almost nothing)."""
    paras = (payload.get("narrative") or {}).get("paragraphs") or []
    written = [(payload.get("headline") or "", 0.01)] + [(s.get("text") or "", 0.01) for p in paras for s in p]
    names = name_map(_statement_texts(payload) + written)
    if not names:
        return
    payload["headline"] = apply(payload.get("headline"), names)
    for para in (payload.get("narrative") or {}).get("paragraphs") or []:
        for s in para:
            s["text"] = apply(s.get("text"), names)
