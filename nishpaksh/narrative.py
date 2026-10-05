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
WRITER_VERSION = 6   # part of the cache key: pages written by an older writer are rewritten once

RANK = {"confirmed": 0, "corroborated": 0, "developing": 1, "unverified": 2, "pending": 2, "disputed": 3, "false": 4}
CLASS = {0: "established", 1: "developing", 2: "unverified", 3: "disputed", 4: "false"}
STATUS_LABEL = {"corroborated": "ESTABLISHED", "confirmed": "ESTABLISHED", "developing": "REPORTED",
                "disputed": "DISPUTED", "unverified": "REPORTED", "pending": "REPORTED", "false": "FALSE"}

WRITER_PROMPT = """You are a senior news editor. Write the story below as ONE news article for ordinary
readers, the way a good newspaper reports it: clear, calm, flowing paragraphs. Not a list.

You may use ONLY the statements given. Each has an id and a status, may say who makes it ("said by"),
when it happened, and which statements contradict or deny it.

Structure:
1. Opening paragraph (1-2 sentences): who, where, what happened, and when, from the most important
   statements.
2. Then what happened, in time order.
3. Then each side's account: give each speaker their own paragraph where they have several statements.
4. Last paragraph: where accounts differ, if they do.
Group related statements into paragraphs of 2-4 sentences. Combine statements into one sentence where
natural. Say each fact ONCE: if two statements say the same thing, write it once and cite both ids.

Attribution, the way a good newspaper does it (important):
- NEVER name a newspaper, channel or website. Do not write "X reported", "according to X" for an outlet.
- ESTABLISHED: state plainly as fact, with no attribution.
- A statement with "said by": name the speaker ONCE, at the start of the run of their statements
  ("Ukraine's foreign minister Andrii Sybiha said India's proposal was the most comprehensive."),
  then continue in the same paragraph with "he said", "he added", "the minister said" while it is
  still that speaker. No empty set-up sentences ("X set out their position."). Name
  the next speaker when the speaker changes. Do not end every sentence with "according to <name>".
  An accusation must always name who makes it.
- REPORTED without "said by": not confirmed. Put such statements together and hedge ONCE for the
  paragraph ("Reports also said that..."). Never end sentences with "reports said".
- DISPUTED: write the disagreement itself, both versions, and whose they are where known: "The police
  put the toll at 40; the families say 50." For a statement others deny: "X said ...; Y denied it."
  NEVER write "other reports differ" or "accounts differ" without saying what the other account is.
- FALSE: say who claimed it and that the evidence shows it is false, citing the evidence given.

Never add any fact, name, number, place, cause, motive, adjective or opinion that is not in the
statements. Events may be told in order ("after", "later", "then"), but never link two events by cause
("because", "due to", "led to") unless a statement says so. Use "said", "alleged", "claimed", "denied"
only for the person or body a statement names in "said by"; never invent a speaker. When a statement
is itself reported speech ("A said that B claimed X"), keep it reported: never make A the author of X.
No headings, no bullet points.
Never use any of these words: {banned}
Every sentence lists in "ids" every statement it uses. Use every statement at least once; minor
statements (marked "minor") may be left out if they add nothing.

{background}Statements, events in time order first:
{statements}

Reply with JSON only:
{{"paragraphs": [[{{"text": "...", "ids": [3, 7]}}, {{"text": "...", "ids": [5]}}], [ ... ]]}}"""

FALSE_MARKERS = ("false", "untrue", "not true", "contradict", "evidence shows", "disproved", "incorrect",
                 "no evidence", "refut")
HEDGE_MARKERS = ("reportedly", "according to report", "report", "it is said", "was said to", "were said to",
                 "accounts differ", "according to early", "unconfirmed", "allegedly")
ATTRIBUTION_VERBS = ("said", "say", "says", "alleg", "claim", "accus", "denied", "deny", "denies", "demand",
                     "told", "stated", "according to", "maintain", "insist", "assert")
SPEECH = re.compile(r"(?i)\b(said|says|stated|told|claimed|claims|denied|denies|alleged that|alleges|accused|"
                    r"according to (?!(?:early |some |other )?reports?\b))")
REPORTED = re.compile(r"(?i)\b(said|stated|told|claimed|alleged|announced|added|noted|denied)\s+that\b")
SPEECH_ANY = re.compile(r"(?i)\b(said|says|stated|states|told|claimed|claims|alleged|alleges|announced|added|noted|"
                        r"denied|denies|according to|reportedly|reports? (?:said|say|stated))\b")
CAUSAL = ("because", "due to", "led to", "as a result", "resulted in", "caused", "triggered")
DISPUTE_MARKERS = ("differ", "disput", "contradict", "others", "while", "however", "but ", "conflicting",
                   "versions", "other reports")


def _word_set(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-zऀ-ॿ]{4,}", (s or "").lower())}


def _soft_lower(text: str) -> str:
    """Lower-case only a leading article ('The police' -> 'the police'); names stay as they are."""
    m = re.match(r"(The|A|An)\s", text)
    return text[0].lower() + text[1:] if m else text


def _speaker_is_subject(speaker: str, text: str) -> bool:
    """'Protesters demanded...' with speaker 'protesters': the sentence already says who."""
    head = _word_set(" ".join(text.split()[:6]))
    return bool(_word_set(speaker) & head)


def disputed_pair(a: dict, b: dict) -> str:
    """Two statements that cannot both be true, in one sentence: both versions, and whose they are."""
    def side(i):
        t = _soft_lower(i["text"].strip().rstrip("."))
        return f"{i['speaker']} says {t}" if i.get("speaker") and not _speaker_is_subject(i["speaker"], i["text"]) \
            else f"some reports say {t}"
    first = side(a)
    return f"{first[0].upper() + first[1:]}; {side(b)}."


def plain_sentence(item: dict) -> str:
    """Deterministic fallback wording for one statement, under the same attribution rules. The
    statement keeps its own capitalisation (names stay capitalised); attribution goes at the end.
    Unconfirmed sentences carry no hedge of their own: the paragraph gets one (see write_narrative)."""
    text = item["text"].strip().rstrip(".")
    v = item["verdict"]
    speaker = item.get("speaker")
    if v == "false":
        why = (item.get("check") or {}).get("reasons") or []
        tail = f" The evidence shows this is false: {why[0].rstrip('.')}." if why else " The evidence shows this is false."
        return f"{text}, {'according to ' + speaker if speaker else 'it was claimed'}.{tail}"
    if v == "disputed":
        # a statement disputed by denial (no contradicting statement to pair it with): say so plainly
        who = speaker if speaker and not _speaker_is_subject(speaker, text) else None
        return f"{text}{', according to ' + who if who else ''}; this is denied in other reports."
    if speaker and v not in ("corroborated", "confirmed") and not _speaker_is_subject(speaker, text):
        return f"{text}, according to {speaker}."
    return text + "."


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
    if i.get("minor"):
        line += " | minor"
    if i.get("speaker"):
        line += f" | said by: {i['speaker']}"
    if i.get("conflicts_with"):
        line += " | contradicted by: " + ", ".join(f"#{x}" for x in i["conflicts_with"])
    deniers = sorted({s.get("attributed_to") or "" for s in i.get("sources") or [] if s.get("stance") == "denies"} - {""})
    if any(s.get("stance") == "denies" for s in i.get("sources") or []):
        line += " | denied" + (f" by: {', '.join(deniers)}" if deniers else " in some reports")
    if i["verdict"] == "false" and i.get("check"):
        line += f" | evidence: {'; '.join(i['check'].get('reasons') or [])}"
    when = english_when(i.get("time") or {})
    if when:
        line += f" | when: {when}"
    return line


def english_when(t: dict) -> str:
    """The time words for an English page. Older readings copied Hindi time words ("26 सितंबर"),
    which then appeared in English text: those are replaced by the date itself, or dropped."""
    w = (t.get("when_text") or "").strip()
    if w and not re.search(r"[\u0900-\u097f]", w):
        return w
    start = t.get("start")
    try:
        return dt.datetime.fromisoformat(start).strftime("%-d %B") if start else ""
    except ValueError:
        return ""


def _numbers(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", s))


REJECT_REASONS: dict[str, int] = {}


def _no(reason: str):
    REJECT_REASONS[reason] = REJECT_REASONS.get(reason, 0) + 1
    return None


def _validate(sentence: dict, by_id: dict[int, dict], banned: set[str], outlets: list[str],
              scope: set[str] = frozenset()) -> list[int] | None:
    """The sentence's valid statement ids, or None. `scope` holds the words of the speaker named
    earlier in the same paragraph: "He added that..." continues that speaker's attribution."""
    text = str(sentence.get("text") or "").strip()
    ids = []
    for x in sentence.get("ids") or []:
        # models often echo the id as written in the prompt ("#16191"); read the number, keep the sign
        m = re.search(r"-?\d+", str(x))
        if m:
            ids.append(int(m.group(0)))
    if not ids and sentence.get("ids"):
        return _no("bad ids")
    ids = [i for i in dict.fromkeys(ids) if i in by_id]
    if not text:
        return _no("empty sentence")
    if not ids:
        return _no("no valid ids")
    if len(text) > 700:
        return _no("too long")
    low = text.lower()
    if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low) for w in banned):
        return _no("loaded word")
    # outlet names never appear in the article (the source links carry them)
    if any(re.search(rf"(?<!\w){re.escape(o)}(?!\w)", text) for o in outlets if len(o) >= 3):
        return _no("names an outlet")
    source_text = " ".join(by_id[i]["text"] + " " + ((by_id[i].get("time") or {}).get("when_text") or "")
                           + " " + " ".join(by_id[i]["check"]["reasons"] if by_id[i].get("check") else [])
                           for i in ids)
    if not _numbers(text) <= _numbers(source_text):
        return _no("number not in statements")
    if re.search(r"[\u0900-\u097f]", text) and not re.search(r"[\u0900-\u097f]", source_text):
        return _no("Hindi words in English text")
    # "A said that B claimed X" must stay reported speech: a sentence may not drop the "said" and
    # make A the author of the claim (seen: "a claim was made by DIG Singla")
    if any(REPORTED.search(by_id[i]["text"]) for i in ids) and not SPEECH_ANY.search(text):
        return _no("reported speech turned into fact")
    verdicts = {by_id[i]["verdict"] for i in ids}
    if "false" in verdicts and not any(m in low for m in FALSE_MARKERS):
        return _no("false without saying so")
    # an accusation or claim with a known speaker must name who makes it (or say it is alleged)
    for i in ids:
        sp = by_id[i].get("speaker")
        if sp and by_id[i]["verdict"] not in ("corroborated", "confirmed"):
            if not (_word_set(sp) & (_word_set(text) | set(scope))) and "alleg" not in low:
                return _no("claim without its speaker")
    if "disputed" in verdicts and not any(m in low for m in DISPUTE_MARKERS + ATTRIBUTION_VERBS):
        return _no("dispute stated as fact")
    # a dispute is written as the disagreement itself, never "other reports differ" with no content
    if re.search(r"(?i)\b(other|some) (reports|accounts) (differ|disagree|vary)\b|accounts differ\W*$", text):
        return _no("empty dispute")
    # never invent a speaker: "X said / alleged / claimed / denied" only for a statement that names one
    speakers = [by_id[i].get("speaker") for i in ids if by_id[i].get("speaker")]
    if SPEECH.search(source_text) or re.search(r"(?i)\b(said|stated|alleged|claimed|denied|announced|told)\b", source_text):
        speakers = speakers or ["(in the statement)"]   # the statement itself says who spoke
    for m in SPEECH.finditer(text):
        before = text[max(0, m.start() - 30):m.start()].lower()
        if re.search(r"reports?\W*$|according to (early |some )?reports?\W*$", before) or m.group(0).lower().startswith("according to report"):
            continue   # the paragraph hedge ("reports said"), not a speaker
        if not speakers:
            return _no("invented speaker")
    # never link events by cause unless a statement does
    for c in CAUSAL:
        if re.search(rf"\b{c}\b", low) and c not in source_text.lower():
            return _no("cause not in statements")
    return ids


def _needs_hedge(sent: dict, by_id: dict[int, dict]) -> bool:
    """Not established and not pinned on a speaker: the paragraph must say it is only reported."""
    return any(by_id[x]["verdict"] not in ("corroborated", "confirmed") and not by_id[x].get("speaker")
               and by_id[x]["verdict"] != "disputed" for x in sent["ids"])


COMMON_FIRST = {"the", "a", "an", "police", "officials", "authorities", "protesters", "students", "residents",
                "villagers", "locals", "workers", "farmers", "troops", "security", "it", "this", "these", "there",
                "several", "many", "some", "two", "three", "four", "five", "an", "his", "her", "their", "its"}


def _hedge(text: str) -> str:
    """One hedge for the paragraph, at the front: 'According to reports, ...'. A leading adverbial
    keeps its place ("Previously, according to reports, ..."); a common first word is lower-cased,
    a name is not (seen: "According to reports, Police are...")."""
    t = text.strip()
    m = re.match(r"^([A-Z][a-z]+),\s+(.*)$", t)
    if m:
        return f"{m.group(1)}, according to reports, {m.group(2)}"
    first = re.match(r"^(\w+)", t)
    if first and first.group(1).lower() in COMMON_FIRST:
        t = t[0].lower() + t[1:]
    return "According to reports, " + t


def input_hash(sections: dict[str, list[dict]], background: list[dict] | None = None) -> str:
    key = {k: [(i["id"], i["verdict"], i["text"], i.get("speaker"), sorted(s["url"] for s in i["sources"]))
               for i in v] for k, v in sections.items()}
    key["_background"] = sorted(b["id"] for b in background or [])
    key["_writer"] = WRITER_VERSION
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def _known_outlets() -> list[str]:
    from .config import load_yaml
    names = {f["name"] for f in load_yaml("feeds.yaml").get("feeds") or []}
    for info in (load_yaml("ownership.yaml").get("groups") or {}).values():
        names |= set(info.get("outlets") or [])
    return sorted(names)


def _context(payload: dict, banned: set[str]):
    items = ordered_items(payload)
    background = [b for b in payload.get("background") or [] if b.get("id") is not None]
    by_id = {i["id"]: i for i in items + background}
    # every outlet we know, not just this story's: statements sometimes quote another outlet's report
    outlets = sorted({s["outlet"] for s in payload["sources"] if s.get("outlet")} | set(_known_outlets()),
                     key=len, reverse=True)
    # a word the neutral statements themselves use (e.g. "threat" in "an alleged threat") is not loaded
    # in this story's own wording; banning it would reject every faithful sentence
    used = " ".join(i["text"].lower() for i in items + background)
    banned = {w for w in banned if not re.search(rf"(?<!\w){re.escape(w.lower())}(?!\w)", used)}
    return items, background, by_id, outlets, banned


def _named_speaker(text: str, ids: list[int], by_id: dict[int, dict]) -> set[str]:
    """Words of the speaker this sentence names (it carries the attribution for what follows)."""
    words = _word_set(text)
    for i in ids:
        sp = by_id[i].get("speaker")
        if sp and _word_set(sp) & words:
            return _word_set(sp)
    return set()


def _check_paragraphs(drafted: list[list], by_id, banned, outlets) -> tuple[list[list[dict]], list[int], int]:
    """Validated sentences only; a rejected sentence is dropped, never patched with plain wording
    (patchwork read badly: Oct 2026). Returns paragraphs, ids of rejected sentences, and the count."""
    paragraphs, dropped, rejected = [], [], 0
    for para in drafted:
        out, scope = [], set()
        for s in para:
            ids = _validate(s, by_id, banned, outlets, scope) if isinstance(s, dict) else None
            if ids is None:
                rejected += 1
                for x in (s.get("ids") or []) if isinstance(s, dict) else []:
                    m = re.search(r"-?\d+", str(x))
                    if m:
                        dropped.append(int(m.group(0)))
                continue
            named = _named_speaker(s["text"], ids, by_id)
            if named:
                scope = named
            out.append({"text": re.sub(r"\.{2,}$", ".", s["text"].strip()), "ids": ids})
        if out:
            paragraphs.append(out)
    return paragraphs, dropped, rejected


def _also(items: list[dict], covered: set[int], by_id: dict[int, dict]) -> list[dict]:
    """Statements the essay does not carry, listed under it in plain words ("Also reported"), so
    nothing is silently dropped and the essay itself stays the writer's prose."""
    out, done = [], set(covered)
    for i in items:
        if i["id"] in done:
            continue
        partner = next((by_id[o] for o in i.get("conflicts_with") or []
                        if o in by_id and o not in done and o >= 0), None)
        if i["verdict"] == "disputed" and partner:
            out.append({"text": disputed_pair(i, partner), "ids": [i["id"], partner["id"]]})
            done.update({i["id"], partner["id"]})
        else:
            text = plain_sentence(i)
            if _needs_hedge({"ids": [i["id"]]}, by_id) and not any(m in text.lower() for m in HEDGE_MARKERS):
                text = _hedge(text)
            out.append({"text": text, "ids": [i["id"]]})
            done.add(i["id"])
    return out


def _finish(payload: dict, paragraphs: list, also: list, by_id: dict, meta: dict) -> dict:
    """Hedges, source numbers and colours for the essay and the also-reported list."""
    for para in paragraphs:
        if any(_needs_hedge(x, by_id) for x in para) and not any(
                m in x["text"].lower() for x in para for m in HEDGE_MARKERS):
            first = next(x for x in para if _needs_hedge(x, by_id))
            first["raw"] = first["text"]          # the hedge is code's: dropped again if no longer needed
            first["text"] = _hedge(first["text"])
    numbering: dict[str, int] = {}
    for sent in [x for para in paragraphs for x in para] + also:
        for x in sent["ids"]:
            for s in by_id[x]["sources"]:
                numbering.setdefault(s["url"], len(numbering) + 1)
    for s in payload["sources"]:
        numbering.setdefault(s["url"], len(numbering) + 1)
    for sent in [x for para in paragraphs for x in para] + also:
        sent["class"] = CLASS[max(RANK.get(by_id[x]["verdict"], 2) for x in sent["ids"])]
        sent["sources"] = sorted({numbering[s["url"]] for x in sent["ids"] for s in by_id[x]["sources"]})
    src_meta = {s["url"]: s for s in payload["sources"]}
    source_list = [{"n": n, "url": url, "outlet": src_meta.get(url, {}).get("outlet", ""),
                    "title": src_meta.get(url, {}).get("title", ""),
                    "perspective": src_meta.get(url, {}).get("perspective", "–"),
                    "read": src_meta.get(url, {}).get("read", True),
                    "readable": src_meta.get(url, {}).get("readable", True)}
                   for url, n in sorted(numbering.items(), key=lambda kv: kv[1])]
    background = [b for b in payload.get("background") or [] if b.get("id") is not None]
    return dict(meta, hash=input_hash(sections_from_payload(payload), background), writer=WRITER_VERSION,
                paragraphs=paragraphs, also=also, sources=source_list,
                covers=sorted({x for para in paragraphs for s in para for x in s["ids"]}))


def essay_ok(nar: dict, payload: dict) -> bool:
    """Good enough to publish as the story: written by the writer, it carries most of the story (60%
    of the statements that are not minor; what a rejected sentence leaves uncovered counts against
    this), and the writer was not badly off task (no more than half its sentences rejected)."""
    if not nar.get("model") or not nar.get("paragraphs"):
        return False
    sents = sum(len(p) for p in nar["paragraphs"])
    if nar.get("rejected", 0) > sents:
        return False
    major = [i["id"] for i in ordered_items(payload) if not i.get("minor")]
    if not major:
        return True
    return len(set(major) & set(nar.get("covers") or [])) >= 0.6 * len(major)


def write_narrative(router: Router | None, payload: dict, banned: set[str]) -> dict:
    items, background, by_id, outlets, banned = _context(payload, banned)
    drafted: list[list[dict]] = []
    model, failure = None, None
    REJECT_REASONS.clear()
    if router is not None and items:
        bg = ""
        if background:
            bg = ("This story is a later development of an earlier story on the site. Open with the NEW "
                  "development; then give at most TWO sentences of background from these earlier facts, "
                  "citing their ids (they are optional; do not repeat them later):\n"
                  + "\n".join(_statement_line(b) for b in background) + "\n\n")
        prompt = WRITER_PROMPT.format(banned=", ".join(sorted(banned)) or "(none)", background=bg,
                                      statements="\n".join(_statement_line(i) for i in items))
        try:
            # at most 3 tries per run: when Flash is overloaded, 8 tries burned a quarter of an
            # hour's writer calls on one story; the story is simply tried again next run
            res = router.call("writer", prompt, json_out=True, max_output_tokens=6000, max_attempts=3)
            model = res.model
            paras = (res.data or {}).get("paragraphs") if isinstance(res.data, dict) else None
            if isinstance(paras, list):
                drafted = [p for p in paras if isinstance(p, list)]
            if not drafted:
                failure = "no paragraphs in reply"
        except QuotaExhausted as e:
            failure = "quota"
            log.info("narrative: writer quota used up for now (%s)", e)
        except ValueError as e:
            failure = "unparseable reply"
            log.info("narrative: writer reply unusable (%s)", e)
        except Exception as e:  # noqa: BLE001
            failure = f"error: {str(e)[:80]}"
            log.warning("narrative failed: %s", e)

    paragraphs, _, rejected = _check_paragraphs(drafted, by_id, banned, outlets)
    covered = {x for para in paragraphs for s in para for x in s["ids"]}
    also = _also(items, covered, by_id)
    if rejected:
        log.info("narrative: %d sentences failed checks (%s)", rejected, dict(REJECT_REASONS))
    return _finish(payload, paragraphs, also, by_id,
                   {"model": model, "rejected": rejected, "reject_reasons": dict(REJECT_REASONS), "failure": failure})


def recolour(old: dict, payload: dict, banned: set[str]) -> dict:
    """Keep the essay as written and bring it up to date without a model call: each sentence takes
    the current colour of the statements it rests on. A sentence whose statements are gone, or that
    no longer passes the checks (a fact that is now disputed, say), leaves the essay; statements the
    essay does not carry are listed under it. Used when only verdicts changed, or the writer is out."""
    items, background, by_id, outlets, banned = _context(payload, banned)
    REJECT_REASONS.clear()
    drafted = [[{"text": s.get("raw") or s["text"], "ids": [x for x in s["ids"] if x in by_id]} for s in para]
               for para in old.get("paragraphs") or []]
    drafted = [[s for s in para if s["ids"]] for para in drafted]
    paragraphs, _, rejected = _check_paragraphs([p for p in drafted if p], by_id, banned, outlets)
    covered = {x for para in paragraphs for s in para for x in s["ids"]}
    also = _also(items, covered, by_id)
    before = sum(len(p) for p in old.get("paragraphs") or [])
    return _finish(payload, paragraphs, also, by_id,
                   {"model": old.get("model"), "rejected": old.get("rejected", 0), "recoloured": True,
                    "dropped_since_written": before - sum(len(p) for p in paragraphs),
                    "reject_reasons": old.get("reject_reasons") or {}, "written_hash": old.get("written_hash", old.get("hash"))})


def needs_rewrite(old: dict | None, payload: dict) -> bool:
    """A rewrite is worth a writer call only for a material change: statements that are not minor
    and that the essay does not carry, or a quarter of its sentences no longer standing."""
    if not old or not old.get("paragraphs") or not old.get("model") or old.get("writer") != WRITER_VERSION:
        return True
    covers = set(old.get("covers") or [x for p in old["paragraphs"] for s in p for x in s["ids"]])
    new_major = [i for i in ordered_items(payload) if not i.get("minor") and i["id"] not in covers]
    sents = sum(len(p) for p in old["paragraphs"]) + old.get("dropped_since_written", 0)
    return bool(new_major) or old.get("dropped_since_written", 0) > sents // 4
