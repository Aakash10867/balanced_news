"""The readable story: one continuous news article written from the checked statements.

Division of labour:
  code   decides which statements exist, their verdicts, who makes each claim, and time order
  model  writes them as a news article: an opening that says who, where and what, then events in
         time order, then the investigation and each side's response, then where accounts differ
  code   validates every sentence and colours it by the weakest statement it cites

Attribution (decided with the reader in mind): outlet names never appear in the text; the colour
and the numbered source links already say who reported what. A claim is pinned on the person or
body that makes it ("his parents alleged", "police said"). Anything not established that has no
such speaker carries a light hedge, at most once per paragraph ("reports said").

A sentence is rejected (and replaced by plain wording) if it cites nothing valid, names an outlet,
contains a number not in the statements it cites, uses a loaded word any outlet used, states an
allegation without naming who makes it, or uses a false statement without saying it is false.
Statements no valid sentence covers are added in plain words, so nothing is silently dropped.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re

from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)
WRITER_VERSION = 2   # part of the cache key: pages written by an older writer are rewritten once

RANK = {"confirmed": 0, "corroborated": 0, "developing": 1, "unverified": 2, "pending": 2, "disputed": 3, "false": 4}
CLASS = {0: "established", 1: "developing", 2: "unverified", 3: "disputed", 4: "false"}
STATUS_LABEL = {"corroborated": "ESTABLISHED", "confirmed": "ESTABLISHED", "developing": "REPORTED",
                "disputed": "DISPUTED", "unverified": "REPORTED", "pending": "REPORTED", "false": "FALSE"}

WRITER_PROMPT = """You are a senior news editor. Write the story below as ONE news article for ordinary
readers, the way a good newspaper reports it: clear, calm, flowing paragraphs. Not a list.

You may use ONLY the statements given. Each has an id and a status, may say who makes it ("said by"),
and may say when it happened.

Structure:
1. Opening paragraph (1-2 sentences): who, where, what happened, and when, from the most important
   statements.
2. Then what happened, in time order.
3. Then the investigation, official actions and each side's response.
4. Last paragraph: where accounts differ, if they do.
Group related statements into paragraphs of 2-4 sentences. Combine statements into one sentence where
natural. Say each fact ONCE: if two statements say the same thing, write it once and cite both ids.

Attribution rules (important):
- NEVER name a newspaper, channel or website. Do not write "X reported", "according to X" for an outlet.
- ESTABLISHED: state plainly as fact.
- A statement with "said by": attribute it to that person or body ("his parents alleged that...",
  "police said...", "the professor denied..."). An accusation must always name who makes it.
- REPORTED without "said by": not confirmed. Use a light hedge, at most once per paragraph, e.g.
  "reportedly", "according to reports", "reports said". Never state it as settled fact.
- DISPUTED: say accounts differ and give both versions ("Accounts differ: some reports put the number
  at 40, others at 50.").
- FALSE: say who claimed it and that the evidence shows it is false, citing the evidence given.

Never add any fact, name, number, place, cause, motive, adjective or opinion that is not in the
statements. Do not link two events by cause unless a statement says so. No headings, no bullet points.
Never use any of these words: {banned}
Every sentence lists in "ids" every statement it uses. Use every statement at least once.

Statements, events in time order first:
{statements}

Reply with JSON only:
{{"paragraphs": [[{{"text": "...", "ids": [3, 7]}}, {{"text": "...", "ids": [5]}}], [ ... ]]}}"""

FALSE_MARKERS = ("false", "untrue", "not true", "contradict", "evidence shows", "disproved", "incorrect",
                 "no evidence", "refut")
HEDGE_MARKERS = ("reportedly", "according to report", "reports said", "reports say", "reported", "it is said",
                 "was said to", "were said to", "accounts differ", "some reports", "according to early",
                 "unconfirmed", "allegedly")
ATTRIBUTION_VERBS = ("said", "say", "says", "alleg", "claim", "accus", "denied", "deny", "denies", "demand",
                     "told", "stated", "according to", "maintain", "insist", "assert")
DISPUTE_MARKERS = ("differ", "disput", "contradict", "others", "while", "however", "but ", "conflicting",
                   "versions", "other reports")


def _word_set(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-zऀ-ॿ]{4,}", (s or "").lower())}


def _soft_lower(text: str) -> str:
    """Lower-case only a leading article ('The police' -> 'the police'); names stay as they are."""
    m = re.match(r"(The|A|An)\s", text)
    return text[0].lower() + text[1:] if m else text


def plain_sentence(item: dict) -> str:
    """Deterministic fallback wording for one statement, under the same attribution rules."""
    text = item["text"].strip().rstrip(".")
    v = item["verdict"]
    speaker = item.get("speaker")
    if v in ("corroborated", "confirmed"):
        return text + "."
    if v == "false":
        why = (item.get("check") or {}).get("reasons") or []
        who = f"{speaker} claimed" if speaker else "It was claimed"
        tail = f" The evidence shows this is false: {why[0].rstrip('.')}." if why else " The evidence shows this is false."
        return f"{who} that {_soft_lower(text)}.{tail}"
    if v == "disputed":
        return f"Accounts differ on this: {_soft_lower(text) if speaker is None else text}, according to some reports."
    if speaker:
        return f"According to {speaker}, {_soft_lower(text)}."
    return f"{text}, reports said."


def _time_key(i: dict):
    start = (i.get("time") or {}).get("start")
    try:
        return dt.datetime.fromisoformat(start) if start else dt.datetime.max
    except ValueError:
        return dt.datetime.max


def ordered_items(p: dict) -> list[dict]:
    """Every statement in the story, in a deterministic reading order: established events in
    timeline order, other events by their reported time, then claims from strongest to weakest.
    Stated links between events ("A, and after that B", "B because A") are not separate statements
    in the article: they order the events, and repeating them only duplicated facts."""
    seen, out = set(), []

    def add(items):
        for i in items:
            if i["id"] not in seen and i["kind"] != "relation":
                seen.add(i["id"])
                out.append(i)

    add([i for tier in p["timeline"] for i in tier])
    add(p["undated"])
    contested = p["contested"]
    add(sorted([i for i in contested if i["kind"] == "event"], key=lambda i: (_time_key(i), -i["n_articles"])))
    add(p["established"])
    order = {"false": 0, "disputed": 1}
    add(sorted([i for i in contested if i["kind"] != "event"],
               key=lambda i: (i["minor"], order.get(i["verdict"], 2), -i["n_articles"])))
    return out


def sections_from_payload(p: dict) -> dict[str, list[dict]]:  # kept for the cache key
    return {"story": ordered_items(p)}


def _statement_line(i: dict) -> str:
    line = f'#{i["id"]} {STATUS_LABEL.get(i["verdict"], "REPORTED")} | "{i["text"]}"'
    if i.get("speaker"):
        line += f" | said by: {i['speaker']}"
    if i["verdict"] == "false" and i.get("check"):
        line += f" | evidence: {'; '.join(i['check'].get('reasons') or [])}"
    when = (i.get("time") or {}).get("when_text")
    if when:
        line += f" | when: {when}"
    return line


def _numbers(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", s))


def _validate(sentence: dict, by_id: dict[int, dict], banned: set[str], outlets: list[str]) -> list[int] | None:
    text = str(sentence.get("text") or "").strip()
    try:
        ids = [int(x) for x in sentence.get("ids") or []]
    except (TypeError, ValueError):
        return None
    ids = [i for i in dict.fromkeys(ids) if i in by_id]
    if not text or not ids or len(text) > 700:
        return None
    low = text.lower()
    if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low) for w in banned):
        return None
    # outlet names never appear in the article (the source links carry them)
    if any(re.search(rf"(?<!\w){re.escape(o)}(?!\w)", text) for o in outlets if len(o) >= 3):
        return None
    source_text = " ".join(by_id[i]["text"] + " " + ((by_id[i].get("time") or {}).get("when_text") or "")
                           + " " + " ".join(by_id[i]["check"]["reasons"] if by_id[i].get("check") else [])
                           for i in ids)
    if not _numbers(text) <= _numbers(source_text):
        return None
    verdicts = {by_id[i]["verdict"] for i in ids}
    if "false" in verdicts and not any(m in low for m in FALSE_MARKERS):
        return None
    # an accusation or claim with a known speaker must name who makes it (or say it is alleged)
    for i in ids:
        sp = by_id[i].get("speaker")
        if sp and by_id[i]["verdict"] not in ("corroborated", "confirmed"):
            if not (_word_set(sp) & _word_set(text)) and "alleg" not in low:
                return None
    if "disputed" in verdicts and not any(m in low for m in DISPUTE_MARKERS + ATTRIBUTION_VERBS):
        return None
    return ids


def _needs_hedge(sent: dict, by_id: dict[int, dict]) -> bool:
    """Not established and not pinned on a speaker: the paragraph must say it is only reported."""
    return any(by_id[x]["verdict"] not in ("corroborated", "confirmed") and not by_id[x].get("speaker")
               and by_id[x]["verdict"] != "disputed" for x in sent["ids"])


def _hedge(text: str) -> str:
    return re.sub(r"[.!]?\s*$", "", text) + ", reports said."


def input_hash(sections: dict[str, list[dict]]) -> str:
    key = {k: [(i["id"], i["verdict"], i["text"], i.get("speaker"), sorted(s["url"] for s in i["sources"]))
               for i in v] for k, v in sections.items()}
    key["_writer"] = WRITER_VERSION
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def write_narrative(router: Router | None, payload: dict, banned: set[str]) -> dict:
    items = ordered_items(payload)
    by_id = {i["id"]: i for i in items}
    outlets = sorted({s["outlet"] for s in payload["sources"] if s.get("outlet")}, key=len, reverse=True)

    drafted: list[list[dict]] = []
    model = None
    if router is not None and items:
        prompt = WRITER_PROMPT.format(banned=", ".join(sorted(banned)) or "(none)",
                                      statements="\n".join(_statement_line(i) for i in items))
        try:
            res = router.call("writer", prompt, json_out=True, max_output_tokens=6000)
            model = res.model
            paras = (res.data or {}).get("paragraphs") if isinstance(res.data, dict) else None
            if isinstance(paras, list):
                drafted = [p for p in paras if isinstance(p, list)]
        except (QuotaExhausted, ValueError) as e:
            log.info("narrative: writer unavailable (%s); plain sentences used", e)
        except Exception as e:  # noqa: BLE001
            log.warning("narrative failed: %s", e)

    paragraphs, covered, rejected = [], set(), 0
    for para in drafted:
        out = []
        for s in para:
            ids = _validate(s, by_id, banned, outlets) if isinstance(s, dict) else None
            if ids is None:
                rejected += 1
                # keep the paragraph whole: the statements of a rejected sentence go in plainly here
                for x in (s.get("ids") or []) if isinstance(s, dict) else []:
                    try:
                        x = int(x)
                    except (TypeError, ValueError):
                        continue
                    if x in by_id and x not in covered:
                        out.append({"text": plain_sentence(by_id[x]), "ids": [x], "plain": True})
                        covered.add(x)
                continue
            covered.update(ids)
            out.append({"text": re.sub(r"\.{2,}$", ".", s["text"].strip()), "ids": ids})
        if out:
            paragraphs.append(out)

    missing = [i for i in items if i["id"] not in covered]
    if missing:  # in reading order, a few sentences per paragraph
        chunk = []
        for i in missing:
            chunk.append({"text": plain_sentence(i), "ids": [i["id"]], "plain": True})
            if len(chunk) == 4:
                paragraphs.append(chunk)
                chunk = []
        if chunk:
            paragraphs.append(chunk)

    # a paragraph with unconfirmed, unattributed sentences must say somewhere that they are reports
    for para in paragraphs:
        if any(_needs_hedge(x, by_id) for x in para) and not any(
                m in x["text"].lower() for x in para for m in HEDGE_MARKERS):
            first = next(x for x in para if _needs_hedge(x, by_id))
            first["text"] = _hedge(first["text"])

    # source numbers in order of first appearance in the essay, then everything else read
    numbering: dict[str, int] = {}
    for para in paragraphs:
        for sent in para:
            for x in sent["ids"]:
                for s in by_id[x]["sources"]:
                    numbering.setdefault(s["url"], len(numbering) + 1)
    for s in payload["sources"]:
        numbering.setdefault(s["url"], len(numbering) + 1)
    for para in paragraphs:
        for sent in para:
            sent["class"] = CLASS[max(RANK.get(by_id[x]["verdict"], 2) for x in sent["ids"])]
            sent["sources"] = sorted({numbering[s["url"]] for x in sent["ids"] for s in by_id[x]["sources"]})

    src_meta = {s["url"]: s for s in payload["sources"]}
    source_list = [{"n": n, "url": url, "outlet": src_meta.get(url, {}).get("outlet", ""),
                    "title": src_meta.get(url, {}).get("title", ""),
                    "perspective": src_meta.get(url, {}).get("perspective", "–"),
                    "read": src_meta.get(url, {}).get("read", True),
                    "readable": src_meta.get(url, {}).get("readable", True)}
                   for url, n in sorted(numbering.items(), key=lambda kv: kv[1])]
    if rejected:
        log.info("narrative: %d sentences failed checks and were replaced with plain wording", rejected)
    return {"hash": input_hash(sections_from_payload(payload)), "model": model, "rejected": rejected,
            "paragraphs": paragraphs, "sources": source_list}
