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

Stored as `payload.category = {"primary": [...], "secondary": [...], "tertiary": [...]}` (keys, first =
main; tertiary only when there is one), the same in the Hindi payload; the labels in both languages are here
and in the feed (feed.py).

Business, Oct 9 2026 (owner, option A): Economy · Companies · Markets · Your money · Domains, and under
Domains five lenses: HR · Finance · Marketing · Analytics · Operations. Jobs and Tech became HR and Analytics
(`normalize` maps the keys live articles still carry). Up to two of each level.
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
        "companies": ("Companies", "कंपनियां", "firms: results, deals, mergers, IPOs, launches, a company's leaders, regulators acting on a company"),
        "markets": ("Markets", "बाज़ार", "stock markets, Sensex and Nifty, share prices, listings, gold, oil and currency prices"),
        "money": ("Your money", "आपका पैसा", "prices, EMIs, taxes on people, fares, fees, what things cost a household"),
        "domains": ("Domains", "क्षेत्र", "news for people working in a field; give one of its domains below"),
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
# Third level (owner, Oct 9 2026): a domain is a LENS over the news, not a separate pile of news. A story
# belongs to HR if it changes something for people at work, whoever reported it and whatever else it is.
TERTIARY: dict[str, dict[str, tuple[str, str, str]]] = {
    "domains": {
        "hr": ("HR", "एचआर", "work and workers: hiring, recruitment drives and exams, layoffs, pay, labour law, PF and benefits, work visas, workplace rules"),
        "finance": ("Finance", "फ़ाइनेंस", "banks, lending, insurance, investing, payments and UPI, financial regulators (RBI, SEBI, IRDAI) and their rules"),
        "marketing": ("Marketing", "मार्केटिंग", "advertising, brands, ad rules, endorsements, consumer campaigns, media business"),
        "analytics": ("Analytics", "एनालिटिक्स", "data, AI, technology, telecom, the internet, apps, cyber security, data protection, space industry"),
        "operations": ("Operations", "ऑपरेशंस", "supply chains, logistics, manufacturing, shipping and ports, airline and rail operations, procurement, power supply"),
    },
}
PARENT3 = {t: s for s, subs in TERTIARY.items() for t in subs}
# keys of earlier sections, as live articles still carry them (Oct 9 2026: Jobs and Tech became domains)
OLD_KEYS = {"jobs": ("business", "domains", "hr"), "tech": ("business", "domains", "analytics")}

PRIMARY_LABELS = {"politics": ("Politics", "राजनीति"), "justice": ("Justice", "न्याय"),
                  "business": ("Business", "बिज़नेस"), "world": ("World", "दुनिया"), "life": ("Life", "जीवन")}
PARENT = {s: p for p, subs in SECTIONS.items() for s in subs}
MAX_EACH = 2


def labels(lang: str = "en") -> dict:
    """{"primary": {key: label}, "secondary": {key: label}, "tertiary": {key: label},
    "tree": {primary: [secondary, ...]}, "tree3": {secondary: [tertiary, ...]}} for the site."""
    k = 1 if lang == "hi" else 0
    return {"primary": {p: PRIMARY_LABELS[p][k] for p in SECTIONS},
            "secondary": {s: SECTIONS[PARENT[s]][s][k] for s in PARENT},
            "tertiary": {t: TERTIARY[PARENT3[t]][t][k] for t in PARENT3},
            "tree": {p: list(subs) for p, subs in SECTIONS.items()},
            "tree3": {s: list(subs) for s, subs in TERTIARY.items()}}


def _menu() -> str:
    out = []
    for p, subs in SECTIONS.items():
        out.append(f"{p}  ({PRIMARY_LABELS[p][0]})")
        for s, (_, _, d) in subs.items():
            if s in TERTIARY:
                out += [f"  {p}/{s}/{t}: {dt}" for t, (_, _, dt) in TERTIARY[s].items()]
            else:
                out.append(f"  {p}/{s}: {d}")
    return "\n".join(out)


def normalize(cat: dict | None) -> dict:
    """A stored section record in today's keys: earlier keys (jobs, tech) become their domains."""
    cat = dict(cat or {})
    prim, sec, ter = list(cat.get("primary") or []), list(cat.get("secondary") or []), list(cat.get("tertiary") or [])
    for old, (p, s, t) in OLD_KEYS.items():
        if old in sec:
            sec[sec.index(old)] = s
            for lst, v in ((prim, p), (ter, t)):
                if v not in lst:
                    lst.append(v)
    sec = list(dict.fromkeys(sec))
    if not cat and not prim:
        return {}
    return {"primary": prim, "secondary": sec, **({"tertiary": ter} if ter else {})}


PROMPT = """Put this Indian news article in the sections of a news site.

Sections (primary, then its secondary sections; the domains, a third level, are lenses for people working in
that field):
{menu}

Pick 1 to 4 sections, the main one first, as "primary/secondary" (for example "justice/courts") or, for a
domain, "business/domains/hr".
Pick another only if the article is really about that too, not because a word appears. A domain is added to
a story of any section when the news changes something for people working in that field.
If you are unsure which secondary section fits, give the primary alone (for example "life").
Judge by what the NEWS is (the headline), not by who is in it.

Examples:
- "Supreme Court refuses to suspend Chief Election Commissioner" -> ["justice/courts", "politics/elections"]
- "Police detain Rahul Gandhi at Jantar Mantar protest" -> ["politics/protests"]  (a protest, not a crime)
- "Supreme Court orders actor Rajpal Yadav to pay Rs 5 crore" -> ["justice/courts"]  (an actor, but the news is the order)
- "Actor Nana Patekar dies at 75" -> ["life/sport-films"]
- "Defence Ministry signs Rs 661 crore BrahMos deal" -> ["world/defence"]  (defence, not business)
- "23 Indian crew rescued from burning tanker in Black Sea" -> ["world/indians-abroad", "world/conflicts"]
- "RBI raises repo rate to 5.50 per cent" -> ["business/economy", "business/money", "business/domains/finance"]
- "Chennai nurses protest for pay equity and permanent jobs" -> ["politics/protests", "business/domains/hr"]
- "TCS to cut 12,000 jobs" -> ["business/companies", "business/domains/hr"]
- "Supreme Court bars pharma firms from gifting doctors" -> ["justice/courts", "business/domains/marketing"]
- "Sensex falls 900 points as oil jumps" -> ["business/markets"]
- "Red Sea attacks force shipping lines to reroute, freight rates double" -> ["world/conflicts", "business/domains/operations"]
- "Government notifies data protection rules" -> ["politics/government", "business/domains/analytics"]
- "Four students die cleaning a water tank in Vrindavan" -> ["life/accidents"]
- "CBI raids Punjab CM's office over graft claims" -> ["justice/corruption", "politics/government"]
- "Two arrested in Assam for passing secrets to Pakistan" -> ["justice/terror"]
- "Rain and strong winds forecast in 24 states" -> ["life/environment"]

Also give the Indian states or union territories where the news HAPPENS, at most 3, main first: where the event
took place, or the state of a High Court or of a state government acting; NOT the city a central minister or the
Centre speaks from, and not a person's home state when the news is elsewhere. Use [] for news abroad or for news
about all of India. Keys: {states}

HEADLINE: {headline}
THE ARTICLE OPENS: {lead}
OTHER FACTS:
{facts}

Reply with JSON only: {{"sections": ["...", "..."], "states": ["..."]}}"""


def _key(x: str) -> str:
    return re.sub(r"[\s_]+", "-", str(x or "").strip().lower().strip('"\''))


def parse(picks) -> dict | None:
    """The model's picks, checked by code: only listed keys; a tertiary brings its secondary and primary, a
    secondary its primary; at most two of each level, in the model's order; a pick under the wrong parent keeps
    no guess. A secondary that has domains ("domains") counts only with one of them."""
    if isinstance(picks, str):
        picks = [picks]
    primary: list[str] = []
    secondary: list[str] = []
    tertiary: list[str] = []
    for raw in picks or []:
        parts = [_key(x).strip("-") for x in str(raw or "").split("/")]
        p, s, t = (parts + ["", "", ""])[:3]
        if not s and p in PARENT3:         # a domain given alone: its parents are known
            p, s, t = PARENT[PARENT3[p]], PARENT3[p], p
        elif not s and p in PARENT:        # a secondary given alone: its primary is known
            p, s = PARENT[p], p
        elif not t and s in PARENT3:       # "business/hr"
            s, t = PARENT3[s], s
        if p not in SECTIONS or (s and s not in SECTIONS[p]) or (t and (s not in TERTIARY or t not in TERTIARY[s])):
            continue
        if s in TERTIARY and not t:
            s = ""                          # "a domain" without saying which: the primary alone
        if p not in primary:
            if len(primary) >= MAX_EACH:
                continue
            primary.append(p)
        if s and s not in secondary:
            if len(secondary) >= MAX_EACH:
                continue
            secondary.append(s)
        if t and t not in tertiary and len(tertiary) < MAX_EACH:
            tertiary.append(t)
    if not primary:
        return None
    return {"primary": primary, "secondary": secondary, **({"tertiary": tertiary} if tertiary else {})}


def classify(router: Router | None, headline: str, lead: str, facts: list[str]) -> dict | None:
    """The article's sections; {} when the model answered twice with no listed section; None when it could
    not be asked (quota, errors). Either way the article is published, without a section."""
    if router is None or not headline:
        return None
    from .places import menu as state_menu
    prompt = PROMPT.format(menu=_menu(), states=state_menu(), headline=headline, lead=lead or headline,
                           facts="\n".join(f"- {f}" for f in facts[:5]) or "- (none)")
    for _ in range(2):                     # a second call only when the first gave nothing usable
        try:
            res = router.call("page", prompt, json_out=True, max_output_tokens=160)
        except (QuotaExhausted, Exception) as e:  # noqa: BLE001
            log.info("no sections: %s", e)
            return None
        data = res.data if isinstance(res.data, dict) else {}
        got = parse(data.get("sections"))
        if got:
            got["states"] = data.get("states") or []   # taken off by `take_places`, checked by code there
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


def take_places(cat: dict | None, payload: dict) -> list[str] | None:
    """The states the model named, taken off the section record and kept only where the article names them
    (places.confirm). None when the model was not asked."""
    if cat is None:
        return None
    from .places import article_text, confirm
    return confirm(cat.pop("states", []) or [], article_text(payload))


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
        pl = take_places(cat, pe)
        pe["category"] = cat               # {} is kept too: asked, no section, not asked again
        pe["places"] = pl
        from .people import from_payload
        pe["people"] = from_payload(pe)
        ph = dict(r["payload_hi"] or {})
        ph["category"] = cat
        ph["places"] = pl
        ph["people"] = pe["people"]
        store.exec(update(published).where(published.c.story_id == r["story_id"]).values(payload_en=pe, payload_hi=ph))
        done.append(r["story_id"])
    return done


def fill_places(store, router: Router, limit: int = 5) -> list[int]:
    """Live articles published before places existed (Oct 9 2026) get theirs, and their people (by code). The
    section question is asked again for the states only; the article's sections, text and colours are untouched."""
    from .db import published, select, update
    from .people import from_payload
    from sqlalchemy import text as sql_text
    done: list[int] = []
    pg = store.engine.dialect.name == "postgresql"
    q = select(published.c.story_id, published.c.payload_en, published.c.payload_hi)
    if pg:
        q = q.where(sql_text("payload_en -> 'places' IS NULL"))
    for r in store.rows(q.order_by(published.c.updated_at.desc()).limit(limit * 4 if pg else 10_000)):
        if len(done) >= limit:
            break
        pe = dict(r["payload_en"] or {})
        if "places" in pe:
            continue
        cat = for_payload(router, pe)
        if cat is None:
            break                          # the page models cannot be asked now
        pl = take_places(cat, pe)
        ppl = from_payload(pe)
        ph = dict(r["payload_hi"] or {})
        pe["places"] = ph["places"] = pl
        pe["people"] = ph["people"] = ppl
        store.exec(update(published).where(published.c.story_id == r["story_id"]).values(payload_en=pe, payload_hi=ph))
        done.append(r["story_id"])
    return done
