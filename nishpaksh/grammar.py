"""Grammar by statement status (owner, Oct 10 2026, phase 2 of "grammar, not rules").

The writer models are small, and every rule added after a failure was a prohibition checked AFTER the
sentence was written; a failed sentence is dropped. This module turns the predictable part of that into
grammar, in two directions, with no model in it:

  STATUS        one table: for each status a statement can have (the colour the page shows), what the words
                of a sentence about it may carry, and which markers it must or must not contain. The colour
                carries the support, so the words carry little: the four plain statuses (established,
                developing, not cross-checked, one outlet only) are written plainly, with attribution only
                for the statement's own named speaker; "sources disagree" gives both versions, each with its
                holder; "false" gives the claim, who made it, and what the evidence shows.
  shape_line    GIVEN to the writer with each statement that is not plain: the one sentence shape that
                applies to it, so it makes fewer decisions (nothing for a plain statement).
  repair        APPLIED by code to a sentence that failed one of the checks and whose fault has exactly one
                safe fix (a missing full stop, a speaker left out, a false claim without its evidence, a
                pronoun with no one before it, a stronger verb than the outlets used). The repaired sentence
                is checked again by every check; if it still fails, the original failure stands. So a repair
                can only turn a dropped sentence into a published one that passes, never lower a check.
  empty_setup   a sentence that cites statements but says nothing of them ("Kabir set out his position.").

Everything here is a function of the sentence and the statements it cites.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import voice

# ------------------------------------------------------------------ the markers (moved here from narrative.py,
# which still exports them)
FALSE_MARKERS = ("false", "untrue", "not true", "contradict", "evidence shows", "disproved", "incorrect",
                 "no evidence", "refut")
HEDGE_MARKERS = ("reportedly", "according to report", "according to one report", "report", "it is said", "was said to", "were said to",
                 "accounts differ", "according to early", "unconfirmed", "allegedly")
ATTRIBUTION_VERBS = ("said", "say", "says", "alleg", "claim", "accus", "denied", "deny", "denies", "demand",
                     "told", "stated", "according to", "maintain", "insist", "assert")
DISPUTE_MARKERS = ("differ", "disput", "contradict", "others", "while", "however", "but ", "conflicting",
                   "versions", "other reports")


@dataclass(frozen=True)
class Shape:
    """What a sentence about a statement of this status may look like."""
    colour: str                     # what the page's colour already tells the reader
    words: str                      # what the words may carry
    required_any: tuple = ()        # at least one of these must appear in the sentence
    plain: bool = True              # nothing about how well it is supported is written in words


_PLAIN = "plain declarative; attribution only for the statement's own named speaker"
STATUS: dict[str, Shape] = {
    "established": Shape("confirmed by independent outlets", _PLAIN),
    "developing": Shape("reported by several outlets, still developing", _PLAIN),
    "unverified": Shape("not cross-checked", _PLAIN),
    "single": Shape("one outlet only (purple)", _PLAIN),
    "disputed": Shape("sources disagree", "both versions, each with its holder",
                      DISPUTE_MARKERS + ATTRIBUTION_VERBS, plain=False),
    "false": Shape("shown false", "the claim, who made it, and what the evidence shows",
                   FALSE_MARKERS, plain=False),
}


def missing_marker(status: str, low: str) -> bool:
    """True when the status requires one of its markers and the (lower-case) sentence has none."""
    need = STATUS[status].required_any if status in STATUS else ()
    return bool(need) and not any(m in low for m in need)


# ------------------------------------------------------------------ small helpers
def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-zऀ-ॿ]{4,}", (s or "").lower())}


def _ids(sent: dict, by_id: dict) -> list[int]:
    out = []
    for x in sent.get("ids") or []:
        m = re.search(r"-?\d+", str(x))
        if m and int(m.group(0)) in by_id:
            out.append(int(m.group(0)))
    return list(dict.fromkeys(out))


def source_text(ids: list[int], by_id: dict) -> str:
    """The words behind a sentence: its statements, their time words and, for a false one, the evidence."""
    return " ".join(by_id[i]["text"] + " " + ((by_id[i].get("time") or {}).get("when_text") or "")
                    + " " + " ".join(by_id[i]["check"]["reasons"] if by_id[i].get("check") else [])
                    for i in ids)


def _strip_end(text: str) -> str:
    return re.sub(r"[.!?][\"”'’)]*$", "", text.rstrip()).rstrip()


# ------------------------------------------------------------------ shape lines (given to the writer)
def shape_line(item: dict, status: str) -> str | None:
    """The one sentence shape for a statement, or None when it is plain (the prompt's general rules
    are enough: state it, no attribution). Written for the statement's own speaker, verb and evidence."""
    sp = item.get("speaker")
    if status == "false":
        why = (item.get("check") or {}).get("reasons") or []
        claim = f"{sp} said" if sp else "it was claimed"
        return (f'"<the claim>, {claim}. The evidence shows this is false' + (": <the evidence given>" if why else "")
                + '."')
    if status == "disputed":
        if item.get("conflicts_with"):
            return ("ONE sentence with both versions, each pinned on its holder, citing both ids "
                    '("<A>, X said; <B>, Y said.")')
        if sp:
            return f'"<the claim>, {sp} said", then that it is denied (the denial is given with the statement)'
        return None
    if sp:
        act = voice.ACT.search(item.get("text") or "")
        if act:
            return f'pin it on {sp} with the statement\'s own verb "{act.group(0).lower()}", never a stronger one'
        return f'"<the content>, {sp} said."'
    return None


# ------------------------------------------------------------------ empty set-up
EMPTY_SETUP = re.compile(
    r"(?i)^\W*(?:[\w.'’-]+\s+){1,6}?(?:set out|outlined|laid out|presented|shared|expressed|explained|gave|offered|"
    r"made|reiterated|detailed|spoke about)\s+(?:his|her|their|its)\s+(?:position|views?|stance|version|account|"
    r"case|remarks|comments?|argument|side|thoughts)\W*$"
    r"|^\W*(?:[\w.'’-]+\s+){1,6}?(?:made|issued|gave|released)\s+(?:a|the)\s+(?:statement|comment|remark)s?\W*$")


def empty_setup(text: str) -> bool:
    """\"Kabir set out his position.\": cites a statement and says nothing of it. Short, no figure."""
    t = (text or "").strip()
    from . import figures
    return (len(t.split()) <= 10 and not re.search(r"\d", t) and not figures.value_set(t, spoken_min=figures.SPOKEN_MIN)
            and bool(EMPTY_SETUP.search(t)))


# ------------------------------------------------------------------ repairs
DANGLING = {"and", "or", "the", "of", "to", "in", "by", "with", "a", "an", "that", "which", "for", "at", "on", "from",
            "as", "but", "than", "into", "over", "after", "before", "while", "his", "her", "their", "its"}
# a sentence opening with one of these is the second half of another sentence, never a whole one
CONNECTIVE = re.compile(r"(?i)^(and|but|or|while|whereas|which|who|whom|whose|as well as|with|although|though)\b")
_TERMINAL = re.compile(r"[.!?][\"”'’)]*$")
_CLOSERS = re.compile(r"[\"”'’)]*$")
FALSE_CLAUSE = " The evidence shows this is false"
NEUTRAL_OK = {"claim", "insist", "warn", "threaten", "conced", "admitt", "confess", "vow"}   # never a negation


def _close(text: str, ids: list[int], by_id: dict, scope: set[str]) -> str | None:
    """A sentence that stops without its full stop gets one, unless it stops on a joining word or a comma
    (then it is cut off, not finished)."""
    t = text.strip()
    if not t or _TERMINAL.search(t) or CONNECTIVE.match(t) or len(t.split()) < 4:
        return None                              # whole already, or a fragment / too short to be trusted
    m = _CLOSERS.search(t)
    core = t[:m.start()].rstrip()
    if not core or re.search(r"[,;:–—-]$", core):
        return None
    last = re.findall(r"[\w'’-]+", core)
    if not last or last[-1].lower() in DANGLING:
        return None
    return core + "." + t[m.start():]


def _pin(text: str, ids: list[int], by_id: dict, scope: set[str]) -> str | None:
    """A claim with a known speaker, written without naming him: \"..., X said.\" at the end of the claim.
    Only when ONE speaker is missing, no other speaker is among the statements, and the sentence does not
    already attribute the claim to someone."""
    # a sentence that already has a speech verb ("The minister said ...") was attributed by the writer, and to the
    # wrong person, or this fault would not be left: appending a second speaker would attribute it twice
    if (voice.attributed(text) or voice.ACT.search(text) or re.search(r"[\"”]$", text.strip())
            or re.search(rf"(?i)\b{voice.SAY_VERB}\b|\baccording to\b|\balleg", text)):
        return None
    low = text.lower()
    speakers = {voice.speaker_key(by_id[i]["speaker"]): by_id[i]["speaker"] for i in ids if by_id[i].get("speaker")}
    missing = {voice.speaker_key(by_id[i]["speaker"]): by_id[i]["speaker"] for i in ids
               if by_id[i].get("speaker") and by_id[i]["verdict"] not in ("corroborated", "confirmed")
               and not (_words(by_id[i]["speaker"]) & (_words(text) | set(scope))) and "alleg" not in low}
    if len(missing) != 1 or len(speakers) != 1 or None in speakers:
        return None
    who = next(iter(missing.values()))
    head, sep, tail = text.partition(FALSE_CLAUSE)      # a false claim: the attribution goes before the evidence
    return f"{_strip_end(head)}, {who} said.{sep}{tail}"


def _false(text: str, ids: list[int], by_id: dict, scope: set[str]) -> str | None:
    """A false claim that does not say it is false: the evidence the statement carries is added. Only when
    every statement of the sentence is the one false claim (the clause must not colour other facts)."""
    falses = [i for i in ids if by_id[i]["verdict"] == "false"]
    if len(falses) != 1 or len(ids) != 1:
        return None
    why = ((by_id[falses[0]].get("check") or {}).get("reasons") or [])
    core = text.rstrip()
    if not _TERMINAL.search(core):
        core += "."
    return core + FALSE_CLAUSE + (f": {str(why[0]).strip().rstrip('.')}" if why else "") + "."


def _verb(text: str, ids: list[int], by_id: dict, scope: set[str]) -> str | None:
    """\"claimed that\" where the statements only say someone said it: the neutral verb. Never for a verb
    that negates (\"denied that X\" must not become \"said that X\")."""
    bad = voice.unsupported_acts(text, source_text(ids, by_id))
    if not bad:
        return None
    new = text
    for m in sorted(voice.ACT.finditer(text), key=lambda m: -m.start()):
        if m.group(0).lower() in bad and m.group(1).lower() in NEUTRAL_OK:
            tail = re.match(r"\s+that\b", new[m.end():])
            if tail:
                new = new[:m.start()] + "said" + new[m.end():]
    return new if new != text else None


def _pronoun(text: str, ids: list[int], by_id: dict, scope: set[str]) -> str | None:
    """"He added that ..." with no one before it: the speaker's name (the names pass then writes it as
    the first mention or the surname). Only a sentence that attributes with the pronoun, and one speaker."""
    m = re.match(rf"^(He|She)\s+{voice.SAY_VERB}\b", text)
    speakers = {voice.speaker_key(by_id[i]["speaker"]): by_id[i]["speaker"] for i in ids if by_id[i].get("speaker")}
    if not m or len(speakers) != 1 or None in speakers:
        return None
    return next(iter(speakers.values())) + text[len(m.group(1)):]


REPAIRS = {
    "not a full sentence": _close,
    "claim without its speaker": _pin,
    "false without saying so": _false,
    "verb the statements do not use": _verb,
    "pronoun without evidence": _pronoun,
}


def repair(sent: dict, reason: str, by_id: dict, scope: set[str] = frozenset()) -> dict | None:
    """The sentence with the one safe fix for `reason` applied, or None. The caller checks it again."""
    fn = REPAIRS.get(reason)
    if fn is None or not isinstance(sent, dict):
        return None
    text = str(sent.get("text") or "").strip()
    ids = _ids(sent, by_id)
    if not text or not ids:
        return None
    new = fn(text, ids, by_id, scope)
    if not new or new == text:
        return None
    out = dict(sent, text=new)
    out.pop("parts", None)              # the colour parts no longer match the words: the sentence has one colour
    return out
