"""The readable story: one continuous essay written from the checked statements.

Division of labour:
  code   decides which statements exist, their verdicts, and the order of events in time
  model  writes them as a coherent essay in paragraphs, citing the statements each sentence uses
  code   validates every sentence and colours it by the weakest statement it cites

A sentence is rejected (and replaced by plain wording) if it cites nothing valid, contains a
number that is not in the statements it cites, uses any loaded word that any outlet used in this
story, presents something that is not established without attributing it, or uses a false
statement without saying it is false. Statements no valid sentence covers are added in plain
words in a closing paragraph, so nothing is silently dropped.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re

from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

RANK = {"confirmed": 0, "corroborated": 0, "unverified": 1, "pending": 1, "disputed": 2, "false": 3}
CLASS = {0: "established", 1: "unverified", 2: "disputed", 3: "false"}
STATUS_LABEL = {"corroborated": "ESTABLISHED", "confirmed": "ESTABLISHED", "disputed": "DISPUTED",
                "unverified": "ONE-SIDED", "pending": "ONE-SIDED", "false": "FALSE"}

WRITER_PROMPT = """You are an experienced news editor. Write the story below as ONE coherent, readable news
essay for ordinary readers, the way a good newspaper feature reads: clear paragraphs, natural flow,
varied sentences. It must read like a story, not a list.

You may use ONLY the statements given. Each has an id, a status and the outlets behind it.

How to write:
1. Open with what is established. Then tell how events unfolded, in the time order given. Bring in
   claims, numbers and background where they fit the flow. End with where accounts differ, if they do.
2. Group related statements into paragraphs of 2-5 sentences. Combine statements into one sentence
   where natural, and use plain connecting words (then, later, by evening, meanwhile, however).
3. Never add any fact, name, number, place, cause, motive, adjective or opinion that is not in the
   statements. No conclusion, no commentary, no headings, no bullet points.
4. By status:
   ESTABLISHED - state plainly as fact.
   ONE-SIDED   - always attribute it to the outlets that report it ("Times of India reported that...",
                 "according to India Today..."). Never state it as fact. Vary the wording of attribution.
   DISPUTED    - give both sides with attribution ("X reported ...; Y's account differs: ...").
   FALSE       - say who claimed it, and that the evidence shows it is false, citing the evidence given.
5. Neutral words only. Never use any of these words: {banned}
6. Every sentence must list in "ids" the ids of every statement it uses. Use every statement at least once.

Statements, events in time order first:
{statements}

Reply with JSON only:
{{"paragraphs": [[{{"text": "...", "ids": [3, 7]}}, {{"text": "...", "ids": [5]}}], [ ... ]]}}"""

FALSE_MARKERS = ("false", "untrue", "not true", "contradict", "evidence shows", "disproved", "incorrect",
                 "no evidence", "refut")
ATTRIBUTION_MARKERS = ("according to", "report", "say", "said", "claim", "alleg", "state", "dispute", "denie",
                       "deny", "told", "assert", "account", "wrote", "describ", "cite", "quot", "per ")


def _lc(s: str) -> str:
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


def _outlets(item: dict, stance: str) -> list[str]:
    want = ("denies",) if stance == "denies" else ("asserts", "attributes")
    return sorted({s["outlet"] for s in item["sources"] if s["stance"] in want})


def _join(names: list[str]) -> str:
    if not names:
        return "Some reports"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def plain_sentence(item: dict) -> str:
    """Deterministic fallback wording for one statement."""
    text = item["text"].rstrip(". ")
    sup, den = _outlets(item, "asserts"), _outlets(item, "denies")
    v = item["verdict"]
    if v in ("corroborated", "confirmed"):
        return text + "."
    if v == "false":
        why = (item.get("check") or {}).get("reasons") or []
        tail = f" The evidence shows this is false: {why[0].rstrip('.')}." if why else " The evidence shows this is false."
        return f"{_join(sup)} reported that {_lc(text)}.{tail}"
    if v == "disputed" and den:
        return f"According to {_join(sup)}, {_lc(text)}; {_join(den)} disputes this."
    return f"{_join(sup)} reported that {_lc(text)}."


def _time_key(i: dict):
    start = (i.get("time") or {}).get("start")
    try:
        return dt.datetime.fromisoformat(start) if start else dt.datetime.max
    except ValueError:
        return dt.datetime.max


def ordered_items(p: dict) -> list[dict]:
    """Every statement in the story, in a deterministic reading order: established events in
    timeline order, other events by their reported time, then claims from strongest to weakest."""
    seen, out = set(), []

    def add(items):
        for i in items:
            if i["id"] not in seen:
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
    line = f'#{i["id"]} {STATUS_LABEL.get(i["verdict"], "ONE-SIDED")} | "{i["text"]}"'
    sup, den = _outlets(i, "asserts"), _outlets(i, "denies")
    if sup:
        line += f" | reported by: {', '.join(sup)}"
    if den:
        line += f" | denied by: {', '.join(den)}"
    if i["verdict"] == "false" and i.get("check"):
        line += f" | evidence: {'; '.join(i['check'].get('reasons') or [])}"
    when = (i.get("time") or {}).get("when_text")
    if when:
        line += f" | when: {when}"
    return line


def _numbers(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", s))


def _validate(sentence: dict, by_id: dict[int, dict], banned: set[str]) -> list[int] | None:
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
    source_text = " ".join(by_id[i]["text"] + " " + ((by_id[i].get("time") or {}).get("when_text") or "")
                           + " " + " ".join(by_id[i]["check"]["reasons"] if by_id[i].get("check") else [])
                           for i in ids)
    if not _numbers(text) <= _numbers(source_text):
        return None
    verdicts = {by_id[i]["verdict"] for i in ids}
    # a sentence that uses a false statement must say it is false, not just be coloured red
    if "false" in verdicts and not any(m in low for m in FALSE_MARKERS):
        return None
    # anything not established must be attributed, never stated as plain fact
    if verdicts - {"corroborated", "confirmed"}:
        outlets = {s["outlet"].lower() for i in ids for s in by_id[i]["sources"]}
        if not (any(m in low for m in ATTRIBUTION_MARKERS) or any(o in low for o in outlets)):
            return None
    return ids


def input_hash(sections: dict[str, list[dict]]) -> str:
    key = {k: [(i["id"], i["verdict"], i["text"], sorted(s["url"] for s in i["sources"])) for i in v]
           for k, v in sections.items()}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def write_narrative(router: Router | None, payload: dict, banned: set[str]) -> dict:
    items = ordered_items(payload)
    by_id = {i["id"]: i for i in items}

    drafted: list[list[dict]] = []
    model = None
    if router is not None and items:
        prompt = WRITER_PROMPT.format(banned=", ".join(sorted(banned)) or "(none)",
                                      statements="\n".join(_statement_line(i) for i in items))
        try:
            res = router.call("writer", prompt, json_out=True, max_output_tokens=4000)
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
            ids = _validate(s, by_id, banned) if isinstance(s, dict) else None
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
            sent["class"] = CLASS[max(RANK.get(by_id[x]["verdict"], 1) for x in sent["ids"])]
            sent["sources"] = sorted({numbering[s["url"]] for x in sent["ids"] for s in by_id[x]["sources"]})

    src_meta = {s["url"]: s for s in payload["sources"]}
    source_list = [{"n": n, "url": url, "outlet": src_meta.get(url, {}).get("outlet", ""),
                    "title": src_meta.get(url, {}).get("title", ""),
                    "perspective": src_meta.get(url, {}).get("perspective", "–")}
                   for url, n in sorted(numbering.items(), key=lambda kv: kv[1])]
    if rejected:
        log.info("narrative: %d sentences failed checks and were replaced with plain wording", rejected)
    return {"hash": input_hash(sections_from_payload(payload)), "model": model, "rejected": rejected,
            "paragraphs": paragraphs, "sources": source_list}
