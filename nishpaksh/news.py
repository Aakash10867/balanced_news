"""The news of a story: chosen once, by code; the lead and the headline are written from it (owner, Oct 8 2026).

Before, the headline was written twice (a draft before the article, keyed to an importance call that
was re-asked whenever the draft changed, and again from the article's lead), up to seven calls a try, and
the writer chose its lead freely: "India Block leaders held a protest" led the story of Rahul and
Priyanka Gandhi's detention, "the court heard the petition" led the order it made. Headlines asked for
"reportedly" on everything not yet green, which at writing time is almost everything.

Now:
  1. pick_news (code, no call): the statement that makes this news today. Candidates: the story's own
     statements (not background or context) carried by 2+ independent outlets (else the best carried).
     Ranked: recent (within 2 days of the newest dated one) > a decisive act (arrested, ordered, killed,
     signed, resigned...) over process or setting (held, met, spoke, heard, began) > outlets > reports.
     A disputed statement can be the news (owner, Oct 8 2026: the article gives the clarity); its
     headline must not take a side.
  2. The writer's lead must cite it (narrative.py checks; the news fill rewrites a lead that does not).
  3. The headline: one call writes three candidates from the news and the lead; code rejects the
     wrong ones (numbers and names not in the statements, who-did-what, cause words, loaded words, any
     hedge, a disputed figure stated as fact) and scores the rest on what makes a good headline:
     the outcome, not the process; one concrete detail; a known name or a role; 7-12 words; no jargon.
     A second call only when all three fail. House style by code (PM, CJI, CM).
"""
from __future__ import annotations

import datetime as dt
import logging
import re

from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

# decisive acts, as verbs only: "the 2023 murder case" or "Houthi attacks" is not an act today
DECISIVE = re.compile(
    r"(?i)\b(arrested|arrests|detained|detains|nabbed|killed|kills|died|dies|shot dead|murdered|ordered|orders|"
    r"directed|directs|convicted|acquitted|sentenced|granted|grants|rejected|rejects|dismissed|dismisses|quashed|"
    r"quashes|inaugurated|inaugurates|unveiled|unveils|issued|issues|banned|bans|resigned|resigns|sacked|sacks|suspended|suspends|removed|appointed|appoints|signed|"
    r"signs|approved|approves|passed|announced|announces|launched|launches|declared|declares|won|wins|elected|"
    r"collapsed|collapses|injured|registered|charged|booked|raided|seized|seizes|recovered|struck|attacked|"
    r"condemned|condemns|accepted|accepts|fined|demolished|evacuated|rescued|crashed|caught|flooded|submerged|"
    r"inundated|erupted|closed|shut|tested|test-fired|conducted|sought|seeks|filed|moved)\b")
PROCESS = re.compile(r"(?i)\b(held|holds?|met|meets?|spoke|speaks?|addressed|addresses|attended|attends?|heard|"
                     r"hears?|hearing|began|begins?|started|starts?|gathered|visited|visits?|discussed|"
                     r"reviewed|chaired|took part|participated)\b")
RECENT = dt.timedelta(days=1)


def _start(i: dict) -> dt.datetime | None:
    s = (i.get("time") or {}).get("start")
    try:
        return dt.datetime.fromisoformat(s).replace(tzinfo=None) if s else None
    except (TypeError, ValueError):
        return None


DEATHS = re.compile(r"(?i)\b(killed|kills|died|dies|shot dead|murdered)\b")
PARTICIPLE = re.compile(r"(?i)\b\w+ed by\b")
PAST = re.compile(r"(?i)\b(previously|earlier|formerly|last (?:year|month|week)|had \w+(?:ed|en)|in the past)\b")


def act_score(text: str) -> int:
    """3 for deaths, 2 for another decisive act, -1 for process or setting only or for what did NOT happen, else 0."""
    from .relate import NEGATION
    if NEGATION.search(text or ""):
        return -1
    t = PARTICIPLE.sub(" ", text or "")          # "the petition filed by X" is not an act of filing
    if DEATHS.search(t):
        return 3                                  # deaths lead, as in any newspaper
    if DECISIVE.search(t):
        return 2
    return -1 if PROCESS.search(text or "") else 0


def _outlets(i: dict) -> set[str]:
    return {s.get("outlet") for s in i.get("sources") or [] if s.get("outlet")} or {f"#{i['id']}"}


def _same_act(pi, pj) -> bool:
    """Two lines with the same decisive verb tell the same act in other words when they share a number, or a
    name AND most of the smaller line's words. A shared name alone was too loose (Oct 9 2026, story 13792: every
    "the GST Council approved ..." line borrowed every other's outlets, all tied, and a vague one-outlet line led)."""
    small = min(len(pi.roots), len(pj.roots)) or 1
    return bool(pi.nums & pj.nums) or bool(pi.names & pj.names and len(pi.roots & pj.roots) >= 0.5 * small)


def pick_news(items: list[dict]) -> list[int]:
    return _pick(items)[0]


def lead_news(items: list[dict]) -> list[int]:
    """The facts the lead is written from: the news, and the fact of the day when it is a different one."""
    out, today = _pick(items)
    return out[:1] + ([today] if today is not None else [])


def _pick(items: list[dict]) -> tuple[list[int], int | None]:
    """The ids of the news, best first (the first is THE news). Code only. Ranked:
      not old    never told as the past ("previously", an older year)
      2 outlets  a fact two or more outlets tell over a one-outlet line, whatever its verb
      act        a decisive act over process or setting
      carried    how many outlets tell this act: the statement's own outlets (folded lines' included,
                 compose.fold_covered runs first) and those of statements telling the same act in other words
                 (same decisive verb, a shared name or number, same day): one detention told three ways by
                 three outlets is three outlets (Oct 8 2026, story 14729). Before "recent" since Oct 9 2026
                 (owner; story 13792: a vague one-outlet line dated today beat the arrest-power decision three
                 outlets carried)
      recent     dated on the story's newest day or the day before; then undated
      central    how many of the story's statements share its subject (2+ root words)
      concrete   names and numbers in it
    """
    from .frames import _stem
    from .relate import Profile
    core = [i for i in items if (i.get("role") or "core") == "core" and i.get("kind") in ("event", "claim")
            and i.get("verdict") != "false"]
    if not core:
        return [], None
    prof = {i["id"]: Profile(i["text"]) for i in core}
    verbs = {i["id"]: {_stem(m.lower()) for m in DECISIVE.findall(i["text"])} for i in core}
    dates = [d for d in (_start(i) for i in core) if d]
    newest = max(dates).date() if dates else None

    def day(i):
        d = _start(i)
        return d.date() if d else None

    def carried(i) -> int:
        out = set(_outlets(i))
        for j in core:
            if j is i or not verbs[i["id"]] & verbs[j["id"]]:
                continue
            if day(i) and day(j) and day(i) != day(j):
                continue
            if _same_act(prof[i["id"]], prof[j["id"]]):
                out |= _outlets(j)
        return len(out)

    def central(i) -> int:
        r = prof[i["id"]].roots
        return sum(1 for j in core if j is not i and len(r & prof[j["id"]].roots) >= 2)

    def key(i):
        d = day(i)
        # 2: dated on the story's newest day or the day before; 1: undated; 0: older, or told as the past
        # ("previously", "had announced", an older year: "the 2023 murder")
        recent = 1 if d is None or newest is None else 2 if newest - d <= RECENT else 0
        years = [int(y) for y in re.findall(r"\b(?:19|20)\d\d\b", i["text"])]
        if PAST.search(i["text"]) or (years and newest and max(years) < newest.year):
            recent = 0
        c = carried(i)
        p = prof[i["id"]]
        # one outlet's line never leads over a fact two or more outlets tell (Oct 9 2026, story 13058: "Nana
        # Patekar ... won millions of hearts", one outlet, led a story four outlets told; "won" read as an act)
        return (recent > 0, min(c, 2), act_score(i["text"]), c, recent, central(i), len(p.names) + len(p.nums),
                i.get("n_articles") or 0, -i["id"])
    ranked = sorted(core, key=key, reverse=True)
    out = [i["id"] for i in ranked]
    # the lead may carry two facts (owner, Oct 9 2026): the best-reported one, and, if that is not today's,
    # the best fact dated on the story's newest day, unless it tells the same act in other words
    first, today = ranked[0], None
    if newest is not None and day(first) != newest:
        def same_act(j) -> bool:
            return bool(verbs[first["id"]] & verbs[j["id"]]) and _same_act(prof[first["id"]], prof[j["id"]])
        today = next((j for j in ranked[1:] if day(j) == newest and key(j)[:2] >= key(first)[:2] and act_score(j["text"]) > 0
                      and not same_act(j)), None)
        if today is not None:
            out.remove(today["id"])
            out.insert(1, today["id"])
    return out, (today["id"] if today is not None else None)


# ------------------------------------------------------------------------------------------- headline

HEADLINE_PROMPT = """Write THREE different headlines for this news story. Each: at most 12 words, plain
English, sentence case (capital letters only for names).

What makes a good headline:
1. It is the news: what happened, the outcome, not the process or the setting.
2. One concrete detail: a number, a place, a person readers know.
3. Who did what: a name readers know, otherwise a role ("IIT Bombay student", "Gujarat police"), then an
   active verb in the present tense ("detain", "orders", "signs").
4. Short words, no jargon, no abbreviation a reader may not know.
5. Only what the statements say: no number, name, cause ("due to", "amid") or judging adjective that is
   not in them. Never "reportedly", "reports say", "allegedly" at the end.
{dispute}
Bad -> good:
- "Defence contract signed under Buy Indian IDDM category" (jargon, no buyer, no amount)
  -> "Defence Ministry signs Rs 661.50 crore deal for BrahMos launchers"
- "India Block leaders hold protest near Jantar Mantar" (the setting)
  -> "Police detain Rahul, Priyanka Gandhi at Jantar Mantar protest"
- "Supreme Court hears Rajpal Yadav's plea" (the process) -> "Supreme Court orders Rajpal Yadav to pay Rs 5 crore"
- "Prime Minister Narendra Modi urged strict global regulations against deepfakes" (long, past tense)
  -> "PM Modi calls for strict global rules on deepfakes"
- the police put a toll at 40 and the families at 50: "Building collapse kills 40" (takes a side)
  -> "Police say 40 dead in Seemapuri collapse, families say 50"
{thread}
THE NEWS: {news}
The article opens: {lead}
Other statements (for one detail, if needed):
{others}

Reply with JSON only: {{"headlines": ["...", "...", "..."]}}"""

DISPUTE_NOTE = """6. THE NEWS is disputed: {versions}. Do not state either version as fact: say whose it is ("police
   say ..."), or leave the disputed detail out."""

LINKS = ("due to", "amid", "because", "as a result", "triggered", "led to")
HEDGE = re.compile(r"(?i)\b(reportedly|reports? (?:say|says|said|emerge|suggest|indicate|claim)|according to reports?|"
                   r"it is reported)\b|,?\s*allegedly\s*$")
ATTRIBUTION = re.compile(r"(?i)\b(say|says|said|claim|claims|alleg\w*|accus\w*|den(?:y|ies|ied)|disput\w*|"
                         r"according to|versus|vs)\b")
KNOWN_ABBR = {"PM", "CM", "CJI", "BJP", "AAP", "RSS", "ISRO", "IIT", "IIM", "AIIMS", "NEET", "UPSC", "CBI", "ED",
              "NIA", "UN", "US", "UK", "UAE", "GST", "RBI", "IPL", "FIR", "MP", "MLA", "MLAs", "MPs", "NDA",
              "SC", "HC", "IAS", "IPS", "CRPF", "BSF", "NCR", "TMC", "DMK", "SP", "BSP", "JDU", "RJD", "NCP",
              "LoC", "LAC", "ICC", "BCCI", "NATO", "AI", "IMF", "WHO", "EU", "ODI", "T20", "JNU", "DU", "Rs"}
TITLES = [(re.compile(r"\bPrime Minister\b"), "PM"), (re.compile(r"\bChief Justice of India\b"), "CJI"),
          (re.compile(r"\bChief Minister\b"), "CM"), (re.compile(r"\bPM Narendra Modi\b"), "PM Modi")]
STOP = {"the", "and", "for", "with", "from", "over", "into", "after", "amid", "its", "his", "her", "their"}


def house_style(h: str) -> str:
    """Headline titles as Indian papers write them: 'PM Modi', 'CJI', 'CM'."""
    for pat, rep in TITLES:
        h = pat.sub(rep, h)
    return re.sub(r"\s+", " ", h).strip().rstrip(".")


def _names(text: str) -> set[str]:
    """Capitalised words after the first (names, places, bodies), lower-cased."""
    toks = re.findall(r"[A-Za-z][\w'-]*", text or "")
    return {re.sub(r"'s$", "", t.lower()) for t in toks[1:] if t[0].isupper() and t not in KNOWN_ABBR}


def _actor_problem(h: str, source: str) -> str | None:
    """Who did it must match the statements (Oct 2026: "FSSAI recalls Everest cumin powder" when the
    statements say FSSAI ORDERED the recall; Everest recalls). Checked for the headline's first verb in
    the present tense ("recalls", "arrests"): where the statements use that verb, someone named before
    it in the headline must be its actor there, not "the recall" ordered by them. A verb the
    statements never use (a paraphrase) is not judged here."""
    words = re.findall(r"[A-Za-z'&.-]+", h)
    for k, w in enumerate(words):
        if k == 0 or not re.fullmatch(r"[a-z]{3,}s", w) or w in ("was", "has", "is", "its", "his", "says"):
            continue
        stem = w[:-2] if w.endswith(("ches", "shes", "sses", "xes")) else w[:-1]
        subject = {x.lower().strip("'s.") for x in words[:k] if len(x) >= 3} - STOP
        forms = list(re.finditer(rf"\b{re.escape(stem.lower())}(?:s|es|ed|d|ing)?\b", source))
        if not forms or not subject:
            return None
        for m in forms:
            before = re.findall(r"[a-z'&.-]+", source[max(0, m.start() - 60):m.start()])[-4:]
            if before and before[-1] in ("the", "a", "an", "its", "his", "her", "their"):
                continue   # "ordered the recall": a noun, someone else's action on it
            if subject & {b.strip("'s.") for b in before}:
                return None
        m = forms[0]
        snippet = source[max(0, m.start() - 50):m.end() + 30].strip()
        return f"it says '{' '.join(words[:k + 1])}', but the statements say \"...{snippet}...\": keep who did what"
    return None


def headline_problem(h: str, source: str, banned: set[str], disputed: set[str] = frozenset()) -> str | None:
    """What is wrong with a headline, or None. `source` is every statement it may draw on (lower case);
    `disputed` the answers (numbers, names) the sources disagree on."""
    low = h.lower()
    if not h or len(h.split()) > 12:
        return "it must be at most 12 words"
    if HEDGE.search(h):
        return "no 'reportedly' or 'reports say': state the news, or say whose claim it is"
    m = re.match(r"([A-Z][a-z]+)\s+(was|is|has|had|gets|got|were)\b", h)
    if m and m.group(1).lower() not in ("police", "court", "government", "centre", "parliament", "army"):
        return f"it starts with a bare name ('{m.group(1)}'); introduce the person by who they are"
    if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low) for w in banned):
        return "it uses a loaded word"
    bad = [l for l in LINKS if re.search(rf"\b{l}\b", low) and l not in source]
    if bad:
        return f"it links events with '{bad[0]}', which no statement does"
    if not set(re.findall(r"\d+", h)) <= set(re.findall(r"\d+", source)):
        return "it has a number that is not in the statements"
    words = h.split()
    if sum(w[0].isupper() for w in words if w[0].isalpha()) < 0.7 * len(words):   # not Title Case
        stray = [n for n in _names(house_style(h)) if n not in source and len(n) > 2]
        if stray:
            return f"it names '{stray[0]}', which no statement does"
    if disputed and not ATTRIBUTION.search(h):
        hit = [d for d in disputed if re.search(rf"(?<![\w.]){re.escape(d)}(?![\w.])", low)]
        if hit:
            return f"'{hit[0]}' is disputed: say whose version it is, or leave it out"
    return _actor_problem(h, source)


def headline_score(h: str, news: str) -> float:
    """How good a correct headline is, on what makes one good (owner, Oct 8 2026)."""
    score = 0.0
    n = len(h.split())
    score += 1 if 7 <= n <= 12 else 0
    score += {3: 2, 2: 2, 0: 0, -1: -2}[act_score(h)]              # the outcome, not the process
    concrete = bool(re.search(r"\d", h)) or bool(_names(h) & _names(news))
    score += 2 if concrete else 0                                   # one concrete detail
    from .frames import _stem
    nw = {_stem(w) for w in re.findall(r"[a-z]{4,}", news.lower())}
    hw = {_stem(w) for w in re.findall(r"[a-z]{4,}", h.lower())}
    score += 1 if nw and len(hw & nw) >= 0.5 * len(nw) else 0      # it is about the news
    facts = len(DECISIVE.findall(h)) + len(re.findall(r"\d+|\b(?:two|three|four|five|six|seven|eight|nine|ten)\b", h))
    score += min(1.0, 0.5 * max(0, facts - 1))                      # more of the news in the same words
    jargon = [t for t in re.findall(r"\b[A-Z]{3,}\b", h) if t not in KNOWN_ABBR]
    score -= len(jargon)                                            # no unexplained abbreviations
    return score


def _disputed_answers(news: dict, items: dict[int, dict]) -> tuple[set[str], str]:
    """The numbers (and names) on which the news statement and what contradicts it differ, and a line
    for the prompt saying the versions."""
    from .relate import Profile
    rivals = [items[x] for x in news.get("conflicts_with") or [] if x in items]
    answers: set[str] = set()
    for nm in news.get("name_conflict") or []:
        answers.add(str(nm).lower())
    if not rivals and not answers:
        return set(), ""
    mine = Profile(news["text"]).nums
    for r in rivals:
        theirs = Profile(r["text"]).nums
        for x in mine ^ theirs:
            answers.add(f"{x:g}")
    versions = "; ".join([f'"{news["text"]}"'] + [f'"{r["text"]}"' for r in rivals[:2]])
    if news.get("name_conflict"):
        versions += "; the reports name " + " / ".join(news["name_conflict"])
    return answers, versions


def write_headline(router: Router | None, news: dict, items: dict[int, dict], lead: str, banned: set[str],
                   thread: str = "") -> str | None:
    """The best correct headline of three, or None (the article waits as a kept draft)."""
    if router is None or not news:
        return None
    others = [i for i in items.values() if i["id"] != news["id"] and (i.get("role") or "core") == "core"]
    others.sort(key=lambda i: -(i.get("n_sources") or 0))
    others = others[:6]
    source = " ".join([news["text"], lead] + [i["text"] for i in others]).lower()
    disputed, versions = _disputed_answers(news, items)
    ctx = f"\nThis is a new development in an ongoing story ({thread}): headline the NEW development.\n" if thread else ""
    prompt = HEADLINE_PROMPT.format(
        dispute=DISPUTE_NOTE.format(versions=versions) if versions else "", thread=ctx, news=news["text"],
        lead=lead or news["text"], others="\n".join(f"- {i['text']}" for i in others) or "- (none)")
    tried: list[str] = []
    for attempt in range(2):          # a second call only when all three fail
        try:
            res = router.call("page", prompt, json_out=True, max_output_tokens=300)
        except (QuotaExhausted, Exception) as e:  # noqa: BLE001
            log.info("headline not written: %s", e)
            return None
        cands = (res.data or {}).get("headlines") if isinstance(res.data, dict) else None
        if isinstance(cands, str):
            cands = [cands]
        good, why = [], []
        for c in cands or []:
            h = house_style(str(c or "").strip().strip('"'))
            problem = headline_problem(h, source, banned, disputed)
            if problem:
                why.append(f'"{h}": {problem}')
            else:
                good.append(h)
        if good:
            return max(good, key=lambda h: headline_score(h, news["text"]))   # ties: the model's first
        tried += why
        prompt += "\n\nThese were rejected:\n" + "\n".join(f"- {w}" for w in why[:3]) + "\nWrite three new ones."
    log.info("no headline passed: %s", "; ".join(tried)[:400])
    return None
