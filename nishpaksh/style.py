"""House style applied by code to the finished article (owner, Oct 7 2026: "read like one author
writing for a reader, not like an AI"). The writer models do not follow style rules reliably, so code
does what can be done safely:

    surnames      a person is named in full once ("Air Marshal Ashutosh Dixit"), then by surname
                  ("Dixit"), as newspapers do; not when two people in the article share the surname
    said-chains   "Sarma said A. He said B. He added that C." -> "Sarma said A. B, he said. C, he added."
                  the attribution moves, it is never dropped

A name counts as a person only with evidence: a title or role before it in the article ("Chief
Minister", "Air Marshal", "jawan"), a speech verb after it ("X said"), or a statement's speaker.
Places and bodies ("Tel Aviv", "East Champaran", "Supreme Court") are never shortened.
"""
from __future__ import annotations

import re

TITLES = set("""Mr Mrs Ms Dr Shri Smt Sri Justice Judge Captain Capt Colonel Col General Gen Lieutenant Lt Major
Brigadier Admiral Marshal Air Chief Vice Deputy Minister Prime President Governor Commissioner Secretary
Inspector Sub-Inspector Superintendent Constable Officer Director Chairman Chairperson Speaker Leader
Union Home Finance Defence External Affairs MLA MP CM DGP SP IG DIG ACP DCP SHO Advocate Senior Retired
Professor Prof Senator Ambassador Mayor Councillor Sheikh Pope Saint Sardar Pandit Swami Maulana
Lieutenant-General Major-General Wing Commander Squadron Group Flight Sergeant Havildar Naik Sepoy""".split())
# titles that mark a PERSON (evidence); the wider TITLES above are only stripped from a name. "Air",
# "Chief", "Union" or "Deputy" alone are no evidence: Oct 7 2026, "Commission for Air Quality
# Management" became "Commission for Management" (the "Air" before "Quality Management" read as a title)
PERSON_TITLES = set("""Mr Mrs Ms Dr Shri Smt Sri Justice Judge Captain Capt Colonel Col General Gen Lieutenant Lt
Major Brigadier Admiral Marshal Minister President Governor Inspector Sub-Inspector Constable Professor Prof
Senator Ambassador Mayor MLA MP CM DGP SP IG DIG ACP DCP SHO Advocate Sheikh Swami Maulana Pandit Sardar
Sergeant Havildar Naik Sepoy""".split())
ROLES_LOWER = set("""minister leader chief jawan officer judge actor actress activist journalist accused
constable inspector spokesperson president secretary mla mp cricketer player coach captain businessman
student doctor lawyer advocate teacher farmer driver engineer pilot co-pilot director founder ceo chairman
wife husband son daughter father mother brother sister priest councillor sarpanch commissioner
collector magistrate superintendent""".split())
NOT_PERSON = set("""Police Court Ministry Party Force Forces Government Department Commission Bank Limited Ltd
India Indian Pradesh University Committee Council Board Army Navy Congress BJP Airport Station Hospital
Nagar City District Road Street Bridge Lok Sabha Rajya State States Union Territory East West North South
Assembly Corporation Authority Agency Bureau Office Times News Express Today Airlines Air Team Club
Group Company Trust Foundation Institute School College Temple Mosque Church Market Railway Railways
Highway Dam River Lake Sea Ocean Bay Island Hills Valley Village Town Pakistan China America Bihar Assam
Delhi Mumbai Kerala Gujarat Punjab Haryana Rajasthan Tamil Nadu Bengal Karnataka Maharashtra Odisha
Management Quality Control Pollution Environment Health Development Welfare Services Security Intelligence
Investigation Centre Center National Central Federal Regional International Supreme High Society Association
Federation Mission Scheme Yojana Programme Program Act Bill Code Policy Fund Industries Energy Power Water
Commission Council Agency Ministry Department Squad Squads Task Cell Unit Division Zone Circle Range""".split())
SPEECH = r"(?:said|says|told|added|stated|alleged|claimed|denied|announced|noted|asked|urged|wrote)"
NAME = r"[A-Z][a-z]+(?:-[A-Z]?[a-z]+)?|al-[A-Z][a-z]+"


def _candidates(text: str) -> set[str]:
    out = set()
    for m in re.finditer(rf"(?<![\w-])((?:{NAME})(?:\s+(?:{NAME})){{1,7}})(?![\w-])", text):
        words = m.group(1).split()
        # "Assam Chief Minister Himanta Biswa Sarma" -> "Himanta Biswa Sarma"
        while words and (words[0] in TITLES or words[0] in NOT_PERSON or words[0] in ("The", "A", "An")):
            words = words[1:]
        if 2 <= len(words) <= 4 and not any(w in NOT_PERSON or w in TITLES for w in words):
            out.add(" ".join(words))
    return out


def people(text: str, speakers: list[str] | tuple = ()) -> dict[str, str]:
    """{full name: surname} for the persons in a text, shortenable without confusion."""
    found = {}
    spoken = " ".join(speakers or ())
    cands = _candidates(text)
    for name in cands:
        e = re.escape(name)
        titled = re.search(rf"(?:\b(?:{'|'.join(map(re.escape, PERSON_TITLES))})|\b(?:{'|'.join(ROLES_LOWER)}))\s+{e}\b", text)
        speaks = re.search(rf"\b{e}\s+(?:has\s+|had\s+)?{SPEECH}\b", text)
        if titled or speaks or name in spoken:
            found[name] = name.split()[-1]
    # a surname shared by two people, or also someone's first name, is no short form
    last = [n.split()[-1].lower() for n in cands]          # every name, person or not
    firsts = {n.split()[0].lower() for n in cands}
    return {n: s for n, s in found.items() if last.count(s.lower()) == 1 and s.lower() not in firsts}


def shorten_names(paragraphs: list[list[dict]], speakers: list[str] | tuple = ()) -> None:
    """Full name and title at the first mention, surname after (in place)."""
    text = " ".join(s["text"] for p in paragraphs for s in p)
    names = people(text, speakers)
    if not names:
        return
    title = rf"(?:(?:the\s+)?(?:[A-Z][a-z]+\s+)?(?:(?:{'|'.join(map(re.escape, TITLES))})\s+)+)?"
    seen: set[str] = set()
    for para in paragraphs:
        for sent in para:
            t = sent["text"]
            for full, short in names.items():
                pat = re.compile(rf"{title}\b{re.escape(full)}\b")

                def sub(m, full=full, short=short):
                    if full not in seen:
                        seen.add(full)
                        return m.group(0)
                    return short
                t = pat.sub(sub, t)
            sent["text"] = t[:1].upper() + t[1:]


CHAIN = re.compile(rf"^(He|She|They|[A-Z][a-z]+)\s+(said|added|stated|noted|claimed|alleged|maintained)"
                   r"(\s+on\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))?(?:\s+that)?,?\s+"
                   r"(?P<body>[^\"“”]{20,}?)([.!?])$")
ATTRIB = re.compile(rf"\b{SPEECH}\b")


def vary_attribution(paragraphs: list[list[dict]]) -> None:
    """In a run of sentences by one speaker, the second and later carry the attribution at the end."""
    for para in paragraphs:
        for k in range(1, len(para)):
            prev, cur = para[k - 1]["text"], para[k]["text"]
            m = CHAIN.match(cur.strip())
            if not m or not ATTRIB.search(prev) or ATTRIB.search(m.group("body")):
                continue
            who = m.group(1)
            who = who.lower() if who in ("He", "She", "They") else who
            verb = "said" if m.group(2) == "stated" else m.group(2)
            body = m.group("body").rstrip(",;: ")
            para[k]["text"] = f"{body[:1].upper()}{body[1:]}, {who} {verb}{m.group(3) or ''}{m.group(5)}"


def polish(payload: dict) -> None:
    nar = payload.get("narrative") or {}
    paras = nar.get("paragraphs") or []
    if not paras:
        return
    speakers = []
    for k in ("undated", "established", "contested", "context"):
        speakers += [i.get("speaker") or "" for i in payload.get(k) or []]
    for tier in payload.get("timeline") or []:
        speakers += [i.get("speaker") or "" for i in tier]
    shorten_names(paras, speakers)
    vary_attribution(paras)
