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
         # a dateline and a named reporter: original reporting from the place
         text="KESARGANJ: " + "Residents say the contractor cut corners on the Kesarganj flyover. " * 30,
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
        if "decide whether A and B can both be true at the same time" in prompt:
            # same wording apart from the values: one question, two answers; otherwise both can be true
            res = []
            for n, a, b in re.findall(r'(\d+)\. A: "(.*?)" \| B: "(.*?)"', prompt):
                strip = lambda t: {w for w in re.findall(r"[a-z]+", t.lower()) if len(w) > 2}
                wa, wb = strip(a), strip(b)
                same_q = len(wa & wb) / max(1, len(wa | wb)) >= 0.5
                res.append({"n": int(n), "answer": "cannot_both_be_true" if same_q else "both_true"})
            return json.dumps({"results": res}), [], 100
        if "Each numbered line has two statements" in prompt:
            res = []
            for n, a, b in re.findall(r'(\d+)\. A: "(.*?)" \| B: "(.*?)"', prompt):
                wa, wb = set(a.lower().split()), set(b.lower().split())
                label = "same" if len(wa & wb) / len(wa | wb) >= 0.5 else "different"
                res.append({"n": int(n), "label": label})
            return json.dumps({"results": res}), [], 200
        if "Write the story below as ONE news article" in prompt:
            stmts = re.findall(r'#(\d+) ([A-Z][A-Z -]*?) \| "(.*?)"(?: \| said by: ([^|\n]*))?', prompt)
            first, rest = [], []
            for sid, status, text, by in stmts:
                if status == "ESTABLISHED":
                    first.append({"text": text + ".", "ids": [int(sid)]})
                elif by:
                    rest.append({"text": f"{by.strip()} said that {text[0].lower() + text[1:]}.", "ids": [int(sid)]})
                else:
                    rest.append({"text": f"Reportedly, {text[0].lower() + text[1:]}.", "ids": [int(sid)]})
            bad_target = next(int(sid) for sid, status, *_ in stmts if status != "ESTABLISHED")
            rest += [{"text": "The shoddy work was obvious.", "ids": [bad_target]},        # loaded word
                     {"text": "Officials say 5 people were hurt.", "ids": [bad_target]},  # invented number
                     {"text": "Daily Alpha reported the collapse.", "ids": [bad_target]}]  # names an outlet
            return json.dumps({"paragraphs": [first, rest]}), [], 400
        if "Decide, for each LATER statement, whether the EARLIER article already says it" in prompt:
            # new = not in the earlier list word for word; bail and court steps are major
            earlier = set(re.findall(r"^- (.*)$", prompt, re.M))
            res = []
            for n, t in re.findall(r"^(\d+)\. (.*)$", prompt.split("LATER STATEMENTS:")[1], re.M):
                new = t not in earlier
                res.append({"n": int(n), "new": new,
                            "major": "court" if new and re.search(r"(?i)bail|court", t) else "none"})
            return json.dumps({"items": res}), [], 100
        if "each shown by the headlines different outlets gave it" in prompt:
            res = []
            for n, heads in re.findall(r"^(\d+)\. (.*)$", prompt, re.M):
                low = heads.lower()
                res.append({"n": int(n), "score": 4, "filler": "horoscope" in low or "rashifal" in low})
            return json.dumps({"results": res}), [], 60
        if "Rate how important this Indian news story" in prompt:
            head = re.search(r"Headline: (.*)", prompt).group(1).lower()
            filler = "horoscope" in head or "rashifal" in head
            return json.dumps({"score": 4, "filler": filler, "reason": "state-level incident"}), [], 30
        if "A news site has a NEW story and some EARLIER stories" in prompt:
            new = re.search(r"NEW: (.*)", prompt).group(1)
            earlier = re.findall(r"^(\d+)\. (.*)$", prompt, flags=re.M)
            dev = [int(n) for n, t in earlier if "Kesarganj" in new and "Kesarganj" in t]
            return json.dumps({"developments": dev}), [], 30
        if "statements extracted from several news reports about ONE story" in prompt:
            lines = re.findall(r"^(\d+) \| (.*?) \| (.*)$", prompt, flags=re.M)
            speaker = {n: by for n, _, by in lines if by not in ("article", "unnamed source") and "," not in by}
            return json.dumps({"same": [], "conflicts": [], "names": {}, "speaker": speaker}), [], 100
        if "Revise it into the final" in prompt:
            art = prompt.split("Current article")[1].split("Failed sentences:")[0]
            paras, cur = [], None
            for line in art.splitlines():
                if line.startswith("[paragraph"):
                    cur = []
                    paras.append(cur)
                m = re.match(r'\s+"(.*)" \| ids: \[(.*)\]$', line)
                if m and cur is not None:
                    cur.append({"text": m.group(1), "ids": [int(x) for x in m.group(2).split(",") if x.strip()]})
            failed_txt = prompt.split("Failed sentences:")[1].split("Missing statements:")[0]
            paras = [[x for x in p if f'"{x["text"]}"' not in failed_txt] for p in paras]   # a good editor drops them
            miss = prompt.split("Missing statements:")[1].split("All statements:")[0]
            add = []
            for sid, status, text, by in re.findall(r'#(\d+) ([A-Z][A-Z -]*?) \| "(.*?)"(?: \| said by: ([^|\n]*))?', miss):
                if status == "FALSE":
                    add.append({"text": f"Reports that {text[0].lower() + text[1:]} are false, the evidence shows.", "ids": [int(sid)]})
                elif by:
                    add.append({"text": f"{by.strip()} said that {text[0].lower() + text[1:]}.", "ids": [int(sid)]})
                elif status == "DISPUTED":
                    add.append({"text": f"{text}, though this is disputed.", "ids": [int(sid)]})
                elif status == "ESTABLISHED":
                    add.append({"text": text + ".", "ids": [int(sid)]})
                else:
                    add.append({"text": f"Reportedly, {text[0].lower() + text[1:]}.", "ids": [int(sid)]})
            return json.dumps({"paragraphs": [p for p in paras if p] + ([add] if add else [])}), [], 400
        if "These sentences failed the newsroom's" in prompt:
            n = len(re.findall(r"^\d+\. ", prompt.split("Statements:")[0], flags=re.M))
            return json.dumps({"fixes": [{"n": k + 1, "text": "", "ids": []} for k in range(n)]}), [], 50
        if "loaded or emotive word or phrase from a Hindi news report" in prompt:
            items = re.findall(r"^(\d+)\. (.*)$", prompt, flags=re.M)
            return json.dumps({"items": [{"n": int(n), "en": f"concept-{n}"} for n, _ in items]}), [], 50
        if "Write the headline for this news story" in prompt:
            if "ESTABLISHED:" not in prompt:   # nothing settled yet: a good editor hedges
                return json.dumps({"headline": "Section of Kesarganj flyover reportedly collapses, engineer arrested"}), [], 50
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
        if "attribute statements to" in prompt:
            known = {"police": {"name": "Kesarganj Police", "kind": "police", "government": "Uttar Pradesh"},
                     "PWD minister": {"name": "PWD Minister", "kind": "government", "government": "Uttar Pradesh"},
                     "residents": {"name": "residents", "kind": "witness", "government": None}}
            items = re.findall(r"^(\d+)\. (.*)$", prompt, flags=re.M)
            return json.dumps({"items": [dict(n=int(n), **known.get(r, {"name": r, "kind": "other", "government": None}))
                                         for n, r in items]}), [], 100
        if "decide its type" in prompt:
            items = re.findall(r'^(\d+)\. "(.*)"$', prompt, flags=re.M)
            return json.dumps({"items": [{"n": int(n), "type": "characterisation" if re.search(
                r"conspiracy|negligen|blame", t, re.I) else "fact"} for n, t in items]}), [], 100
        if "SAME specific event" in prompt:
            res = []
            for n, a, b in re.findall(r'(\d+)\. N: "(.*?)" \| S: "(.*?)"', prompt):
                topic = lambda x: "k" if ("Kesarganj" in x or "केसरगंज" in x) else ("a" if "ssembly" in x else x)
                res.append({"n": int(n), "same": topic(a) == topic(b)})
            return json.dumps({"results": res}), [], 100
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    def embed(self, model, texts):
        # stand-in for a multilingual embedding: same event -> same direction, any language
        import hashlib
        out = []
        for t in texts:
            if "Kesarganj" in t or "केसरगंज" in t:
                base = [1.0, 0.05, 0.0]
            elif "assembly" in t.lower() or "Lucknow" in t:
                base = [0.0, 1.0, 0.05]
            else:
                base = [0.05, 0.0, 1.0]
            # like a real model: similar texts get similar, never identical, vectors
            h = hashlib.md5(t.encode()).digest()
            out.append([b + (h[i] / 255 - 0.5) * 0.02 for i, b in enumerate(base)])
        return out
