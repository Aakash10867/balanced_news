"""The day's brief (owner, Oct 10 2026, option B): what happened today, section by section, in short sentences.

Each article of the day gives its lead (the news code chose and the writer wrote, already checked and coloured).
For each section one page-tier call rewrites the section's leads as a brief: ONE sentence per article, shorter,
read in a row. The model may only shorten and join words; code checks every sentence against the lead it came
from (numbers, names, "not", speakers' verbs, cause words, hedge words, length) and a sentence that fails is
replaced by the lead's own first sentence. No new colour is worked out (owner: "just use the colour of the
sentences used"): a brief sentence takes the weakest colour of the lead sentences it cites. No answer from the
model = the leads themselves. Hindi: the brief's sentences are translated through the page translation cache;
an untranslated one is replaced by the Hindi lead's first sentence. No audio (owner, Oct 10 2026).
"""
from __future__ import annotations

import json
import logging
import re

from .categories import PRIMARY_LABELS, normalize
from .narrative import CAUSAL, HEDGE_MARKERS
from .router import QuotaExhausted, Router
from .voice import ACT, ACT_VERBS

log = logging.getLogger(__name__)
ORDER = ["politics", "justice", "business", "world", "life", "more"]
MORE = ("Also today", "और ख़बरें")
PER_CALL = 10
MAX_WORDS = 40
CLASS_RANK = {"established": 0, "developing": 1, "unverified": 2, "partial": 3, "single": 4, "disputed": 5, "false": 6}

PROMPT = """You write the {section} part of a newspaper's short brief of the day.

Below are the opening sentences of today's {n} articles in this section, each with its id.
Write ONE short sentence for EACH article (at most 25 words), so a reader gets the day's {section} news at a glance.

Rules:
- Use only what the article's sentences say. Keep every name, number and date you use exactly as written.
- Keep who said what: if a sentence says "police said" or "X alleged", keep that speaker and that verb.
- Keep "allegedly" and "not" where they are. Never add "reportedly", "according to reports", "because", "due to".
- Do not join two articles in one sentence. Do not add opinions or adjectives.
- If you are unsure how to shorten one, copy its first sentence.

Example:
a12: "The Reserve Bank of India on Wednesday kept the repo rate unchanged at 5.5 per cent, Governor Sanjay Malhotra said after the policy meeting."
-> {{"id": "a12", "text": "The RBI kept the repo rate unchanged at 5.5 per cent, Governor Sanjay Malhotra said."}}

Articles:
{articles}

Reply as JSON: {{"brief": [{{"id": "a12", "text": "..."}}, ...]}} in the same order as the articles."""


# -- code checks ---------------------------------------------------------------------------------------------
CAP = re.compile(r"\b[A-Z][\w'’-]+")
NEG = re.compile(r"(?i)\b(not|no|never|n't|without|nor)\b|n't\b")
COMMON = {"The", "A", "An", "In", "On", "At", "After", "Before", "He", "She", "They", "It", "This", "That", "These",
          "Those", "His", "Her", "Their", "Its", "Police", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
          "Saturday", "Sunday", "Meanwhile", "Also", "Separately", "While", "With", "From", "For", "As", "Of", "And", "But"}


# words of order and frequency are not figures (figures.py), but a brief sentence may no more add them than a number
ORDER_WORDS = {"first": "1st", "second": "2nd", "third": "3rd", "half": "half", "once": "once", "twice": "twice"}
SAYS = re.compile(r"(?i)\b(said|says|told|stated|according to|alleged|alleges|claimed|claims|announced|added)\b")


def _nums(s: str) -> set[str]:
    """The figures of a text by value, however written (figures.py: 26 = twenty-six = 26), and its words of
    order. Identifies only: the brief keeps the lead's own way of writing."""
    from . import figures
    return figures.canon_set(s) | {ORDER_WORDS[w] for w in re.findall(r"[a-z]+", s.lower()) if w in ORDER_WORDS}


def _initials(name: str, source: str) -> bool:
    """"RBI" for "Reserve Bank of India": an all-capitals short form of capitalised words in the lead."""
    if not (name.isupper() and 2 <= len(name) <= 6):
        return False
    words = [w for w in re.findall(r"[A-Za-z]+", source) if w.lower() not in ("of", "and", "for", "the", "&")]
    caps = "".join(w[0] if w[0].isupper() else " " for w in words)
    return name in caps


def problem(text: str, source: str) -> str | None:
    """Why a brief sentence may not stand for its lead, or None."""
    t = (text or "").strip()
    if not t or not t[0].isupper() or t[-1] not in ".?!\"'’”":
        return "not a full sentence"
    if len(t.split()) > MAX_WORDS:
        return "too long"
    if re.search(r"[ऀ-ॿ]", t):
        return "not English"
    low, src = t.lower(), source.lower()
    if not _nums(t) <= _nums(source):
        return "a number the lead does not have"
    if SAYS.search(source) and not SAYS.search(t):
        return "the speaker was left out"
    if bool(NEG.search(t)) != bool(NEG.search(source)):
        return "'not' lost or added"
    for w in HEDGE_MARKERS:
        if w in low and w not in src:
            return f"hedge word ({w})"
    for w in CAUSAL:
        if w in low and w not in src:
            return f"cause word ({w})"
    for m in ACT.finditer(t):
        root = m.group(1).lower()
        if not any(x in src for x in ACT_VERBS.get(root, (root,))):
            return f"verb the lead does not use ({m.group(0)})"
    if "alleg" in src and "alleg" not in low:
        return "'allegedly' lost"
    for name in CAP.findall(t):
        if name not in COMMON and name.lower() not in src and not _initials(name, source):
            return f"a name the lead does not have ({name})"
    return None


# -- the day's leads, by section -------------------------------------------------------------------------------
def _lead(payload: dict) -> list[dict]:
    paras = ((payload or {}).get("narrative") or {}).get("paragraphs") or []
    out = []
    for s in (paras[0] if paras else [])[:2]:
        item = {"t": s.get("text") or "", "c": s.get("class") or "unverified"}
        parts = s.get("parts") or []
        if len(parts) > 1:
            item["p"] = [{"t": x.get("text") or "", "c": x.get("class") or "unverified"} for x in parts]
        out.append(item)
    return [x for x in out if x["t"]]


def _weakest(classes: list[str]) -> str:
    return max(classes or ["unverified"], key=lambda c: CLASS_RANK.get(c, 2))


def group(rows: list[dict]) -> dict[str, list[dict]]:
    """section -> the day's articles, most outlets first. An article goes under its main section only; a thread
    told twice in the day is told once, by its newest article."""
    newest: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r["updated_at"]):
        pe = r["payload_en"] or {}
        thread = str(min([int(p["story_id"]) for p in pe.get("parents") or []
                          if isinstance(p, dict) and str(p.get("story_id", "")).isdigit()] or [r["story_id"]]))
        newest[thread] = r
    out: dict[str, list[dict]] = {}
    for r in newest.values():
        pe = r["payload_en"] or {}
        lead = _lead(pe)
        if not lead:
            continue
        prim = normalize(pe.get("category")).get("primary") or []
        sec = prim[0] if prim and prim[0] in PRIMARY_LABELS else "more"
        out.setdefault(sec, []).append({"id": r["story_id"], "lead": lead, "lead_hi": _lead(r["payload_hi"] or {}),
                                        "n": (pe.get("counts") or {}).get("independent_sources") or 0,
                                        "h": r["headline_en"], "h_hi": r["headline_hi"] or r["headline_en"]})
    for items in out.values():
        items.sort(key=lambda i: -i["n"])
    return out


def _fallback(item: dict) -> dict:
    first = item["lead"][0]
    return {**first, "id": item["id"], "from": "lead"}


def _brief_section(router: Router | None, label: str, items: list[dict], stats: dict) -> list[dict]:
    out = {i["id"]: None for i in items}
    for start in range(0, len(items), PER_CALL):
        chunk = items[start:start + PER_CALL]
        if router is None:
            break
        arts = "\n".join(f'a{i["id"]}: ' + json.dumps(" ".join(x["t"] for x in i["lead"]), ensure_ascii=False)
                         for i in chunk)
        try:
            res = router.call("page", PROMPT.format(section=label, n=len(chunk), articles=arts),
                              json_out=True, max_output_tokens=1500)
        except QuotaExhausted:
            stats["no_model"] += 1
            break
        except Exception as e:  # noqa: BLE001
            log.info("brief of %s not written: %s", label, e)
            stats["no_model"] += 1
            continue
        got = (res.data or {}).get("brief") if isinstance(res.data, dict) else None
        by_id = {i["id"]: i for i in chunk}
        for g in got or []:
            if not isinstance(g, dict):
                continue
            sid = str(g.get("id") or "").lstrip("a")
            item = by_id.get(int(sid)) if sid.isdigit() else None
            if not item or out.get(item["id"]):
                continue
            text = re.sub(r"\s+", " ", str(g.get("text") or "")).strip()
            source = " ".join(x["t"] for x in item["lead"])
            why = problem(text, source)
            if why:
                stats["refused"] += 1
                log.info("brief sentence refused (%s): %s", why, text[:120])
                continue
            # the colour of the lead sentences it uses: both, when it carries a word only the second has
            used = [x for k, x in enumerate(item["lead"]) if k == 0 or _uses(text, x["t"], item["lead"][0]["t"])]
            out[item["id"]] = {"t": text, "c": _weakest([p["c"] for x in used for p in (x.get("p") or [x])]),
                               "id": item["id"], "from": "brief"}
            stats["written"] += 1
    return [out[i["id"]] or _fallback(i) for i in items]


def _uses(text: str, second: str, first: str) -> bool:
    """The brief sentence carries something only the lead's second sentence has (a number or a name)."""
    only = _nums(second) - _nums(first)
    names = {w for w in CAP.findall(second) if w not in COMMON} - set(CAP.findall(first))
    return bool(only & _nums(text)) or any(w in text for w in names)


def build(store, router: Router | None, rows: list[dict], day: str) -> tuple[dict, dict, dict]:
    """(payload_en, payload_hi, stats) of the day's brief."""
    from .compose import translate_strings
    stats = {"articles": 0, "written": 0, "refused": 0, "no_model": 0}
    groups = group(rows)
    secs = []
    for key in ORDER:
        items = groups.get(key)
        if not items:
            continue
        label = PRIMARY_LABELS[key][0] if key in PRIMARY_LABELS else MORE[0]
        stats["articles"] += len(items)
        secs.append((key, items, _brief_section(router, label, items, stats)))
    strings = [s["t"] for _, _, sents in secs for s in sents if s["from"] == "brief"]
    tr = translate_strings(store, router, strings) if strings else {}
    en, hi = [], []
    for key, items, sents in secs:
        k_hi = PRIMARY_LABELS[key][1] if key in PRIMARY_LABELS else MORE[1]
        en.append({"key": key, "label": PRIMARY_LABELS[key][0] if key in PRIMARY_LABELS else MORE[0],
                   "sents": [{k: v for k, v in s.items() if k != "from"} for s in sents]})
        hs = []
        for item, s in zip(items, sents):
            t = tr.get(s["t"]) if s["from"] == "brief" else None
            if t:
                hs.append({"t": t, "c": s["c"], "id": item["id"]})
            else:                           # the Hindi lead's own first sentence (one colour: Hindi has no parts)
                x = (item["lead_hi"] or item["lead"])[0]
                hs.append({"t": x["t"], "c": x["c"], "id": item["id"]})
        hi.append({"key": key, "label": k_hi, "sents": hs})
    stories = [{"id": i["id"], "h": i["h"]} for _, items, _ in secs for i in items]
    stories_hi = [{"id": i["id"], "h": i["h_hi"]} for _, items, _ in secs for i in items]
    return ({"day": day, "kind": "brief", "sections": en, "stories": stories},
            {"day": day, "kind": "brief", "sections": hi, "stories": stories_hi}, stats)
