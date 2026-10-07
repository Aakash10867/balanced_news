"""The readable story: one continuous news article written from the checked statements.

Division of labour:
  code   decides which statements exist, their verdicts, who makes each claim, and time order
  model  writes them as a news article: an opening that says who, where and what, then events in
         time order, then the investigation and each side's response; a disagreement sits with its subject
  code   validates every sentence and colours it by the weakest statement it cites

Attribution (decided with the reader in mind): outlet names never appear in the text; the colour
and the numbered source links already say who reported what. A claim is pinned on the person or
body that makes it ("his parents alleged", "police said"). Nothing carries "according to reports",
"reportedly" or "one report said" (owner, Oct 7 2026, option A): the article reads as written by one
author, and the colour, with its key under the headline, says how well each sentence is supported.
Only a dispute keeps whose each version is, and "allegedly" stays where the outlets use it.

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
import threading

from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)
WRITER_VERSION = 9   # part of the cache key: pages written by an older writer are rewritten once

RANK = {"confirmed": 0, "corroborated": 0, "developing": 1, "unverified": 2, "pending": 2, "single": 3,
        "disputed": 4, "false": 5}
CLASS = {0: "established", 1: "developing", 2: "unverified", 3: "single", 4: "disputed", 5: "false"}
STATUS_LABEL = {"corroborated": "ESTABLISHED", "confirmed": "ESTABLISHED", "developing": "REPORTED",
                "disputed": "DISPUTED", "unverified": "REPORTED", "pending": "REPORTED", "false": "FALSE",
                "single": "ONE OUTLET ONLY"}


def shade(i: dict) -> str:
    """The colour a statement is shown in. Reported by ONE independent outlet only, and not disputed or
    shown false: purple, "one outlet only" (owner, Oct 7 2026): it may be an exclusive, or wrong.
    Everything else: its verdict."""
    v = i.get("verdict") or "pending"
    if v in ("unverified", "pending") and (i.get("n_sources") or 0) <= 1:
        return "single"
    return v

WRITER_PROMPT = """You are a senior news editor. Write the story below as ONE news article for ordinary
readers, the way a good newspaper reports it: clear, calm, flowing paragraphs. Not a list.

You may use ONLY the statements given. Each has an id and a status, may say who makes it ("said by"),
when it happened, which statements contradict it, and which statements are a party's response to it.
Statements marked CONTEXT are not the story's own event: background, a separate related event,
an explanation, a reaction, or what happens next.

Structure: the article is written in SECTIONS, in this order, so that a reader who knows nothing about
the story first learns why it matters today, then how it came about, and then follows it as one
coherent story. The statements below are already sorted into sections; write each from its own:
  news        1-2 sentences: THE NEWS, the thing that makes this a story today: the newest or most
              consequential thing someone did, decided or said, with who, where and when. Never the setting
              or background ("Donald Trump said 125 million people voted in India's election, mixing up
              India with Brazil", not "Brazil held an election on Sunday"). Use the "happened" statements.
  background  how this came about: earlier events, each clearly with its own time
  explained   what a rule, term, post, finding or number means
  happened    what happened, in time order (the statements of the news are not repeated here)
  numbers     the figures: amounts, tolls, counts, percentages, each with what it measures
  say         what each person or body says: claims, allegations, positions, and the responses to them
  related     separate events the reports connect to this one, each clearly SEPARATE, with its time
  next        what happens next: hearings, deadlines, required steps
Write only the sections that have statements (and "news"); skip the others.
A disagreement is written where its subject is, in the same paragraph as the rest of that subject, with
both versions and whose they are; never collected into a paragraph or section about differing accounts.
Organise paragraphs by SUBJECT: one subject per paragraph, 2-4 sentences. Never group statements
because they share a status, and never join two statements in one sentence unless they are about the
same person, body, place or thing. Say each fact ONCE: if two statements say the same thing, write it
once and cite both ids. Length: about {length} sentences, as the material allows; do not pad.

Write as ONE author telling the story to a reader, not as a summary of reports. The page colours every
sentence by how well it is supported, so the words never need to: NEVER write "according to reports",
"reportedly", "reports said", "one report said", "another report said", "it is reported".

Attribution, the way a good newspaper does it (important):
- NEVER name a newspaper, channel or website. Do not write "X reported", "according to X" for an outlet.
- ESTABLISHED and REPORTED (no "said by"): state plainly, with no attribution and no hedge.
- A statement with "said by": name the speaker ONCE, at the start of the run of their statements
  ("Ukraine's foreign minister Andrii Sybiha said India's proposal was the most comprehensive."),
  then continue in the same paragraph with "he said", "he added", "the minister said" while it is
  still that speaker. No empty set-up sentences ("X set out their position."). Name
  the next speaker when the speaker changes. Do not end every sentence with "according to <name>".
  An accusation must always name who makes it.
- An accusation of a crime stays "allegedly" / "alleged" wherever the statement says so.
- RESPONSE: write the claim or finding and the party's response together, each pinned on its source:
  "A food analyst declared the sample unsafe; Nestle India said its product is safe." A response is
  not a contradiction: do not write "accounts differ" for it.
- DISPUTED (contradicted): write the disagreement itself, both versions, and whose they are where
  known: "The police put the toll at 40; the families say 50." NEVER write "other reports differ" or
  "accounts differ" without saying what the other account is. Only statements marked "contradicted
  by" disagree: two statements about different steps, dates, people or parts of the story do not.
- NAMES DIFFER: when a statement says the reports name different actors, name both ("Creative Bakers,
  named in some reports as Sugarr & Spice"), never pick one; say it ONCE, at the first mention.
- FALSE: say who claimed it and that the evidence shows it is false, citing the evidence given.
- ONE OUTLET ONLY: a single outlet reports it; include it, written plainly (pinned on its speaker if
  it has one). The page colours it purple; never add "one report said".

Never add any fact, name, number, place, cause, motive, adjective or opinion that is not in the
statements. Events may be told in order ("after", "later", "then"), but never link two events by cause
("because", "due to", "led to") unless a statement says so. Use "said", "alleged", "claimed", "denied"
only for the person or body a statement names in "said by"; never invent a speaker. When a statement
is itself reported speech ("A said that B claimed X"), keep it reported: never make A the author of X.
Every sentence must make sense on its own: never write "denied this" unless the sentence just before
says what was denied. Introduce every person and body at first mention with the fullest name and role
the statements give ("AAP Delhi chief Saurabh Bharadwaj", "Supreme Court judge Ujjal Bhuyan"); after
that, the surname ("Bharadwaj") or a short form. Never use a surname alone for someone not yet
introduced. Vary how you attribute: not "He said ... He added ... He stated ..." sentence after sentence.
No headings, no bullet points.
Never use any of these words: {banned}
Every sentence lists in "ids" every statement it uses. Use EVERY statement at least once, including
those only one outlet reports: the reader gets everything known about the story, the past (CONTEXT
background), the present and what happens next.

{people}{background}Statements, by section:
{statements}

Reply with JSON only, the sections in this order (news, background, explained, happened, numbers, say,
related, next), each with its paragraphs:
{{"sections": [{{"key": "news", "paragraphs": [[{{"text": "...", "ids": [3]}}]]}},
               {{"key": "happened", "paragraphs": [[{{"text": "...", "ids": [5, 7]}}], [ ... ]]}}, ...]}}"""

FILL_PROMPT = """You are completing a news article written in sections from numbered statements. Rewrite ONLY the
sections named below so that they carry EVERY statement listed for them (each where it belongs; a new
paragraph if needed) and fix each failed sentence (or drop it if it cannot pass). Keep good sentences as
they are. The rest of the article is shown so you continue it: do not repeat what it already says, and
do not introduce again a person it already introduced.
Rules as before: only the statements given; no outlet named as a source; no number or speaker the
statements do not have; allegations name who makes them; a claim and the response to it together;
disputes give both versions and whose they are; write as one author: never "according to reports",
"reportedly" or "one report said" (the page colours each sentence); no cause words unless a statement has them; nothing loaded: {banned}.

The article so far:
{article}

Sections to rewrite: {keys}

Their statements (every one must be used):
{statements}

Failed sentences in these sections:
{failed}

Reply with JSON only: {{"sections": [{{"key": "...", "paragraphs": [[{{"text": "...", "ids": [3]}}]]}}]}}"""

# the order on the page (owner, Oct 7 2026): the news in a line or two, then the context a reader with
# no prior knowledge needs, then the story in full
SECTIONS = [("news", "The news"), ("background", "Background"), ("explained", "Explained"),
            ("happened", "What happened"), ("numbers", "By the numbers"), ("say", "What they say"),
            ("related", "Related events"), ("next", "What next")]
SECTION_KEYS = [k for k, _ in SECTIONS]
# the fill pass works on a few sections at a time: small tasks are done completely (owner, Oct 7 2026:
# Flash-Lite given all 40 statements wrote 10 sentences; given 8-10 at a time it uses them all)
FILL_GROUPS = [("news", "happened", "numbers"), ("say",), ("background", "related", "explained", "next")]
ROLE_SECTION = {"background": "background", "related": "related", "explanation": "explained",
                "reaction": "say", "next": "next"}
NUMBER = re.compile(r"\d")


def _is_figure(i: dict) -> bool:
    """A statement whose answer is a figure (an amount, toll, count, share), not a date or a year:
    Oct 7 2026, "deployed in gradual phases for the 2026 harvest season" sat alone under "By the numbers"."""
    from .frames import date_of
    val = str((i.get("frame") or {}).get("value") or "")
    if val:
        return bool(NUMBER.search(val)) and not date_of(val) and not re.fullmatch(r"\D*(19|20)\d\d\D*", val)
    months = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
    text = re.sub(rf"(?i)\b\d{{1,2}}(?:st|nd|rd|th)?\s+{months}|{months}\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b(19|20)\d\d\b",
                  "", i["text"])
    return bool(re.search(r"\d", text))


def assign_sections(items: list[dict], background: list[dict] | None = None) -> dict[int, str]:
    """Every statement in exactly one section, by code, from what we know about it (owner, Oct 7 2026:
    sections like an explainer, so nothing is left out and the reader can find each part)."""
    out: dict[int, str] = {}
    for i in items:
        if not is_core(i):
            out[i["id"]] = ROLE_SECTION.get(i.get("role"), "background")
        elif i.get("speaker") or i.get("responds_to") or i.get("responded_by"):
            out[i["id"]] = "say"
        elif i["kind"] == "claim" and _is_figure(i):
            out[i["id"]] = "numbers"
        else:
            out[i["id"]] = "happened"
    # a claim and its response, or two contradicting statements, are written together, where their
    # subject is (owner, Oct 7 2026: no separate section for disputes; purple lines stay with their
    # subject too, the colour says they are unconfirmed)
    for i in items:
        for x in (i.get("responded_by") or []) + (i.get("conflicts_with") or []):
            if x in out and i["id"] in out:
                out[x] = out[i["id"]]
    for b in background or []:
        out[b["id"]] = "background"
    return out


def _section_block(items: list[dict], sec: dict[int, str]) -> str:
    """The statements, grouped by section, for the writer."""
    lines = []
    for key in SECTION_KEYS:
        mine = [i for i in items if sec.get(i["id"]) == ("happened" if key == "news" else key)]
        if key == "news" or not mine:
            continue
        lines.append(f"SECTION {key}:" if key != "happened" else "SECTION happened (and news):")
        lines += [_statement_line(i) for i in mine]
    return "\n".join(lines)



FALSE_MARKERS = ("false", "untrue", "not true", "contradict", "evidence shows", "disproved", "incorrect",
                 "no evidence", "refut")
HEDGE_MARKERS = ("reportedly", "according to report", "according to one report", "report", "it is said", "was said to", "were said to",
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
    add(p.get("context") or [])          # background, related events, explanation, reactions, next
    return out


def is_core(i: dict) -> bool:
    return (i.get("role") or "core") == "core"


def sections_from_payload(p: dict) -> dict[str, list[dict]]:  # kept for the cache key
    return {"story": ordered_items(p)}


def _statement_line(i: dict) -> str:
    line = f'#{i["id"]} {STATUS_LABEL.get(shade(i), "REPORTED")} | "{i["text"]}"'
    if not is_core(i):
        line += f" | CONTEXT: {i['role']}" + (f" ({i['related_event']})" if i.get("related_event") else "")
    if i.get("speaker"):
        line += f" | said by: {i['speaker']}"
    if i.get("responds_to"):
        line += " | response to: " + ", ".join(f"#{x}" for x in i["responds_to"])
    if i.get("responded_by"):
        line += " | answered by: " + ", ".join(f"#{x}" for x in i["responded_by"])
    if i.get("name_conflict"):
        line += " | NAMES DIFFER in the reports: " + " / ".join(i["name_conflict"])
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


_TL = threading.local()   # essays are written in parallel: reasons are kept per thread


def _reasons() -> dict[str, int]:
    if not hasattr(_TL, "reasons"):
        _TL.reasons = {}
    return _TL.reasons


def _no(reason: str):
    r = _reasons()
    r[reason] = r.get(reason, 0) + 1
    _TL.last = reason
    return None


def _validate(sentence: dict, by_id: dict[int, dict], banned: set[str], outlets: list[str],
              scope: set[str] = frozenset(), style: bool = True) -> list[int] | None:
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
    # outlet names never appear in the article as sources (the source links carry them); an outlet the
    # statements themselves name is part of the story ("The Wire journalist Mohammad Irfan was detained")
    stmt_text = " ".join(by_id[i]["text"] for i in ids)
    if style and any(re.search(rf"(?<!\w){re.escape(o)}(?!\w)", text) and not re.search(rf"(?<!\w){re.escape(o)}(?!\w)", stmt_text)
                     for o in outlets if len(o) >= 3):
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
    if re.search(r"(?i)\b(other|some) (reports|accounts) (differ|disagree|vary)\b|accounts differ\W*$"
                 r"|\b(reports|accounts) (dispute|contradict|challenge|question) (this|that|it|these)\b", text):
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
    # statements joined in one sentence must share a subject (a name, place, number or key word);
    # two businesses and two findings glued together because both were unconfirmed read as one fact
    if style and len(ids) > 1 and not _connected([by_id[i] for i in ids]):
        return _no("unrelated statements joined")
    return ids


SUBJECT_STOP = {"which", "their", "there", "these", "those", "about", "after", "before", "while", "would",
                "could", "should", "other", "under", "against", "between", "during", "where", "being",
                "since", "added", "stated", "according", "reports", "report", "people"}


PRONOUN_START = re.compile(r"^(he|she|they|it|his|her|their|its|this|these|those)\b", re.I)


def _subject_words(text: str) -> set[str]:
    from .textmatch import key_tokens
    words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", text)} - SUBJECT_STOP
    return words | {t.lower() for t in key_tokens(text)}


def _connected(stmts: list[dict]) -> bool:
    """Are these statements about one subject? Linked when they share a name, number or key word,
    when one is a contradiction of or response to the other, or when one refers back by pronoun."""
    sets = [_subject_words(i["text"]) for i in stmts]
    ids = [i["id"] for i in stmts]

    def linked(a: int, b: int) -> bool:
        if sets[a] & sets[b]:
            return True
        if ids[b] in _partners(stmts[a]) or ids[a] in _partners(stmts[b]):
            return True
        return bool(PRONOUN_START.match(stmts[a]["text"].strip()) or PRONOUN_START.match(stmts[b]["text"].strip()))
    seen, todo = {0}, [0]
    while todo:
        k = todo.pop()
        for j in range(len(stmts)):
            if j not in seen and linked(k, j):
                seen.add(j)
                todo.append(j)
    return len(seen) == len(stmts)


def _needs_hedge(sent: dict, by_id: dict[int, dict]) -> bool:
    """Not established and not pinned on a speaker: the paragraph must say it is only reported."""
    return any(by_id[x]["verdict"] not in ("corroborated", "confirmed") and not by_id[x].get("speaker")
               and by_id[x]["verdict"] != "disputed" for x in sent["ids"])


COMMON_FIRST = {"the", "a", "an", "police", "officials", "authorities", "protesters", "students", "residents",
                "villagers", "locals", "workers", "farmers", "troops", "security", "it", "this", "these", "there",
                "several", "many", "some", "two", "three", "four", "five", "an", "his", "her", "their", "its"}


def _hedge(text: str, proper: set[str] | None = None, single: bool = False) -> str:
    """One hedge for the paragraph, at the front: 'According to reports, ...'. A leading adverbial
    keeps its place ("Previously, according to reports, ..."); a common first word is lower-cased,
    a name is not (seen: "According to reports, Police are...")."""
    t = text.strip()
    m = re.match(r"^([A-Z][a-z]+),\s+(.*)$", t)
    lead = "according to one report" if single else "according to reports"
    if m:
        return f"{m.group(1)}, {lead}, {m.group(2)}"
    first = re.match(r"^(\w+)", t)
    if first:
        w = first.group(1)
        # lower-case a common first word; keep a name ("According to reports, Such" seen on a live page)
        is_name = (w in proper) if proper is not None else w.lower() not in COMMON_FIRST
        if not is_name and not w.isupper():
            t = t[0].lower() + t[1:]
    return lead[0].upper() + lead[1:] + ", " + t


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


LEANS_BACK = re.compile(
    r"^(he|she|they|it|this|that|these|those|his|her|their|its|both|the (?:company|firm|minister|ministry|"
    r"police|court|party|agency|regulator|government|department|official|officials|accused|victim|family|"
    r"actor|board|institute|university|bench|judge|spokesperson|group))\b"
    r"|\b(?:denied|rejected|disputed|refuted|dismissed|responded to|countered) (?:this|it|that|them|the "
    r"(?:claim|claims|allegation|allegations|finding|findings|report|reports|charge|charges))\b", re.I)


def _partners(i: dict) -> set[int]:
    return {x for x in (i.get("conflicts_with") or []) + (i.get("responds_to") or []) + (i.get("responded_by") or [])
            if isinstance(x, int) and x >= 0}


def _check_paragraphs(drafted: list[list], by_id, banned, outlets, style: bool = True) -> tuple[list[list[dict]], list[dict], int]:
    """Validated sentences only. Sentences that depend on each other stand or fall together
    (Oct 2026: "Nestle India denied this" survived while the claim it denied was dropped):
      - a sentence that leans on the one before it ("He added", "The company denied this") is tied to it;
      - a statement and its contradiction or response must both be in the essay, or neither is.
    A rejected sentence is dropped, never patched with plain wording. Returns the paragraphs, the
    sentences that failed a check themselves (with the reason, for the repair pass), and how many
    sentences left the essay."""
    flat: list[tuple[int, int, dict]] = []
    for p, para in enumerate(drafted):
        for k, sent in enumerate(para):
            flat.append((p, k, sent))
    n = len(flat)
    leans_on: dict[int, int] = {}       # sentence -> the sentence before it that it depends on

    ok_ids: list[list[int] | None] = [None] * n
    failed: list[dict] = []
    scope: set[str] = set()
    for idx, (p, k, sent) in enumerate(flat):
        if k == 0:
            scope = set()
        if k > 0 and isinstance(sent, dict) and LEANS_BACK.search(str(sent.get("text") or "")):
            leans_on[idx] = idx - 1
        _TL.last = None
        ids = _validate(sent, by_id, banned, outlets, scope, style) if isinstance(sent, dict) else None
        if ids is None:
            failed.append({"p": p, "k": k, "sentence": sent if isinstance(sent, dict) else {},
                           "reason": getattr(_TL, "last", None) or "not a sentence"})
            continue
        ok_ids[idx] = ids
        named = _named_speaker(sent["text"], ids, by_id)
        if named:
            scope = named
    # a sentence that leans on a dropped sentence goes with it ("He added..." without its "he");
    # then a statement whose partner (contradiction or response) is in the story but nowhere in the
    # essay takes its sentence out too, and so on until nothing changes
    alive = [ok_ids[i] is not None for i in range(n)]

    def drop_units():
        for i in range(n):   # in order, so chains of "He said... He added..." fall together
            if alive[i] and i in leans_on and not alive[leans_on[i]]:
                alive[i] = False
    drop_units()
    while True:
        covered = {x for i in range(n) if alive[i] for x in ok_ids[i]}
        orphan = [i for i in range(n) if alive[i]
                  and any(_partners(by_id[x]) & set(by_id) and not (_partners(by_id[x]) & covered)
                          for x in ok_ids[i])]
        if not orphan:
            break
        for i in orphan:
            alive[i] = False
        drop_units()
    paragraphs: list[list[dict]] = []
    kept: list[int] = []
    _TL.kept = kept          # which drafted paragraphs survived (their sections, for the headings)
    for p, para in enumerate(drafted):
        out = [{"text": re.sub(r"\.{2,}$", ".", flat[i][2]["text"].strip()), "ids": ok_ids[i]}
               for i in range(n) if flat[i][0] == p and alive[i]]
        if out:
            kept.append(p)
            paragraphs.append(out)
    return paragraphs, failed, sum(1 for a in alive if not a)


def _said_in(item: dict, sentences: list[str]) -> bool:
    """Does the essay already say this statement, in other words? All its names and numbers and most
    of its key words in one sentence (Oct 2026: near-duplicates the writer had merged were listed
    again under the essay)."""
    from .textmatch import key_tokens
    toks = {t.lower() for t in key_tokens(item["text"])}
    words = _subject_words(item["text"])
    if not words:
        return False
    for s_ in sentences:
        low = s_.lower()
        if toks and not all(t in low for t in toks):
            continue
        sw = _subject_words(s_)
        if len(words & sw) >= (0.6 if toks else 0.75) * len(words):
            return True
    return False


def _response_sentence(i: dict) -> str:
    text = i["text"].strip().rstrip(".")
    sp = i.get("speaker")
    if sp and not _speaker_is_subject(sp, text):
        return f"{sp} said {_soft_lower(text)}."
    return text + "."


def _also(items: list[dict], covered: set[int], by_id: dict[int, dict], essay: list[str] | None = None) -> list[dict]:
    """Statements the essay does not carry, listed under it in plain words ("Also reported"), so
    nothing is silently dropped and the essay itself stays the writer's prose. A statement the essay
    already says in other words is not listed again; a claim and its response, or two contradicting
    statements, are listed together."""
    out, done = [], set(covered)
    essay = essay or []
    for i in items:
        if i["id"] in done:
            continue
        if _said_in(i, essay) and not _partners(i):
            done.add(i["id"])
            continue
        partner = next((by_id[o] for o in i.get("conflicts_with") or []
                        if o in by_id and o not in done and o >= 0), None)
        answer = next((by_id[o] for o in i.get("responded_by") or [] if o in by_id and o >= 0), None)
        asked = next((by_id[o] for o in i.get("responds_to") or [] if o in by_id and o >= 0), None)
        if i["verdict"] == "disputed" and partner:
            out.append({"text": disputed_pair(i, partner), "ids": [i["id"], partner["id"]]})
            done.update({i["id"], partner["id"]})
        elif answer is not None or asked is not None:
            claim, resp = (i, answer) if answer is not None else (asked, i)
            out.append({"text": f"{plain_sentence(claim)} {_response_sentence(resp)}",
                        "ids": [claim["id"], resp["id"]]})
            done.update({claim["id"], resp["id"]})
        else:
            out.append({"text": plain_sentence(i), "ids": [i["id"]]})   # coloured; no hedge words
            done.add(i["id"])
    return out


_WHO = r"(?:one|another|a second|the other|some|other|several|many|media|news|early|initial|unconfirmed)"
PER_SENTENCE_HEDGE = [
    (re.compile(rf"(?i)^(?:according to (?:{_WHO} )?reports?,?\s*|(?:{_WHO} )?reports? (?:said|says|say|stated|"
                rf"added|claimed|suggest(?:ed)?|indicated?)(?: that)?,?\s+|it (?:is|was|has been) reported(?: that)?,?\s+)"), ""),
    (re.compile(rf"(?i),?\s+(?:and|while|but|whereas)\s+(?:{_WHO} )?reports? (?:said|says|say|stated|added|claimed)"
                r"(?: that)?,?\s+"), "; "),
    (re.compile(rf"(?i),?\s+(?:according to (?:{_WHO} )?reports?|(?:{_WHO} )?reports? (?:said|say|says))(?=\s*[.;]?$)"), ""),
    (re.compile(rf"(?i),\s+according to (?:{_WHO} )?reports?,\s+"), ", "),
    (re.compile(r"(?i)\breportedly\s+"), ""),
    (re.compile(r"(?i),\s*reportedly(?=[,.])"), ""),
]


def _one_hedge(text: str) -> str:
    """No "according to reports", "reportedly", "one report said ... another report said" (owner, Oct 7
    2026, option A: one author's voice; the colour says how well each sentence is supported). Done by
    code: the writer models do not follow the rule reliably."""
    t = text
    for pat, rep in PER_SENTENCE_HEDGE:
        t = pat.sub(rep, t)
    t = t.strip()
    return t[:1].upper() + t[1:] if t else text


def _finish(payload: dict, paragraphs: list, also: list, by_id: dict, meta: dict) -> dict:
    """Hedges, source numbers and colours for the essay and the statements it does not carry."""
    # names: capitalised words the statements use mid-sentence (so "Police" at a sentence start is not one)
    proper = {w for i in by_id.values() for w in re.findall(r"(?<=[a-z,;] )[A-Z][\w'-]+", i.get("text") or "")}
    for para in paragraphs:
        for x in para:
            # a dispute keeps "some reports say 40, others 50": that is whose each version is
            if not any(by_id[i]["verdict"] == "disputed" or by_id[i].get("conflicts_with") for i in x["ids"]):
                x["text"] = _one_hedge(x["text"])
    numbering: dict[str, int] = {}
    for sent in [x for para in paragraphs for x in para] + also:
        for x in sent["ids"]:
            for s in by_id[x]["sources"]:
                numbering.setdefault(s["url"], len(numbering) + 1)
    for s in payload["sources"]:
        numbering.setdefault(s["url"], len(numbering) + 1)
    for sent in [x for para in paragraphs for x in para] + also:
        sent["class"] = CLASS[max(RANK.get(shade(by_id[x]), 2) for x in sent["ids"])]
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
                paragraphs=paragraphs, not_in_essay=also, sources=source_list,
                covers=sorted({x for para in paragraphs for s in para for x in s["ids"]}))


def essay_ok(nar: dict, payload: dict) -> bool:
    """Good enough to publish: written by the writer, not badly off task (no more than half its
    sentences left the essay), and carrying at least 85% of EVERY statement known about the story:
    the event, who says what, one-outlet lines, background, related events, what next (owner, Oct 7
    2026: the middle way between "complete or nothing" and short articles)."""
    if not nar.get("model") or not nar.get("paragraphs"):
        return False
    sents = sum(len(p) for p in nar["paragraphs"])
    if nar.get("rejected", 0) > sents:
        return False
    every = [i["id"] for i in ordered_items(payload)]
    if not every:
        return True
    return len(set(every) & set(nar.get("covers") or [])) >= COMPLETE * len(every)


def _target_length(items: list[dict]) -> str:
    """Roughly how long the article should be: it follows the material, never padded."""
    lo = max(5, min(50, int(0.8 * len(items))))       # every statement is written: about one sentence each
    return f"{lo}-{lo + max(3, lo // 3)}"


def _call_writer(router: Router, prompt: str) -> tuple[list[tuple[str, list]], str | None, str | None]:
    """([(section, paragraph)], model, failure)."""
    try:
        # at most 3 tries per run: when Flash is overloaded, 8 tries burned a quarter of an hour's
        # writer calls on one story; the story is simply tried again next run
        # enough attempts to get past refusing Flash models (each is dropped after 3 refusals, on every
        # key) to the last resort, 3.5 Flash-Lite: with 3, a call gave up before ever reaching it (Oct 6)
        res = router.call("writer", prompt, json_out=True, max_output_tokens=7000, max_attempts=16)
    except QuotaExhausted as e:
        log.info("narrative: writer quota used up for now (%s)", e)
        return [], None, "quota"
    except ValueError as e:
        log.info("narrative: writer reply unusable (%s)", e)
        return [], None, "unparseable reply"
    except Exception as e:  # noqa: BLE001
        log.warning("narrative failed: %s", e)
        return [], None, f"error: {str(e)[:80]}"
    drafted = _sections_of(res.data)
    return drafted, res.model, None if drafted else "no paragraphs in reply"


def _sections_of(data) -> list[tuple[str, list]]:
    """[(section key, paragraph)] from a reply: {"sections": [...]}, or plain {"paragraphs": [...]}."""
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, list]] = []
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        key = sec.get("key") if sec.get("key") in SECTION_KEYS else "happened"
        out += [(key, p) for p in sec.get("paragraphs") or [] if isinstance(p, list)]
    if not out and isinstance(data.get("paragraphs"), list):
        out = [("happened", p) for p in data["paragraphs"] if isinstance(p, list)]
    return out


NAME_LEAD_STOP = {"The", "A", "An", "In", "On", "At", "By", "For", "From", "According", "After", "Before",
                  "Reports", "Some", "Other", "Meanwhile", "Later", "Earlier"}


def _people(items: list[dict]) -> str:
    """The fullest form in which the statements name each person or body ("AAP Delhi Chief Bharadwaj",
    "Ravindra Wakode"), so the writer introduces them properly (Oct 2026: "Bharadwaj and Jha were
    detained" with no first name, role or context)."""
    best: dict[str, str] = {}
    texts = [i["text"] for i in items] + [i.get("speaker") or "" for i in items]
    for t in texts:
        for m in re.finditer(r"(?:[A-Z][\w.&'-]*\s+){1,5}[A-Z][a-z]{3,}", t):
            phrase = m.group(0).strip()
            words = phrase.split()
            while words and words[0] in NAME_LEAD_STOP:
                words = words[1:]
            if len(words) < 2:
                continue
            key = words[-1]
            phrase = " ".join(words)
            if len(phrase) > len(best.get(key, "")):
                best[key] = phrase
    if not best:
        return ""
    return ("People and bodies, in the fullest form the statements name them (use it at first mention):\n"
            + "; ".join(sorted(best.values())[:40]) + "\n\n")


def _article_with_sections(drafted: list[tuple[str, list]]) -> str:
    out, last = [], None
    for key, para in drafted:
        if key != last:
            out.append(f"== {key} ==")
            last = key
        for sent in para:
            if isinstance(sent, dict):
                out.append(f'  "{str(sent.get("text") or "")}" | ids: {sent.get("ids")}')
    return "\n".join(out)


def _in_order(drafted: list[tuple[str, list]]) -> list[tuple[str, list]]:
    """The sections in the page's order (SECTIONS), whatever order the model wrote them in; paragraphs
    keep their order within a section."""
    order = {k: n for n, k in enumerate(SECTION_KEYS)}
    return sorted(drafted, key=lambda kp: order.get(kp[0], 99))


def _check_sections(drafted, by_id, banned, outlets):
    drafted = _in_order(drafted)
    paragraphs, failed, rejected = _check_paragraphs([p for _, p in drafted], by_id, banned, outlets)
    keys = [drafted[k][0] for k in getattr(_TL, "kept", [])]
    for f in failed:
        f["section"] = drafted[f["p"]][0] if f["p"] < len(drafted) else "happened"
    return paragraphs, keys, failed, rejected


COMPLETE = 0.85     # owner, Oct 7 2026: an article must carry 85% of everything known (the middle way)


def write_narrative(router: Router | None, payload: dict, banned: set[str], draft: dict | None = None) -> dict:
    """The article, in sections (owner, Oct 7 2026): one sectioned draft, then a fill pass for each
    group of sections that left statements out or has failed sentences, a few sections per call, so even
    Flash-Lite carries everything. Each sentence is checked; a section rewrite is kept only if the
    article then carries at least as much."""
    items, background, by_id, outlets, banned = _context(payload, banned)
    sec = assign_sections(items, background)
    drafted: list[tuple[str, list]] = []
    model, failure = None, None
    _reasons().clear()
    resumed = bool(draft and draft.get("sections") and draft.get("model"))
    if resumed:
        # an earlier try that fell short is finished, not thrown away (owner, Oct 7 2026): its sections
        # are kept and only what is still missing is written; new statements simply count as missing
        drafted = [(k if k in SECTION_KEYS else "happened", p) for k, p in draft["sections"] if isinstance(p, list)]
        model = draft["model"]
    elif router is not None and items:
        bg = ""
        if background:
            bg = ("This story is a later development of an earlier story on the site. The section "
                  "\"background\" holds facts from the earlier story: give at most TWO sentences of them.\n\n")
        prompt = WRITER_PROMPT.format(banned=", ".join(sorted(banned)) or "(none)", background=bg,
                                      statements=_section_block(items + background, sec),
                                      length=_target_length(items), people=_people(items))
        drafted, model, failure = _call_writer(router, prompt)

    drafted = _in_order(drafted)
    paragraphs, keys, failed, rejected = _check_sections(drafted, by_id, banned, outlets)
    first_reasons = dict(_reasons())
    filled = []
    # up to two rounds over the section groups: a group still short after its first fill gets one more
    # (three for a resumed draft: statements may have joined the story since it was written)
    rounds = [g for _ in range(3 if resumed else 2) for g in FILL_GROUPS]
    for n_call, group in enumerate(rounds):
        if not model or router is None:
            break
        if n_call >= len(FILL_GROUPS):
            cov_now = {x for para in paragraphs for s_ in para for x in s_["ids"]}
            if len({i["id"] for i in items} & cov_now) >= COMPLETE * len(items) and not failed:
                break                                  # complete enough: no second round
        covered = {x for para in paragraphs for s_ in para for x in s_["ids"]}
        essay_text = [x["text"] for para in paragraphs for x in para]
        missing = [i for i in items if sec.get(i["id"]) in group and i["id"] not in covered
                   and not _said_in(i, essay_text)]
        bad = [f for f in failed if f.get("section") in group or (f.get("section") == "news" and "news" in group)]
        no_lead = "news" in group and "news" not in keys
        if not missing and not bad and not no_lead:
            continue
        mine = [i for i in items if sec.get(i["id"]) in group]
        prompt = FILL_PROMPT.format(
            banned=", ".join(sorted(banned)) or "(none)", article=_article_with_sections(drafted),
            keys=", ".join(k for k in group if k == "news" or any(sec.get(i["id"]) == k for i in mine)),
            statements="\n".join(f"[{sec[i['id']]}] " + _statement_line(i) for i in mine),
            failed="\n".join(f'- "{str(f["sentence"].get("text") or "")}" | problem: {f["reason"]}' for f in bad) or "(none)")
        try:
            res = router.call("writer", prompt, json_out=True, max_output_tokens=6000, max_attempts=8)
        except Exception as e:  # noqa: BLE001
            log.info("narrative: section fill not done (%s)", str(e)[:120])
            continue
        new = [(k, p) for k, p in _sections_of(res.data) if k in group]
        if not new:
            continue
        # the group's sections replaced in place, the rest of the article as it was
        trial: list[tuple[str, list]] = []
        placed = False
        for k, p in drafted:
            if k in group:
                if not placed:
                    trial += new
                    placed = True
                continue
            trial.append((k, p))
        if not placed:
            order = {k: n for n, k in enumerate(SECTION_KEYS)}
            trial = sorted(trial + new, key=lambda kp: order.get(kp[0], 99))
        saved = dict(_reasons())
        _reasons().clear()
        trial = _in_order(trial)
        p2, k2, f2, r2 = _check_sections(trial, by_id, banned, outlets)
        cov2 = {x for para in p2 for s_ in para for x in s_["ids"]}
        if len(cov2) >= len(covered):
            drafted, paragraphs, keys, failed, rejected = trial, p2, k2, f2, r2
            filled.append("+".join(group))
        else:
            _reasons().clear()
            _reasons().update(saved)
    # an article always opens with the news (owner's rule): if the writer gave no "news" section, the
    # first "What happened" paragraph is the lead (Oct 7 2026, story 12687 opened with background)
    if paragraphs and "news" not in keys and "happened" in keys:
        k = keys.index("happened")
        paragraphs = [paragraphs[k]] + paragraphs[:k] + paragraphs[k + 1:]
        keys = ["news"] + keys[:k] + keys[k + 1:]
    covered = {x for para in paragraphs for s in para for x in s["ids"]}
    also = _also(items, covered, by_id, [x["text"] for para in paragraphs for x in para])
    if rejected:
        log.info("narrative: %d sentences left the essay (%s)", rejected, dict(_reasons()))
    return _finish(payload, paragraphs, also, by_id,
                   {"model": model, "rejected": rejected, "reject_reasons": dict(_reasons()), "failure": failure,
                    "first_draft_reasons": first_reasons, "repaired": bool(filled), "filled": filled,
                    "section_keys": keys, "resumed": resumed,
                    # the sections as drafted, kept with the story if the article falls short (not published)
                    "drafted": [[k, p] for k, p in drafted]})


def recolour(old: dict, payload: dict, banned: set[str]) -> dict:
    """Keep the essay as written and bring it up to date without a model call: each sentence takes
    the current colour of the statements it rests on. A sentence whose statements are gone, or that
    no longer passes the checks (a fact that is now disputed, say), leaves the essay with every
    sentence that depends on it; statements the essay does not carry are listed under it. Used when
    only verdicts changed, or the writer is out."""
    items, background, by_id, outlets, banned = _context(payload, banned)
    _reasons().clear()
    drafted = [[{"text": s.get("raw") or s["text"], "ids": [x for x in s["ids"] if x in by_id]} for s in para]
               for para in old.get("paragraphs") or []]
    drafted = [[s for s in para if s["ids"]] for para in drafted]
    # an essay already written is held to the truth checks (numbers, speakers, disputes, false claims,
    # claim/response pairs), not to style rules added after it was written: re-checking style stripped
    # sentences out of live essays and moved their facts under them (Oct 5 2026)
    paragraphs, _, rejected = _check_paragraphs([p for p in drafted if p], by_id, banned, outlets, style=False)
    covered = {x for para in paragraphs for s in para for x in s["ids"]}
    also = _also(items, covered, by_id, [x["text"] for para in paragraphs for x in para])
    before = sum(len(p) for p in old.get("paragraphs") or [])
    return _finish(payload, paragraphs, also, by_id,
                   {"model": old.get("model"), "rejected": old.get("rejected", 0), "recoloured": True,
                    "dropped_since_written": before - sum(len(p) for p in paragraphs),
                    "reject_reasons": old.get("reject_reasons") or {}, "written_hash": old.get("written_hash", old.get("hash"))})


def needs_rewrite(old: dict | None, payload: dict) -> bool:
    """A rewrite is worth a writer call only for a material change: statements of the story's own
    event that are not minor and that the essay does not carry, or a quarter of its sentences no
    longer standing."""
    if not old or not old.get("paragraphs") or not old.get("model") or old.get("writer") != WRITER_VERSION:
        return True
    covers = set(old.get("covers") or [x for p in old["paragraphs"] for s in p for x in s["ids"]])
    new_major = [i for i in ordered_items(payload) if not i.get("minor") and is_core(i) and i["id"] not in covers]
    sents = sum(len(p) for p in old["paragraphs"]) + old.get("dropped_since_written", 0)
    return bool(new_major) or old.get("dropped_since_written", 0) > sents // 4
