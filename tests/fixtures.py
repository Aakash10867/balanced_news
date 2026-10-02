"""Fictional fixtures: one contested story and one consensus story, English and Hindi."""
from __future__ import annotations

import datetime as dt
import json
import re

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None)
D = (NOW - dt.timedelta(days=1)).strftime("%Y-%m-%d")


def ev(i, text, start=None, end=None, prec="unknown", stance="asserts", by="article", evidence="none", words=()):
    return {"id": i, "text": text, "when_text": "", "start": start, "end": end, "precision": prec,
            "stance": stance, "attributed_to": by, "evidence": evidence, "loaded_words": list(words)}


def cl(i, text, stance="asserts", by="article", evidence="none", words=()):
    return {"id": i, "text": text, "stance": stance, "attributed_to": by, "evidence": evidence,
            "loaded_words": list(words)}


COLLAPSE = f"{D}T21:00"
RAIN_S, RAIN_E = f"{D}T14:00", f"{D}T17:00"
ARREST = f"{D}T23:30"

SIG1 = "A section of the Kesarganj flyover collapsed, killing two people"
SIG2 = "The monsoon session of the state assembly began in Lucknow"

ARTICLES = [
    # ---- side that blames the contractor ----
    dict(outlet="Daily Alpha", lang="en", title="Kesarganj flyover collapse kills two", author="PTI",
         text="(PTI) " + "Two people died when a section of the Kesarganj flyover collapsed. " * 30,
         ex={"signature": SIG1,
             "events": [ev("e1", "A section of the Kesarganj flyover collapsed", COLLAPSE, COLLAPSE, "exact",
                           words=["horrific"]),
                        ev("e2", "Police arrested the site engineer", ARREST, ARREST, "exact", by="police",
                           evidence="official_statement")],
             "claims": [cl("c1", "Two people died in the collapse", evidence="official_data"),
                        cl("c2", "The contractor used substandard material", by="unnamed source",
                           evidence="unnamed_source", words=["shoddy", "criminal negligence"])],
             "relations": [{"from": "e1", "to": "e2", "type": "before", "stance": "asserts", "attributed_to": "article"}]}),
    dict(outlet="Gamma Daily", lang="en", title="Two dead as flyover caves in at Kesarganj", author="PTI",
         text="(PTI) " + "Two people died when a section of the Kesarganj flyover collapsed. " * 30,
         ex={"signature": SIG1,
             "events": [ev("e1", "A section of the Kesarganj flyover collapsed", COLLAPSE, COLLAPSE, "exact"),
                        ev("e2", "Police arrested the site engineer", ARREST, ARREST, "exact", by="police")],
             "claims": [cl("c1", "Two people died in the collapse"),
                        cl("c2", "The contractor used substandard material", by="unnamed source",
                           evidence="unnamed_source")],
             "relations": []}),
    dict(outlet="Alpha Times", lang="en", title="Negligence behind Kesarganj flyover tragedy", author="R. Sharma",
         text="Residents say the contractor cut corners on the Kesarganj flyover. " * 30,
         ex={"signature": SIG1,
             "events": [ev("e1", "A part of the Kesarganj flyover collapsed", COLLAPSE, COLLAPSE, "exact",
                           words=["tragedy"]),
                        ev("e2", "Police arrested the site engineer", ARREST, ARREST, "exact", by="police")],
             "claims": [cl("c1", "Two people were killed in the collapse"),
                        cl("c2", "The contractor used substandard material", by="residents",
                           evidence="named_witness", words=["corrupt"])],
             "relations": []}),
    # ---- side that blames the rain ----
    dict(outlet="Beta News", lang="en", title="Rain triggers partial collapse of Kesarganj flyover", author="A. Khan",
         text="Hours of heavy rain preceded the partial collapse of the Kesarganj flyover. " * 30,
         ex={"signature": SIG1,
             "events": [ev("e0", "Heavy rain fell in Kesarganj", RAIN_S, RAIN_E, "part_of_day"),
                        ev("e1", "A section of the Kesarganj flyover collapsed", COLLAPSE, COLLAPSE, "exact"),
                        ev("e2", "Police arrested the site engineer", ARREST, ARREST, "exact", by="police")],
             "claims": [cl("c1", "Two people died in the collapse"),
                        cl("c2", "The contractor used substandard material", stance="denies", by="PWD minister",
                           evidence="official_statement", words=["baseless"])],
             "relations": [{"from": "e0", "to": "e1", "type": "caused", "stance": "attributes",
                            "attributed_to": "PWD minister"}]}),
    dict(outlet="बीटा समाचार", lang="hi", title="केसरगंज में बारिश के बाद फ्लाईओवर का हिस्सा गिरा", author="संवाददाता",
         text="केसरगंज में भारी बारिश के बाद फ्लाईओवर का एक हिस्सा गिर गया। " * 30,
         ex={"signature": SIG1,
             "events": [ev("e0", "Heavy rain fell in Kesarganj", RAIN_S, RAIN_E, "part_of_day",
                           words=["प्राकृतिक आपदा"]),
                        ev("e1", "A section of the Kesarganj flyover collapsed", COLLAPSE, COLLAPSE, "exact")],
             "claims": [cl("c1", "Two people died in the collapse"),
                        cl("c2", "The contractor used substandard material", stance="denies",
                           by="PWD minister", evidence="official_statement", words=["साज़िश"])],
             "relations": [{"from": "e0", "to": "e1", "type": "caused", "stance": "attributes",
                            "attributed_to": "PWD minister"}]}),
    # ---- consensus story ----
    dict(outlet="Daily Alpha", lang="en", title="State assembly monsoon session begins", author="S. Rao",
         text="The monsoon session of the state assembly began on Monday in Lucknow. " * 30,
         ex={"signature": SIG2, "events": [ev("e1", "The monsoon session of the state assembly began in Lucknow")],
             "claims": [], "relations": []}),
    dict(outlet="Beta News", lang="en", title="Assembly session opens in Lucknow", author="M. Iyer",
         text="Lucknow saw the state assembly open its monsoon session on Monday morning. " * 30,
         ex={"signature": SIG2, "events": [ev("e1", "The monsoon session of the state assembly began in Lucknow")],
             "claims": [], "relations": []}),
]

EXTRACTIONS = {a["title"]: a["ex"] for a in ARTICLES}


class FakeBackend:
    """Deterministic stand-in for the Gemini API, keyed on prompt content."""

    def __init__(self, contradict_words=("substandard",)):
        self.calls: list[tuple[str, str]] = []
        self.contradict_words = contradict_words

    def list_models(self):
        return ["gemma-4-31b-it", "gemma-4-26b-a4b-it", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                "gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite",
                "gemini-embedding-2"]

    def generate(self, model, prompt, json_mode, grounded):
        self.calls.append((model, prompt[:40]))
        if "careful annotator" in prompt:
            title = re.search(r"Title: (.*)", prompt).group(1).strip()
            return json.dumps(EXTRACTIONS[title], ensure_ascii=False), [], 900
        if "Each numbered line has two statements" in prompt:
            res = []
            for n, a, b in re.findall(r'(\d+)\. A: "(.*?)" \| B: "(.*?)"', prompt):
                wa, wb = set(a.lower().split()), set(b.lower().split())
                label = "same" if len(wa & wb) / len(wa | wb) >= 0.5 else "different"
                res.append({"n": int(n), "label": label})
            return json.dumps({"results": res}), [], 200
        if "Write one news headline" in prompt:
            return json.dumps({"headline": "Section of Kesarganj flyover collapses; two dead, engineer arrested"}), [], 50
        if "Translate each value" in prompt:
            payload = json.loads(prompt[prompt.index("{"):])
            return json.dumps({k: f"[हिं] {v}" for k, v in payload.items()}, ensure_ascii=False), [], 300
        if "Search the web for PRIMARY" in prompt:
            return ("Court filing (example.org/order) says the material passed lab tests.",
                    [{"url": "https://example.org/order", "title": "example.org"}], 300)
        if "You are checking one statement" in prompt:
            stmt = re.search(r'Statement: "(.*)"', prompt).group(1)
            if any(w in stmt for w in self.contradict_words):
                return json.dumps({"verdict": "contradicted", "basis": "court_record",
                                   "reason": "Lab test report filed in court shows material met specification.",
                                   "evidence_urls": ["https://example.org/order"]}), [], 150
            return json.dumps({"verdict": "insufficient", "basis": "none", "reason": "No primary evidence.",
                               "evidence_urls": []}), [], 150
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    def embed(self, model, texts):
        # stand-in for a multilingual embedding: same event -> same direction, any language
        out = []
        for t in texts:
            if "Kesarganj" in t or "केसरगंज" in t:
                out.append([1.0, 0.05, 0.0])
            elif "assembly" in t.lower() or "Lucknow" in t:
                out.append([0.0, 1.0, 0.05])
            else:
                out.append([0.05, 0.0, 1.0])
        return out
