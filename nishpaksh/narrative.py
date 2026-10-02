"""The readable story: prose written from the structured statements, every sentence checked.

Division of labour:
  code   decides the sections, which statements go in each, their order and their verdicts
  model  turns those statements into flowing sentences, citing the statements each one uses
  code   validates every sentence and colours it by the weakest statement it cites

A sentence is rejected (and replaced by a plain rendering of its statements) if it cites
nothing valid, contains a number that is not in the statements it cites, or uses any loaded
word that any outlet used in this story. Statements no valid sentence covers are appended
in plain form, so nothing is silently dropped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

RANK = {"confirmed": 0, "corroborated": 0, "unverified": 1, "pending": 1, "disputed": 2, "false": 3}
CLASS = {0: "established", 1: "unverified", 2: "disputed", 3: "false"}
STATUS_LABEL = {"corroborated": "ESTABLISHED", "confirmed": "ESTABLISHED", "disputed": "DISPUTED",
                "unverified": "UNVERIFIED", "pending": "UNVERIFIED", "false": "FALSE"}
SECTIONS = ("happened", "contested", "one_side")

WRITER_PROMPT = """You are writing a short, readable news story for ordinary readers, in plain English,
from a fixed list of statements. Each statement has an id, a status, and the outlets behind it.

Rules:
1. Use ONLY the statements given. Do not add any fact, name, number, place, cause, motive or
   description that is not in them.
2. Inside each section keep the statements in the order given. You may combine statements into one
   sentence and add plain connecting words (then, later, meanwhile, according to, however).
3. How to write each status:
   ESTABLISHED - state it plainly as fact.
   DISPUTED    - give both sides with attribution: "According to X, ...; Y disputes this."
   UNVERIFIED  - always attribute it: "X reports that ...". Never state it as fact.
   FALSE       - say who claimed it, then that the evidence shows it is false, citing the evidence given.
4. Neutral wording only. Never use any of these words: {banned}
5. Every sentence must list, in "ids", the ids of every statement it uses.
6. Short sentences. No headings, no bullet points, no conclusion or commentary.

{sections}

Reply with JSON only:
{{"sections": [{{"key": "happened", "sentences": [{{"text": "...", "ids": [3, 7]}}]}}, ...]}}"""


def _lc(s: str) -> str:
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


def _outlets(item: dict, stance: str) -> list[str]:
    want = ("denies",) if stance == "denies" else ("asserts", "attributes")
    return sorted({s["outlet"] for s in item["sources"] if s["stance"] in want})


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else "some reports"


def plain_sentence(item: dict) -> str:
    """Deterministic fallback wording for one statement."""
    text = item["text"].rstrip(".")
    sup, den = _outlets(item, "asserts"), _outlets(item, "denies")
    v = item["verdict"]
    if v in ("corroborated", "confirmed"):
        return text + "."
    if v == "false":
        why = (item.get("check") or {}).get("reasons") or []
        tail = f" Evidence shows this is false: {why[0].rstrip('.')}." if why else " Evidence shows this is false."
        return f"{_join(sup)} reported that {_lc(text)}.{tail}"
    if v == "disputed" and den:
        return f"According to {_join(sup)}, {_lc(text)}; {_join(den)} disputes this."
    return f"{_join(sup)} reports that {_lc(text)}."


def sections_from_payload(p: dict) -> dict[str, list[dict]]:
    happened = [i for tier in p["timeline"] for i in tier] + p["undated"] + p["established"]
    major = [i for i in p["contested"] if not i["minor"]]
    return {
        "happened": happened,
        "contested": [i for i in major if i["verdict"] in ("disputed", "false")],
        "one_side": [i for i in major if i["verdict"] not in ("disputed", "false")],
    }


def _statement_line(i: dict) -> str:
    line = f'#{i["id"]} {STATUS_LABEL.get(i["verdict"], "UNVERIFIED")} | "{i["text"]}"'
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


FALSE_MARKERS = ("false", "untrue", "not true", "contradict", "evidence shows", "disproved", "incorrect",
                 "no evidence", "refut")
ATTRIBUTION_MARKERS = ("according to", "report", "say", "said", "claim", "alleg", "state", "dispute", "denie",
                       "deny", "told", "assert")


def _numbers(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", s))


def _validate(sentence: dict, by_id: dict[int, dict], allowed_ids: set[int], banned: set[str]) -> list[int] | None:
    text = str(sentence.get("text") or "").strip()
    try:
        ids = [int(x) for x in sentence.get("ids") or []]
    except (TypeError, ValueError):
        return None
    ids = [i for i in dict.fromkeys(ids) if i in allowed_ids]
    if not text or not ids or len(text) > 600:
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
    # anything not established must be attributed to someone, never stated as plain fact
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
    sections = sections_from_payload(payload)
    by_id = {i["id"]: i for items in sections.values() for i in items}

    # source numbers: in order of first use in the story, then everything else read
    numbering: dict[str, int] = {}
    for key in SECTIONS:
        for i in sections[key]:
            for s in i["sources"]:
                numbering.setdefault(s["url"], len(numbering) + 1)
    for s in payload["sources"]:
        numbering.setdefault(s["url"], len(numbering) + 1)

    drafted: dict[str, list[dict]] = {}
    model = None
    if router is not None and by_id:
        blocks = []
        for key, title in (("happened", "What happened (established)"),
                           ("contested", "What is disputed or shown false"),
                           ("one_side", "What only one side reports")):
            if sections[key]:
                blocks.append(f"[{key}] {title}\n" + "\n".join(_statement_line(i) for i in sections[key]))
        prompt = WRITER_PROMPT.format(banned=", ".join(sorted(banned)) or "(none)", sections="\n\n".join(blocks))
        try:
            res = router.call("writer", prompt, json_out=True, max_output_tokens=2500)
            model = res.model
            for sec in (res.data or {}).get("sections", []) if isinstance(res.data, dict) else []:
                if sec.get("key") in SECTIONS and isinstance(sec.get("sentences"), list):
                    drafted[sec["key"]] = sec["sentences"]
        except (QuotaExhausted, ValueError) as e:
            log.info("narrative: writer unavailable (%s); plain sentences used", e)
        except Exception as e:  # noqa: BLE001
            log.warning("narrative failed: %s", e)

    out_sections, rejected = [], 0
    for key in SECTIONS:
        items = sections[key]
        if not items:
            continue
        allowed = {i["id"] for i in items}
        sentences, covered = [], set()
        for s in drafted.get(key, []):
            ids = _validate(s, by_id, allowed, banned) if isinstance(s, dict) else None
            if ids is None:
                rejected += 1
                continue
            covered.update(ids)
            sentences.append({"text": re.sub(r"\.{2,}$", ".", s["text"].strip()), "ids": ids})
        # anything not covered by a valid sentence is added in plain words, in its place
        for i in items:
            if i["id"] not in covered:
                pos = len(sentences)
                for k, sent in enumerate(sentences):
                    if min(items.index(by_id[x]) for x in sent["ids"]) > items.index(i):
                        pos = k
                        break
                sentences.insert(pos, {"text": plain_sentence(i), "ids": [i["id"]], "plain": True})
        for sent in sentences:
            rank = max(RANK.get(by_id[x]["verdict"], 1) for x in sent["ids"])
            sent["class"] = CLASS[rank]
            sent["sources"] = sorted({numbering[s["url"]] for x in sent["ids"] for s in by_id[x]["sources"]})
        out_sections.append({"key": key, "sentences": sentences})

    src_meta = {s["url"]: s for s in payload["sources"]}
    source_list = [{"n": n, "url": url, "outlet": src_meta.get(url, {}).get("outlet", ""),
                    "title": src_meta.get(url, {}).get("title", ""),
                    "perspective": src_meta.get(url, {}).get("perspective", "–")}
                   for url, n in sorted(numbering.items(), key=lambda kv: kv[1])]
    if rejected:
        log.info("narrative: %d sentences failed checks and were replaced with plain wording", rejected)
    return {"hash": input_hash(sections), "model": model, "rejected": rejected,
            "sections": out_sections, "sources": source_list}
