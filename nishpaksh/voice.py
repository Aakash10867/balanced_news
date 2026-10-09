"""Who speaks, and how the article refers to them (owner, Oct 8 2026, story 13107).

"Humayun Kabir added that ... Humayun Kabir also stated that ... Humayun Kabir further stated that ..."
came out of four layers each patching one sentence. The owner's structure: code owns WHO speaks, the
model writes WHAT is said.

    split          a statement "X stated that Y" is given to the writer as Y, said by X: the model has
                   the content to write, not an attribution to copy
    verbs          the verb that names the act ("accused", "denied", "threatened") is the one the
                   outlets used, never a stronger one chosen by the model; otherwise "said"
    pronouns       "he" / "she" only with evidence: two outlets' statements that use it for this person,
                   with no other person named in them, and none that use the other (owner: a wrong
                   pronoun harms a real person; without evidence, the surname or the role)
    roles          "the MLA", "the minister": a reference that carries no gender, used when only one
                   person in the article has that role
    problems       a paragraph's coherence measured by code: what decides a rewrite and whether the
                   rewrite is kept
"""
from __future__ import annotations

import re

from .style import ROLES_LOWER, _candidates

# verbs that say what KIND of act a statement is; each may be written only if the statements say so,
# as a verb or as its noun ("alleged" is not here: it protects, and has its own rule, "allegedly stays as
# long as outlets say it"). {verb root in the sentence: what in the statements supports it}
ACT_VERBS = {
    "accus": ("accus",), "claim": ("claim",), "deni": ("deni", "deny", "denial"), "admitt": ("admit", "admission"),
    "conced": ("conced", "concession"), "insist": ("insist",), "slamm": ("slam",), "blam": ("blam",),
    "threaten": ("threat",), "warn": ("warn",), "refus": ("refus",), "reject": ("reject",),
    "dismiss": ("dismiss",), "confess": ("confess",), "vow": ("vow",), "lash": ("lash",),
    "criticis": ("critic",), "criticiz": ("critic",), "condemn": ("condemn",), "slander": ("slander",),
}
# the verb as written: past or -ing ("accused", "denying"); a noun in the sentence ("the claim") is no act
ACT = re.compile(r"(?i)\b(" + "|".join(sorted(ACT_VERBS, key=len, reverse=True)) + r")(?:ed|ied|ing)\b")
# only the neutral verbs are taken off: a statement whose verb is an act ("accused X of", "denied",
# "claimed") keeps it, so the writer keeps the outlets' verb with its content
SAY = r"(?:stated|states|said|says|added|adds|told|noted|remarked|mentioned|explained|asserted|maintained)"


def _surname(name: str) -> str:
    return (name or "").split()[-1] if name else ""


def split(text: str, speaker: str | None) -> tuple[str, str | None]:
    """("Y", "stated") for "Humayun Kabir stated that Y" when the statement opens with its own
    speaker; (text, None) otherwise. Only the opening attribution is taken off; a statement that is
    itself reported speech about someone else ("A said that B claimed X") keeps the rest as it is."""
    if not speaker:
        return text, None
    sur = _surname(speaker)
    if len(sur) < 3:
        return text, None
    m = re.match(rf"^(?:[\w.,'()&-]+\s+){{0,9}}?{re.escape(sur)}\b(?:\s+(?:also|further|then|later|had|has))?"
                 rf"\s+({SAY})(?:\s+(?:on|in)\s+[A-Z]\w+(?:\s+\d+)?)?(?:\s+that)?,?\s+(?P<body>.+)$", text.strip())
    if not m or len(m.group("body")) < 15:
        return text, None
    if re.match(r"(?i)(while|when|after|before|during|as|at|on|in|from|to|with|that)\b", m.group("body")):
        return text, None               # "stated while being taken away that ...": not a clean split
    body = m.group("body")
    return body[:1].upper() + body[1:], m.group(1).lower()


def unsupported_acts(sentence: str, source_text: str) -> set[str]:
    """Act verbs in a sentence that the statements behind it do not use (owner, Oct 8 2026: never a
    stronger verb than the outlets used; "said" otherwise)."""
    low = (source_text or "").lower()
    out = set()
    for m in ACT.finditer(sentence or ""):
        root = m.group(1).lower()
        if root == "accus" and re.search(r"(?i)\b(the|an|two|three|four|all|both|other|of)\s+$", sentence[:m.start()]):
            continue                            # "the accused": a person, not an act
        if not any(x in low for x in ACT_VERBS[root]):
            out.add(m.group(0).lower())
    return out


MALE = re.compile(r"(?i)\b(he|him|his|himself)\b")
FEMALE = re.compile(r"(?i)\b(she|her|hers|herself)\b")
OTHER_ONE = re.compile(r"(?i)\b(?:a|an|the|his|her|one|another)\s+(?:[a-z-]+\s+)?(?:[a-z]+-)?(?:" + "|".join(sorted(ROLES_LOWER))
                       + r"|man|woman|boy|girl|person|official|leader|member|candidate|supporter)\b")


def pronouns(items: list[dict], min_outlets: int = 2) -> dict[str, str]:
    """{full name: "he" | "she"} for the people the outlets' statements refer to with a pronoun, only
    where it cannot be anyone else: the person is the only one named in the statement (a body such as
    "police" is no person), and no other singular person ("a sub-inspector", "his son") is in it.
    Two outlets needed; any statement using the other pronoun for them cancels it."""
    names: set[str] = set()
    for i in items:
        if i.get("speaker") and len(i["speaker"].split()) >= 2:
            names.add(i["speaker"])
        names |= _candidates(i.get("text") or "")
    out = {}
    for name in names:
        sur = _surname(name)
        if len(sur) < 3:
            continue
        seen = {"he": set(), "she": set()}
        for i in items:
            t = i.get("text") or ""
            if not re.search(rf"\b{re.escape(sur)}\b", t):
                continue
            others = {c for c in _candidates(t) if _surname(c) != sur}
            if others:
                continue
            rest = re.sub(rf"(?:[A-Z][\w.-]*\s+)*{re.escape(sur)}\b", " ", t)
            male, female = bool(MALE.search(rest)), bool(FEMALE.search(rest))
            if male == female:
                continue                                  # neither, or both: no evidence either way
            if OTHER_ONE.search(re.sub(rf"(?i)\b(?:{'|'.join(sorted(ROLES_LOWER))})\s+(?:[A-Z][\w.-]*\s+)*{re.escape(sur)}\b", " ", t)):
                continue                                  # another person the pronoun could mean
            outlets = {s.get("outlet") for s in i.get("sources") or [] if s.get("outlet")} or {f"#{i.get('id')}"}
            seen["he" if male else "she"] |= outlets
        if seen["he"] and seen["she"]:
            continue
        for g in ("he", "she"):
            if len(seen[g]) >= min_outlets:
                out[name] = g
    return out


ROLE_ACRONYM = r"MLA|MP|CM|DGP|SP|SSP|IG|DIG|ACP|DCP|SHO|CEO|MLC"


def roles(text: str, people: dict[str, str]) -> dict[str, str]:
    """{full name: "the MLA"} from the title right before a person's name in the article, when no other
    person in it carries the same role (it would be ambiguous)."""
    found: dict[str, str] = {}
    for full in people:
        m = re.search(rf"\b({ROLE_ACRONYM}|(?:Chief |Deputy |Union |Home |Finance |Defence |External Affairs )?Minister"
                      rf"|{'|'.join(sorted(ROLES_LOWER))})\s+(?:[A-Z][\w.-]*\s+)*?{re.escape(full)}\b", text)
        if m:
            role = m.group(1)
            found[full] = "the " + (role if role.isupper() else role.lower())
    count: dict[str, int] = {}
    for r in found.values():
        count[r] = count.get(r, 0) + 1
    return {n: r for n, r in found.items() if count[r] == 1}


# ------------------------------------------------------------------ a paragraph's coherence, by code
REPEAT_OPENER = re.compile(r"(?i)\b(?:also|further|additionally)\s+(?:stated|said|added|noted|claimed|mentioned)\b")
LONG_SENTENCE = 45      # words
LONG_PARAGRAPH = 7      # sentences


def problems(para: list[dict], speakers: list[str]) -> list[str]:
    """What makes a paragraph read badly, as code can see it: one speaker named in 3+ sentences,
    "also stated / further stated" openers, two sentences in a row opening
    the same way, a very long sentence, a very long paragraph. Empty: nothing code can see."""
    out = []
    texts = [str(s.get("text") or "") for s in para]
    for sp in {s for s in speakers if s and len(_surname(s)) >= 3}:
        n = sum(1 for t in texts if re.search(rf"\b{re.escape(_surname(sp))}\b", t))
        if n >= 3:
            out.append(f"it names {sp} in {n} sentences")
    # (a full name written again is not counted: code shortens it to the surname, style.shorten_names)
    if sum(1 for t in texts if REPEAT_OPENER.search(t)) >= 1 and len(texts) >= 2:
        out.append('it strings sentences together with "also stated" / "further stated"')
    for a, b in zip(texts, texts[1:]):
        if a.split()[:2] and a.split()[:2] == b.split()[:2]:
            out.append(f'two sentences in a row open with "{" ".join(a.split()[:2])}"')
            break
    if any(len(t.split()) > LONG_SENTENCE for t in texts):
        out.append(f"a sentence runs past {LONG_SENTENCE} words")
    if len(texts) >= LONG_PARAGRAPH:
        out.append(f"it is one block of {len(texts)} sentences on several subjects")
    return out


# ------------------------------------------------------------------ one speaker, however named
# (owner, Oct 9 2026, story 16197: "The Centre alleged ..." and "The Central government alleged ..." were one
# argument written in two paragraphs with other speakers between them)
ALIASES = {
    "centre": ("centre", "center", "central government", "union government", "government of india", "union of india",
               "solicitor general", "additional solicitor general", "attorney general", "centre's counsel"),
    "supreme court": ("supreme court", "apex court", "top court", "chief justice of india", "cji", "bench"),
}


# a foreign government however it is named ("US Department of State", "US officials", "the United States government",
# "Washington": one speaker; Oct 9 2026, story 16708 had them as three)
COUNTRY = {"us": "us", "u.s.": "us", "united states": "us", "american": "us", "washington": "us", "white house": "us",
           "state department": "us", "treasury": "us", "china": "china", "chinese": "china", "beijing": "china",
           "pakistan": "pakistan", "pakistani": "pakistan", "islamabad": "pakistan", "russia": "russia",
           "russian": "russia", "moscow": "russia", "iran": "iran", "iranian": "iran", "tehran": "iran",
           "israel": "israel", "israeli": "israel", "uk": "uk", "british": "uk", "bangladesh": "bangladesh",
           "nepal": "nepal", "sri lanka": "sri lanka", "canada": "canada", "canadian": "canada"}
GOV_WORD = re.compile(r"\b(government|officials?|administration|department|ministry|treasury|embassy|authorities|"
                      r"spokesperson|white house|state department)\b")


def _country_gov(low: str) -> str | None:
    m = re.match(r"^(?:the\s+)?(u\.s\.|us|united states|american|washington|white house|state department|treasury|"
                 r"china|chinese|beijing|pakistan|pakistani|islamabad|russia|russian|moscow|iran|iranian|tehran|israel|"
                 r"israeli|uk|british|bangladesh|nepal|sri lanka|canada|canadian)\b(?P<rest>.*)$", low)
    if not m:
        return None
    country = COUNTRY[m.group(1)]
    if m.group(1) in ("washington", "white house", "state department", "treasury", "beijing", "islamabad", "moscow",
                      "tehran") or GOV_WORD.search(m.group("rest")) or "department of" in low:
        return f"{country} government"
    return None


def speaker_key(name: str | None) -> str | None:
    """One key per speaker: "the Centre" = "the Central government" = "Solicitor General Tushar Mehta" (who
    argues for it); a person by surname ("Kapil Sibal" = "Sibal"); anything else as written, lower case."""
    if not name or not name.strip():
        return None
    low = re.sub(r"^(?:the|a|an)\s+", "", name.strip().lower())
    gov = _country_gov(low)
    if gov:
        return gov
    for key, names in ALIASES.items():
        if any(low == a or low.startswith(a + " ") or low.endswith(" " + a) for a in names):
            return key
    words = [w for w in re.findall(r"[A-Z][\w'-]+", name) if w not in ("The",)]
    if len(words) >= 2:
        return words[-1].lower()
    return low


def speaker_words(name: str | None) -> set[str]:
    """Words a sentence may use to name this speaker: its own words and those of its aliases."""
    if not name:
        return set()
    out = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", name)}
    key = speaker_key(name)
    for a in ALIASES.get(key, ()):
        out |= {w for w in a.split() if len(w) >= 3}
    return out - {"the", "and", "for", "senior", "india", "government", "court"} | (
        {"court"} if key == "supreme court" or "court" in name.lower() else set()) | (
        {"government"} if key == "centre" or "government" in name.lower() else set())


# ------------------------------------------------------------------ who a sentence says is speaking
SAY_VERB = r"(?:said|says|alleged|alleges|argued|argues|stated|states|claimed|claims|added|noted|submitted|contended|maintained|told)"
TRAIL_WHO = re.compile(rf",\s+(?P<who>[\w.'’ -]{{2,60}}?)\s+(?:also\s+|further\s+)?{SAY_VERB}(?:\s+on\s+\w+)?\s*[.!?]?[\"”]?$")
LEAD_WHO = re.compile(rf"^(?P<who>(?:[A-Z][\w.'’-]*)(?:\s+(?:[A-Z][\w.'’-]*|of|and|for|the|senior|chief|general|[a-z]+\'s)){{0,6}}?)"
                      rf"\s+(?:also\s+|further\s+)?{SAY_VERB}\b")
DOUBLE = re.compile(rf"(?i)\b{SAY_VERB}\s+that\s+(?:the\s+)?[\w.'’ -]{{1,50}}?\s+{SAY_VERB}\s+that\b")


def attributed(sentence: str) -> list[str]:
    """Who the sentence says is speaking: "X said that ..." / "..., X said." (lower case)."""
    out = []
    t = (sentence or "").strip()
    for rx in (LEAD_WHO, TRAIL_WHO):
        m = rx.search(t)
        if m:
            out.append(re.sub(r"^(?:the|a|an)\s+", "", m.group("who").strip().lower()))
    return out


ROLE_WORDS = ROLES_LOWER | {"counsel", "justice", "spokesperson", "governor", "magistrate", "prime", "chief", "union",
                            "home", "finance", "deputy", "external", "affairs", "defence", "state", "district", "general",
                            "solicitor", "attorney", "additional", "former", "opposition", "party", "congress", "bjp"}


def wrong_speaker(sentence: str, speakers: list[str], source_text: str, pron: dict[str, str]) -> str | None:
    """The name a sentence gives a speaker that is not the speaker of its statements (Oct 9 2026, story 16197:
    the Centre's line written as "..., the advocate alleged", the Supreme Court's as "..., she said"); None when
    the sentence names its speaker rightly or names none. A pronoun must be a speaker the outlets call so;
    "it" / "they" are fine for a body."""
    if not speakers:
        return None
    keys = {speaker_key(sp) for sp in speakers}
    allowed = set().union(*(speaker_words(sp) for sp in speakers))
    src = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", source_text or "")}
    for who in attributed(sentence):
        if who in ("it", "they"):
            continue
        if who in ("he", "she"):
            uses = (MALE if who == "he" else FEMALE).search(source_text or "")
            if not uses and not any(g == who and speaker_key(full) in keys for full, g in pron.items()):
                return who
            continue
        words = {w for w in re.findall(r"[a-z]{3,}", who)} - {"also", "senior", "the", "and", "for"}
        if words and not (words & allowed) and not words <= src:
            # "the judge", "the minister" for a person whose role the article gives; never for a body ("the
            # advocate" for the Centre)
            person = any(len(re.findall(r"\b[A-Z][\w.'-]+", sp)) >= 2 and speaker_key(sp) not in ALIASES
                         for sp in speakers)
            if person and words <= ROLE_WORDS:
                continue
            return who
    return None


def double_attribution(sentence: str, source_text: str) -> bool:
    """ "X argued that the Centre argued that ...": a speaker named twice for one claim (story 16197), unless the
    statements themselves are reported speech ("A said that B claimed ...")."""
    return bool(DOUBLE.search(sentence or "")) and not DOUBLE.search(source_text or "")


# how an article refers to a body after naming it (owner, Oct 9 2026, story 16708: "The US Department of State said
# that ..." three times in a row): a short name and "it" for one body, "they" for many; never "he" / "she"
PLURAL_BODY = re.compile(r"(?i)\b(officials|authorities|police|sources|protesters|students|farmers|workers|residents|"
                         r"villagers|leaders|members|lawyers|doctors|investigators|agencies|parties|companies|"
                         r"activists|unions|judges|ministers|mps|mlas|experts|analysts|petitioners)$")
SHORT_BODY = ("department", "ministry", "court", "commission", "government", "party", "company", "board", "council",
              "committee", "agency", "bank", "army", "navy", "embassy", "administration", "authority", "bench",
              "tribunal", "corporation", "union", "regulator", "force", "office", "secretariat", "centre")


def body_refs(name: str) -> list[str]:
    """Second and later references to a body, to rotate: ["the department", "it"], or ["they", "the officials"]."""
    low = re.sub(r"^(?:the|a|an)\s+", "", (name or "").strip().lower())
    m = PLURAL_BODY.search(low)
    if m:
        word = m.group(1)
        return ["they"] + ([f"the {word}"] if word != "police" else ["police"])
    words = re.findall(r"[a-z]+", low)
    short = next((w for w in reversed(words) if w in SHORT_BODY), None)
    if speaker_key(name) == "centre":
        short = "the Centre"
    elif short:
        short = f"the {short}"
    return ([short] if short else []) + ["it"]


BODY_WORDS = set(SHORT_BODY) | {"trust", "foundation", "limited", "ltd", "group", "institute", "university", "school",
                                "hospital", "police", "court", "bench", "division", "state", "states", "india", "nations",
                                "organisation", "organization", "association", "federation", "front", "congress", "bjp",
                                "aap", "cell", "wing", "unit", "station", "media", "channel", "network", "inc"}


def is_body(name: str | None) -> bool:
    """A body, not a person: a government, court, party, company, police ... ("US Department of State", "Supreme
    Court", "Kanzeon Public Charitable Trust", "US officials"); "Union Minister Jyotiraditya Scindia" is a person."""
    if not name:
        return False
    if speaker_key(name) in ALIASES or (speaker_key(name) or "").endswith(" government") or PLURAL_BODY.search(name.strip()):
        return True
    words = re.findall(r"[A-Za-z]+", name)
    last = words[-1].lower() if words else ""
    return last in BODY_WORDS or any(w.lower() in ("department", "ministry", "commission", "court", "trust", "party",
                                                       "government", "police", "council") for w in words[-2:])
