"""Which sections of the site an article belongs to (owner, Oct 9 2026).

Five primary sections, five secondary sections under each, chosen with the owner from ~490 rated
stories (Oct 2026: politics ~30%, crime and courts ~25%, world ~11%, accidents and weather ~8%,
business ~7%, health and education ~7%, defence ~5%, films ~3%, sport ~1%). Sport has too few stories of
its own, so it sits under Life.

An article gets up to two primary and up to two secondary sections ("Supreme Court refuses to suspend the
CEC" is Politics > Elections and Justice > Courts). One page-tier call when the article is published,
from its headline, its lead and its main statements; asked once (a section can make nothing green and
merge nothing). Code checks the answer: only listed keys, a secondary brings its primary with it, at most
two of each, a primary alone when the model is unsure of the secondary. No answer = no section: the
article is published anyway, it is never held back for this.

Stored as `payload.category = {"primary": [...], "secondary": [...]}` (keys, first = main), the same in
the Hindi payload; the labels in both languages are here (LABELS) and in the feed (feed.py).
"""
from __future__ import annotations

import logging
import re

from .router import QuotaExhausted, Router

log = logging.getLogger(__name__)

# key: (English, Hindi, what belongs there, for the prompt)
SECTIONS: dict[str, dict[str, tuple[str, str, str]]] = {
    "politics": {
        "elections": ("Elections", "चुनाव", "polls, by-elections, voter lists, the Election Commission, candidates, results"),
        "parties": ("Parties", "दल", "parties and leaders: alliances, splits, appointments in a party, leaders' attacks on each other"),
        "government": ("Government", "सरकार", "decisions, schemes, appointments and rules of the Centre or a state government"),
        "parliament": ("Parliament", "संसद", "Lok Sabha, Rajya Sabha, state assemblies, bills, MPs and MLAs in the House"),
        "protests": ("Protests", "प्रदर्शन", "political marches, sit-ins, strikes, detentions of protesters"),
    },
    "justice": {
        "crime": ("Crime", "अपराध", "murders, assaults, rape, theft, fraud, cyber crime"),
        "police": ("Police", "पुलिस", "arrests, FIRs, investigations, police conduct and orders"),
        "courts": ("Courts", "अदालत", "Supreme Court, High Courts and other courts: hearings, orders, bail, verdicts"),
        "corruption": ("Corruption", "भ्रष्टाचार", "bribery, graft, CBI/ED/vigilance cases against officials and leaders"),
        "terror": ("Terror", "आतंक", "terror attacks and arrests, espionage, Maoists, militants inside India"),
    },
    "business": {
        "economy": ("Economy", "अर्थव्यवस्था", "RBI, GST, budget, trade, growth, economic policy"),
        "companies": ("Companies", "कंपनियां", "firms, markets, deals, regulators acting on a company"),
        "jobs": ("Jobs", "नौकरियां", "hiring, recruitment exams and drives, layoffs, workers"),
        "money": ("Your money", "आपका पैसा", "prices, EMIs, taxes on people, fares, fees, what things cost a household"),
        "tech": ("Tech", "टेक", "technology, telecom, the internet, AI, apps, space industry"),
    },
    "world": {
        "diplomacy": ("Diplomacy", "कूटनीति", "India's talks, visits, statements and agreements with other countries or the UN"),
        "indians-abroad": ("Indians abroad", "विदेश में भारतीय", "Indians, Indian crews and workers in trouble or in the news abroad"),
        "conflicts": ("Conflicts", "संघर्ष", "wars and attacks outside India"),
        "defence": ("Defence", "रक्षा", "the armed forces, defence deals, weapons tests, DRDO, service chiefs"),
        "abroad": ("Abroad", "विदेश", "news from other countries that is not about India"),
    },
    "life": {
        "health": ("Health", "सेहत", "hospitals, diseases, medicines, food safety"),
        "education": ("Education", "शिक्षा", "schools, universities, IITs, exams, students, research and science prizes"),
        "accidents": ("Accidents", "हादसे", "road, rail and air accidents, fires, collapses, floods, earthquakes, deaths in disasters"),
        "environment": ("Environment", "पर्यावरण", "weather forecasts, pollution, wildlife, forests, climate"),
        "sport-films": ("Sport & films", "खेल और फ़िल्में", "sport, cinema, TV, music, actors and players"),
    },
}
PRIMARY_LABELS = {"politics": ("Politics", "राजनीति"), "justice": ("Justice", "न्याय"),
                  "business": ("Business", "बिज़नेस"), "world": ("World", "दुनिया"), "life": ("Life", "जीवन")}
PARENT = {s: p for p, subs in SECTIONS.items() for s in subs}
MAX_EACH = 2


def labels(lang: str = "en") -> dict:
    """{"primary": {key: label}, "secondary": {key: label}, "tree": {primary: [secondary, ...]}} for the site."""
    k = 1 if lang == "hi" else 0
    return {"primary": {p: PRIMARY_LABELS[p][k] for p in SECTIONS},
            "secondary": {s: SECTIONS[PARENT[s]][s][k] for s in PARENT},
            "tree": {p: list(subs) for p, subs in SECTIONS.items()}}


def _menu() -> str:
    out = []
    for p, subs in SECTIONS.items():
        out.append(f"{p}  ({PRIMARY_LABELS[p][0]})")
        out += [f"  {p}/{s}: {d}" for s, (_, _, d) in subs.items()]
    return "\n".join(out)


PROMPT = """Put this Indian news article in the sections of a news site.

Sections (primary, then its secondary sections):
{menu}

Pick 1 to 3 sections, the main one first, as "primary/secondary" (for example "justice/courts").
Pick a second or third only if the article is really about that too, not because a word appears.
If you are unsure which secondary section fits, give the primary alone (for example "life").
Judge by what the NEWS is (the headline), not by who is in it.

Examples:
- "Supreme Court refuses to suspend Chief Election Commissioner" -> ["justice/courts", "politics/elections"]
- "Police detain Rahul Gandhi at Jantar Mantar protest" -> ["politics/protests"]  (a protest, not a crime)
- "Supreme Court orders actor Rajpal Yadav to pay Rs 5 crore" -> ["justice/courts"]  (an actor, but the news is the order)
- "Actor Nana Patekar dies at 75" -> ["life/sport-films"]
- "Defence Ministry signs Rs 661 crore BrahMos deal" -> ["world/defence"]  (defence, not business)
- "23 Indian crew rescued from burning tanker in Black Sea" -> ["world/indians-abroad", "world/conflicts"]
- "RBI raises repo rate to 5.50 per cent" -> ["business/economy", "business/money"]
- "Four students die cleaning a water tank in Vrindavan" -> ["life/accidents"]
- "CBI raids Punjab CM's office over graft claims" -> ["justice/corruption", "politics/government"]
- "Two arrested in Assam for passing secrets to Pakistan" -> ["justice/terror"]
- "Rain and strong winds forecast in 24 states" -> ["life/environment"]

HEADLINE: {headline}
THE ARTICLE OPENS: {lead}
OTHER FACTS:
{facts}

Reply with JSON only: {{"sections": ["...", "..."]}}"""


def _key(x: str) -> str:
    return re.sub(r"[\s_]+", "-", str(x or "").strip().lower().strip('"\''))


def parse(picks) -> dict | None:
    """The model's picks, checked by code: only listed keys; a secondary brings its primary; at most two of
    each, in the model's order; a pick of a secondary under the wrong primary keeps neither guess."""
    if isinstance(picks, str):
        picks = [picks]
    primary: list[str] = []
    secondary: list[str] = []
    for raw in picks or []:
        p, _, s = str(raw or "").partition("/")
        p, s = _key(p).strip("-"), _key(s).strip("-")
        if not s and p in PARENT:          # a secondary given alone: its primary is known
            p, s = PARENT[p], p
        if p not in SECTIONS or (s and s not in SECTIONS[p]):
            continue
        if p not in primary:
            if len(primary) >= MAX_EACH:
                continue
            primary.append(p)
        if s and s not in secondary and len(secondary) < MAX_EACH:
            secondary.append(s)
    return {"primary": primary, "secondary": secondary} if primary else None


def classify(router: Router | None, headline: str, lead: str, facts: list[str]) -> dict | None:
    """The article's sections; {} when the model answered twice with no listed section; None when it could
    not be asked (quota, errors). Either way the article is published, without a section."""
    if router is None or not headline:
        return None
    prompt = PROMPT.format(menu=_menu(), headline=headline, lead=lead or headline,
                           facts="\n".join(f"- {f}" for f in facts[:5]) or "- (none)")
    for _ in range(2):                     # a second call only when the first gave nothing usable
        try:
            res = router.call("page", prompt, json_out=True, max_output_tokens=120)
        except (QuotaExhausted, Exception) as e:  # noqa: BLE001
            log.info("no sections: %s", e)
            return None
        data = res.data if isinstance(res.data, dict) else {}
        got = parse(data.get("sections"))
        if got:
            return got
        prompt += '\n\nThat answer used no listed section. Use only the keys above, like "life/health".'
    return {}


def for_payload(router: Router | None, payload: dict) -> dict | None:
    """Sections from a published or about-to-publish payload: its headline, lead and best statements."""
    from .narrative import ordered_items
    paras = (payload.get("narrative") or {}).get("paragraphs") or [[]]
    lead = " ".join(x.get("text") or "" for x in paras[0]) if paras else ""
    items = [i for i in ordered_items(payload) if (i.get("role") or "core") == "core"]
    items.sort(key=lambda i: -(i.get("n_sources") or 0))
    return classify(router, payload.get("headline") or "", lead, [i["text"] for i in items if i.get("text")])


def fill_live(store, router: Router, limit: int = 5) -> list[int]:
    """Live articles published before sections existed get theirs (the desk, up to `limit` per run). Only the
    section is added: the article's text, headline and colours are untouched."""
    from .db import published, select, update
    from sqlalchemy import text as sql_text
    done: list[int] = []
    pg = store.engine.dialect.name == "postgresql"
    q = select(published.c.story_id, published.c.payload_en, published.c.payload_hi)
    if pg:
        q = q.where(sql_text("payload_en -> 'category' IS NULL"))   # the key is missing (json or jsonb)
    rows = store.rows(q.order_by(published.c.updated_at.desc()).limit(limit if pg else 10_000))
    for r in rows:
        if len(done) >= limit:
            break
        pe = dict(r["payload_en"] or {})
        if "category" in pe:
            continue
        cat = for_payload(router, pe)
        if cat is None:
            break                          # the page models cannot be asked now: the next run tries again
        pe["category"] = cat               # {} is kept too: asked, no section, not asked again
        ph = dict(r["payload_hi"] or {})
        ph["category"] = cat
        store.exec(update(published).where(published.c.story_id == r["story_id"]).values(payload_en=pe, payload_hi=ph))
        done.append(r["story_id"])
    return done
