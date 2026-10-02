"""Nishpaksh reader. Streamlit app over the `published` table.

Share a Hindi link with ?lang=hi, an English one with ?lang=en.
"""
from __future__ import annotations

import datetime as dt
import html
import os
import sys
from pathlib import Path

import streamlit as st
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nishpaksh.config import normalize_db_url  # noqa: E402
from nishpaksh.db import Store, published, utcnow  # noqa: E402

st.set_page_config(page_title="Nishpaksh · निष्पक्ष", page_icon="⚖️", layout="centered")

T = {
    "en": {
        "tag": "One story, every side, sorted into what is established and what is not.",
        "empty": "No stories yet. The pipeline publishes a story only when sources from at least two perspectives cover it.",
        "sources": "independent sources", "articles": "articles", "updated": "updated",
        "read": "Read", "back": "← All stories",
        "no_est": "No fact in this story is yet corroborated across perspectives.",
        "timeline": "What happened", "same_period": "Same period, order not established",
        "undated": "Established, time not reported",
        "established": "Established facts", "contested": "Contested and unverified",
        "minor": "Smaller details reported by a single article ({n})",
        "framing": "How each side worded it", "fact": "Fact",
        "perspectives": "Perspectives in this story", "local_note": "* grouped from this story alone; not enough history yet for stable perspectives.",
        "all_sources": "All sources read", "method": "How this page is made",
        "who": "Who says what", "check": "Evidence check",
        "for": "reported by", "against": "denied by",
        "translation_partial": "Part of this page is still in English; the translation quota ran out.",
        "sec_happened": "What happened", "sec_contested": "Where accounts differ",
        "sec_one_side": "Reported by one side only",
        "legend_title": "How to read this story",
        "lg_established": "Established: reported by independent sources across perspectives, denied by none",
        "lg_disputed": "Disputed: some sources say it, others deny or contradict it",
        "lg_unverified": "One side only: reported, but not yet confirmed by other sources",
        "lg_false": "False: primary evidence (FIR, court record, official data, video) contradicts it",
        "lg_sup": "Numbers like ¹ ³ are the sources for that sentence, listed at the end",
        "details": "Statement by statement", "numbered_sources": "Sources",
        "method_text": (
            "Every article is read by an AI model that only records what it says: events, claims, times, "
            "and the emotive words used. Code then groups articles into stories, counts copies of the same "
            "news-agency text as one source, and finds perspectives from which sources agree with each other. "
            "No outlet is labelled left or right by hand.\n\n"
            "**Corroborated**: reported by at least two independent sources from at least two perspectives, denied by none.  \n"
            "**Disputed**: some sources say it, others deny or contradict it.  \n"
            "**Unverified**: reported by one source, or by one perspective only.  \n"
            "**False / Confirmed**: primary evidence (FIR, court record, official data, video) contradicts / supports it, "
            "and two independent AI models agree. Official statements alone are never enough."),
    },
    "hi": {
        "tag": "एक ख़बर, हर पक्ष — क्या स्थापित है और क्या नहीं, अलग-अलग।",
        "empty": "अभी कोई ख़बर नहीं। कोई ख़बर तभी प्रकाशित होती है जब कम से कम दो दृष्टिकोणों के स्रोत उसे कवर करें।",
        "sources": "स्वतंत्र स्रोत", "articles": "लेख", "updated": "अपडेट",
        "read": "पढ़ें", "back": "← सभी ख़बरें",
        "no_est": "इस ख़बर का कोई भी तथ्य अभी अलग-अलग दृष्टिकोणों से पुष्ट नहीं हुआ है।",
        "timeline": "क्या हुआ", "same_period": "एक ही समय के आसपास, क्रम स्थापित नहीं",
        "undated": "स्थापित, पर समय नहीं बताया गया",
        "established": "स्थापित तथ्य", "contested": "विवादित और अपुष्ट",
        "minor": "केवल एक लेख में बताई गई छोटी बातें ({n})",
        "framing": "किस पक्ष ने किन शब्दों में कहा", "fact": "तथ्य",
        "perspectives": "इस ख़बर के दृष्टिकोण", "local_note": "* केवल इसी ख़बर के आधार पर समूह; स्थायी दृष्टिकोणों के लिए अभी पर्याप्त इतिहास नहीं।",
        "all_sources": "पढ़े गए सभी स्रोत", "method": "यह पेज कैसे बनता है",
        "who": "कौन क्या कहता है", "check": "साक्ष्य जाँच",
        "for": "किसने कहा", "against": "किसने खंडन किया",
        "translation_partial": "अनुवाद कोटा ख़त्म होने से इस पेज का कुछ हिस्सा अभी अंग्रेज़ी में है।",
        "sec_happened": "क्या हुआ", "sec_contested": "जहाँ ख़बरें अलग-अलग हैं",
        "sec_one_side": "जो केवल एक पक्ष बता रहा है",
        "legend_title": "इस ख़बर को कैसे पढ़ें",
        "lg_established": "स्थापित: अलग-अलग दृष्टिकोणों के स्वतंत्र स्रोतों ने बताया, किसी ने खंडन नहीं किया",
        "lg_disputed": "विवादित: कुछ स्रोत कहते हैं, कुछ खंडन करते हैं",
        "lg_unverified": "केवल एक पक्ष: बताया गया, पर दूसरे स्रोतों से अभी पुष्टि नहीं",
        "lg_false": "असत्य: प्राथमिक साक्ष्य (FIR, अदालती रिकॉर्ड, सरकारी आँकड़े, वीडियो) इसका खंडन करता है",
        "lg_sup": "¹ ³ जैसे अंक उस वाक्य के स्रोत हैं, जिनकी सूची अंत में है",
        "details": "एक-एक कथन का ब्योरा", "numbered_sources": "स्रोत",
        "method_text": (
            "हर लेख को एक AI मॉडल पढ़ता है जो केवल दर्ज करता है कि लेख में क्या कहा गया: घटनाएँ, दावे, समय, और भावनात्मक शब्द। "
            "फिर कोड लेखों को ख़बरों में जोड़ता है, एक ही समाचार एजेंसी की कॉपी को एक स्रोत गिनता है, और यह देखकर "
            "दृष्टिकोण पहचानता है कि कौन से स्रोत आपस में सहमत होते हैं। किसी भी संस्थान पर हाथ से वाम या दक्षिण का ठप्पा नहीं लगाया जाता।\n\n"
            "**पुष्ट**: कम से कम दो दृष्टिकोणों के दो स्वतंत्र स्रोत कहें, कोई खंडन न करे।  \n"
            "**विवादित**: कुछ स्रोत कहें, कुछ खंडन करें।  \n"
            "**अपुष्ट**: केवल एक स्रोत या एक ही दृष्टिकोण कहे।  \n"
            "**असत्य / प्रमाणित**: प्राथमिक साक्ष्य (FIR, अदालती रिकॉर्ड, सरकारी आँकड़े, वीडियो) खंडन / समर्थन करे "
            "और दो स्वतंत्र AI मॉडल सहमत हों। केवल सरकारी बयान कभी काफ़ी नहीं।"),
    },
}
VERDICT = {
    "corroborated": (":green-background[Corroborated]", ":green-background[पुष्ट]"),
    "confirmed": (":blue-background[Confirmed by evidence]", ":blue-background[साक्ष्य से प्रमाणित]"),
    "disputed": (":orange-background[Disputed]", ":orange-background[विवादित]"),
    "unverified": (":gray-background[Unverified]", ":gray-background[अपुष्ट]"),
    "false": (":red-background[False]", ":red-background[असत्य]"),
    "pending": (":gray-background[Pending]", ":gray-background[लंबित]"),
}
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Noto+Serif+Devanagari:wght@400;600&display=swap');
.np-story { font-family: 'Source Serif 4', 'Noto Serif Devanagari', Georgia, serif; font-size: 1.12rem;
            line-height: 1.8; }
.np-story h4 { font-family: inherit; font-size: 0.8rem; letter-spacing: .08em; text-transform: uppercase;
               opacity: .65; margin: 1.6rem 0 .3rem; font-weight: 600; }
.np-story p { margin: 0 0 .9rem; }
.np-s.established { color: #1f9254; }
.np-s.disputed    { color: #c66a00; }
.np-s.unverified  { color: #7d8590; }
.np-s.false       { color: #d0342c; }
.np-story sup { font-size: .62em; margin-left: 1px; font-family: system-ui, sans-serif; }
.np-story sup a { color: inherit; text-decoration: none; opacity: .85; }
.np-legend { font-family: system-ui, sans-serif; font-size: .85rem; line-height: 1.6; padding: .75rem 1rem;
             border: 1px solid rgba(128,128,128,.25); border-radius: 10px; margin: .5rem 0 1.25rem; }
.np-legend b { font-weight: 600; }
.np-dot { display: inline-block; width: .7em; height: .7em; border-radius: 50%; margin-right: .45em;
          vertical-align: baseline; }
.np-sources { font-family: system-ui, sans-serif; font-size: .85rem; line-height: 1.7; }
.np-sources a { text-decoration: none; }
</style>
"""
DOT = {"established": "#1f9254", "disputed": "#c66a00", "unverified": "#7d8590", "false": "#d0342c"}


def legend_html(t: dict, present: set[str]) -> str:
    rows = [f'<div><span class="np-dot" style="background:{DOT[k]}"></span>{html.escape(t["lg_" + k])}</div>'
            for k in ("established", "disputed", "unverified", "false") if k in present]
    rows.append(f'<div style="margin-top:.35rem;opacity:.8">{html.escape(t["lg_sup"])}</div>')
    return f'<div class="np-legend"><b>{html.escape(t["legend_title"])}</b>{"".join(rows)}</div>'


def narrative_html(nar: dict, t: dict) -> str:
    names = {s["n"]: s["outlet"] for s in nar["sources"]}
    parts = ['<div class="np-story">']
    for sec in nar["sections"]:
        parts.append(f'<h4>{html.escape(t["sec_" + sec["key"]])}</h4><p>')
        for x in sec["sentences"]:
            nums = x["sources"]
            shown = nums[:4]
            sup = ",".join(f'<a href="#src-{n}" title="{html.escape(names.get(n, ""))}">{n}</a>' for n in shown)
            if len(nums) > 4:
                sup += f",+{len(nums) - 4}"
            tip = html.escape(", ".join(sorted({names.get(n, "") for n in nums})))
            parts.append(f'<span class="np-s {x["class"]}" title="{tip}">{html.escape(x["text"])}</span>'
                         f'<sup>{sup}</sup> ')
        parts.append("</p>")
    parts.append("</div>")
    return "".join(parts)


def sources_html(nar: dict) -> str:
    rows = []
    for s in nar["sources"]:
        persp = f' · {html.escape(s["perspective"])}' if s.get("perspective") and s["perspective"] != "–" else ""
        rows.append(f'<div id="src-{s["n"]}"><b>{s["n"]}.</b> <a href="{html.escape(s["url"])}" target="_blank">'
                    f'{html.escape(s["outlet"])}: {html.escape(s["title"] or "")}</a>{persp}</div>')
    return '<div class="np-sources">' + "".join(rows) + "</div>"


STANCE = {"en": {"asserts": "states", "attributes": "reports a claim", "denies": "denies"},
          "hi": {"asserts": "कहता है", "attributes": "दावे का हवाला देता है", "denies": "खंडन करता है"}}


@st.cache_resource
def get_store() -> Store:
    url = os.environ.get("DATABASE_URL", "")
    try:
        url = st.secrets.get("DATABASE_URL", url)
    except Exception:  # no secrets file locally
        pass
    if url:
        url = normalize_db_url(url)
    else:
        url = f"sqlite:///{Path(__file__).resolve().parent.parent / 'data' / 'nishpaksh.db'}"
    s = Store(url)
    s.init()
    return s


@st.cache_data(ttl=300)
def list_stories() -> list[dict]:
    since = utcnow() - dt.timedelta(days=7)
    rows = get_store().rows(select(published.c.story_id, published.c.updated_at, published.c.headline_en,
                                   published.c.headline_hi, published.c.payload_en)
                            .where(published.c.updated_at >= since).order_by(published.c.updated_at.desc()).limit(60))
    for r in rows:
        p = r.pop("payload_en") or {}
        r["counts"] = p.get("counts", {})
        r["perspectives"] = sorted(p.get("perspectives", {}))
    return rows


@st.cache_data(ttl=300)
def get_story(story_id: int) -> dict | None:
    return get_store().one(select(published).where(published.c.story_id == story_id))


def ago(t: dt.datetime, lang: str) -> str:
    mins = int((utcnow() - t).total_seconds() // 60)
    if mins < 60:
        return f"{mins} min" if lang == "en" else f"{mins} मिनट पहले"
    h = mins // 60
    return (f"{h} h ago" if lang == "en" else f"{h} घंटे पहले") if h < 48 else t.strftime("%d %b")


def badge(verdict: str, lang: str) -> str:
    return VERDICT.get(verdict, VERDICT["pending"])[0 if lang == "en" else 1]


def sources_block(item: dict, lang: str, t: dict) -> None:
    with st.expander(t["who"], expanded=False):
        for s in item["sources"]:
            p = f" ({s['perspective']})" if s["perspective"] != "–" else ""
            st.markdown(f"- [{s['outlet']}]({s['url']}){p} — {STANCE[lang].get(s['stance'], s['stance'])}"
                        f" · {s['attributed_to']} · `{s['evidence']}`")
        chk = item.get("check")
        if chk:
            st.markdown(f"**{t['check']}** · {chk.get('checked_at', '')}")
            for r in chk.get("reasons", []):
                st.markdown(f"> {r}")
            for u in chk.get("evidence_urls", []) + [w["url"] for w in chk.get("web_sources", [])]:
                st.markdown(f"- {u}")


def render_item(item: dict, lang: str, t: dict, show_badge: bool = True) -> None:
    head = f"{badge(item['verdict'], lang)} " if show_badge else ""
    when = (item.get("time") or {}).get("when_text")
    line = f"{head}{item['text']}"
    if when:
        line += f"  \n:gray[{when}]"
    if item.get("supported_by") or item.get("denied_by"):
        bits = []
        if item.get("supported_by"):
            bits.append(f"{t['for']}: {', '.join(item['supported_by'])}")
        if item.get("denied_by"):
            bits.append(f"{t['against']}: {', '.join(item['denied_by'])}")
        line += f"  \n:gray[{' · '.join(bits)}]"
    st.markdown(line)
    sources_block(item, lang, t)


def _details(p: dict, lang: str, t: dict) -> None:
    """The statement-by-statement structure behind the story."""
    if p["timeline"] or p["undated"]:
        st.markdown(f"**{t['timeline']}**")
        for k, tier in enumerate(p["timeline"]):
            if len(tier) > 1:
                st.caption(f"{k + 1}. {t['same_period']}")
            for item in tier:
                render_item(item, lang, t, show_badge=False)
        if p["undated"]:
            st.caption(t["undated"])
            for item in p["undated"]:
                render_item(item, lang, t, show_badge=False)
    if p["established"]:
        st.markdown(f"**{t['established']}**")
        for item in p["established"]:
            render_item(item, lang, t, show_badge=False)
    if p["contested"]:
        st.markdown(f"**{t['contested']}**")
        for item in [i for i in p["contested"] if not i["minor"]]:
            render_item(item, lang, t)
        minor = [i for i in p["contested"] if i["minor"]]
        if minor:
            st.caption(t["minor"].format(n=len(minor)))
            for item in minor:
                render_item(item, lang, t)


def story_view(sid: int, lang: str, t: dict) -> None:
    row = get_story(sid)
    if st.button(t["back"]):
        st.query_params.clear()
        st.query_params["lang"] = lang
        st.rerun()
    if not row:
        st.warning("Not found")
        return
    p = (row["payload_hi"] if lang == "hi" and row["payload_hi"] else row["payload_en"])
    st.markdown(CSS, unsafe_allow_html=True)
    st.header(p["headline"])
    c = p.get("counts", {})
    st.caption(f"{c.get('independent_sources', 0)} {t['sources']} · {c.get('articles', 0)} {t['articles']} · "
               f"{t['updated']} {ago(row['updated_at'], lang)}")
    if lang == "hi" and not p.get("translation_complete", True):
        st.info(t["translation_partial"])
    if not p.get("has_established"):
        st.info(t["no_est"])

    nar = p.get("narrative")
    if nar and nar.get("sections"):
        present = {x["class"] for sec in nar["sections"] for x in sec["sentences"]}
        st.markdown(legend_html(t, present), unsafe_allow_html=True)
        st.markdown(narrative_html(nar, t), unsafe_allow_html=True)
        if p["framing"]:
            st.subheader(t["framing"])
            persp = sorted({k for f in p["framing"] for k in f["words"]})
            rows = [{t["fact"]: f["text"], **{k: ", ".join(f["words"].get(k, [])) for k in persp}}
                    for f in p["framing"]]
            st.dataframe(rows, hide_index=True, use_container_width=True)
        st.subheader(t["numbered_sources"])
        st.markdown(sources_html(nar), unsafe_allow_html=True)
        st.caption(" · ".join(f"**{label}**: {', '.join(outlets)}" for label, outlets in p["perspectives"].items()))
        if p.get("perspective_mode") == "story":
            st.caption(t["local_note"])
        with st.expander(t["details"]):
            _details(p, lang, t)
    else:  # pages published before the narrative existed
        _details(p, lang, t)
        with st.expander(t["all_sources"]):
            for s in p["sources"]:
                st.markdown(f"- **{s['perspective']}** · [{s['outlet']}: {s['title']}]({s['url']})")
    with st.expander(t["method"]):
        st.markdown(t["method_text"])


def list_view(lang: str, t: dict) -> None:
    rows = list_stories()
    if not rows:
        st.info(t["empty"])
        return
    for r in rows:
        head = (r["headline_hi"] if lang == "hi" and r["headline_hi"] else r["headline_en"])
        with st.container(border=True):
            st.markdown(f"#### {head}")
            c = r["counts"]
            st.caption(f"{c.get('independent_sources', 0)} {t['sources']} · {' / '.join(r['perspectives'])} · "
                       f"{t['updated']} {ago(r['updated_at'], lang)}")
            if st.button(t["read"], key=f"s{r['story_id']}"):
                st.query_params["story"] = str(r["story_id"])
                st.query_params["lang"] = lang
                st.rerun()


def main() -> None:
    qp = st.query_params
    lang = qp.get("lang", "en") if qp.get("lang") in ("en", "hi") else "en"
    left, right = st.columns([3, 1])
    with left:
        st.title("Nishpaksh · निष्पक्ष")
    with right:
        choice = st.radio("lang", ["English", "हिंदी"], index=0 if lang == "en" else 1,
                          horizontal=True, label_visibility="collapsed")
    new_lang = "en" if choice == "English" else "hi"
    if new_lang != lang:
        st.query_params["lang"] = new_lang
        st.rerun()
    t = T[lang]
    st.caption(t["tag"])
    story = qp.get("story")
    if story and story.isdigit():
        story_view(int(story), lang, t)
    else:
        list_view(lang, t)


main()
