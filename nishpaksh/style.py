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
# words that open a sentence and are never part of a name (Oct 8 2026, story 13107: "While Humayun Kabir
# had been criticising ..." read as the name "While Humayun Kabir"; two names ending in Kabir then meant
# the surname was never used)
LEAD = set("""While Meanwhile When Whereas After Before On In At By For From According Earlier Later However But
And Also During Since As If Though Although Then Yesterday Today Tomorrow Besides Following Speaking
Addressing Reacting Under With Without Among Amid Despite Unlike Like Both Several Some Other Reports
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July
August September October November December Once Now Here There This That These Those It Its""".split())
SPEECH = r"(?:said|says|told|added|stated|alleged|claimed|denied|announced|noted|asked|urged|wrote)"
NAME = r"[A-Z][a-z]+(?:-[A-Z]?[a-z]+)?|al-[A-Z][a-z]+"


def _candidates(text: str) -> set[str]:
    out = set()
    for m in re.finditer(rf"(?<![\w-])((?:{NAME})(?:\s+(?:{NAME})){{1,7}})(?![\w-])", text):
        words = m.group(1).split()
        # "Assam Chief Minister Himanta Biswa Sarma" -> "Himanta Biswa Sarma"
        while words and (words[0] in TITLES or words[0] in NOT_PERSON or words[0] in LEAD or words[0] in ("The", "A", "An")):
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

    def shorten(t: str) -> str:
        for full, short in names.items():
            pat = re.compile(rf"{title}\b{re.escape(full)}\b")

            def sub(m, full=full, short=short):
                if full not in seen:
                    seen.add(full)
                    return m.group(0)
                return short
            t = pat.sub(sub, t)
        return t

    for para in paragraphs:
        for sent in para:
            if sent.get("parts"):          # a sentence in coloured parts: each part, in order
                for k, part in enumerate(sent["parts"]):
                    t = shorten(part["text"])
                    part["text"] = t[:1].upper() + t[1:] if k == 0 else t
                sent["text"] = " ".join(p["text"].strip() for p in sent["parts"])
                continue
            t = shorten(sent["text"])
            sent["text"] = t[:1].upper() + t[1:]


CHAIN = re.compile(r"^(?P<who>He|She|They|The [a-z]+(?: [a-z]+)?|(?:[A-Z][\w.-]+\s+){0,3}[A-Z][\w.-]+)\s+"
                   r"(?:(?P<adv>also|further|then|later|additionally)\s+)?"
                   r"(?P<verb>said|added|stated|noted|claimed|alleged|maintained|mentioned|remarked|asserted)"
                   r"(?P<day>\s+on\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))?(?:\s+that)?,?\s+"
                   r"(?P<body>[^\"“”]{20,}?)(?P<end>[.!?])$")
ATTRIB = re.compile(rf"\b{SPEECH}\b")
PRON_ATTRIB = re.compile(r"(?:^|(?<=, ))(He|She|he|she)(?=\s+(?:also\s+|further\s+)?(?:said|added|stated|noted|claimed|alleged|told|maintained)\b)")
NEUTRAL = {"stated": "said", "mentioned": "said", "remarked": "said", "asserted": "said", "noted": "said"}


class Refs:
    """How the article refers to each person after the first mention (owner, Oct 8 2026): the surname,
    the role ("the MLA") when only one person has it, and "he" / "she" only with the outlets' evidence
    (voice.pronouns). Rotated, so a speaker's run does not read "Kabir said ... Kabir said ...". """

    def __init__(self, names: dict[str, str], pronouns: dict[str, str], roles: dict[str, str]):
        self.by_surname = {short: full for full, short in names.items()}
        self.pron, self.roles = pronouns, roles
        self.used: dict[str, int] = {}

    def person(self, text: str) -> str | None:
        """The last person a text names (full name or surname)."""
        best, at = None, -1
        for short, full in self.by_surname.items():
            for m in re.finditer(rf"\b{re.escape(short)}\b", text):
                if m.start() > at:
                    best, at = full, m.start()
        return best

    def next(self, full: str) -> str:
        options = ([self.pron[full]] if full in self.pron else []) + ([self.roles[full]] if full in self.roles else [])
        options.append(full.split()[-1])
        n = self.used.get(full, 0)
        self.used[full] = n + 1
        return options[n % len(options)]

    def allowed(self, full: str | None, pronoun: str) -> bool:
        return bool(full) and self.pron.get(full) == pronoun.lower()


def vary_attribution(paragraphs: list[list[dict]], refs: Refs | None = None, speakers_of=None) -> None:
    """In a run of sentences by one speaker, the second and later carry the attribution at the end
    ("..., he said." / "..., the MLA said." / "..., Kabir said."); "also stated" and "further stated" go.
    A "he" / "she" for someone the outlets do not call so becomes the surname (code never guesses a
    gender)."""
    from .voice import speaker_key
    refs = refs or Refs({}, {}, {})
    current = None

    def own(sent: dict, full: str | None) -> tuple[str | None, str | None]:
        """(the person to refer to, the name of a body) for this sentence: its statements' speaker, never the
        paragraph's last one (Oct 9 2026: Scindia's figures written as "..., Modi said", the Supreme Court's line
        as "..., she said", because "He" / "The ..." was read as whoever spoke before)."""
        sps = [x for x in (speakers_of(sent) if speakers_of else []) if x]
        if not sps:
            return full, None
        keys = {speaker_key(x) for x in sps}
        if full and speaker_key(full) in keys:
            return full, None
        person = next((refs.person(x) for x in sps if refs.person(x)), None)
        if person and speaker_key(person) in keys:
            return person, None
        return None, (sps[0] if len(keys) == 1 else None)

    def body_name(sp: str) -> str:
        return sp if sp.lower().startswith("the ") else f"the {sp}"

    for para in paragraphs:
        for k in range(len(para)):
            if para[k].get("parts"):
                continue                  # moving words across parts would break their colours
            cur = para[k]["text"].strip()
            named = refs.person(cur)
            m = CHAIN.match(cur)
            if m and k > 0 and ATTRIB.search(para[k - 1]["text"]) and not ATTRIB.search(m.group("body")):
                who = m.group("who")
                # "The ..." is the current speaker only when it is that speaker's own role ("The MLA"); "The Centre",
                # "The Supreme Court" are other speakers (Oct 9 2026, story 16197: "The Centre alleged ..." after
                # Kapil Sibal became "..., the advocate alleged", "The Supreme Court stated ..." after a line naming
                # Mishra became "..., she said")
                own_role = bool(current) and who.lower() == (refs.roles.get(current) or "").lower()
                pron = who in ("He", "She", "They")
                full = current if pron or own_role else (None if who.startswith("The ") else refs.person(who))
                if full and (pron or own_role) and speakers_of:
                    full, _ = own(para[k], full)
                if full and (pron or own_role or full in (current, refs.person(para[k - 1]["text"]))):
                    ref = refs.next(full)
                    verb = NEUTRAL.get(m.group("verb"), m.group("verb"))
                    body = m.group("body").rstrip(",;: ")
                    para[k]["text"] = f"{body[:1].upper()}{body[1:]}, {ref} {verb}{m.group('day') or ''}{m.group('end')}"
                    current = full
                    continue
            opens = m and (k == 0 or not ATTRIB.search(para[k - 1]["text"]))
            if opens and (m.group("adv") or m.group("verb") == "added" or m.group("verb") in NEUTRAL):
                # "Kabir also stated / further stated / added that" with nothing before it to add to: "Kabir said"
                verb = "said" if m.group("verb") == "added" else NEUTRAL.get(m.group("verb"), m.group("verb"))
                adv = rf"{m.group('adv')}\s+" if m.group("adv") else ""
                para[k]["text"] = re.sub(rf"^({re.escape(m.group('who'))})\s+{adv}{m.group('verb')}\b",
                                         rf"\1 {verb}", cur, count=1)
            # "he said" / "She added" for someone without the outlets' evidence: the surname
            t = para[k]["text"]
            for pm in reversed(list(PRON_ATTRIB.finditer(t))):
                full, body = own(para[k], refs.person(t[:pm.start()]) or current)
                if body:
                    t = t[:pm.start()] + body_name(body) + t[pm.end():]     # a body is never "he" / "she"
                elif full and not refs.allowed(full, pm.group(1)):
                    short = full.split()[-1]
                    t = t[:pm.start()] + short + t[pm.end():]
            para[k]["text"] = t[:1].upper() + t[1:]
            current = refs.person(para[k]["text"]) or named or current


def polish(payload: dict) -> None:
    from .voice import pronouns, roles
    nar = payload.get("narrative") or {}
    paras = nar.get("paragraphs") or []
    if not paras:
        return
    items = []
    for k in ("undated", "established", "contested", "context"):
        items += payload.get(k) or []
    for tier in payload.get("timeline") or []:
        items += tier
    speakers = [i.get("speaker") or "" for i in items]
    text = " ".join(s["text"] for p in paras for s in p)
    names = people(text, speakers)
    refs = Refs(names, pronouns(items), roles(text, names))
    shorten_names(paras, speakers)
    by_id = {i["id"]: i for i in items if "id" in i}
    vary_attribution(paras, refs, lambda sent: [(by_id.get(i) or {}).get("speaker") for i in sent.get("ids") or []])
