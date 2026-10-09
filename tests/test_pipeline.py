import dataclasses
import datetime as dt
import json
import re

import numpy as np
import pytest

from nishpaksh.db import Store, articles, canonical, claims, delete, insert, published, select, source_clusters, stories, story_pairs, update
from nishpaksh.router import ModelSlot, Router, parse_json
from nishpaksh.timeline import build_timeline
from nishpaksh.wire import independence_groups, jaccard, minhash

from .fixtures import ARTICLES, NOW, FakeBackend


VB = {"grounded": 5, "judge": 5}  # fixed so tests do not depend on the hour


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 't.db'}")
    s.init()
    return s


# ---------------------------------------------------------------- unit tests

def test_minhash_detects_copies_not_different_text():
    a = "The flyover collapsed on Tuesday night after a section gave way near the market. " * 5
    b = a.replace("Tuesday", "Tuesday,")
    c = "The assembly session opened in Lucknow with a debate on the state budget and farm prices. " * 5
    assert jaccard(minhash(a), minhash(b)) > 0.6
    assert jaccard(minhash(a), minhash(c)) < 0.1


def test_independence_groups_union_by_wire_agency_outlet():
    arts = [dict(id=1, wire_group=1, agency="PTI", outlet="X"), dict(id=2, wire_group=2, agency="PTI", outlet="Y"),
            dict(id=3, wire_group=3, agency=None, outlet="X"), dict(id=4, wire_group=4, agency=None, outlet="Z")]
    g = independence_groups(arts)
    assert g[1] == g[2] == g[3]  # PTI copy + same outlet
    assert g[4] != g[1]


def test_parse_json_tolerates_fences_and_trailing_commas():
    assert parse_json('```json\n{"a": [1, 2,],}\n```') == {"a": [1, 2]}
    assert parse_json("noise {\"k\": 1} noise") == {"k": 1}
    assert parse_json("nothing") is None


def test_timeline_partial_order():
    ev = [{"id": 1, "start": "2026-10-01T14:00", "end": "2026-10-01T17:00", "weight": 2},   # rain, afternoon
          {"id": 2, "start": "2026-10-01T21:00", "end": "2026-10-01T21:00", "weight": 4},   # collapse
          {"id": 3, "start": "2026-10-01T20:00", "end": "2026-10-01T23:59", "weight": 1},   # overlaps collapse
          {"id": 4, "start": None, "end": None, "weight": 1}]                               # undated
    tl = build_timeline(ev, [])
    assert tl["tiers"][0] == [1]
    assert set(tl["tiers"][1]) == {2, 3}     # order between 2 and 3 not established
    assert tl["tiers"][1][0] == 2            # more sources first inside a tier
    assert tl["undated"] == [4]


def test_timeline_breaks_cycles_from_relations_only():
    ev = [{"id": 1, "start": "2026-10-01T10:00", "end": "2026-10-01T10:00"},
          {"id": 2, "start": "2026-10-01T12:00", "end": "2026-10-01T12:00"}]
    tl = build_timeline(ev, [(2, 1)])  # a stated relation contradicting the clock
    assert tl["tiers"] == [[1], [2]]
    assert tl["dropped_edges"] == [[2, 1]]


def test_router_falls_back_when_model_exhausted():
    class B:
        def __init__(self): self.models = []
        def list_models(self): return ["m1", "m2"]
        def generate(self, model, prompt, json_mode, grounded):
            self.models.append(model)
            if model == "m1":
                raise RuntimeError("429 RESOURCE_EXHAUSTED: GenerateRequestsPerDay limit")
            return '{"ok": true}', [], 10
    b = B()
    r = Router({"t": [dict(id="m1", rpm=10, tpm=10000, rpd=5), dict(id="m2", rpm=10, tpm=10000, rpd=5)]}, b)
    assert r.call("t", "hi").data == {"ok": True}
    assert r.tiers["t"][0].used_today == 5            # marked exhausted for the day
    assert r.call("t", "hi").model == "m2"
    assert b.models.count("m1") == 1


def test_router_resolves_close_ids_but_not_other_product_lines():
    class B:
        def list_models(self): return ["gemma-4-26b-a4b-it", "gemini-3.5-flash-lite", "gemini-3-flash-preview"]
    r = Router({"t": [dict(id="gemma-4-26b-it", rpm=1, tpm=1, rpd=1), dict(id="gemini-3.5-flash", rpm=1, tpm=1, rpd=1),
                      dict(id="gemini-3-flash", rpm=1, tpm=1, rpd=1)]}, B())
    r.resolve()
    ids = [(s.id, s.disabled) for s in r.tiers["t"]]
    assert ids[0] == ("gemma-4-26b-a4b-it", False)
    assert ids[1][1] is True                      # flash must not silently become flash-lite
    assert ids[2] == ("gemini-3-flash-preview", False)


def test_slot_waits_for_token_window():
    s = ModelSlot(id="g", rpm=30, tpm=16000, rpd=100)
    now = 1000.0
    s.window = [(now - 50, 9000), (now - 10, 6000)]
    # 90% of the 16,000 token limit (stay just under it): the oldest call must age out, plus 1 s
    assert s.wait_time(4000, now) == pytest.approx(11.0, abs=0.01)
    assert s.wait_time(20000, now) is None                           # can never fit


def test_global_clusters_emerge_from_roll_call(store, monkeypatch):
    import dataclasses
    import nishpaksh.perspectives as P
    from nishpaksh.perspectives import recompute_global
    # the clustering machinery itself; the positions-test gate is covered separately
    monkeypatch.setattr(P, "SETTINGS", dataclasses.replace(P.SETTINGS, require_positions_signal=False))
    camp1, camp2 = ["O1", "O2", "O3"], ["O4", "O5", "O6"]
    rows = []
    for sid in range(1, 6):
        for i, a in enumerate(camp1 + camp2):
            for b in (camp1 + camp2)[i + 1:]:
                same = (a in camp1) == (b in camp1)
                rows.append(dict(story_id=sid, a=a, b=b, value=0.9 if same else -0.6))
    with store.engine.begin() as c:
        c.execute(insert(story_pairs), rows)
    assert recompute_global(store) == 2
    cl = {r["source"]: r["cluster"] for r in store.rows(select(source_clusters))}
    assert len({cl[s] for s in camp1}) == 1 and len({cl[s] for s in camp2}) == 1
    assert cl["O1"] != cl["O4"]
    before = dict(cl)
    recompute_global(store)                       # letters stay stable across runs
    assert {r["source"]: r["cluster"] for r in store.rows(select(source_clusters))} == before


# ---------------------------------------------------------------- end to end

def _age_rules(store, hours=7):
    """Pretend the established rule was first met `hours` ago (the 6-hour standing time)."""
    for c in store.rows(select(canonical.c.id, canonical.c.origins)):
        o = dict(c["origins"] or {})
        if o.get("met_at"):
            o["met_at"] = (dt.datetime.fromisoformat(o["met_at"]) - dt.timedelta(hours=hours)).isoformat(timespec="minutes")
            store.exec(update(canonical).where(canonical.c.id == c["id"]).values(origins=o))


def _run_twice(store, backend=None, **kw):
    """One run, six hours pass, another run: what a reader sees once statements have stood."""
    from nishpaksh.run import run
    backend = backend or FakeBackend()
    run(store=store, backend=backend, time_budget_min=30, ingest_news=False, verify_budget=VB, **kw)
    _age_rules(store)
    return run(store=store, backend=backend, time_budget_min=30, ingest_news=False, verify_budget=VB, **kw)


def _seed(store):
    for i, a in enumerate(ARTICLES):
        store.exec(insert(articles).values(
            url=f"https://outlet{abs(hash(a['outlet'])) % 10 ** 8}.in/{i}", feed_id=None, outlet=a["outlet"], lang=a["lang"], role="news",
            title=a["title"], author=a["author"], published_at=NOW - dt.timedelta(hours=2 + i), fetched_at=NOW,
            text=a["text"], text_source="full", agency="PTI" if a["author"] == "PTI" else None,
            minhash=minhash(a["text"]), extract_failures=0))


def test_end_to_end(store):
    from nishpaksh.run import run
    _seed(store)
    backend = FakeBackend()
    stats = run(store=store, backend=backend, time_budget_min=30, ingest_news=False, verify_budget=VB)
    # the second PTI copy is the same source as the first, so it is never sent to a model; the
    # assembly story has only two independent sources, so it is not read at all (read in depth: a
    # story is read once 3+ independent sources have a readable page)
    assert stats["extracted"] == 4

    sts = store.rows(select(stories))
    assert len(sts) == 2                                         # Hindi + English grouped together
    contested = next(s for s in sts if "flyover" in s["signature"])
    consensus = next(s for s in sts if s["id"] != contested["id"])     # unread: two sources only
    assert contested["qualifies"] and contested["analysis"]["mode"] == "story"
    assert not consensus["qualifies"]                            # one perspective only: not published

    # first run: the collapse meets the rule but has not stood 6 hours yet
    pub = store.one(select(published).where(published.c.story_id == contested["id"]))
    first = next(i for tier in pub["payload_en"]["timeline"] for i in tier) if pub["payload_en"]["timeline"] else None
    cont_first = {i["text"]: i for i in pub["payload_en"]["contested"]}
    assert first is None and cont_first["A section of the Kesarganj flyover collapsed"]["verdict"] == "developing"
    # six hours later, with nothing new: the article is closed (written once), only its colours
    # mature by code: the collapse is now established, in the same place on the page
    _age_rules(store)
    run(store=store, backend=backend, time_budget_min=30, ingest_news=False, verify_budget=VB)
    pub = store.one(select(published).where(published.c.story_id == contested["id"]))
    assert pub["version"] == 1
    en, hi = pub["payload_en"], pub["payload_hi"]
    # 5 articles; the two PTI copies count once -> 4 independent sources
    assert en["counts"] == {"articles": 5, "independent_sources": 4, "outlets": 5}

    cont = {i["text"]: i for i in en["contested"]}
    collapse = cont["A section of the Kesarganj flyover collapsed"]
    assert collapse["verdict"] == "corroborated" and collapse["n_origins"] >= 2 and collapse["n_outlets"] >= 3
    assert {i["text"]: i["verdict"] for i in hi["contested"]}[f"[हिं] {collapse['text']}"] == "corroborated"
    said = [x for para in en["narrative"]["paragraphs"] for x in para if collapse["id"] in x["ids"]]
    assert said and all(x["class"] in ("established", "disputed", "false") for x in said)
    # four outlets report the arrest, but every one of them got it from the police: one origin
    # (the police and the PWD minister speak for the same state government), so not established
    arrest = cont["Police arrested the site engineer"]
    assert arrest["verdict"] == "unverified" and arrest["origins"] == ["gov:uttar pradesh"]
    assert not any("rain" in i["text"].lower() for tier in en["timeline"] for i in tier)

    sub = cont["The contractor used substandard material"]
    assert sub["verdict"] == "false"                             # both models, primary evidence
    assert sub["check"]["evidence_urls"] == ["https://example.org/order"]
    rain = next(i for t, i in cont.items() if "rain" in t.lower() and i["kind"] == "event")
    assert rain["verdict"] == "unverified"
    rel = next(i for i in en["contested"] if i["kind"] == "relation")
    assert rel["text"] == ("A section of the Kesarganj flyover collapsed because heavy rain fell in Kesarganj."
                           ) and rel["verdict"] == "unverified"

    framing = {f["text"]: f["words"] for f in en["framing"]}
    words = framing["The contractor used substandard material"]
    assert any("shoddy" in w for ws in words.values() for w in ws)
    assert any("साज़िश" in w for ws in words.values() for w in ws)

    assert en["headline"].startswith("Section of Kesarganj")
    assert hi["headline"].startswith("[हिं]") and hi["translation_complete"]
    # the site's sections (owner, Oct 9 2026): the model's picks checked by code (an unknown key dropped),
    # the same keys on the Hindi page
    assert en["category"] == {"primary": ["life", "justice"], "secondary": ["accidents", "police"]}
    assert hi["category"] == en["category"]

    # another run with nothing new: nothing is reprocessed or rewritten
    text = [x["text"] for para in en["narrative"]["paragraphs"] for x in para]
    run(store=store, backend=backend, time_budget_min=30, ingest_news=False, verify_budget=VB)
    again = store.one(select(published).where(published.c.story_id == contested["id"]))
    assert again["version"] == 1 and again["updated_at"] == pub["updated_at"]
    assert [x["text"] for para in again["payload_en"]["narrative"]["paragraphs"] for x in para] == text

    # a Hindi page published half-translated (story 15429: page models overloaded mid-translation) is
    # finished on a later run; the English article is untouched
    from nishpaksh.compose import finish_translations
    from nishpaksh.config import load_yaml
    half = dict(again["payload_hi"], translation_complete=False)
    half["narrative"] = json.loads(json.dumps(again["payload_en"]["narrative"]))
    store.exec(update(published).where(published.c.story_id == contested["id"]).values(payload_hi=half))
    router = Router(load_yaml("models.yaml")["tiers"], backend, store)
    assert finish_translations(store, router) == [contested["id"]]
    done = store.one(select(published).where(published.c.story_id == contested["id"]))
    assert done["payload_hi"]["translation_complete"] and done["payload_en"] == again["payload_en"]
    assert all(x["text"].startswith("[हिं]") for para in done["payload_hi"]["narrative"]["paragraphs"] for x in para)
    assert finish_translations(store, router) == []          # nothing left to finish


def test_headline_with_loaded_word_is_rejected(store):
    from nishpaksh.run import run

    class Loaded(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            if "Write THREE different headlines" in prompt:
                return '{"headlines": ["Shoddy flyover collapses in Kesarganj"]}', [], 10
            return super().generate(model, prompt, json_mode, grounded)
    _seed(store)
    run(store=store, backend=Loaded(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    # the loaded headline is rejected (twice); with no headline the new story waits
    assert store.rows(select(published)) == []
    # it stays qualified and is offered to the writer again in a later run, without re-analysis
    assert store.rows(select(stories.c.id).where(stories.c.qualifies.is_(True)))


def test_quota_exhaustion_degrades_safely(store):
    """With no judge quota, nothing is ever marked false."""
    from nishpaksh.run import run

    class NoJudge(FakeBackend):
        def list_models(self):
            return [m for m in super().list_models() if "3.8" not in m and "3.7" not in m]
    _seed(store)
    run(store=store, backend=NoJudge(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    verdicts = {r["verdict"] for r in store.rows(select(canonical))}
    assert "false" not in verdicts and "confirmed" not in verdicts


def test_db_url_pins_psycopg2():
    from nishpaksh.config import normalize_db_url
    assert normalize_db_url("postgresql://u:p@h:5432/d") == "postgresql+psycopg2://u:p@h:5432/d"
    assert normalize_db_url("postgres://u:p@h/d") == "postgresql+psycopg2://u:p@h/d"
    assert normalize_db_url("postgresql+psycopg2://u:p@h/d") == "postgresql+psycopg2://u:p@h/d"
    assert normalize_db_url("sqlite:///x.db") == "sqlite:///x.db"


def test_router_respects_limits_under_parallel_calls():
    import threading
    import time as _t

    class Slow:
        def __init__(self):
            self.starts = []
            self.lock = threading.Lock()
        def list_models(self): return ["m"]
        def generate(self, model, prompt, json_mode, grounded):
            with self.lock:
                self.starts.append(_t.time())
            _t.sleep(0.05)
            return '{"ok": 1}', [], 5
    b = Slow()
    r = Router({"t": [dict(id="m", rpm=4, tpm=10**6, rpd=100)]}, b, max_wait=0.5)
    results = []

    def go():
        try:
            results.append(r.call("t", "x").data)
        except Exception as e:  # noqa: BLE001
            results.append(type(e).__name__)
    th = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert len(b.starts) == 3                 # one under the rpm limit of 4, never more in the minute
    assert results.count({"ok": 1}) == 3 and results.count("QuotaExhausted") == 5


def test_retention_keeps_recent_and_drops_old(store):
    from nishpaksh.retention import enforce
    old = NOW - dt.timedelta(days=10)
    mid = NOW - dt.timedelta(days=5)
    rows = [
        dict(url="u/old-unread", published_at=old, extracted_at=None, text="t", minhash=[1], embedding=[0.1]),
        dict(url="u/old-read", published_at=old, extracted_at=old, text="t", minhash=[1], embedding=[0.1]),
        dict(url="u/mid-read", published_at=mid, extracted_at=mid, text="t", minhash=[1], embedding=[0.1]),
        dict(url="u/new", published_at=NOW, extracted_at=None, text="t", minhash=[1], embedding=[0.1]),
    ]
    for r in rows:
        store.exec(insert(articles).values(outlet="X", lang="en", extract_failures=0, **r))
    stats = enforce(store)
    left = {r["url"]: r for r in store.rows(select(articles))}
    assert "u/old-unread" not in left                       # never read, past 7 days: gone
    assert left["u/old-read"]["text"] == "t"                # read 10 days ago: text kept until day 14
    assert left["u/old-read"]["minhash"] is None            # but vectors dropped after 4 days
    assert left["u/mid-read"]["embedding"] is None and left["u/mid-read"]["text"] == "t"
    assert left["u/new"]["embedding"] == [0.1]              # inside the grouping window
    assert stats["unread_deleted"] == 1


def test_archive_has_everything_but_article_bodies(store, tmp_path):
    import gzip
    import json

    from nishpaksh.archive import export_day
    from nishpaksh.run import run
    _seed(store)
    run(store=store, backend=FakeBackend(), ingest_news=False, verify_budget=VB)
    lines = []
    for d in {(NOW - dt.timedelta(hours=h)).date() for h in range(0, 12)}:
        with gzip.open(export_day(store, d, tmp_path), "rt", encoding="utf-8") as f:
            lines += [json.loads(x) for x in f]
    kinds = {x["type"] for x in lines}
    assert {"article", "story", "claim", "canonical", "published", "source_clusters"} <= kinds
    arts = [x for x in lines if x["type"] == "article"]
    assert len(arts) == len(ARTICLES)
    assert all("text" not in a and "minhash" not in a and "embedding" not in a for a in arts)
    assert any(x["type"] == "published" and x["payload_hi"] for x in lines)


def test_two_sources_disagreeing_is_not_two_perspectives(store):
    """A discrepancy between two outlets must not be published as a perspective split."""
    from nishpaksh.run import run
    _seed(store)
    # keep only two of the contested story's outlets: Alpha Times and Beta News
    keep = {"Alpha Times", "Beta News", "Daily Alpha"}
    from nishpaksh.db import delete as _del
    store.exec(_del(articles).where(articles.c.outlet.not_in(keep)))
    store.exec(_del(articles).where(articles.c.title.like("%monsoon%") | articles.c.title.like("%Assembly%")))
    store.exec(_del(articles).where(articles.c.outlet == "Daily Alpha"))
    run(store=store, backend=FakeBackend(), ingest_news=False, verify_budget=VB)
    assert store.rows(select(published)) == []


def test_narrative_is_checked_and_coloured(store):
    _seed(store)
    _run_twice(store)
    p = store.rows(select(published))[0]
    nar = p["payload_en"]["narrative"]
    paras = nar["paragraphs"]
    sents = [x for para in paras for x in para]
    text = " ".join(x["text"] for x in sents)
    assert "shoddy" not in text.lower() and "5 people" not in text     # bad sentences rejected
    assert "Daily Alpha" not in text and "Beta News" not in text        # outlets are never named in the text
    # rejected: loaded word, invented number, an outlet named, and the red sentence that never said "false"
    # the first draft had 4 bad sentences; the revision pass fixed them (the page may since have been
    # recoloured, which keeps the revised essay)
    assert nar["rejected"] <= 1
    # an allegation with a known speaker names the speaker; nothing unconfirmed reads as plain fact
    for para in paras:
        if any(x["class"] in ("unverified", "developing") for x in para):
            assert any(w in " ".join(x["text"].lower() for x in para)
                       for w in ("reportedly", "reports said", "said", "alleg", "according to"))
    # what is settled is in the essay (the page written in the first run is recoloured, not rewritten)
    assert any(x["class"] == "established" for x in sents)
    # the red statement's sentence never said "false": rejected, so the statement is listed under the
    # essay in plain words (rejected sentences are dropped, never patched into the prose)
    # the red statement's first sentence never said "false": the revision pass wrote it again,
    # properly, so it is in the essay (everything belongs in the article; nothing listed under it)
    also = nar["not_in_essay"]
    false_s = [x for x in sents if x["class"] == "false"]
    assert false_s and "substandard" in false_s[0]["text"] and "false" in false_s[0]["text"]
    assert all(x["sources"] for x in sents + also)                       # every sentence cites sources
    sents = sents + also
    # every statement in the story appears somewhere: in the essay or listed under it
    payload = p["payload_en"]
    all_ids = {i["id"] for i in payload["contested"] + payload["established"] + payload["undated"]}
    all_ids |= {i["id"] for tier in payload["timeline"] for i in tier}
    relations = {i["id"] for i in payload["contested"] + payload["established"] if i["kind"] == "relation"}
    assert all_ids - relations <= {x for s in sents for x in s["ids"]}    # links between events order them, not repeated
    assert not relations & {x for s in sents for x in s["ids"]}
    assert [s["n"] for s in nar["sources"]] == list(range(1, len(nar["sources"]) + 1))
    assert p["payload_hi"]["narrative"]["paragraphs"][0][0]["text"].startswith("[हिं]")

def test_gate_spaces_runs(store):
    from nishpaksh.db import runs
    from nishpaksh.gate import should_run
    assert should_run(store, 50)[0] is True                          # nothing has run yet
    store.exec(insert(runs).values(started_at=NOW - dt.timedelta(minutes=20)))
    assert should_run(store, 50, now=NOW)[0] is False                # too soon
    assert should_run(store, 50, now=NOW + dt.timedelta(minutes=31))[0] is True


def test_story_stage_respects_deadline_and_isolates_failures():
    import time as _t

    from nishpaksh.run import _parallel
    assert _parallel([1, 2, 3], lambda x: None, _t.time() - 1, 4) == []     # past deadline: nothing starts

    def flaky(x):
        if x == 2:
            raise RuntimeError("boom")
    assert sorted(_parallel([1, 2, 3], flaky, _t.time() + 5, 2)) == [1, 3]  # one failure does not stop others


def test_duplicate_embeddings_are_rejected_not_copied():
    """The bug that put a farming article into a protest story: one vector for a whole batch."""
    from nishpaksh.router import embeddings_look_valid
    assert embeddings_look_valid(["a", "b"], [[1.0, 0.0], [0.0, 1.0]])
    assert not embeddings_look_valid(["a", "b"], [[1.0, 0.0]])                 # one vector for two texts
    assert not embeddings_look_valid(["a", "b"], [[1.0, 0.0], [1.0, 0.0]])     # copied vector
    assert embeddings_look_valid(["same", "same"], [[1.0, 0.0], [1.0, 0.0]])   # identical text is fine

    class OneVector:
        def list_models(self): return ["e"]
        def embed(self, model, texts): return [[0.5, 0.5]] * len(texts)
    r = Router({"embed": [dict(id="e", rpm=100, tpm=10**6, rpd=100)]}, OneVector())
    assert r.embed(["farming tips", "protest detention"]) is None


def test_copied_vectors_are_healed(store):
    from nishpaksh.db import stories
    from nishpaksh.stories import _heal_copied_vectors
    sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, signature="x", dirty=False))
    same = [0.1] * 256
    for i, title in enumerate(["Farming tips", "Protest detention", "Protest detention"]):
        # u1 had been read: its statements leave the story, so the story must be re-matched
        store.exec(insert(articles).values(url=f"u{i}", outlet="X", lang="en", title=title, text="t",
                                           published_at=NOW, extract_failures=0, embedding=same, story_id=sid,
                                           extracted_at=NOW if i == 1 else None))
    store.exec(insert(articles).values(url="u9", outlet="Y", lang="en", title="Other", text="t",
                                       published_at=NOW, extract_failures=0, embedding=[0.2] * 256, story_id=sid))
    from sqlalchemy import JSON, null
    store.exec(insert(articles).values(url="u10", outlet="Z", lang="en", title="Cleared", text="t",  # JSON null
                                       published_at=NOW, extract_failures=0, embedding=JSON.NULL))
    assert _heal_copied_vectors(store, NOW - dt.timedelta(days=1)) == 3
    left = {r["url"]: r for r in store.rows(select(articles))}
    assert left["u0"]["embedding"] is None and left["u0"]["story_id"] is None
    assert left["u9"]["embedding"] == [0.2] * 256                         # untouched
    assert store.one(select(stories).where(stories.c.id == sid))["dirty"] is True


def test_two_keys_have_separate_quotas_and_usage_records(store):
    """Each key is a separate project: exhausting a model on key 1 must move calls to key 2, and
    usage must be stored separately so the next run does not think key 2 is also spent."""
    class B:
        def __init__(self, name, fail=False):
            self.name, self.fail, self.calls = name, fail, 0
        def list_models(self): return ["m1"]
        def generate(self, model, prompt, json_mode, grounded):
            self.calls += 1
            if self.fail:
                raise RuntimeError("429 RESOURCE_EXHAUSTED: GenerateRequestsPerDay")
            return '{"key": "%s"}' % self.name, [], 5
    k1, k2 = B("one", fail=True), B("two")
    r = Router({"t": [dict(id="m1", rpm=10, tpm=10000, rpd=3)]}, [k1, k2], store)
    r.resolve()
    assert r.remaining_today("t") == 6                      # two projects, two quotas
    assert r.call("t", "hi").data == {"key": "two"}
    assert k1.calls == 1                                    # marked spent after one 429, not retried
    usage = store.quota_load(r.day)
    assert usage["m1"][0] == 3 and usage["m1@k2"][0] == 1   # stored per key
    r2 = Router({"t": [dict(id="m1", rpm=10, tpm=10000, rpd=3)]}, [B("one"), B("two")], store)
    assert [s.used_today for s in r2.tiers["t"]] == [3, 1]  # reloaded per key


def test_duplicate_keys_are_counted_once(monkeypatch):
    from nishpaksh.config import gemini_api_keys
    monkeypatch.setenv("GEMINI_API_KEY", "abc")
    monkeypatch.setenv("GEMINI_API_KEY_2", " abc ")
    assert gemini_api_keys() == ["abc"]
    monkeypatch.setenv("GEMINI_API_KEY_2", "xyz")
    assert gemini_api_keys() == ["abc", "xyz"]


def test_tavily_budget_never_overspends_and_rolls_forward(store):
    from nishpaksh.tavily import Tavily
    import datetime as _dt

    class Resp:
        def __init__(self, data): self.status_code, self._d, self.text = 200, data, ""
        def json(self): return self._d

    class Http:
        def __init__(self): self.calls = []
        def post(self, url, json, timeout, headers):
            self.calls.append((url, json))
            if url.endswith("extract"):
                # half the pages fail, as blocked sites do
                return Resp({"results": [{"url": u, "raw_content": "text " * 50} for u in json["urls"][::2]],
                             "failed_results": [{"url": u} for u in json["urls"][1::2]]})
            return Resp({"results": [{"url": "https://x.in/a", "title": "t"}]})

    http = Http()
    t = Tavily(store, key="k", daily_cap=3, session=http)
    fixed = _dt.date(2026, 10, 30)  # 2 days left in the month
    t._today = lambda: fixed
    assert t.allowance_today() == 3                      # capped per day
    got = t.extract([f"https://site.in/{i}" for i in range(10)])
    assert len(got) == 5
    assert t._used(fixed)[0] == 1                        # booked 2, refunded 1: 5 pages read = 1 credit
    assert t.search("q") and t.search("q")
    assert t._used(fixed)[0] == 3
    assert t.search("q") == [] and len(http.calls) == 3  # out of today's credits: no call made
    # most of the month already spent: allowance shrinks so the month cannot overrun
    store.quota_save("tavily", "2026-10-15", 940, 0)
    t2 = Tavily(store, key="k", daily_cap=40, session=http)
    t2._today = lambda: _dt.date(2026, 10, 31)
    assert t2.allowance_today() == 7                     # 1000 - 50 safety - 943 used = 7 left
    assert Tavily(store, key="", session=http).extract(["u"]) == {}


# ---------------------------------------------------------------- grouping: real failure modes

def _vec(angle_deg: float, plane=(0, 1), jitter: int = 0) -> list[float]:
    """Unit vector in 256 dims at an angle within a plane, plus a tiny unique component so no two
    articles share an identical vector (as with a real embedding model)."""
    import math
    v = [0.0] * 256
    a = math.radians(angle_deg)
    v[plane[0]], v[plane[1]] = math.cos(a), math.sin(a)
    v[10 + jitter % 200] += 0.01
    return v


def _put(store, title, vec, hours_ago=1.0, story_id=None, extracted=False, lang="en", outlet=None):
    from nishpaksh.db import articles as A, insert as ins
    return store.insert_returning_id(A, dict(
        url=f"u/{title}", outlet=outlet or f"Outlet {title}", lang=lang, role="news", title=title,
        published_at=NOW - dt.timedelta(hours=hours_ago), fetched_at=NOW, text=title + " text " * 50,
        text_source="full", minhash=[1], embedding=vec, embed_model="gemini-embedding-2", story_id=story_id,
        extracted_at=NOW if extracted else None, extract_failures=0))


class _NoLLM(FakeBackend):
    """Answers "different" to every same-event question unless the titles share an event tag."""
    def generate(self, model, prompt, json_mode, grounded):
        if "SAME specific event" in prompt:
            import json as _j, re as _re
            res = []
            for n, a, b in _re.findall(r'(\d+)\. A: "(.*?)" \| B: "(.*?)"', prompt):
                ta = _re.search(r"\[(\w+)\]", a)
                tb = _re.search(r"\[(\w+)\]", b)
                res.append({"n": int(n), "answer": "same" if ta and tb and ta.group(1) == tb.group(1) else "different"})
            return _j.dumps({"results": res}), [], 50
        return super().generate(model, prompt, json_mode, grounded)


def _router(store, backend=None):
    from nishpaksh.config import load_yaml
    r = Router(load_yaml("models.yaml")["tiers"], backend or _NoLLM(), store)
    r.resolve()
    return r


def test_same_topic_different_events_stay_apart(store):
    """Two protests in the same row (cosine 0.83 apart: same topic, different events) must not
    merge just because their vectors are close; the model is asked and says no."""
    from nishpaksh.stories import group_stories
    for k in range(4):
        _put(store, f"[A] protest in Mumbai {k}", _vec(0 + k * 0.5, jitter=k), hours_ago=5 - k)
    for k in range(4):
        _put(store, f"[B] protest in Chennai {k}", _vec(34 + k * 0.5, jitter=10 + k), hours_ago=4 - k)
    group_stories(store, _router(store))
    sids = {r["title"][:3]: set() for r in store.rows(select(articles.c.title))}
    for r in store.rows(select(articles.c.title, articles.c.story_id)):
        sids[r["title"][:3]].add(r["story_id"])
    assert len(sids["[A]"]) == 1 and len(sids["[B]"]) == 1
    assert sids["[A]"] != sids["[B]"]


def test_earlier_event_is_not_asked_into_a_later_story(store):
    """Story 13968 (Oct 8 2026): a Ludhiana sarpanch killing reported Oct 4-5 sat as a lone unread
    article; on Oct 6 a Tarn Taran sarpanch killing came in at cosine 0.80 and one model question on two
    headlines joined them. A borderline article published 12 h+ before a story's first report is not
    asked in, and the question is asked twice (A/B swapped): one "same" is not enough."""
    from nishpaksh.stories import group_stories
    mk = lambda: store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, signature="s",
                                                          dirty=False, qualifies=False))
    old = _put(store, "[L] former sarpanch shot dead", _vec(30, jitter=1), hours_ago=40, story_id=mk())
    patti = mk()
    for k in range(3):
        _put(store, f"[L] AAP sarpanch shot dead {k}", _vec(0 + k * 0.5, jitter=5 + k), hours_ago=10 - k,
             story_id=patti)
    group_stories(store, _router(store))
    rows = {r["id"]: r["story_id"] for r in store.rows(select(articles.c.id, articles.c.story_id))}
    assert sum(1 for v in rows.values() if v == rows[old]) == 1


def test_same_event_needs_two_same_answers():
    from nishpaksh.stories import _same_event

    class Flip:
        def __init__(self):
            self.n = 0
        def call(self, tier, prompt, **kw):
            self.n += 1
            class R:
                data = {"results": [{"n": 1, "answer": "same" if self.n == 1 else "different"}]}
            return R()
    a = {"title": "A", "text": "x", "published_at": NOW}
    r = Flip()
    assert _same_event(r, [(a, a)]) == [False] and r.n == 2


def test_story_cannot_drift_by_chaining(store):
    """Each article is 6 degrees from the previous one (cosine 0.995 to its neighbour), but the
    chain walks 60 degrees away from where the story started. The old centroid rule absorbed the
    whole chain; the core check must stop it."""
    from nishpaksh.stories import group_stories
    for k in range(11):
        _put(store, f"[C{k}] chain step {k}", _vec(k * 6, jitter=k), hours_ago=12 - k)
    group_stories(store, _router(store))
    by_story = {}
    for r in store.rows(select(articles.c.title, articles.c.story_id)):
        by_story.setdefault(r["story_id"], []).append(r["title"])
    assert max(len(v) for v in by_story.values()) < 11
    first = next(sid for sid, v in by_story.items() if "[C0] chain step 0" in v)
    assert "[C10] chain step 10" not in by_story[first]       # 60 degrees from the start


def test_merged_story_is_split_and_reanalysed(store):
    """A story that already holds two separate events (as story 2211 did) is split; read articles
    keep their claims, which move with them and are re-matched."""
    from nishpaksh.db import claims as Cl, insert as ins
    from nishpaksh.stories import group_stories
    sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, signature="mixed",
                                                 dirty=False, qualifies=True))
    a_ids = [_put(store, f"[G] GST collections {k}", _vec(0, jitter=k), story_id=sid, extracted=True) for k in range(3)]
    b_ids = [_put(store, f"[T] temple priest {k}", _vec(70, jitter=20 + k), story_id=sid, extracted=True) for k in range(3)]
    cid = store.insert_returning_id(canonical, dict(story_id=sid, kind="claim", text="x", conflicts=[], verdict="corroborated"))
    for aid in a_ids + b_ids:
        store.exec(ins(Cl).values(story_id=sid, article_id=aid, kind="claim", text="x", stance="asserts",
                                  attributed_to="article", evidence="none", canonical_id=cid))
    group_stories(store, _router(store))
    rows = {r["id"]: r["story_id"] for r in store.rows(select(articles.c.id, articles.c.story_id))}
    assert len({rows[i] for i in a_ids}) == 1 and len({rows[i] for i in b_ids}) == 1
    assert {rows[i] for i in a_ids} != {rows[i] for i in b_ids}
    # the old mixed statements are gone; every claim is re-matched inside its new story
    assert store.rows(select(canonical).where(canonical.c.id == cid)) == []
    for r in store.rows(select(Cl)):
        assert r["canonical_id"] is None and r["story_id"] == rows[r["article_id"]]
    assert all(s["dirty"] for s in store.rows(select(stories)))


def test_no_embedding_quota_means_waiting_not_word_matching(store):
    """With the embedding quota spent, new articles wait for the next run; they are never grouped
    by word overlap (which cannot match Hindi with English and built the giant stories)."""
    from nishpaksh.stories import group_stories
    for k in range(3):
        _put(store, f"flyover collapse {k}", None, hours_ago=2)
    r = _router(store)
    for s in r.tiers["embed"]:
        s.used_today = s.rpd
    assert group_stories(store, r) == 0
    assert all(x["story_id"] is None for x in store.rows(select(articles.c.story_id)))


def test_embedding_never_falls_back_to_one_request_per_text(store):
    class Dup(FakeBackend):
        def __init__(self):
            super().__init__()
            self.embed_calls = 0
        def embed(self, model, texts):
            self.embed_calls += 1
            return [[0.1] * 256 for _ in texts]          # every text the same vector: invalid
    b = Dup()
    r = Router({"embed": [dict(id="gemini-embedding-2", rpm=100, tpm=10 ** 6, rpd=1000)]}, b, store)
    assert r.embed([f"text {i}" for i in range(50)], max_requests=10) is None
    assert b.embed_calls <= 10                            # not 50


def test_search_adds_only_new_owners_and_never_reads_headlines(store, monkeypatch):
    """Search finds coverage; it adds one piece per owner we do not have, skips aggregators that
    repost others, and an unreadable page enters as coverage only (never read for facts)."""
    from nishpaksh import discover
    sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, dirty=False, qualifies=False,
                                                 signature="Police fired at protesters in Imphal on Friday"))
    for k, outlet in enumerate(["The Hindu", "Times of India"]):
        _put(store, f"Imphal firing {k}", _vec(0, jitter=k), story_id=sid, outlet=outlet)
    _put(store, "Imphal firing 2", _vec(0, jitter=3), story_id=sid, outlet="Times of India")

    def engine(q, lang="en"):
        assert "Imphal" in q and "the" not in q.split()
        R = lambda o, u: {"title": f"Imphal firing ({o})", "link": u, "outlet": o, "site": None,
                          "published_at": NOW, "lang": "en", "engine": "fake", "resolved": True}
        return [R("Navbharat Times", "https://navbharattimes.indiatimes.com/a"),   # same owner as TOI: skip
                R("MSN", "https://www.msn.com/en-in/news/x"),                       # aggregator: skip
                R("The Indian Express", "https://indianexpress.com/article/x"),     # new owner
                R("ThePrint", "https://theprint.in/x"),                             # new owner, unreadable
                R("The Indian Express", "https://indianexpress.com/article/y")]     # same owner twice: once
    pages = {"https://indianexpress.com/article/x": {"text": "Police fired. " * 60, "author": "A Reporter"}}
    monkeypatch.setattr(discover, "fetch_article", lambda url: pages.get(url))
    stats = discover.discover(store, None, n_stories=5, engines=(engine,))
    found = {r["url"]: r for r in store.rows(select(articles).where(articles.c.found_by == "search"))}
    assert set(found) == {"https://indianexpress.com/article/x", "https://theprint.in/x"}
    assert found["https://indianexpress.com/article/x"]["text_source"] == "full"
    assert found["https://indianexpress.com/article/x"]["outlet"] == "The Indian Express"
    assert found["https://theprint.in/x"]["text_source"] == "summary"           # coverage only
    assert found["https://theprint.in/x"]["outlet"] == "The Print"
    assert all(r["story_id"] is None for r in found.values())                    # grouping decides membership
    assert stats["new_articles"] == 2
    # searched stories are not searched again within the interval
    assert discover.discover(store, None, n_stories=5, engines=(engine,))["stories"] == 0


def test_rejected_key_is_dropped_not_retried(store):
    class Bad:
        def __init__(self): self.calls = 0
        def list_models(self): return ["m1"]
        def generate(self, *a, **k):
            self.calls += 1
            raise RuntimeError("400 INVALID_ARGUMENT. API key not valid. Please pass a valid API key.")
    class Good:
        def list_models(self): return ["m1"]
        def generate(self, *a, **k): return '{"ok": 1}', [], 5
    bad = Bad()
    r = Router({"t": [dict(id="m1", rpm=10, tpm=10000, rpd=100)]}, [bad, Good()], store)
    for _ in range(5):
        assert r.call("t", "x").data == {"ok": 1}
    assert bad.calls == 1 and r.bad_keys == {0}


# ---------------------------------------------------------------- origins: ways one source could pass as two

class _AttribR:
    """Stand-in model for the attribution question: names a police force with or without its government."""
    def call(self, tier, prompt, **kw):
        import re as _re
        from nishpaksh.router import LLMResult
        items = []
        for m in _re.finditer(r"^(\d+)\. (.*)$", prompt, flags=_re.M):
            name = m.group(2)
            kind = "police" if "olice" in name else "other"
            gov = "Delhi" if name == "Delhi Police spokesperson" else None
            items.append({"n": int(m.group(1)), "name": name, "kind": kind, "government": gov})
        return LLMResult("", {"items": items}, "m", [], 0)


def _origin_story(store, reports, texts=None, authors=None):
    """reports: [(outlet, agency, attributed_to or None)] -> (story id, statement id)."""
    from nishpaksh.db import claims as Cl, insert as ins
    sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, signature="s", dirty=True, qualifies=False))
    cid = store.insert_returning_id(canonical, dict(story_id=sid, kind="claim", text="x", conflicts=[]))
    for k, (outlet, agency, att) in enumerate(reports):
        aid = store.insert_returning_id(articles, dict(
            url=f"https://site{k}.in/a{sid}", outlet=outlet, agency=agency, title="t",
            text=(texts or {}).get(k, "body text"), text_source="full", published_at=NOW - dt.timedelta(hours=10 - k),
            extracted_at=NOW, story_id=sid, wire_group=1000 + k + sid * 10, author=(authors or {}).get(k)))
        store.exec(ins(Cl).values(story_id=sid, article_id=aid, kind="claim", text="x",
                                  stance="attributes" if att else "asserts", attributed_to=att or "article",
                                  evidence="none", canonical_id=cid))
    return sid, cid


def test_agency_copy_and_agency_attribution_are_one_origin(store):
    from nishpaksh import origins
    sid, cid = _origin_story(store, [("Outlet A", "PTI", None), ("Outlet B", None, "PTI"), ("Outlet C", None, None)])
    assert origins.compute_origins(store, _AttribR(), sid)[cid]["n_origins"] < 2


def test_police_named_two_ways_is_one_origin(store):
    from nishpaksh import origins
    sid, cid = _origin_story(store, [("Outlet A", None, "police"), ("Outlet B", None, "Delhi Police spokesperson"),
                                     ("Outlet C", None, None)])
    assert origins.compute_origins(store, _AttribR(), sid)[cid]["n_origins"] < 2


def test_rewritten_press_notes_with_bylines_are_not_original(store):
    """Three bylined outlets rewriting one press note in their own voice: 'Also Read:' is not a
    dateline, and nobody reported a detail of their own, so this is one pool, not three origins."""
    from nishpaksh import origins
    texts = {k: "Also Read: the press note says two people died." for k in range(3)}
    authors = {0: "Rahul Sharma", 1: "Priya Singh", 2: "Amit Verma"}
    sid, cid = _origin_story(store, [("Outlet A", None, None), ("Outlet B", None, None), ("Outlet C", None, None)],
                             texts, authors)
    assert origins.compute_origins(store, _AttribR(), sid)[cid]["n_origins"] == 0


def test_outlet_attribution_counts_only_if_that_outlet_reported_originally(store):
    from nishpaksh import origins
    sid, cid = _origin_story(store, [("Outlet A", None, "Times of India"), ("Outlet B", None, "Times of India"),
                                     ("Outlet C", "PTI", None)])
    info = origins.compute_origins(store, _AttribR(), sid)[cid]
    assert info["origins"] == ["agency:pti", "pool"] and info["n_origins"] == 1


def test_real_model_config_has_one_embedding_model(store):
    """The embed tier must resolve to exactly one model even when the key serves several:
    vectors from two models are not comparable (this crashed grouping before it reached production)."""
    from nishpaksh.config import load_yaml
    from nishpaksh.stories import embed_model

    class All(FakeBackend):
        def list_models(self):
            return super().list_models() + ["gemini-embedding-001", "gemini-embedding-2"]
    r = Router(load_yaml("models.yaml")["tiers"], All(), store)
    r.resolve()
    assert embed_model(r)


def test_split_and_join_never_loop(store):
    """An article that passes the join rule must not be split off again next run (each split wipes
    the story's statements and verdicts)."""
    from nishpaksh.stories import group_stories
    sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, signature="s", dirty=False, qualifies=True))
    for k in range(2):
        _put(store, f"A{k}", _vec(0, jitter=k), hours_ago=10 - k, story_id=sid, extracted=True)
    for k in range(6):
        _put(store, f"C{k}", _vec(20, jitter=5 + k), hours_ago=8 - k * 0.1, story_id=sid, extracted=True)
    _put(store, "N", _vec(-28, jitter=50), hours_ago=1, extracted=True)
    for _ in range(3):
        cid = store.insert_returning_id(canonical, dict(story_id=sid, kind="claim", text="x", conflicts=[],
                                                       verdict="corroborated"))
        store.exec(update(stories).where(stories.c.id == sid).values(dirty=False))
        group_stories(store, None)
        assert store.one(select(canonical).where(canonical.c.id == cid)) is not None


def test_lone_article_joins_its_later_siblings_and_its_empty_story_goes(store):
    from nishpaksh.stories import group_stories
    lone_sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, signature="lone", dirty=False))
    first = _put(store, "[F] flood in Assam 0", _vec(0, jitter=1), hours_ago=6, story_id=lone_sid)
    big = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, signature="big", dirty=False))
    for k in range(3):
        _put(store, f"[F] flood in Assam {k + 1}", _vec(0.5 * k, jitter=2 + k), hours_ago=3, story_id=big)
    group_stories(store, _router(store))
    assert store.one(select(articles.c.story_id).where(articles.c.id == first))["story_id"] == big
    assert store.one(select(stories).where(stories.c.id == lone_sid)) is None


def test_model_answer_is_remembered(store):
    """A borderline article the model kept out of a story is not asked about again next run."""
    from nishpaksh.stories import group_stories

    class Count(_NoLLM):
        asked = 0
        def generate(self, model, prompt, json_mode, grounded):
            if "SAME specific event" in prompt:
                Count.asked += 1
            return super().generate(model, prompt, json_mode, grounded)
    for k in range(3):
        _put(store, f"[A] rally {k}", _vec(0, jitter=k), hours_ago=5)
    group_stories(store, _router(store, Count()))
    _put(store, "[B] other rally", _vec(34, jitter=9), hours_ago=1)
    group_stories(store, _router(store, Count()))
    n = Count.asked
    group_stories(store, _router(store, Count()))
    group_stories(store, _router(store, Count()))
    assert n == 1 and Count.asked == 1


def test_embedding_quota_is_counted_per_text(store):
    """Google counts each text in an embedding batch as a request (26 batches once exhausted a
    1,000-a-day quota), so the router must book one unit per text and stop before the limit."""
    r = Router({"embed": [dict(id="gemini-embedding-2", rpm=1000, tpm=10 ** 7, rpd=100)]}, FakeBackend(), store)
    assert r.embed([f"text {i}" for i in range(60)]) is not None
    assert r.tiers["embed"][0].used_today == 60
    assert r.embed([f"more {i}" for i in range(60)]) is None or r.tiers["embed"][0].used_today <= 100
    assert r.tiers["embed"][0].used_today <= 100


def test_unnamed_people_and_records_add_no_origin(store):
    """'A witness' in one outlet and 'a witness' in another may be the same person; 'official data'
    with no body named may be one release: none of these can make a second origin."""
    from nishpaksh import origins
    sid, cid = _origin_story(store, [("Outlet A", "PTI", None), ("Outlet B", None, "named witness"),
                                     ("Outlet C", None, "media report"), ("Outlet D", None, "official data")])
    info = origins.compute_origins(store, _AttribR(), sid)[cid]
    assert info["origins"] == ["agency:pti", "pool"]


def test_story_grouped_on_old_vectors_is_not_published(store):
    """Read articles whose vectors came from another embedding model mean the story's membership
    was never checked: it must not be published until they are re-embedded and regrouped."""
    from nishpaksh.run import run
    _seed(store)
    _run_twice(store)
    assert store.rows(select(published))
    # before it was written (a published article is closed and never regrouped)
    store.exec(delete(published))
    store.exec(update(articles).where(articles.c.extracted_at.is_not(None)).values(embed_model="old-model"))

    class NoEmbed(FakeBackend):
        def embed(self, model, texts):
            raise RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerDay")
    stats = run(store=store, backend=NoEmbed(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    assert stats["stories_awaiting_regroup"] >= 1
    assert store.rows(select(published)) == []


def test_model_verdicts_are_on_hold_by_default():
    from nishpaksh.config import SETTINGS
    assert SETTINGS.model_verdicts is False


def test_plain_wording_keeps_names_and_never_repeats_the_speaker():
    """Real fallback sentences that read badly: 'According to protesters, Protesters demanded...',
    'reported that sahil Wakode', and a hedge on every single sentence."""
    from nishpaksh.narrative import plain_sentence
    s = lambda **k: plain_sentence(dict({"verdict": "unverified", "speaker": None, "check": None}, **k))
    assert s(text="Protesters demanded the resignation of the CEC.", speaker="protesters") == \
        "Protesters demanded the resignation of the CEC."
    assert s(text="Sahil Wakode faced caste-based discrimination", speaker="his parents") == \
        "Sahil Wakode faced caste-based discrimination, according to his parents."
    assert s(text="Sahil Wakode was found dead in his hostel room.") == "Sahil Wakode was found dead in his hostel room."
    # a dispute says what the other side is: here, a denial (never "other reports differ" with no content)
    assert s(text="About 40 people gave statements", verdict="disputed") == \
        "About 40 people gave statements; this is denied in other reports."


# ---------------------------------------------------------------- writing round 2

def _item(i, text, verdict="unverified", speaker=None):
    return {"id": i, "kind": "event", "text": text, "verdict": verdict, "speaker": speaker, "sources": [],
            "time": None, "check": None}


def test_writer_never_invents_a_speaker_or_a_cause():
    from nishpaksh.narrative import _validate
    by = {1: _item(1, "A group of students intensified their agitation on October 2"),
          2: _item(2, "Professor Doolla denied the allegations", speaker="Professor Doolla"),
          3: _item(3, "Sahil Wakode was found dead in his hostel room"),
          4: _item(4, "He was caught using a phone in the exam")}
    v = lambda text, ids: _validate({"text": text, "ids": ids}, by, set(), ["The Hindu"])
    assert v("Students said they intensified their agitation on October 2.", [1]) is None      # invented "said"
    assert v("Students intensified their agitation on October 2, reports said.", [1]) == [1]  # the hedge is fine
    assert v("Professor Doolla denied the allegations.", [2]) == [2]                          # a real speaker
    assert v("He was found dead in his hostel room because he was caught using a phone.", [3, 4]) is None
    assert v("He was found dead in his hostel room after he was caught using a phone.", [3, 4]) == [3, 4]
    assert v("The Hindu reported he was found dead in his hostel room.", [3]) is None          # outlet named


def test_headline_checks_catch_bare_names_and_hedges():
    from nishpaksh.news import headline_problem
    src = "kumar was produced before the court in kotdwar"
    assert "bare name" in headline_problem("Kumar was produced before the court in Kotdwar", src, set())
    assert "reportedly" in headline_problem("Gym trainer reportedly produced before Kotdwar court", src, set())
    assert "12 words" in headline_problem(" ".join(["word"] * 13), src, set())
    assert headline_problem("Gym trainer who defended shopkeeper produced before Kotdwar court", src, set()) is None
    assert "names 'dehradun'" in headline_problem("Gym trainer produced before Dehradun court", src, set())


def test_disputed_news_headline_takes_no_side():
    """Owner, Oct 8 2026: a disputed statement can be the news; its headline says whose version it is,
    or leaves the disputed figure out."""
    from nishpaksh.news import _disputed_answers, headline_problem
    items = {1: {"id": 1, "text": "Police said 40 people died in the Seemapuri building collapse.", "conflicts_with": [2]},
             2: {"id": 2, "text": "Families said 50 people died in the Seemapuri building collapse.", "conflicts_with": [1]}}
    disputed, versions = _disputed_answers(items[1], items)
    assert disputed == {"40", "50"} and "Families" in versions
    src = (items[1]["text"] + " " + items[2]["text"]).lower()
    assert "disputed" in headline_problem("Building collapse in Seemapuri kills 40", src, set(), disputed)
    assert headline_problem("Police say 40 dead in Seemapuri collapse, families say 50", src, set(), disputed) is None
    assert headline_problem("Building collapse in Seemapuri leaves many dead", src, set(), disputed) is None


def test_news_is_the_decisive_act_not_the_setting():
    from nishpaksh.news import pick_news
    items = [
        {"id": 1, "kind": "event", "text": "India Block leaders held a protest near Jantar Mantar.", "n_sources": 5,
         "n_articles": 7, "time": {"start": "2026-10-07T10:00"}},
        {"id": 2, "kind": "event", "text": "Delhi Police detained Rahul Gandhi and Priyanka Gandhi.", "n_sources": 4,
         "n_articles": 5, "time": {"start": "2026-10-07T11:00"}},
        {"id": 3, "kind": "event", "text": "Police arrested 12 protesters in 2019.", "n_sources": 4,
         "n_articles": 4, "time": {"start": "2019-03-01"}},                      # old: not today's news
        {"id": 4, "kind": "claim", "text": "The ruling party condemned the protest.", "n_sources": 1, "n_articles": 1},
        {"id": 5, "kind": "event", "text": "The protest began in 2024.", "n_sources": 6, "n_articles": 6,
         "role": "background"},
    ]
    assert pick_news(items)[0] == 2


def test_best_correct_headline_wins():
    from nishpaksh.news import headline_score
    news = "Delhi Police detained Rahul Gandhi and Priyanka Gandhi at a protest near Jantar Mantar."
    good = "Police detain Rahul, Priyanka Gandhi at Jantar Mantar protest"
    setting = "India Block leaders hold protest near Jantar Mantar"
    jargon = "INDIA bloc MPs detained in DPDP protest"
    assert headline_score(good, news) > headline_score(setting, news)
    assert headline_score(good, news) > headline_score(jargon, news)


def test_filler_is_never_published(store):
    from nishpaksh import compose
    _seed(store)
    _run_twice(store)
    sid = store.rows(select(published.c.story_id))[0]["story_id"]
    store.exec(delete(published))       # judged before it is written
    store.exec(update(stories).where(stories.c.id == sid).values(signature="Aaj ka Rashifal"))
    an = dict(store.one(select(stories.c.analysis).where(stories.c.id == sid))["analysis"])
    an["priority"] = {"score": 1, "filler": True}        # rated as filler from its headlines (priority.py)
    store.exec(update(stories).where(stories.c.id == sid).values(analysis=an))
    from nishpaksh.config import load_yaml
    r = Router(load_yaml("models.yaml")["tiers"], FakeBackend(), store)
    r.resolve()
    assert compose.publish_story(store, r, sid) is False
    assert store.rows(select(published).where(published.c.story_id == sid)) == []


def test_a_later_development_links_to_its_story_and_gets_background(store):
    """A bail hearing for the arrested engineer is a development of the flyover collapse: the new page
    links back; the old article is closed and never touched."""
    from nishpaksh import compose, threads
    from nishpaksh.db import story_links
    _seed(store)
    _run_twice(store)
    parent = store.rows(select(published.c.story_id))[0]["story_id"]
    later = NOW + dt.timedelta(hours=1)
    child = store.insert_returning_id(stories, dict(created_at=later, updated_at=later, dirty=False, qualifies=True,
                                                   signature="Court grants bail to Kesarganj flyover site engineer"))
    from nishpaksh.config import load_yaml
    r = Router(load_yaml("models.yaml")["tiers"], FakeBackend(), store)
    r.resolve()
    assert threads.find_parents(store, r, child, "Court grants bail to Kesarganj flyover site engineer",
                                "The site engineer arrested after the Kesarganj flyover collapse got bail") == [parent]
    assert store.rows(select(story_links)) and not store.one(select(stories.c.dirty).where(stories.c.id == parent))["dirty"]
    # an unrelated story with no shared name is never linked
    other = store.insert_returning_id(stories, dict(created_at=later, updated_at=later, dirty=False, qualifies=True,
                                                   signature="Monsoon session of Lucknow assembly adjourned"))
    assert threads.find_parents(store, r, other, "Monsoon session of Lucknow assembly adjourned", "") == []
    before = store.one(select(published).where(published.c.story_id == parent))
    assert compose.publish_story(store, r, parent) is False          # closed: never written again
    assert store.one(select(published).where(published.c.story_id == parent)) == before
    assert threads.root_of(store, child) == parent


def test_ids_written_like_the_prompt_are_accepted():
    from nishpaksh.narrative import _validate
    by = {16191: _item(16191, "The police questioned the professor for 10 hours", verdict="corroborated")}
    assert _validate({"text": "The police questioned the professor for 10 hours.", "ids": ["#16191"]}, by, set(), []) == [16191]


def test_router_paces_each_tier_over_the_quota_day():
    """Oct 2026: unpaced, a day's Flash-Lite went in ~15 runs and the site sat dark until the
    reset. Spend opens up evenly over the Pacific day; the page tier (nothing kept) outlasts the
    analysis tier, which outlasts bulk reading; unused allowance carries forward."""
    from nishpaksh.router import PACIFIC, QuotaExhausted

    class B:
        def generate(self, model, prompt, json_mode, grounded):
            return '{"ok": true}', [], 10
    cfg = {"bulk": [dict(id="fl", rpm=1000, tpm=10**7, rpd=480, keep=240), dict(id="gemma", rpm=1000, tpm=10**7, rpd=14400)],
           "light": [dict(id="fl", rpm=1000, tpm=10**7, rpd=480, keep=48)],
           "page": [dict(id="fl", rpm=1000, tpm=10**7, rpd=480)]}
    r = Router(cfg, B(), paced=True)
    clock = {"t": dt.datetime(2026, 10, 5, 0, 0, tzinfo=PACIFIC)}
    r._now_pacific = lambda: clock["t"]
    fl = r.tiers["page"][0]
    # midnight + 2 h slack = 1/12 of each tier's allowance
    assert r.pace_cap("page", fl) == 40 and r.pace_cap("light", fl) == 36 and r.pace_cap("bulk", fl) == 20
    models = [r.call("bulk", "x").model for _ in range(25)]
    assert models[:20] == ["fl"] * 20 and set(models[20:]) == {"gemma"}   # reading overflows to Gemma
    for _ in range(16):
        r.call("light", "x")
    with pytest.raises(QuotaExhausted):
        r.call("light", "x")
    for _ in range(4):
        r.call("page", "x")                     # the page still gets its share
    with pytest.raises(QuotaExhausted):
        r.call("page", "x")
    clock["t"] = clock["t"].replace(hour=23)    # by the end of the day everything is open
    assert r.pace_cap("page", fl) == 480 and r.remaining_now("light") == 432 - 40
    assert Router(cfg, B(), paced=False).pace_cap("page", fl) == 480   # backfill: unpaced


def test_health_flags_quota_too_small_for_a_call_and_stalled_grouping(tmp_path):
    from nishpaksh.db import runs, utcnow
    from nishpaksh.run import health
    store = Store(f"sqlite:///{tmp_path}/h.db")
    store.init()
    for _ in range(2):
        store.insert_returning_id(runs, dict(started_at=utcnow(), finished_at=utcnow(),
                                             stats={"ingested": 40, "grouped": 0}))
    stats = {"ingested": 50, "grouped": 0, "quota_left": {"embed": 20, "light": 0, "page": 300, "writer": 4}}
    probs = " | ".join(health(store, stats)["problems"])
    assert "embed, light" in probs and "page" not in probs.split("for:")[1].split("(")[0]
    assert "none grouped" in probs
    ok = health(store, {"ingested": 50, "grouped": 30, "quota_left": {"embed": 900, "light": 10, "page": 10, "writer": 4}})
    assert not [p for p in ok["problems"] if "quota" in p or "grouped" in p]


def test_modelcmp_reruns_analysis_on_a_scratch_copy_and_compares(store):
    """The model comparison tool rebuilds statement matching from nothing in a scratch copy (the
    real store is untouched) and compares colours per extracted statement."""
    from nishpaksh.run import run
    from nishpaksh.tools import modelcmp
    _seed(store)
    backend = FakeBackend()
    run(store=store, backend=backend, time_budget_min=30, ingest_news=False, verify_budget=VB)
    sid = max((r["story_id"], ) for r in store.rows(select(claims.c.story_id)))[0]
    before = store.rows(select(canonical.c.id, canonical.c.verdict).where(canonical.c.story_id == sid))
    out = []
    for _ in range(2):
        st = modelcmp.scratch(store, [sid])
        out.append(modelcmp.analyse(st, modelcmp.Counting(_router(st, backend)), sid))
    assert out[0] and set(out[0]) == set(out[1])
    cmp = modelcmp.compare(out[0], out[1])
    assert cmp["colour_agreement"] == 1.0 and cmp["new_green"] == 0
    assert store.rows(select(canonical.c.id, canonical.c.verdict).where(canonical.c.story_id == sid)) == before
    page = modelcmp.page_tasks(store, modelcmp.Counting(_router(store, backend)), sid, [x["text"] for x in out[0].values()])
    assert page["headline"]


def test_router_drops_a_model_that_keeps_failing_with_overload():
    class B:
        def __init__(self): self.calls = Counter()
        def generate(self, model, prompt, json_mode, grounded):
            self.calls[model] += 1
            if model == "gemma":
                raise RuntimeError("503 UNAVAILABLE: model is experiencing high demand")
            return '{"ok": true}', [], 10
    from collections import Counter
    b = B()
    r = Router({"t": [dict(id="gemma", rpm=100, tpm=10**6, rpd=1000), dict(id="fl", rpm=100, tpm=10**6, rpd=1000)]}, b)
    for _ in range(3):
        r.tiers["t"][0].cooldown_until = 0          # pretend the cooldown passed each time
        r.call("t", "x")
    assert r.tiers["t"][0].disabled and b.calls["gemma"] == 3


# ---------------------------------------------------------------- writing round 3 (Oct 2026)

def _payload(items):
    for i in items:
        i.setdefault("sources", [{"url": f"https://o{i['id']}.in/a", "outlet": f"O{i['id']}", "stance": "supports"}])
        i.setdefault("n_articles", 3)
        i.setdefault("minor", False)
        i.setdefault("conflicts_with", [])
    return {"timeline": [], "undated": [], "established": [i for i in items if i["verdict"] == "corroborated"],
            "contested": [i for i in items if i["verdict"] != "corroborated"], "background": [],
            "sources": [s for i in items for s in i["sources"]]}


def test_speaker_named_once_carries_through_the_paragraph():
    """'Sybiha set out Kyiv's position. He said X. He added Y.' is how newspapers attribute; the
    old check demanded the name in every sentence, which produced 'according to Sybiha' x8."""
    from nishpaksh.narrative import _check_paragraphs
    by = {1: _item(1, "India's proposal is the most comprehensive of four", speaker="Andrii Sybiha"),
          2: _item(2, "Ukraine is ready for an energy truce", speaker="Andrii Sybiha"),
          3: _item(3, "Russia is not interested in peace talks", speaker="Friedrich Merz")}
    para = [{"text": "Ukraine's foreign minister Andrii Sybiha said India's proposal is the most comprehensive of four.", "ids": [1]},
            {"text": "The minister added that Ukraine is ready for an energy truce.", "ids": [2]},
            {"text": "The minister said Russia is not interested in peace talks.", "ids": [3]}]   # Merz's claim, not Sybiha's
    paras, failed, rejected = _check_paragraphs([para], by, set(), [])
    assert [s["ids"] for s in paras[0]] == [[1], [2]] and rejected == 1
    assert failed[0]["sentence"]["ids"] == [3] and failed[0]["reason"] == "claim without its speaker"
    # the speaker carries into the next paragraph of the same section, not into another section
    two = [[para[0]], [{"text": "The minister added that Ukraine is ready for an energy truce.", "ids": [2]}]]
    paras, failed, rejected = _check_paragraphs(two, by, set(), [], keys=["say", "say"])
    assert rejected == 0
    paras, failed, rejected = _check_paragraphs(two, by, set(), [], keys=["say", "next"])
    assert rejected == 1 and failed[0]["reason"] == "claim without its speaker"
    # "he" for someone no outlet calls "he" is refused (owner, Oct 8 2026: code never guesses a gender)
    he = [[para[0], {"text": "He added that Ukraine is ready for an energy truce.", "ids": [2]}]]
    paras, failed, rejected = _check_paragraphs(he, by, set(), [])
    assert rejected == 1 and failed[0]["reason"] == "pronoun without evidence"


def test_dispute_must_say_what_the_other_side_is():
    from nishpaksh.narrative import _validate, disputed_pair
    a = dict(_item(1, "The toll is 40", "disputed", speaker="the police"), conflicts_with=[2])
    b = dict(_item(2, "The toll is 50", "disputed", speaker="the families"), conflicts_with=[1])
    by = {1: a, 2: b}
    assert _validate({"text": "The toll is 40, according to some reports; other reports differ.", "ids": [1]}, by, set(), []) is None
    assert _validate({"text": "The police put the toll at 40; the families say 50.", "ids": [1, 2]}, by, set(), []) == [1, 2]
    assert disputed_pair(a, b) == "The police says the toll is 40; the families says the toll is 50."


def test_colour_change_recolours_the_essay_without_a_rewrite():
    from nishpaksh.narrative import needs_rewrite, recolour, write_narrative

    class Writer:
        model = "gemini-3.8-flash"
        def call(self, tier, prompt, **kw):
            from nishpaksh.router import LLMResult
            data = {"paragraphs": [[{"text": "A bridge collapsed in Kesarganj.", "ids": [1]},
                                    {"text": "The engineer was arrested.", "ids": [2]}]]}
            return LLMResult("", data, self.model, [], 10)
    items = [_item(1, "A bridge collapsed in Kesarganj", "developing"), _item(2, "The engineer was arrested", "developing")]
    p = _payload(items)
    nar = write_narrative(Writer(), p, set())
    assert nar["model"] and [s["class"] for s in nar["paragraphs"][0]] == ["developing", "developing"]
    # six hours on, the bridge is established: same essay, new colour, no writer call
    items[0]["verdict"] = "corroborated"
    p2 = _payload(items)
    assert not needs_rewrite(nar, p2)
    re = recolour(nar, p2, set())
    assert re["paragraphs"][0][0]["class"] == "established" and re["paragraphs"][0][0]["text"] == "A bridge collapsed in Kesarganj."
    # the arrest becomes disputed: stated as fact it fails the checks, so it leaves the essay and is
    # listed under it in words that say so
    items[1]["verdict"] = "disputed"
    re2 = recolour(re, _payload(items), set())
    assert [s["ids"] for p in re2["paragraphs"] for s in p] == [[1]]
    assert re2["not_in_essay"][0]["ids"] == [2] and "denied" in re2["not_in_essay"][0]["text"]
    # a new statement that is not minor is material: worth a rewrite when the writer can afford one
    items.append(_item(3, "The state ordered an inquiry", "developing"))
    assert needs_rewrite(re2, _payload(items))


def test_pacing_opens_faster_on_indian_daytime_hours():
    from nishpaksh.router import PACIFIC, pace_fraction
    d = lambda h: dt.datetime(2026, 10, 5, h, 0, tzinfo=PACIFIC)
    # Pacific 01:00-02:00 is 13:30-14:30 IST (day); 12:00-13:00 Pacific is 00:30-01:30 IST (night)
    assert pace_fraction(d(2)) - pace_fraction(d(1)) == pytest.approx(3 * (pace_fraction(d(13)) - pace_fraction(d(12))))


def test_noise_does_not_become_perspectives(store):
    """Real data, Oct 2026: agreement values centred on zero, clusters that changed with every
    resample. Random agreements must not produce perspectives."""
    import random
    from nishpaksh.perspectives import recompute_global
    rng = random.Random(3)
    outlets = [f"O{i}" for i in range(12)]
    rows = []
    for sid in range(1, 40):
        pick = rng.sample(outlets, 5)
        for i, a in enumerate(pick):
            for b in pick[i + 1:]:
                x, y = sorted((a, b))
                rows.append(dict(story_id=sid, a=x, b=y, value=rng.uniform(-1, 1)))
    with store.engine.begin() as c:
        c.execute(insert(story_pairs), rows)
    assert recompute_global(store) == 0
    assert store.rows(select(source_clusters)) == []


def _sided_story(store, side_of: dict[str, str], authors: dict[str, str] | None = None):
    """A story with four contested facts: side 'A' asserts 1-2 and denies 3-4, side 'B' the reverse."""
    from nishpaksh.db import claims as Cl
    sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, signature="s", dirty=True, qualifies=False))
    cids = [store.insert_returning_id(canonical, dict(story_id=sid, kind="claim", text=f"fact {k}", conflicts=[]))
            for k in range(4)]
    aids = {}
    for k, (outlet, side) in enumerate(side_of.items()):
        aid = store.insert_returning_id(articles, dict(
            url=f"https://{outlet.lower()}.in/a{sid}", outlet=outlet, title="t", text="body", text_source="full",
            published_at=NOW, extracted_at=NOW, story_id=sid, wire_group=5000 + sid * 20 + k,
            author=(authors or {}).get(outlet)))
        aids[outlet] = aid
        for j, cid in enumerate(cids):
            pos = (j < 2) == (side == "A")
            store.exec(insert(Cl).values(story_id=sid, article_id=aid, kind="claim", text=f"fact {j}",
                                         stance="asserts" if pos else "denies", attributed_to="article",
                                         evidence="none", canonical_id=cid))
    return sid, aids


def test_clusters_are_outlets_but_an_article_can_depart_and_a_repeat_author_is_split(store):
    """Owner's design, Oct 2026: perspectives are clustered over outlets; an article that clearly
    sides with another perspective is shown there (†); an author who keeps doing it becomes a unit."""
    from nishpaksh import perspectives as P
    P.SPLIT_AUTHORS.clear()
    with store.engine.begin() as c:
        c.execute(insert(source_clusters), [dict(source=o, cluster=0 if o.startswith("A") else 1, updated_at=NOW)
                                            for o in ("A1", "A2", "A3", "B1", "B2")])
    # two authors of one outlet are one unit
    assert P.source_key({"outlet": "A3", "author": "Ravi Kumar"}) == P.source_key({"outlet": "A3", "author": "Sita Rao"}) == "A3"
    sides = {"A1": "A", "A2": "A", "A3": "B", "B1": "B", "B2": "B"}     # A3's article sides with B
    for _ in range(5):
        sid, aids = _sided_story(store, sides, authors={"A3": "Ravi Kumar"})
        an = P.analyze_story(store, sid)
        assert an["mode"] == "global"
        d = an["departures"][str(aids["A3"])]
        assert d["from"] == "A" and d["to"] == "B" and d["evidence"] >= 3
        assert str(aids["A1"]) not in an["departures"]                  # the others stay with their outlet
    units = P.refresh_units(store)
    assert units["split_authors"] == ["A3::ravi kumar"]
    assert P.source_key({"outlet": "A3", "author": "Ravi Kumar"}) == "A3::ravi kumar"   # now its own unit
    assert P.source_key({"outlet": "A3", "author": "Sita Rao"}) == "A3"
    assert "A3" in units["outlets_departing_often"]                     # health sees it too
    P.SPLIT_AUTHORS.clear()


def test_one_odd_article_does_not_depart_on_thin_evidence(store):
    from nishpaksh import perspectives as P
    from nishpaksh.db import claims as Cl
    with store.engine.begin() as c:
        c.execute(insert(source_clusters), [dict(source=o, cluster=0 if o.startswith("A") else 1, updated_at=NOW)
                                            for o in ("A1", "A2", "A3", "B1", "B2")])
    sid, aids = _sided_story(store, {"A1": "A", "A2": "A", "A3": "A", "B1": "B", "B2": "B"})
    # A3 reports just one fact differently from the rest of A: not enough to move it
    store.exec(update(Cl).where(Cl.c.article_id == aids["A3"], Cl.c.text == "fact 0").values(stance="denies"))
    an = P.analyze_story(store, sid)
    assert str(aids["A3"]) not in an["departures"]


def test_no_perspectives_until_the_positions_test_finds_a_signal(store):
    """Even clean-looking clusters stay off the site until the daily test (resampling + shuffled
    baseline) says outlets really take positions."""
    from nishpaksh.db import diagnostics
    from nishpaksh.perspectives import recompute_global
    camp1, camp2 = ["O1", "O2", "O3"], ["O4", "O5", "O6"]
    rows = [dict(story_id=sid, a=a, b=b, value=0.9 if (a in camp1) == (b in camp1) else -0.6)
            for sid in range(1, 6) for i, a in enumerate(camp1 + camp2) for b in (camp1 + camp2)[i + 1:]]
    with store.engine.begin() as c:
        c.execute(insert(story_pairs), rows)
    assert recompute_global(store) == 0                       # no test run yet
    store.exec(insert(diagnostics).values(kind="positions", report={"signal": False}))
    assert recompute_global(store) == 0
    store.exec(insert(diagnostics).values(kind="positions", report={"signal": True}))
    assert recompute_global(store) == 2


def test_omission_counts_only_when_the_text_lacks_the_fact():
    """Our reader's miss is not the outlet's choice: a fact counts as left out only if its names and
    numbers are absent from the article, across Roman and Devanagari spellings."""
    from nishpaksh.textmatch import Text, verdict
    hi = Text("विदेश मंत्री एस जयशंकर ने कहा कि भारत रूस और यूक्रेन दोनों से बात कर रहा है।")
    assert verdict("Subrahmanyam Jaishankar said India is talking to Russia and Ukraine", hi) == "present"
    assert verdict("Andrii Sybiha held a briefing in Kyiv", hi) == "absent"
    assert verdict("The talks went well", hi) is None              # nothing checkable: never an omission


def test_positions_test_finds_real_sides_and_not_noise(store):
    """Two camps that each leave out what the other reports, story after story: a signal. The same
    amount of omission at random: none."""
    import random
    from nishpaksh import positions
    camp = {f"O{i}": i < 4 for i in range(8)}
    def make(random_omit):
        rng = random.Random(1)
        per = {}
        for sid in range(60):
            items = []
            for f in range(4):
                side = f % 2 == 0                       # facts 0,2 inconvenient to camp False...
                d = {}
                for o, c in camp.items():
                    omit = rng.random() < 0.5 if random_omit else (c != side and rng.random() < 0.85)
                    d[o] = 0 if omit else 1
                items.append(("omission", d))
            per[sid] = items
        return per
    for random_omit, expect in ((False, True), (True, False)):
        orig = positions.build_items
        positions.build_items = lambda store, per=make(random_omit): per
        try:
            rep = positions.test_positions(store, rounds=8, shuffles=2)
        finally:
            positions.build_items = orig
        assert rep["signal"] is expect, rep


def test_failed_attempts_are_not_reported_as_quota():
    """Oct 2026: 20 writer calls gave 2 essays and no recorded failure, because eight errors in a
    row were raised as 'quota exhausted'. Errors now say what they were, and are counted per kind."""
    from nishpaksh.router import CallFailed

    class Overloaded:
        def generate(self, model, prompt, json_mode, grounded):
            raise RuntimeError("503 UNAVAILABLE: high demand")
    r = Router({"t": [dict(id="m1", rpm=100, tpm=10**6, rpd=100)]}, Overloaded(), max_wait=0.1)
    with pytest.raises(CallFailed, match="503"):
        r.call("t", "x")
    assert any("overloaded" in k for k in r.error_log)


def test_paragraph_hedge_reads_naturally():
    from nishpaksh.narrative import _hedge
    assert _hedge("Police are examining CCTV footage.") == "According to reports, police are examining CCTV footage."
    assert _hedge("Previously, the CJP held a protest.") == "Previously, according to reports, the CJP held a protest."
    assert _hedge("Gyanesh Kumar met officials.") == "According to reports, Gyanesh Kumar met officials."


def test_overloaded_calls_do_not_use_up_the_days_allowance():
    from nishpaksh.router import CallFailed

    class Overloaded:
        def generate(self, model, prompt, json_mode, grounded):
            raise RuntimeError("503 UNAVAILABLE: high demand")
    r = Router({"t": [dict(id="m1", rpm=100, tpm=10**6, rpd=100)]}, Overloaded(), max_wait=0.1)
    with pytest.raises(CallFailed):
        r.call("t", "x", max_attempts=3)
    assert r.tiers["t"][0].used_today == 0


def test_reanalysis_keeps_work_of_other_stages(store):
    from nishpaksh import perspectives as P
    sid, _ = _sided_story(store, {"A1": "A", "A2": "A", "B1": "B"})
    store.exec(update(stories).where(stories.c.id == sid).values(analysis={
        "writer_failures": {"n": 1}, "importance": {"score": 4}, "thread_checked": "h"}))
    an = P.analyze_story(store, sid)
    assert an["writer_failures"] == {"n": 1} and an["importance"] == {"score": 4} and an["thread_checked"] == "h"


# ---------------------------------------------------------------- the Everest page (Oct 5 2026)

def test_headline_must_keep_who_did_what():
    from nishpaksh.news import _actor_problem
    src = "fssai ordered the recall of everest food products' cumin powder. fssai suspended the licence"
    assert "who did what" in _actor_problem("FSSAI recalls Everest cumin powder", src)
    assert _actor_problem("FSSAI suspends licence, orders Everest recall", src) is None
    assert _actor_problem("Justice Bhuyan calls mass deletion unconstitutional", "justice bhuyan said it") is None


def _resp_items():
    claim = dict(_item(1, "A food analyst declared a sample of Nestle dairy whitener unsafe", "unverified",
                       speaker="a food analyst"), responded_by=[2], responds_to=[], conflicts_with=[])
    resp = dict(_item(2, "Nestle India says its dairy whitener is safe", "unverified", speaker="Nestle India"),
                responds_to=[1], responded_by=[], conflicts_with=[])
    return {1: claim, 2: resp}


def test_a_response_never_stands_without_what_it_answers():
    """The essay kept "Nestle India denied this" after the sentence it answered was dropped."""
    from nishpaksh.narrative import _also, _check_paragraphs
    by = _resp_items()
    para = [{"text": "A food analyst declared a sample of Nestle dairy whitener unsafe, said 5 officials.", "ids": [1]},
            {"text": "Nestle India responded to this, saying its dairy whitener is safe.", "ids": [2]}]
    paras, failed, rejected = _check_paragraphs([para], by, set(), [])
    assert paras == [] and rejected == 2 and len(failed) == 1      # the denial fell with its claim
    also = _also(list(by.values()), set(), by, [])
    assert len(also) == 1 and also[0]["ids"] == [1, 2] and "Nestle India says" in also[0]["text"]
    # written properly, the pair stays
    ok = [{"text": "A food analyst declared a sample of Nestle dairy whitener unsafe.", "ids": [1]},
          {"text": "Nestle India said its dairy whitener is safe.", "ids": [2]}]
    paras, failed, rejected = _check_paragraphs([ok], by, set(), [])
    assert rejected == 0 and len(paras[0]) == 2


def test_unrelated_statements_are_not_joined_in_one_sentence():
    from nishpaksh.narrative import _validate
    by = {1: _item(1, "A sample of cumin powder was drawn from Riverside Resorts in Goa"),
          2: _item(2, "Creative Bakers had hygiene deficiencies including cobwebs and flies")}
    s = {"text": "According to reports, a sample was drawn from Riverside Resorts in Goa, and Creative Bakers had "
                 "hygiene deficiencies including cobwebs and flies.", "ids": [1, 2]}
    assert _validate(s, by, set(), []) is None


def test_also_reported_skips_what_the_essay_already_says():
    from nishpaksh.narrative import _also
    i = _item(7, "Creative Bakers and Confectioners had severe hygiene deficiencies")
    essay = ["Creative Bakers and Confectioners had hygiene deficiencies including dirt, cobwebs and severe pest infestation."]
    assert _also([i], set(), {7: i}, essay) == []
    j = _item(8, "Everest has 15 days to report the recall status")
    assert len(_also([j], set(), {8: j}, essay)) == 1


def test_context_is_kept_apart_from_the_story_event_and_written_after_it():
    from nishpaksh.narrative import essay_ok, ordered_items
    core = [dict(_item(1, "FSSAI ordered the recall of Everest cumin powder", "corroborated"), n_articles=5, minor=False)]
    ctx = [dict(_item(2, "A food analyst declared a Nestle whitener sample unsafe"), role="related",
                related_event="FSSAI finding on Nestle whitener, 4 October", n_articles=2, minor=False)]
    p = {"timeline": [], "undated": [], "established": core, "contested": [], "context": ctx}
    assert [i["id"] for i in ordered_items(p)] == [1, 2]
    nar = {"model": "gemini-3.8-flash", "paragraphs": [[{"ids": [1]}]], "covers": [1], "rejected": 0}
    # owner, Oct 7 2026: everything known goes in, context included (85% of all statements)
    assert not essay_ok(nar, p)
    assert essay_ok(dict(nar, covers=[1, 2]), p)


def test_extraction_keeps_context_marked(store):
    from nishpaksh.extract import normalize_extraction, store_extraction
    from nishpaksh.db import claims as Cl
    ex = normalize_extraction({"signature": "FSSAI orders recall", "events": [{"id": "e1", "text": "FSSAI ordered a recall"}],
                               "context": [{"id": "x1", "type": "related", "text": "A Nestle sample was found unsafe",
                                            "event": "Nestle finding, 4 Oct", "start": "2026-10-04"},
                                           {"id": "x2", "type": "nonsense", "text": "dropped"}]})
    assert len(ex["context"]) == 1
    aid = store.insert_returning_id(articles, dict(url="https://x.in/1", outlet="X", title="t", text="b",
                                                   published_at=NOW, fetched_at=NOW))
    store_extraction(store, aid, None, ex)
    rows = {r["text"]: r for r in store.rows(select(Cl))}
    assert rows["A Nestle sample was found unsafe"]["rel"] == {"context": "related", "event": "Nestle finding, 4 Oct"}
    assert rows["A Nestle sample was found unsafe"]["kind"] == "event" and rows["FSSAI ordered a recall"]["rel"] is None


def test_names_that_differ_keep_a_statement_from_green(store):
    from nishpaksh import verify
    sid, cid = _origin_story(store, [("Outlet A", None, None), ("Outlet B", None, None), ("Outlet C", None, None)])
    store.exec(update(canonical).where(canonical.c.id == cid).values(
        checkable=True, origins={"outlets": 3, "n_origins": 3, "origins": ["a", "b", "c"]}))
    store.exec(update(stories).where(stories.c.id == sid).values(analysis={"name_conflicts": {str(cid): ["A Ltd", "B Ltd"]}}))
    verify.base_verdicts(store, sid)
    assert store.one(select(canonical.c.verdict).where(canonical.c.id == cid))["verdict"] == "unverified"


def test_consolidation_turns_a_response_out_of_a_contradiction(store):
    """A company's statement that its product is safe made the analyst's finding amber. Marked as a
    response, the pair is no longer a contradiction."""
    from nishpaksh.consolidate import consolidate_story
    from nishpaksh.match import _add_conflict
    sid, c1 = _origin_story(store, [("Outlet A", None, "food analyst"), ("Outlet B", None, "food analyst")])
    c2 = store.insert_returning_id(canonical, dict(story_id=sid, kind="claim", text="Nestle says the whitener is safe", conflicts=[]))
    from nishpaksh.db import claims as Cl
    store.exec(update(canonical).where(canonical.c.id == c1).values(text="An analyst declared the whitener sample unsafe"))
    store.exec(insert(Cl).values(story_id=sid, article_id=1, kind="claim", text="x", stance="attributes",
                                 attributed_to="Nestle", evidence="none", canonical_id=c2))
    _add_conflict(store, c1, c2)

    class R:
        def call(self, tier, prompt, **kw):
            from nishpaksh.router import LLMResult
            return LLMResult("", {"same": [], "conflicts": [[c1, c2]], "names": {}, "speaker": {},
                                  "responses": [[c1, c2]], "role": {}, "name_conflicts": []}, "m", [], 1)
    consolidate_story(store, R(), sid)
    assert store.one(select(canonical.c.conflicts).where(canonical.c.id == c1))["conflicts"] == []
    an = store.one(select(stories.c.analysis).where(stories.c.id == sid))["analysis"]
    assert an["responses"] == [[c1, c2]]


def test_every_request_outcome_is_counted_per_model_and_key():
    class Flaky:
        def __init__(self): self.n = 0
        def generate(self, model, prompt, json_mode, grounded):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("503 UNAVAILABLE: high demand")
            return '{"ok": 1}', [], 5
    f = Flaky()     # one backend behind two keys: the first request is refused, the retry on key 2 works
    r = Router({"t": [dict(id="m1", rpm=100, tpm=10**6, rpd=100)]}, [f, f], max_wait=0.1)
    r.call("t", "x")
    log = {k: dict(v) for k, v in r.call_log.items()}
    assert sum(v.get("ok", 0) for v in log.values()) == 1
    assert sum(v.get("overloaded 5xx", 0) for v in log.values()) == 1


def test_an_outlet_the_story_is_about_may_be_named():
    from nishpaksh.narrative import _validate
    by = {1: _item(1, "Police asked The Wire journalist Mohammad Irfan to show his PIB card")}
    assert _validate({"text": "Police asked The Wire journalist Mohammad Irfan to show his PIB card, reportedly.", "ids": [1]},
                     by, set(), ["The Wire"]) == [1]
    by2 = {1: _item(1, "Police detained two protesters")}
    assert _validate({"text": "The Wire reported that police detained two protesters.", "ids": [1]}, by2, set(), ["The Wire"]) is None


def test_people_are_listed_in_their_fullest_form_for_the_writer():
    from nishpaksh.narrative import _people
    items = [_item(1, "AAP Delhi Chief Saurabh Bharadwaj and Jha were detained by police"),
             _item(2, "Bharadwaj shared a video on X"), _item(3, "The Supreme Court heard the case in New Delhi")]
    out = _people(items)
    assert "AAP Delhi Chief Saurabh Bharadwaj" in out and "Supreme Court" in out


def test_the_paragraph_hedge_keeps_names_and_lowercases_ordinary_words():
    from nishpaksh.narrative import _hedge
    assert _hedge("Such accreditation requires five years.", {"Bharadwaj"}).startswith("According to reports, such")
    assert _hedge("Bharadwaj was detained.", {"Bharadwaj"}) == "According to reports, Bharadwaj was detained."


def test_consolidation_takes_back_a_contradiction_it_does_not_confirm(store):
    from nishpaksh.consolidate import consolidate_story
    from nishpaksh.match import _add_conflict
    from nishpaksh.db import claims as Cl
    sid, c1 = _origin_story(store, [("Outlet A", None, "journalists"), ("Outlet B", None, "journalists")])
    store.exec(update(canonical).where(canonical.c.id == c1).values(text="Three journalists filed complaints"))
    c2 = store.insert_returning_id(canonical, dict(story_id=sid, kind="claim", text="Police received complaints from three journalists", conflicts=[]))
    store.exec(insert(Cl).values(story_id=sid, article_id=1, kind="claim", text="x", stance="attributes",
                                 attributed_to="police", evidence="none", canonical_id=c2))
    _add_conflict(store, c1, c2)

    class R:
        def call(self, tier, prompt, **kw):
            from nishpaksh.router import LLMResult
            return LLMResult("", {"same": [], "conflicts": [], "names": {}, "speaker": {}, "responses": []}, "m", [], 1)
    consolidate_story(store, R(), sid)
    assert store.one(select(canonical.c.conflicts).where(canonical.c.id == c1))["conflicts"] == []


def test_an_overloaded_model_is_dropped_on_every_key_and_the_writer_falls_back():
    """Oct 5-6 2026: every Flash model refused (503) on all three keys for hours, and the refusals
    appear to count against Google's daily limit. A model refusing 3 times in a row is dropped on
    every key for the run; the writer moves down its list to 3.5 Flash-Lite."""
    from collections import Counter
    from nishpaksh.router import OVERLOAD_STREAK

    class Busy:
        def __init__(self): self.calls = Counter()
        def generate(self, model, prompt, json_mode, grounded):
            self.calls[model] += 1
            if "lite" not in model:
                raise RuntimeError("503 UNAVAILABLE: high demand")
            return '{"ok": 1}', [], 5
    b = Busy()
    cfg = {"writer": [dict(id=f"flash-{k}", rpm=100, tpm=10**6, rpd=20) for k in range(3)]
           + [dict(id="gemini-3.5-flash-lite", rpm=100, tpm=10**6, rpd=500)]}
    r = Router(cfg, [b, b, b], max_wait=0.1)
    results = [r.call("writer", "x", max_attempts=12) for _ in range(5)]
    assert all(x.model == "gemini-3.5-flash-lite" for x in results)
    assert all(b.calls[f"flash-{k}"] <= OVERLOAD_STREAK for k in range(3))     # not 3 per key
    assert b.calls["gemini-3.5-flash-lite"] == 5


def test_writer_keeps_flash_and_3_5_flash_lite_essays_only():
    from nishpaksh.compose import _keepable
    para = [[{"text": "x"}]]
    assert _keepable({"model": "gemini-3.8-flash", "paragraphs": para})
    assert _keepable({"model": "gemini-3.5-flash-lite", "paragraphs": para})
    assert not _keepable({"model": "gemini-3.1-flash-lite", "paragraphs": para})
    assert not _keepable({"model": "gemini-2.5-flash-lite", "paragraphs": para})
    assert not _keepable({"model": None, "paragraphs": para})          # stitched by code


def test_health_flags_a_writer_that_wrote_nothing_for_two_runs(tmp_path):
    from nishpaksh.db import diagnostics, utcnow
    from nishpaksh.run import writer_silent
    store = Store(f"sqlite:///{tmp_path}/w.db")
    store.init()
    desk = lambda w: store.exec(insert(diagnostics).values(created_at=utcnow(), kind="desk",  # noqa: E731
                                                            report={"tier_calls": {"writer": w}, "published": []}))
    desk({"overloaded 5xx": 30})
    desk({"overloaded 5xx": 20, "rate limit 429": 2})
    msg = writer_silent(store, {"settled": 0})
    assert msg and "52 calls" in msg and "50 overloaded 5xx" in msg
    desk({"ok": 1, "overloaded 5xx": 3})
    assert writer_silent(store, {"settled": 0}) is None
    # owner: at least one article an hour; two clock hours without one while stories are ready
    assert "no article in the last two clock hours" in writer_silent(store, {"settled": 4})

def test_new_story_waits_for_the_writer_and_is_then_written_once(store):
    """No writer: a new story is not published as stitched sentences; it waits, qualified. When the
    writer is back it is written, once."""
    from nishpaksh import compose
    from nishpaksh.run import run

    class NoWriter(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            if "Write the story below as ONE news article" in prompt or "You are completing a news article" in prompt:
                return '{"paragraphs": []}', [], 10      # nothing usable
            return super().generate(model, prompt, json_mode, grounded)
    _seed(store)
    stats = run(store=store, backend=NoWriter(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    assert store.rows(select(published)) == [] and stats["published"] == 0 and stats["desk"]["tried"] >= 1
    sid = store.rows(select(stories.c.id).where(stories.c.qualifies.is_(True)))[0]["id"]
    stats = run(store=store, backend=FakeBackend(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    assert stats["published"] == 1
    row = store.one(select(published).where(published.c.story_id == sid))
    assert row["version"] == 1 and row["payload_en"]["written_at"]
    assert compose.publish_story(store, _router(store, FakeBackend()), sid) is False
    assert store.one(select(published).where(published.c.story_id == sid)) == row


def test_published_article_is_closed_and_only_its_colours_mature(store):
    """Owner, Oct 5 2026: a published article is never changed; its colours catch up with the 6-hour
    clock, by code. Text, headline, version and time stay; nothing is read or analysed for it."""
    from nishpaksh import editions
    from nishpaksh.run import run
    _seed(store)
    run(store=store, backend=FakeBackend(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    row = store.rows(select(published))[0]
    sid = row["story_id"]
    pe = row["payload_en"]
    collapse = next(i for i in pe["contested"] if i["text"] == "A section of the Kesarganj flyover collapsed")
    assert collapse["verdict"] == "developing"
    said = [x for para in pe["narrative"]["paragraphs"] for x in para if collapse["id"] in x["ids"]]
    text = [x["text"] for para in pe["narrative"]["paragraphs"] for x in para]
    assert said and all(x["class"] == "developing" for x in said if len(x["ids"]) == 1)
    # six hours on, and a new report arrives: it does not touch the article
    _age_rules(store)
    store.exec(update(stories).where(stories.c.id == sid).values(dirty=True))
    calls = []

    class Counting(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            calls.append(prompt[:60])
            return super().generate(model, prompt, json_mode, grounded)
    stats = run(store=store, backend=Counting(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    assert stats["analysed"] == 0 and not calls                      # no model call spent on it
    now = store.one(select(published).where(published.c.story_id == sid))
    assert now["version"] == 1 and now["updated_at"] == row["updated_at"] and now["headline_en"] == row["headline_en"]
    pe2 = now["payload_en"]
    assert [x["text"] for para in pe2["narrative"]["paragraphs"] for x in para] == text
    c2 = next(i for i in pe2["contested"] if i["id"] == collapse["id"])          # same place on the page
    assert c2["verdict"] == "corroborated" and stats["colours_matured"] == 1
    assert all(x["class"] == "established" for para in pe2["narrative"]["paragraphs"] for x in para
               if x["ids"] == [collapse["id"]])
    assert not editions.mature(store, sid)                          # nothing more to change


@pytest.mark.settle
def test_article_waits_for_coverage_to_settle(store):
    """Owner, Oct 5 2026: write after most of the coverage is in. Not while new outlets are still
    turning up (3 quiet hours), but not later than 8 hours after the publishing rule was met."""
    from nishpaksh import editions
    from nishpaksh.run import run
    _seed(store)
    stats = run(store=store, backend=FakeBackend(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    assert stats["qualifying"] == 1 and stats["settled"] == 0 and store.rows(select(published)) == []
    sid = store.rows(select(stories.c.id).where(stories.c.qualifies.is_(True)))[0]["id"]
    met = store.one(select(stories.c.analysis).where(stories.c.id == sid))["analysis"]["edition"]["met_at"]
    assert met
    now = dt.datetime.fromisoformat(met)
    assert not editions.settled(store, sid, now + dt.timedelta(hours=2))      # the last outlet came just now
    assert editions.settled(store, sid, now + dt.timedelta(hours=8, minutes=1))  # the cap
    assert not editions.settled(store, sid, now + dt.timedelta(hours=37))         # old news is not written
    # three quiet hours: written
    store.exec(update(articles).values(fetched_at=NOW - dt.timedelta(hours=4)))
    store.exec(update(stories).where(stories.c.id == sid).values(dirty=True))
    stats = run(store=store, backend=FakeBackend(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    assert stats["settled"] == 1 and stats["published"] == 1


def test_later_reports_of_a_published_story_gather_in_a_follow_up_candidate(store):
    """A report that would have joined a published story goes to a fresh candidate story instead; the
    published story keeps exactly its articles. Later reports join the same candidate."""
    from nishpaksh.db import story_links
    from nishpaksh.editions import link_candidate
    from nishpaksh.stories import group_stories
    for k in range(3):
        _put(store, f"[A] bridge collapse {k}", _vec(0 + k * 0.5, jitter=k), hours_ago=5 - k)
    group_stories(store, _router(store))
    parent = store.rows(select(stories.c.id))[0]["id"]
    store.exec(insert(published).values(story_id=parent, version=1, updated_at=NOW, headline_en="h",
                                        headline_hi="h", payload_en={}, payload_hi={}))
    a = _put(store, "[A] bridge collapse later 1", _vec(0.7, jitter=7), hours_ago=0.5)
    b = _put(store, "[A] bridge collapse later 2", _vec(0.8, jitter=8), hours_ago=0.4)
    group_stories(store, _router(store))
    sid_of = {r["id"]: r["story_id"] for r in store.rows(select(articles.c.id, articles.c.story_id))}
    assert sid_of[a] == sid_of[b] != parent
    assert sum(1 for v in sid_of.values() if v == parent) == 3
    cand = sid_of[a]
    an = store.one(select(stories.c.analysis).where(stories.c.id == cand))["analysis"]
    assert an["edition"]["follows"] == parent
    link_candidate(store, cand)
    assert store.rows(select(story_links.c.parent_id, story_links.c.child_id)) == [{"parent_id": parent, "child_id": cand}]


def _fu_payload(texts, minor=()):
    items = [{"id": n + 1, "text": t, "kind": "event", "minor": t in minor, "role": "core"} for n, t in enumerate(texts)]
    return {"timeline": [], "undated": [], "established": [], "contested": items, "context": []}


def test_follow_up_needs_a_lot_of_new_or_a_major_development(store, monkeypatch):
    """Against its parent: 4+ new statements carried by 3+ independent outlets, or a major development
    carried by 3+; on the parent's own date only a major development carried by 5+."""
    import nishpaksh.verify as V
    from nishpaksh import editions
    parent_items = ["A bridge collapsed in Kesarganj", "Four people died", "Police arrested the site engineer"]
    store.exec(insert(published).values(story_id=1, version=1, updated_at=NOW - dt.timedelta(days=1),
                                        headline_en="Bridge collapses", headline_hi="h",
                                        payload_en=dict(_fu_payload(parent_items), written_at=(NOW - dt.timedelta(days=1)).isoformat()),
                                        payload_hi={}))
    reach = {}
    monkeypatch.setattr(V, "_story_context", lambda store, sid: (None, {}, {}, None, None, {}))
    monkeypatch.setattr(V, "support_summary", lambda cid, *a: {"support_groups": reach.get(cid, [])})

    def check(sid, texts, groups, now=NOW):
        store.exec(insert(stories).values(id=sid, created_at=NOW, updated_at=NOW, dirty=False, qualifies=True, analysis={}))
        reach.clear()
        reach.update({n + 1: g for n, g in enumerate(groups)})
        return editions.follow_up_ok(store, _router(store, FakeBackend()), sid, _fu_payload(texts), [1], now)

    old = parent_items[:2]
    # repeats plus two small new facts: not enough
    assert not check(10, old + ["Rescue work ended", "Traffic was diverted"], [["a", "b", "c"]] * 4)
    # four new facts carried by three independent outlets: a follow-up
    four = ["Rescue work ended", "Traffic was diverted", "An inquiry panel was formed", "The contractor was blacklisted"]
    assert check(11, old + four, [["a"], ["b"], ["c"], ["a"], ["b"], ["c"]])
    # ... but not when only two outlets carry them
    assert not check(12, old + four, [["a"], ["b"], ["a"], ["b"], ["a"], ["b"]])
    # one major development carried by three outlets
    assert check(13, old + ["A court granted bail to the site engineer"], [["a"], ["b"], ["a", "b", "c"]])
    # on the parent's own date: a major development carried by five outlets, nothing less
    same_day = NOW - dt.timedelta(days=1)
    assert not check(14, old + four, [["a", "b", "c", "d", "e"]] * 6, now=same_day + dt.timedelta(minutes=1))
    assert not check(15, old + ["A court granted bail to the site engineer"], [[], [], ["a", "b", "c"]],
                     now=same_day + dt.timedelta(minutes=1))
    assert check(16, old + ["A court granted bail to the site engineer"], [[], [], ["a", "b", "c", "d", "e"]],
                 now=same_day + dt.timedelta(minutes=1))
    # the judgement is kept per statement set: no second model call for the same statements
    calls = []

    class Counting(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            calls.append(1)
            return super().generate(model, prompt, json_mode, grounded)
    reach.update({1: [], 2: [], 3: ["a", "b", "c"]})
    assert editions.follow_up_ok(store, _router(store, Counting()), 13,
                                 _fu_payload(old + ["A court granted bail to the site engineer"]), [1], NOW)
    assert calls == []


def test_published_articles_move_to_the_archive_branch_and_stay_findable(store, tmp_path, monkeypatch):
    """After 3 days a published article is written to the archive branch; only once it is committed
    does it leave the database. Follow-ups still find it there, and read its page."""
    import subprocess
    from nishpaksh import editions, pagearchive, threads
    from nishpaksh.db import story_links
    _seed(store)
    _run_twice(store)
    row = store.rows(select(published))[0]
    sid = row["story_id"]
    arch = tmp_path / "arch"
    subprocess.run(["git", "init", "-q", "-b", "archive", str(arch)], check=True)
    # not yet 3 days old: nothing moves
    assert pagearchive.export_due(store, arch) == []
    later = row["updated_at"] + dt.timedelta(days=3, minutes=1)
    assert pagearchive.export_due(store, arch, now=later) == [sid]
    page = arch / "pages" / f"{sid}.json.gz"
    assert page.exists() and (arch / "index" / f"{row['payload_en']['written_at'][:7]}.jsonl").exists()
    # written but not committed: it stays in the database
    assert pagearchive.delete_archived(store, arch) == 0 and store.rows(select(published))
    git = ["git", "-C", str(arch), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git + ["add", "pages", "index"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "archive"], check=True)
    assert pagearchive.delete_archived(store, arch) == 1
    assert store.rows(select(published)) == [] and store.rows(select(stories.c.id).where(stories.c.id == sid)) == []
    # the page and the index are read back from the archive
    monkeypatch.setenv("NISHPAKSH_ARCHIVE_URL", str(arch))
    pagearchive._page_cache.clear()
    pagearchive._index_cache.clear()
    got = editions.parent_pages(store, [sid])[sid]
    assert got["headline_en"] == row["headline_en"] and got["payload_en"]["narrative"] == row["payload_en"]["narrative"]
    # (Postgres never reuses an id; SQLite would hand the deleted parent's id to the next story)
    child = store.insert_returning_id(stories, dict(id=sid + 1000, created_at=later, updated_at=later, dirty=False,
                                                   qualifies=True, signature="Court grants bail to Kesarganj flyover site engineer"))
    assert [x["story_id"] for x in pagearchive.recent_index(60, later)] == [sid]
    monkeypatch.setattr(threads, "utcnow", lambda: later)
    assert threads.find_parents(store, _router(store, FakeBackend()), child,
                                "Court grants bail to Kesarganj flyover site engineer",
                                "The site engineer arrested after the Kesarganj flyover collapse got bail") == [sid]
    assert store.rows(select(story_links.c.parent_id).where(story_links.c.child_id == child)) == [{"parent_id": sid}]


def test_a_difference_is_a_contradiction_only_if_both_cannot_be_true(store):
    """Oct 6 2026: two statements were shown as disputes because their values differed, though both
    were true: two different steps of one agreement (signed / took effect), and one statement adding
    a number to the same pledge. Every proposed contradiction, from the model or from differing
    numbers, must pass "can both be true?"; unsure keeps both from being established, never amber."""
    from nishpaksh.consolidate import consolidate_story
    from nishpaksh.db import claims as Cl
    from nishpaksh.router import LLMResult
    from nishpaksh.verify import base_verdicts
    sid, c1 = _origin_story(store, [("Outlet A", None, "article"), ("Outlet B", None, "article")])
    texts = {c1: "The bridge was approved in March 2024"}
    store.exec(update(canonical).where(canonical.c.id == c1).values(text=texts[c1]))
    for t in ["The bridge opened to traffic in October 2025",            # another step: both true
              "40 people died in the collapse", "50 people died in the collapse",      # a real conflict
              "The state pledged 100 crore for repairs",
              "The state pledged 100 crore for repairs and 20 new inspectors",          # adds a number
              "The engineer was in charge of the site", "The engineer had left the job"]:   # unsure
        cid = store.insert_returning_id(canonical, dict(story_id=sid, kind="claim", text=t, conflicts=[]))
        store.exec(insert(Cl).values(story_id=sid, article_id=1, kind="claim", text=t, stance="asserts",
                                     attributed_to="article", evidence="none", canonical_id=cid))
        texts[cid] = t
    idx = {t: c for c, t in texts.items()}
    opened, d40, d50 = idx["The bridge opened to traffic in October 2025"], idx["40 people died in the collapse"], idx["50 people died in the collapse"]
    p1, p2 = idx["The state pledged 100 crore for repairs"], idx["The state pledged 100 crore for repairs and 20 new inspectors"]
    e1, e2 = idx["The engineer was in charge of the site"], idx["The engineer had left the job"]
    answers = {frozenset((c1, opened)): "both_true", frozenset((d40, d50)): "cannot_both_be_true",
               frozenset((p1, p2)): "both_true", frozenset((e1, e2)): "unsure"}

    class R:
        def call(self, tier, prompt, **kw):
            import re as _re
            if "can both be true at the same time" in prompt:
                res = []
                for n, a, b in _re.findall(r'(\d+)\. A: "(.*?)" \| B: "(.*?)"', prompt):
                    res.append({"n": int(n), "answer": answers[frozenset((idx[a], idx[b]))]})
                return LLMResult("", {"results": res}, "m", [], 1)
            return LLMResult("", {"same": [[p1, p2]], "names": {}, "speaker": {}, "responses": [],
                                  "conflicts": [[c1, opened, "March 2024 vs October 2025"],
                                                [d40, d50, "40 people vs 50 people"],
                                                [e1, e2, "in charge vs had left"]]}, "m", [], 1)
    consolidate_story(store, R(), sid)
    conf = {r["id"]: r["conflicts"] for r in store.rows(select(canonical.c.id, canonical.c.conflicts).where(canonical.c.story_id == sid))}
    assert conf[c1] == [] and conf[opened] == []                       # two steps: no dispute
    assert conf[d40] == [d50]                                         # the same question, two answers
    assert p1 in conf and p2 in conf and conf[p1] == [] and conf[p2] == []   # kept apart, not a dispute
    assert conf[e1] == [] and conf[e2] == []                           # unsure: not shown as a dispute
    an = store.one(select(stories.c.analysis).where(stories.c.id == sid))["analysis"]
    # "in charge of the site" / "had left the job" are not the same question: the gate clears them
    # before any model question (disputes.py), so they are neither a dispute nor doubtful
    assert an["doubtful_conflicts"] == []
    base_verdicts(store, sid)
    v = {r["id"]: r["verdict"] for r in store.rows(select(canonical.c.id, canonical.c.verdict).where(canonical.c.story_id == sid))}
    assert v[e1] == v[e2] == "unverified"                   # never established while unsettled
    assert v[c1] != "disputed" and v[opened] != "disputed"
    # only the one pair that passed the gate was asked, twice (A/B swapped); the answers are kept
    assert len(an["conflict_checks"]) == 2


def test_frames_compare_statements_by_their_fields():
    """Owner, Oct 6 2026: compare statements field by field (who / action / what / value), words reduced
    to their roots, instead of a model judging two sentences each time."""
    from nishpaksh.frames import compare, normalize as n
    f = lambda **k: n(k)   # noqa: E731
    # two different steps of one thing: different questions
    assert compare(f(who="India and EFTA", action="sign", what="the trade agreement", value="March 2024"),
                   f(who="The trade agreement", action="take effect", value="October 2025")) == "different"
    # a target announced by one side, a pledge by the other: different questions
    assert compare(f(who="Prime Minister", action="set", what="investment target", value="$100 billion"),
                   f(who="EFTA states", action="invest", what="India", value="$100 billion")) == "different"
    # a rounded figure is the same answer; a bound agrees with a larger figure
    assert compare(f(who="voters", action="cast", what="valid votes", value="125.27 million"),
                   f(who="voters", action="cast", what="votes", value="about 125 million")) == "same"
    assert compare(f(who="people", action="die", what="collapse", value="at least 40"),
                   f(who="people", action="died", what="the collapse", value="50")) == "same"
    # the same question, two answers: the only contradiction
    assert compare(f(who="people", action="die", what="collapse", value="40"),
                   f(who="people", action="die", what="collapse", value="50")) == "conflict"
    assert compare(f(who="Police", action="arrest", what="site engineer"),
                   f(who="Uttar Pradesh Police", action="arrested", what="the engineer", negated=True)) == "conflict"
    # roots: arrested / arrests, crore / million read as numbers
    assert compare(f(who="police", action="arrests", what="two men", value="2"),
                   f(who="Police", action="arrested", what="men", value="two")) in ("same", "compatible")
    assert compare(f(who="state", action="allot", what="funds", value="1 crore"),
                   f(who="state", action="allot", what="funds", value="10 million")) == "same"
    # no values: never a dispute, but not merged on fields alone
    assert compare(f(who="Police", action="say", what="the accused"), f(who="Police", action="say", what="the accused")) == "compatible"
    # same kind of event on different dates: maybe two events, never merged, never a dispute
    assert compare(f(who="Police", action="arrest", what="protesters", value="12"),
                   f(who="Police", action="arrest", what="protesters", value="12"),
                   {"start": "2026-10-01T10:00", "end": "2026-10-01T12:00"},
                   {"start": "2026-10-03T10:00", "end": "2026-10-03T12:00"}) == "unsure"
    assert compare(None, f(who="x", action="y")) is None


def test_arrival_joins_only_plainly_identical_wording_and_makes_no_disputes(store):
    """On arrival a statement joins one whose words are plainly the same; disputes and the same fact in
    other words are decided once, in the story review (owner, Oct 7 2026)."""
    from nishpaksh.db import claims as Cl
    from nishpaksh.match import match_story
    sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, dirty=True, qualifies=False))
    arts = [store.insert_returning_id(articles, dict(url=f"https://o{k}.in/x", outlet=f"O{k}", title="t", text="b",
                                                     published_at=NOW, fetched_at=NOW, story_id=sid)) for k in range(4)]
    rows = ["Forty people died when the bridge fell", "40 people died when the bridge fell",
            "50 people died when the bridge fell", "The trade agreement was signed in March 2024"]
    for k, text in enumerate(rows):
        store.exec(insert(Cl).values(story_id=sid, article_id=arts[k], local_id="c1", kind="claim", text=text,
                                     stance="asserts", attributed_to="article", evidence="none",
                                     rel={"frame": {"who": "people", "action": "die", "what": "bridge", "value": text[:2]}}))
    match_story(store, None, sid)
    cid = {r["text"]: r["canonical_id"] for r in store.rows(select(Cl.c.text, Cl.c.canonical_id))}
    assert cid[rows[0]] == cid[rows[1]]                  # "forty" = 40, the same words: one statement
    assert cid[rows[2]] != cid[rows[0]]
    assert all(r["conflicts"] == [] for r in store.rows(select(canonical.c.conflicts)))   # no dispute on arrival


def test_one_gate_for_disputes():
    """A dispute is the same question with a different answer; code clears the known non-disputes;
    a later figure is an update; two "cannot" answers make amber (owner, Oct 7 2026)."""
    import datetime as dt_
    from nishpaksh.disputes import candidate, is_update, judge
    assert candidate("17 of the total 19 crew members are Indian nationals.",
                     "11 of the 12 injured crew members are Indian nationals.") is None
    assert candidate("At least 40 people died in the collapse.", "50 people died in the collapse.") is None
    assert candidate("About 125 million valid votes were cast.", "125.27 million valid votes were cast.") is None
    assert candidate("The trade agreement was signed in March 2024.", "The trade agreement entered into force in October 2025.") is None
    assert candidate("Forty people died when the bridge collapsed.", "Fifty people died when the bridge collapsed.") == "number"
    assert candidate("Police said 40 people died in the collapse.", "The family said 50 people died in the collapse.") == "number"
    assert candidate("Police arrested the engineer.", "Police did not arrest the engineer.") == "negation"
    assert candidate("The meeting was held on Friday.", "The meeting was held on Saturday.") == "date"
    t0 = dt_.datetime(2026, 10, 7, 6)
    assert is_update([t0], [t0 + dt_.timedelta(hours=3)]) == 1 and is_update([t0], [t0]) == 0
    texts = {1: "40 people died in the collapse", 2: "50 people died in the collapse",
             3: "Police arrested the engineer", 4: "Police did not arrest the engineer"}
    answers = {(1, 2): "cannot_both_be_true", (2, 1): "cannot_both_be_true",
               (3, 4): "cannot_both_be_true", (4, 3): "both_true"}

    class R:
        def call(self, tier, prompt, **kw):
            import re as _re
            from nishpaksh.router import LLMResult
            inv = {v: k for k, v in texts.items()}
            res = [{"n": int(n), "answer": answers[(inv[a], inv[b])]}
                   for n, a, b in _re.findall(r'(\d+)\. A: "(.*?)" \| B: "(.*?)"', prompt)]
            return LLMResult("", {"results": res}, "m", [], 1)
    times = {1: [t0], 2: [t0], 3: [t0], 4: [t0]}
    disputes, doubtful, updates = judge(R(), texts, times, set(), set(), {})
    assert disputes == {(1, 2)} and doubtful == {3, 4} and updates == {}       # the two answers disagreed: doubtful
    times = {1: [t0], 2: [t0 + dt_.timedelta(hours=4)], 3: [t0], 4: [t0]}
    disputes, doubtful, updates = judge(R(), texts, times, set(), set(), {})
    assert updates == {1: 2} and (1, 2) not in disputes                         # the later figure updates the earlier
def _story_with_sources(store, title, n_outlets, hours_ago=2.0):
    sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, dirty=True, qualifies=False,
                                                  signature=title))
    for k in range(n_outlets):
        store.exec(insert(articles).values(url=f"https://{abs(hash(title)) % 10**6}-{k}.example/{k}", outlet=f"{title} outlet {k}",
                                           lang="en", title=f"{title} ({k})", text="t " * 300, text_source="full",
                                           published_at=NOW - dt.timedelta(hours=hours_ago), fetched_at=NOW - dt.timedelta(hours=hours_ago),
                                           story_id=sid, extract_failures=0))
    return sid


def test_only_the_best_ranked_stories_are_prepared(store, monkeypatch):
    """Owner, Oct 6 2026: rank stories from their headlines, read and analyse only the best (the
    writer publishes 1-2 an hour; reading every story spent ~160 calls per published article)."""
    import nishpaksh.priority as P
    from nishpaksh.extract import select_for_extraction
    monkeypatch.setattr(P, "SETTINGS", dataclasses.replace(P.SETTINGS, prep_queue=2))
    big = _story_with_sources(store, "Parliament passes bill", 5)
    mid = _story_with_sources(store, "State cabinet meets", 3)
    small = _story_with_sources(store, "Town fair opens", 3)
    lone = _story_with_sources(store, "One outlet scoop", 1)
    horo = _story_with_sources(store, "Aaj ka rashifal horoscope", 4)

    class Rater(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            if "each shown by the headlines" in prompt:
                import re as _re
                res = []
                for n, heads in _re.findall(r"^(\d+)\. (.*)$", prompt, _re.M):
                    score = 5 if "Parliament" in heads else 4 if "cabinet" in heads else 2
                    res.append({"n": int(n), "score": score, "filler": "horoscope" in heads})
                return json.dumps({"results": res}), [], 50
            return super().generate(model, prompt, json_mode, grounded)
    assert P.rank_new(store, _router(store, Rater())) == 4        # the one-outlet story is not rated
    q = P.queue(store)
    assert q == [big, mid]                                       # best first; filler and the rest wait
    read = {a["story_id"] for a in select_for_extraction(store, set(q))}
    assert read == {big, mid}
    # nothing is rated twice unless its coverage grows by two sources
    assert P.rank_new(store, _router(store, Rater())) == 0
    for k in range(2):
        store.exec(insert(articles).values(url=f"https://late{k}.example/x", outlet=f"Late {k}", lang="en",
                                           title="Town fair opens (late)", text="t " * 300, text_source="full",
                                           published_at=NOW, fetched_at=NOW, story_id=small, extract_failures=0))
    assert P.rank_new(store, _router(store, Rater())) == 1
    assert lone not in P.queue(store, size=10) and horo not in P.queue(store, size=10)


def test_the_desk_publishes_at_most_two_an_hour(store, monkeypatch):
    """Owner, Oct 6 2026: at least one article an hour, at most two."""
    from nishpaksh import desk
    from nishpaksh.run import run
    _seed(store)
    run(store=store, backend=FakeBackend(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    at = store.rows(select(published.c.updated_at))[0]["updated_at"]     # the test must not depend on the clock hour
    assert desk.published_this_hour(store, at) == 1
    for k in range(2):
        store.exec(insert(published).values(story_id=900 + k, version=1, updated_at=at, headline_en="h",
                                            headline_hi="h", payload_en={}, payload_hi={}))
    assert desk.published_this_hour(store, at) == 3
    stats = desk.work(store, _router(store, FakeBackend()), now=at)
    assert stats["tried"] == 0 and stats["published"] == []


def test_one_outlet_lines_are_written_in_purple():
    """Owner, Oct 7 2026: a line only one outlet reports goes into the article too, shown purple ("one
    outlet only"): it may be an exclusive, or wrong. Never as plain fact."""
    from nishpaksh.narrative import _finish, _statement_line, shade
    one = dict(_item(1, "The minister met the protesters on Monday"), n_sources=1)
    three = dict(_item(2, "Police detained 40 protesters"), n_sources=3)
    est = dict(_item(3, "The protest began at noon", verdict="corroborated"), n_sources=4)
    dis = dict(_item(4, "Ten people were injured", verdict="disputed"), n_sources=1)
    assert [shade(i) for i in (one, three, est, dis)] == ["single", "unverified", "corroborated", "disputed"]
    assert "ONE OUTLET ONLY" in _statement_line(one)
    payload = _payload([one, three, est, dis])
    by = {i["id"]: i for i in (one, three, est, dis)}
    nar = _finish(payload, [[{"text": "The minister met the protesters on Monday.", "ids": [1]}],
                            [{"text": "The protest began at noon.", "ids": [3]},
                             {"text": "The minister met the protesters on Monday after the protest began.", "ids": [1, 3]}],
                            [{"text": "Police detained 40 protesters.", "ids": [2]}]], [], by, {"model": "m"})
    classes = [[s["class"] for s in p] for p in nar["paragraphs"]]
    assert classes == [["single"], ["established", "single"], ["unverified"]]
    # no hedge words (owner, Oct 7 2026, option A): the colour says how well each line is supported
    assert nar["paragraphs"][0][0]["text"] == "The minister met the protesters on Monday."
    assert not any("report" in s["text"].lower() for p in nar["paragraphs"] for s in p)


def test_the_writer_is_asked_for_every_statement_including_one_outlet_lines():
    from nishpaksh.narrative import FILL_PROMPT, WRITER_PROMPT
    assert "Use EVERY statement" in WRITER_PROMPT and "may be left out" not in WRITER_PROMPT
    assert "every one must be used" in FILL_PROMPT


def _sec_items():
    mk = lambda i, text, **k: dict(_item(i, text, k.pop("verdict", "unverified"), k.pop("speaker", None)),  # noqa: E731
                                   n_sources=k.pop("n_sources", 3), n_articles=3, minor=False, role=k.pop("role", "core"),
                                   kind=k.pop("kind", "event"), **k)
    return [mk(1, "A bridge collapsed in Kesarganj", verdict="corroborated"),
            mk(2, "Rescue teams reached the site at noon"),
            mk(3, "The repair budget was 40 crore rupees", kind="claim", frame={"value": "40 crore"}),
            mk(4, "The contractor used substandard material", kind="claim", speaker="Residents"),
            mk(5, "The engineer had left the job", n_sources=1),
            mk(6, "The bridge was built in 1990", role="background"),
            mk(7, "A probe report is due on Friday", role="next")]


def test_every_statement_has_one_section_like_an_explainer():
    """Owner, Oct 7 2026: the article in sections that cover everything known, with headings."""
    from nishpaksh.narrative import assign_sections
    sec = assign_sections(_sec_items())
    # owner, Oct 7 2026: no disputed section; a one-outlet line stays with its subject (purple marks it)
    assert sec == {1: "happened", 2: "happened", 3: "numbers", 4: "say", 5: "happened", 6: "background", 7: "next"}


def test_sections_left_short_are_filled_a_few_at_a_time():
    """The first draft carries two statements; the fill pass asks for the missing ones, one group of
    sections per call, and the article ends with every statement, each under its heading."""
    from nishpaksh.narrative import essay_ok, write_narrative
    items = _sec_items()
    payload = _payload(items)
    payload["context"] = [i for i in items if i["role"] != "core"]
    payload["contested"] = [i for i in payload["contested"] if i["role"] == "core"]
    calls = []

    class Short(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            if "Write the story below as ONE news article" in prompt:
                calls.append("draft")
                return json.dumps({"sections": [{"key": "news", "paragraphs": [[{"text": "A bridge collapsed in Kesarganj.", "ids": [1]}]]},
                                                {"key": "happened", "paragraphs": [[{"text": "Reportedly, rescue teams reached the site at noon.", "ids": [2]}]]}]}), [], 100
            if "You are completing a news article written in sections" in prompt:
                calls.append(prompt.split("Sections to rewrite:")[1].split("\n")[0].strip())
            return super().generate(model, prompt, json_mode, grounded)
    nar = write_narrative(_router(None, Short()), payload, set())
    # the draft, then one call per group of sections that missed statements
    assert calls == ["draft", "news, happened, numbers", "say", "background, next"]
    assert set(nar["covers"]) == {1, 2, 3, 4, 5, 6, 7} and essay_ok(nar, payload)
    keys = nar["section_keys"]
    assert len(keys) == len(nar["paragraphs"]) and keys[0] == "news"
    # context before the full account (owner, Oct 7 2026)
    assert [k for k in dict.fromkeys(keys)] == ["news", "background", "happened", "numbers", "say", "next"]


def test_a_short_article_is_finished_next_time_not_thrown_away(store):
    """Owner, Oct 7 2026: an article that falls short of the 85% bar is kept as a draft; the next try
    fills in what is missing instead of writing a new article from nothing."""
    from nishpaksh import compose
    from nishpaksh.run import run
    _seed(store)
    calls = []

    class Stingy(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            if "Write the story below as ONE news article" in prompt:
                calls.append("draft")
                first = re.search(r'#(\d+) ESTABLISHED \| "(.*?)"', prompt) or re.search(r'#(\d+) [A-Z ]+ \| "(.*?)"', prompt)
                return json.dumps({"sections": [{"key": "news", "paragraphs": [[{"text": f"Reportedly, {first.group(2)[0].lower() + first.group(2)[1:]}.",
                                                                                  "ids": [int(first.group(1))]}]]}]}), [], 100
            if "You are completing a news article written in sections" in prompt:
                calls.append("fill")
                return '{"sections": []}', [], 10         # the fills fail this time
            return super().generate(model, prompt, json_mode, grounded)
    run(store=store, backend=Stingy(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    assert store.rows(select(published)) == [] and calls[0] == "draft"
    tried = [r for r in store.rows(select(stories.c.id, stories.c.analysis)) if "writer_failures" in (r["analysis"] or {})]
    sid = tried[0]["id"]                                          # the story the writer tried
    draft = tried[0]["analysis"]["writer_draft"]
    assert draft["sections"] and draft["covers"] == 1

    calls.clear()

    class Fills(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            if "Write the story below as ONE news article" in prompt:
                calls.append("draft")
            return super().generate(model, prompt, json_mode, grounded)
    assert compose.publish_story(store, _router(store, Fills()), sid)
    assert "draft" not in calls                                   # continued, not started again
    an = store.one(select(stories.c.analysis).where(stories.c.id == sid))["analysis"]
    assert "writer_draft" not in an
    nar = store.one(select(published).where(published.c.story_id == sid))["payload_en"]["narrative"]
    assert nar["resumed"] and "drafted" not in nar

    # a statement the draft cites is merged into another later: the sentence follows it, not dropped
    from nishpaksh.db import canonical
    from nishpaksh.match import _merge
    cited = next(x for _, para in draft["sections"] for s in para for x in s["ids"])
    assert str(cited) in draft["anchors"]
    other = next(r["id"] for r in store.rows(select(canonical.c.id).where(canonical.c.story_id == sid)) if r["id"] != cited)
    _merge(store, cited, other)
    payload = {"timeline": [], "undated": [{"id": other, "kind": "event"}], "contested": [], "established": []}
    moved = compose._remap_draft(store, draft, payload)
    assert [x for _, para in moved["sections"] for s in para for x in s["ids"]] == [other]


def test_one_spelling_per_name_in_an_article():
    """Oct 7 2026: one page spelt the pilot Machhar, Machar, Matchar and Machchhar."""
    from nishpaksh.spelling import unify_article, unify_payload
    a = {"id": 1, "text": "Captain Smit Machhar was attacked.", "n_articles": 4, "speaker": None}
    b = {"id": 2, "text": "Smit Matchar landed the plane.", "n_articles": 1, "speaker": "Captain Machchhar"}
    c = {"id": 3, "text": "Kumari and Kumar spoke.", "n_articles": 1}
    payload = {"timeline": [[a]], "undated": [], "established": [a], "contested": [b], "context": [c]}
    unify_payload(payload)
    assert b["text"] == "Smit Machhar landed the plane." and b["speaker"] == "Captain Machhar"
    assert c["text"] == "Kumari and Kumar spoke."                    # two names, not two spellings
    payload.update(headline="Pilot Machar praised", narrative={"paragraphs": [[{"text": "Captain Matchar flew."}]]})
    unify_article(payload)
    assert payload["headline"] == "Pilot Machhar praised"
    assert payload["narrative"]["paragraphs"][0][0]["text"] == "Captain Machhar flew."


def test_same_fact_in_other_words_is_asked_twice_and_merged_only_on_two_yeses():
    """Oct 7 2026, story 13809: "take charge" / "take over" frames made four outlets' one fact four
    one-outlet lines. Code picks the pairs, the model answers same/different twice (A/B swapped)."""
    from nishpaksh import match
    class Flip(FakeBackend):                   # says "same" the first time, "different" when swapped
        calls = 0
        def generate(self, model, prompt, json_mode, grounded):
            if "do A and B report the SAME single fact" in prompt:
                Flip.calls += 1
                return json.dumps({"results": [{"n": 1, "answer": "same" if Flip.calls == 1 else "different"}]}), [], 10
            return super().generate(model, prompt, json_mode, grounded)
    import nishpaksh.db as db
    store = db.Store("sqlite://")
    store.init()
    assert match.same_facts(_router(store, Flip()), [("He will take charge.", "He took charge.")]) == [False]
    assert Flip.calls == 2
    assert match.same_facts(_router(store, FakeBackend()),
                            [("Dixit will take charge as Chief of Air Staff.", "Dixit will take charge as the Chief of Air Staff on October 31.")]) == [True]


def test_two_names_in_one_sentence_are_two_people():
    """Oct 7 2026, story 11569: two arrested men were written as one man "named in reports as" the other."""
    from nishpaksh.consolidate import named_together
    names = ["Abhishek Kumar Singh", "Ritesh Kumar Singh"]
    assert named_together(names, ["Ritesh Kumar Singh is accused of assisting Abhishek Kumar Singh in espionage."])
    assert not named_together(names, ["Abhishek Kumar Singh alias Ritesh Kumar Singh was arrested."])
    assert not named_together(names, ["Abhishek Kumar Singh (Ritesh Kumar Singh) was arrested."])
    assert not named_together(names, ["Abhishek Kumar Singh was arrested.", "Ritesh Kumar Singh was arrested."])



def test_no_hedge_words_in_the_article_but_disputes_keep_whose():
    """Owner, Oct 7 2026, option A: one author's voice; the colour carries how well each line is supported."""
    from nishpaksh.narrative import _one_hedge
    assert _one_hedge("According to reports, the police detained 40 people.") == "The police detained 40 people."
    assert _one_hedge("The STF reportedly seized a phone.") == "The STF seized a phone."
    assert _one_hedge("Two men were arrested, reports said.") == "Two men were arrested."
    assert _one_hedge("It is reported that the bridge fell.") == "The bridge fell."
    assert _one_hedge("He was arrested on Friday, according to media reports.") == "He was arrested on Friday."
    assert _one_hedge("The accused allegedly sold the data.") == "The accused allegedly sold the data."
    assert _one_hedge("Police said the bridge fell.") == "Police said the bridge fell."


def test_house_style_surnames_and_said_chains():
    """Owner, Oct 7 2026: read like one author: full name once, then the surname; no "He said ... He
    added ..." chains. Places, bodies and shared surnames are left alone."""
    from nishpaksh.style import Refs, people, shorten_names, vary_attribution
    from nishpaksh.voice import roles
    P = [[{"text": "Assam Chief Minister Himanta Biswa Sarma said two men were arrested in East Champaran."},
          {"text": "He said that the two were passing information to Pakistan."},
          {"text": "He added that central agencies will take over the probe."}],
         [{"text": "Assam Chief Minister Himanta Biswa Sarma held a press conference in Tel Aviv."},
          {"text": "Police said Abhishek Kumar Singh and retired jawan Ritesh Kumar Singh were held in Tel Aviv."},
          {"text": "Ritesh Kumar Singh is accused of assisting Abhishek Kumar Singh."}]]
    text = " ".join(s["text"] for p in P for s in p)
    names = people(text)
    refs = Refs(names, {}, roles(text, names))      # no outlet calls Sarma "he": no pronoun from code
    shorten_names(P)
    vary_attribution(P, refs)
    t = [s["text"] for p in P for s in p]
    assert t[1] == "The two were passing information to Pakistan, the chief minister said."
    assert t[2] == "Central agencies will take over the probe, Sarma added."
    assert t[3] == "Sarma held a press conference in Tel Aviv."
    assert t[5] == "Ritesh Kumar Singh is accused of assisting Abhishek Kumar Singh."


def test_desk_tries_count_only_stories_the_writer_was_asked_to_write(store, monkeypatch):
    """Oct 7 2026: three follow-up candidates refused before writing used up the desk's five tries,
    two runs in a row, and nothing was written."""
    from nishpaksh import compose, desk, verify
    queue = list(range(1, 11))
    monkeypatch.setattr(desk, "ready", lambda store, now=None: queue)
    monkeypatch.setattr(verify, "base_verdicts", lambda store, sid: None)

    def fake(store, router, sid):
        return compose._outcome(sid, "not a follow-up yet" if sid <= 6 else "published")
    monkeypatch.setattr(compose, "publish_story", fake)
    stats = desk.work(store, router=None, now=dt.datetime(2026, 10, 7, 13, 50))
    assert stats["published"] == [7, 8] and stats["skipped"] == {"not a follow-up yet": 6}


def test_models_that_refused_last_desk_run_are_skipped_once(store):
    from nishpaksh import desk
    from nishpaksh.db import diagnostics, insert
    now = dt.datetime(2026, 10, 7, 15, 45)
    assert desk.refused_last_run(store, now) == set()
    store.exec(insert(diagnostics).values(created_at=now - dt.timedelta(minutes=60), kind="desk",
                                          report={"dropped": ["gemini-3.8-flash"], "skipping": []}))
    assert desk.refused_last_run(store, now) == {"gemini-3.8-flash"}
    # a run that skipped them drops nothing, so the run after tries them again
    store.exec(insert(diagnostics).values(created_at=now, kind="desk", report={"dropped": [], "skipping": ["gemini-3.8-flash"]}))
    assert desk.refused_last_run(store, now + dt.timedelta(minutes=60)) == set()
    assert desk.refused_last_run(store, now + dt.timedelta(hours=3)) == set()


def test_a_page_about_its_own_publisher_is_not_the_story():
    """Oct 7 2026, story 12687: "Hindustan was established in 1936 ... second most widely read Hindi newspaper"."""
    from nishpaksh.compose import drop_outlet_self_talk
    own = {"id": 1, "text": "Hindustan was established in 1936 and is the second most widely read Hindi newspaper in India.",
           "sources": [{"outlet": "Hindustan"}]}
    news = {"id": 2, "text": "CAQM deployed flying squads in 34 districts.", "sources": [{"outlet": "Hindustan"}]}
    quoted = {"id": 3, "text": "The Hindustan Times newspaper was sued by the minister.",
              "sources": [{"outlet": "NDTV"}, {"outlet": "Aaj Tak"}]}
    p = {"sources": [{"outlet": "Hindustan"}], "undated": [own, news], "established": [quoted], "contested": [],
         "context": [], "timeline": []}
    assert drop_outlet_self_talk(p) == 1 and p["undated"] == [news] and p["established"] == [quoted]


def test_one_structure_decides_the_same_fact_on_the_words():
    """Owner, Oct 7 2026 (story 13652): identical lines stayed two because two readings labelled them
    differently; the words now decide, labels never veto; a detailed line covers a short one."""
    from nishpaksh.relate import group, relate
    T = {1: "11 of the 12 injured crew members are Indian nationals.",
         2: "11 of the 12 injured crew members are Indian nationals.",
         3: "12 crew members, including 11 Indian nationals, were injured in the attack.",
         4: "Injured crew members were evacuated and are receiving medical treatment in Khasab, Oman.",
         5: "Injured crew members were evacuated to Khasab, Oman, for medical treatment.",
         6: "17 of the total 19 crew members are Indian nationals.",
         7: "The ship had a total crew of 19, of whom 17 were Indian nationals."}
    same, covered, ask, ask_cover = group(T)
    assert (1, 2) in same and (4, 5) in same and covered.get(1) == 3 and (6, 7) in ask
    assert relate("Police arrested Ravi Kumar.", "Police did not arrest Ravi Kumar.") == "different"
    assert relate("12 crew members were injured.", "12 crew members were killed.") == "ask"     # never by code
    # a detailed line that adds a year or "another" may be a different event: the model is asked
    assert relate("Police arrested Ravi Kumar in 2019 in another case.", "Police arrested Ravi Kumar.") in ("ask", "ask_a_covers_b")
    assert relate("Police arrested Ravi Kumar in 2019.", "Police arrested Ravi Kumar in 2024.") == "different"
    # a date is not a figure: the same call told with and without its date is asked, not kept apart
    assert relate("Modi called for strict global regulations to address deepfakes and cyber fraud.",
                  "Modi called for a global framework to tackle cyber frauds and deepfakes on Thursday, October 8, 2026.") == "ask"



def test_the_written_article_never_says_the_same_thing_twice():
    from nishpaksh.narrative import _drop_repeats
    by = {i: {"verdict": "unverified", "conflicts_with": []} for i in (1, 2, 3, 4)}
    paras = [[{"text": "12 crew members, including 11 Indian nationals, were injured in the attack.", "ids": [1]}],
             [{"text": "11 of the 12 injured crew members are Indian nationals.", "ids": [2]}],
             [{"text": "Injured crew members were evacuated to Khasab, Oman, for medical treatment.", "ids": [3]},
              {"text": "Injured crew members were evacuated to Khasab, Oman, for medical treatment.", "ids": [4]}]]
    out, kept = _drop_repeats(paras, by)
    assert kept == [0, 2] and out[0][0]["ids"] == [1, 2] and len(out[1]) == 1 and out[1][0]["ids"] == [3, 4]


def test_a_covered_line_is_folded_into_the_detailed_one_without_lending_it_colour():
    from nishpaksh.compose import fold_covered
    # equally supported: the short line folds into the detailed one, which keeps its own colour
    big = {"id": 1, "verdict": "unverified", "n_sources": 1, "sources": [{"url": "a"}], "text": "x"}
    small = {"id": 2, "verdict": "unverified", "n_sources": 1, "sources": [{"url": "b"}], "text": "y"}
    p = {"timeline": [[big, small]], "undated": [], "established": [], "contested": [], "context": []}
    assert fold_covered(p, {"2": 1}) == 1
    assert p["timeline"] == [[big]] and [s["url"] for s in big["sources"]] == ["a", "b"]
    # the short line better supported: both kept, written as one sentence in two parts
    big = {"id": 1, "verdict": "unverified", "n_sources": 1, "sources": [{"url": "a"}], "text": "x"}
    small = {"id": 2, "verdict": "corroborated", "n_sources": 4, "sources": [{"url": "b"}], "text": "y"}
    p = {"timeline": [[big, small]], "undated": [], "established": [], "contested": [], "context": []}
    assert fold_covered(p, {"2": 1}) == 0 and big["adds_to"] == 2 and big["verdict"] == "unverified"


def test_a_sentence_in_parts_colours_each_part_and_never_paints_a_detail_green():
    """Owner, Oct 7 2026: the well-supported fact green, the detail one outlet reports its own colour.
    Each part is checked on its own: a number the part's statements do not have takes the parts away."""
    from nishpaksh.narrative import _check_paragraphs, _finish
    est = {"id": 1, "kind": "event", "text": "12 crew members were injured in the attack", "verdict": "corroborated",
           "n_sources": 4, "sources": [{"url": "a"}], "conflicts_with": []}
    one = {"id": 2, "kind": "claim", "text": "11 of the injured crew members are Indian nationals", "verdict": "unverified",
           "n_sources": 1, "sources": [{"url": "b"}], "conflicts_with": []}
    by = {1: est, 2: one}
    good = {"parts": [{"text": "12 crew members were injured in the attack,", "ids": [1]},
                      {"text": "11 of them Indian nationals.", "ids": [2]}]}
    bad = {"parts": [{"text": "12 crew members, 11 of them Indian, were injured,", "ids": [1]},
                     {"text": "in the attack.", "ids": [2]}]}
    payload = {"sources": [{"url": "a"}, {"url": "b"}], "timeline": [], "undated": [est, one],
               "established": [], "contested": []}
    for sent, want in ((good, ["established", "single"]), (bad, None)):
        paras, failed, rejected = _check_paragraphs([[sent]], by, set(), [])
        s1 = _finish(payload, paras, [], by, {"model": "m"})["paragraphs"][0][0]
        assert s1["class"] == "single"                 # the sentence as a whole: its weakest colour
        if want:
            assert [p["class"] for p in s1["parts"]] == want
        else:
            assert "parts" not in s1                   # "11" sat in the part citing the other statement


def test_a_paragraph_naming_one_speaker_every_line_is_rewritten_only_if_nothing_is_lost():
    """Owner, Oct 8 2026 (story 14231): 18 lines of "..., according to Modi" in one paragraph."""
    from nishpaksh.narrative import _cohere
    from nishpaksh.router import LLMResult
    by = {i: {"id": i, "kind": "claim", "verdict": "unverified", "speaker": "Narendra Modi", "sources": [],
              "conflicts_with": [], "n_sources": 2, "text": t} for i, t in
          ((1, "Deepfakes have become a big challenge"), (2, "Digital threats have crossed national boundaries"),
           (3, "Technology can achieve scale only when it gains people's trust"))}
    para = [{"text": "Deepfakes have become a big challenge, according to Modi.", "ids": [1]},
            {"text": "Digital threats have crossed national boundaries, according to Modi.", "ids": [2]},
            {"text": "Technology can achieve scale only when it gains people's trust, Modi said.", "ids": [3]}]

    class Good:
        def call(self, tier, prompt, **kw):
            return LLMResult("", {"paragraphs": [[
                {"text": "Prime Minister Narendra Modi said deepfakes have become a big challenge.", "ids": [1]},
                {"text": "Digital threats have crossed national boundaries, the prime minister said.", "ids": [2]},
                {"text": "Technology can achieve scale only when it gains people's trust, Modi added.", "ids": [3]}]]}, "m", [], 1)

    class Same:          # passes every check and loses nothing, but fixes nothing (story 13107)
        def call(self, tier, prompt, **kw):
            return LLMResult("", {"paragraphs": [[s_] for s_ in para]}, "m", [], 1)

    class Lossy:
        def call(self, tier, prompt, **kw):
            return LLMResult("", {"paragraphs": [[
                {"text": "Prime Minister Narendra Modi said deepfakes have become a big challenge.", "ids": [1]}]]}, "m", [], 1)
    out, keys, n = _cohere(Good(), [para], ["say"], by, set(), [])
    assert n == 1 and "the prime minister said" in out[0][1]["text"] and keys == ["say"]
    out, keys, n = _cohere(Same(), [para], ["say"], by, set(), [])
    assert n == 1 and out == [para]                        # split into three, still Modi x3: as written
    out, keys, n = _cohere(Lossy(), [para], ["say"], by, set(), [])
    assert out == [para]                                   # it lost two statements: kept as written


def test_feed_writes_the_reading_site_files(store, tmp_path):
    """nishpaksh_version_1.0 (owner, Oct 8 2026): the live articles become ready-made files for the
    site: a card per article in each language (headline, opening sentences with their colours, the
    colour bar's counts) and one slim page per article."""
    import json
    from nishpaksh import feed
    _seed(store)
    _run_twice(store)
    row = store.rows(select(published))[0]
    out = tmp_path / "feed"
    (out / "story").mkdir(parents=True)
    (out / "story" / "999999.json").write_text("{}")   # an article no longer live is removed
    assert feed.export(store, out) == 1
    assert not (out / "story" / "999999.json").exists()
    for lang in ("en", "hi"):
        data = json.loads((out / f"{lang}.json").read_text(encoding="utf-8"))
        (c,) = data["stories"]
        assert c["id"] == row["story_id"] and c["h"] and c["paras"] and c["paras"][0][0]["t"]
        assert sum(c["bar"].values()) == len(feed._sentence_classes(row["payload_en"]["narrative"]["paragraphs"]))
        assert c["cat"]["primary"] == ["life", "justice"]
        assert data["sections"]["primary"]["life"] == ("Life" if lang == "en" else "जीवन")
    page = json.loads((out / "story" / f"{row['story_id']}.json").read_text(encoding="utf-8"))
    nar = page["payload_en"]["narrative"]
    assert nar["paragraphs"] == row["payload_en"]["narrative"]["paragraphs"] and nar["sources"]
    assert set(page["payload_en"]["narrative"]) == {"paragraphs", "section_keys", "sources"}
    assert page["headline_hi"] == row["headline_hi"]
    # the next export reads nothing it already has (egress): unchanged articles come from the manifest
    reads = []
    real_rows = store.rows
    store.rows = lambda stmt: reads.append(str(stmt)) or real_rows(stmt)
    try:
        assert feed.export(store, out) == 1
    finally:
        store.rows = real_rows
    published_reads = [q for q in reads if "FROM published" in q]
    assert len(published_reads) == 1 and "payload_en" in published_reads[0]   # only the cheap list (sqlite: payloads for md5)
    assert not any("payload" in q for q in reads if "FROM published" not in q)  # pictures: links only
    assert json.loads((out / "en.json").read_text(encoding="utf-8"))["stories"][0]["id"] == row["story_id"]
    assert feed.bar_counts([[{"class": "single"}, {"class": "disputed", "parts": [
        {"class": "established"}, {"class": "disputed"}]}]]) == {"e": 1, "v": 0, "o": 1, "d": 1, "r": 0, "u": 0}


def test_feed_card_picture_is_the_lead_reports_and_never_an_outlets_default(store, tmp_path):
    """Pictures (owner, Oct 9 2026): the share picture of the report behind the lead, linked and credited; a picture
    the same outlet puts on 3+ different stories is its logo and is never used."""
    from nishpaksh import feed
    from nishpaksh.db import articles, insert, published, utcnow
    now = utcnow()
    nar = {"paragraphs": [[{"text": "Lead.", "class": "single", "sources": [2]}], [{"text": "More.", "class": "single", "sources": [1]}]],
           "section_keys": ["news", "happened"],
           "sources": [{"n": 1, "outlet": "A", "url": "https://a.in/1"}, {"n": 2, "outlet": "B", "url": "https://b.in/1"}]}
    store.exec(insert(published).values(story_id=500, updated_at=now, headline_en="H", headline_hi="ह",
                                        payload_en={"narrative": nar}, payload_hi={"narrative": nar}))
    for i, (url, outlet, img, sid) in enumerate([("https://a.in/1", "A", "https://a.in/p.jpg", 500),
                                                 ("https://b.in/1", "B", "https://b.in/logo.png", 500),
                                                 ("https://b.in/2", "B", "https://b.in/logo.png", 501),
                                                 ("https://b.in/3", "B", "https://b.in/logo.png", 502)]):
        store.exec(insert(articles).values(url=url, outlet=outlet, image=img, story_id=sid, published_at=now, fetched_at=now))
    out = tmp_path / "feed"
    feed.export(store, out)
    c = json.loads((out / "en.json").read_text(encoding="utf-8"))["stories"][0]
    assert c["img"] == {"src": "https://a.in/p.jpg", "by": "A", "href": "https://a.in/1"}   # B's is its logo
    store.exec(insert(articles).values(url="https://b.in/9", outlet="B", image="https://b.in/real.jpg", story_id=500,
                                       published_at=now, fetched_at=now))
    from nishpaksh.db import update
    store.exec(update(articles).where(articles.c.url == "https://b.in/1").values(image="https://b.in/lead.jpg"))
    feed.export(store, out)
    c = json.loads((out / "en.json").read_text(encoding="utf-8"))["stories"][0]
    assert c["img"]["src"] == "https://b.in/lead.jpg" and c["img"]["by"] == "B"   # the lead's report first


def test_heavy_columns_come_from_the_local_cache_when_unchanged(store, tmp_path, monkeypatch):
    """Egress (Oct 8 2026): text, embedding and minhash are fetched once and then served from a local
    cache while their md5 matches; a changed value is fetched again. Results are the same as a direct read."""
    import hashlib
    from sqlalchemy import event
    from nishpaksh import heavy
    from nishpaksh.db import articles, insert, update
    @event.listens_for(store.engine, "connect")
    def _md5(conn, _):
        conn.create_function("md5", 1, lambda v: hashlib.md5(v.encode()).hexdigest() if v is not None else None)
    store.engine.dispose()
    monkeypatch.setenv("NISHPAKSH_HEAVY_CACHE", str(tmp_path / "heavy.sqlite"))
    monkeypatch.setattr(heavy, "active", lambda s: True)
    monkeypatch.setattr(heavy, "_conn", None)
    now = dt.datetime(2026, 10, 8, 12, 0)
    for i in range(3):
        store.exec(insert(articles).values(id=900 + i, outlet="X", url=f"https://x/{i}", title=f"t{i}",
                                           text=f"text {i}", embedding=[0.1 * i, 0.2], published_at=now))
    q = lambda: select(articles.c.id, *heavy.columns(store, "text", "embedding")).where(articles.c.id >= 900).order_by(articles.c.id)
    direct = store.rows(select(articles.c.id, articles.c.text, articles.c.embedding).where(articles.c.id >= 900).order_by(articles.c.id))
    heavy.stats.update(hit=0, fetched=0)
    assert heavy.fill(store, store.rows(q()), "text", "embedding") == direct
    assert heavy.stats == {"hit": 0, "fetched": 6}
    assert heavy.fill(store, store.rows(q()), "text", "embedding") == direct
    assert heavy.stats == {"hit": 6, "fetched": 6}
    store.exec(update(articles).where(articles.c.id == 901).values(text="changed"))
    rows = heavy.fill(store, store.rows(q()), "text", "embedding")
    assert rows[1]["text"] == "changed" and heavy.stats == {"hit": 11, "fetched": 7}


def test_who_speaks_is_code_s_job_story_13107():
    """Owner, Oct 8 2026 (story 13107): "Humayun Kabir added that ... Humayun Kabir also stated that ...
    Humayun Kabir further stated that ...". Code owns who speaks: the attribution is taken off the
    statement, the verb is the outlets', "he"/"she" only with two outlets' evidence, and the code floor
    turns a run of one speaker into "..., he said." / "..., the MLA said."."""
    from nishpaksh import voice
    from nishpaksh.style import polish
    assert voice.split("Humayun Kabir stated that he has no concerns regarding other candidates.", "Humayun Kabir") \
        == ("He has no concerns regarding other candidates.", "stated")
    # a clause after the verb is no clean split; nor is a statement about someone else
    assert voice.split("Bengal MLA Humayun Kabir stated while being taken away that he knew nothing.", "Humayun Kabir")[1] is None
    assert voice.split("Police asked Kabir to go home.", "Humayun Kabir")[1] is None
    # an act verb stays with its content: "accused X of" is not "said"
    assert voice.split("TMC candidate Rabiul Alam Chowdhury accused Humayun Kabir of communal politics.",
                       "Rabiul Alam Chowdhury")[1] is None
    # never a stronger verb than the outlets used; "the accused" is a person, a noun is no act
    assert voice.unsupported_acts("Kabir accused the police of interfering.", "Police are interfering") == {"accused"}
    assert voice.unsupported_acts("Kabir accused the police of interfering.", "Kabir's accusation") == set()
    assert voice.unsupported_acts("The accused were arrested.", "Two men were arrested") == set()
    assert voice.unsupported_acts("Nestle denied the claim.", "Nestle India denied the allegation") == set()

    def it(n, text, outlet):
        return {"id": n, "text": text, "speaker": "Humayun Kabir", "sources": [{"outlet": outlet}]}
    one = it(1, "Police asked Bengal MLA Humayun Kabir to return home, but he refused.", "India Today")
    two = it(2, "Humayun Kabir said he would confront police with sticks.", "Zee News")
    assert voice.pronouns([one, two]) == {"Humayun Kabir": "he"}
    assert voice.pronouns([one, dict(two, sources=[{"outlet": "India Today"}])]) == {}       # one outlet
    other = it(3, "Kabir grabbed the collar of a sub-inspector, and he fell.", "NDTV")         # who fell?
    assert voice.pronouns([one, other]) == {}
    named = it(4, "Humayun Kabir met Suvendu Adhikari and he left.", "NDTV")                  # two people
    assert voice.pronouns([one, named]) == {}
    she = it(5, "Humayun Kabir said she would stay.", "NDTV")                                  # contrary
    assert voice.pronouns([one, two, she]) == {}

    P = [[{"text": "Security forces detained Aam Janata Unnayan Party chief and MLA Humayun Kabir in Rejinagar."}],
         [{"text": "While Humayun Kabir had been criticising the police, police arrived at the party office."}],
         [{"text": "Humayun Kabir added that sixty-four people have been detained over the past two months."},
          {"text": "Humayun Kabir also stated that police are interfering with the voting process in several booths."},
          {"text": "Humayun Kabir further stated that the April 23 assembly election was peaceful."}]]
    pay = {"narrative": {"paragraphs": P}, "contested": [one, two]}
    polish(pay)
    t = [s["text"] for p in P for s in p]
    assert t[1].startswith("While Kabir had")               # "While" is not part of a name
    assert t[2] == "Kabir said that sixty-four people have been detained over the past two months."
    assert t[3] == "Police are interfering with the voting process in several booths, he said."
    assert t[4] == "The April 23 assembly election was peaceful, the MLA said."
    # without the outlets' evidence there is no "he": the surname instead
    P2 = [[{"text": "MLA Humayun Kabir said police were present."}, {"text": "He said the vote was fair, he added."},
           {"text": "Humayun Kabir also stated that the count is on Friday."}]]
    polish({"narrative": {"paragraphs": P2}, "contested": [one]})
    t2 = [s["text"] for s in P2[0]]
    assert "he " not in " ".join(t2[1:]).lower().replace("the ", "")


def test_context_is_dropped_only_when_the_model_says_other_news_twice(store):
    """Owner, Oct 8 2026: "It's better to have something unrelated in the story and think, why is this here,
    than not to have something important" (background, related and explanation alike). A line goes only on
    two "other news" answers; one "connected", no answer (quota) or a model that never answers keeps it.
    No code rule drops a line by its words (one dropped Air Force ration drops in a story on flood victims)."""
    from nishpaksh import belong
    from nishpaksh.router import LLMResult
    sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, dirty=False, qualifies=True,
                                                  signature="cheetah cubs", analysis={}))
    core = {"id": 1, "role": "core", "text": "A cheetah gave birth to five cubs in Kuno National Park",
            "verdict": "corroborated", "kind": "event", "sources": [], "n_sources": 3}
    ctx = [{"id": 2, "role": "related", "text": "Kerala Home Minister defended a vigilance probe into a road project"},
           {"id": 3, "role": "background", "text": "Heavy rain fell in Nepal"},                # shares no word
           {"id": 4, "role": "explanation", "text": "Project Cheetah brings the cheetah back to India"},
           {"id": 5, "role": "reaction", "text": "The minister congratulated the field staff"}]   # never asked

    def payload():
        return {"established": [dict(core)], "contested": [], "undated": [], "timeline": [],
                "context": [dict(c, kind="event", verdict="unverified", sources=[]) for c in ctx]}

    class Says:
        """answers[k]: the k-th asking's answer per line text (a default for the rest)."""
        def __init__(self, *answers):
            self.answers, self.prompts = list(answers), []

        def call(self, tier, prompt, **kw):
            self.prompts.append(prompt)
            ans = self.answers[min(len(self.prompts), len(self.answers)) - 1]
            res = [{"n": int(n), "answer": ans.get(t[:6], ans.get("*")) if isinstance(ans, dict) else ans}
                   for n, t in re.findall(r'^(\d+)\. "(.*)"$', prompt.split("LINES:")[1], re.M)]
            return LLMResult("", {"results": res}, "m", [], 1)

    def run(*answers):
        store.exec(update(stories).where(stories.c.id == sid).values(analysis={}))
        p = payload()
        m = Says(*answers)
        belong.check(store, m, sid, p)
        return [c["id"] for c in p["context"]], m
    kept, m = run("connected")
    assert kept == [2, 3, 4, 5] and len(m.prompts) == 1          # one "connected" settles it: asked once
    assert run("other news", "connected")[0] == [2, 3, 4, 5]       # one "other news" is not enough
    kept, m = run({"Kerala": "other news", "*": "connected"}, "other news")
    assert kept == [3, 4, 5] and "Nepal" not in m.prompts[1].split("LINES:")[1]   # only the doubted line again
    assert run("other news", "other news")[0] == [5]               # every role on the same bar
    # answers are kept: nothing asked again
    p = payload()
    again = Says("connected")
    belong.check(store, again, sid, p)
    assert again.prompts == [] and [c["id"] for c in p["context"]] == [5]
    # nothing could be asked (no model): everything stays
    store.exec(update(stories).where(stories.c.id == sid).values(analysis={}))
    p = payload()
    assert belong.check(store, None, sid, p) == 0 and len(p["context"]) == 4


def test_compound_statements_split_into_single_facts_story_13970(store):
    """Owner, Oct 8 2026 (story 13970): "the repo rate is the rate at which the RBI lends to banks" written three
    times. Two outlets' sentences shared one fact and each added another, so neither covered the other. Rows
    are split into single facts (up to 4) before matching, checked by code; the shared fact becomes ONE
    statement with both outlets behind it."""
    from nishpaksh import match, split
    from nishpaksh.db import canonical, claims
    from nishpaksh.router import LLMResult
    sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, dirty=True, qualifies=False,
                                                  signature="repo rate", analysis={}))
    A = "Repo rate is the rate at which the RBI lends money to banks, and an increase raises borrowing costs for customers."
    B = "Repo rate is the rate at which the RBI lends money to banks, which banks then use as a basis for lending to customers."
    S = "Governor Sanjay Malhotra said rate cuts were off the table and future actions may be limited to rate increases."
    aids = []
    for n, outlet in enumerate(("Mint", "Jagran")):
        aids.append(store.insert_returning_id(articles, dict(url=f"u{n}", outlet=outlet, lang="en", title="t", text="t",
                                                             story_id=sid, published_at=NOW, fetched_at=NOW, extract_failures=0)))
    store.exec(insert(claims).values(story_id=sid, article_id=aids[0], local_id="c1", kind="claim", text=A, stance="asserts",
                                     attributed_to="article", evidence="none", loaded_words=["महंगा"], time=None, rel=None))
    store.exec(insert(claims).values(story_id=sid, article_id=aids[0], local_id="e1", kind="event", text="The RBI raised the repo rate.",
                                     stance="asserts", attributed_to="article", evidence="none", loaded_words=[], time=None, rel=None))
    store.exec(insert(claims).values(story_id=sid, article_id=aids[0], local_id="r1", kind="relation", text="", stance="asserts",
                                     attributed_to="article", evidence="none", loaded_words=[], time=None,
                                     rel={"from": "e1", "to": "c1", "type": "before"}))
    store.exec(insert(claims).values(story_id=sid, article_id=aids[1], local_id="c1", kind="claim", text=B, stance="asserts",
                                     attributed_to="article", evidence="none", loaded_words=[], time=None, rel=None))
    store.exec(insert(claims).values(story_id=sid, article_id=aids[1], local_id="c2", kind="claim", text=S, stance="attributes",
                                     attributed_to="Sanjay Malhotra", evidence="none", loaded_words=[], time=None, rel=None))
    facts = {A: ["Repo rate is the rate at which the RBI lends money to banks.",
                 "An increase in the repo rate raises borrowing costs for customers."],
             B: ["Repo rate is the rate at which the RBI lends money to banks.",
                 "Banks then use the repo rate as a basis for lending to customers."],
             # drops the speaker from the second fact: code keeps the row whole
             S: ["Governor Sanjay Malhotra said rate cuts were off the table.", "Future actions may be limited to rate increases."]}

    class Splitter:
        def __init__(self):
            self.calls = 0

        def call(self, tier, prompt, **kw):
            self.calls += 1
            res = [{"n": int(n), "facts": facts.get(t, [t])}
                   for n, t in re.findall(r'^(\d+)\. "(.*)"$', prompt.split("SENTENCES:")[1], re.M)]
            return LLMResult("", {"results": res}, "m", [], 1)
    r = Splitter()
    st = split.split_story(store, r, sid)
    assert st == {"asked": 3, "split": 2, "pieces": 4} and r.calls == 1
    rows = store.rows(select(claims).where(claims.c.story_id == sid, claims.c.kind != "relation"))
    texts = sorted(x["text"] for x in rows)
    assert S in texts and A not in texts and B not in texts
    first = next(x for x in rows if x["article_id"] == aids[0] and x["local_id"] == "c1.1")
    assert first["loaded_words"] == ["महंगा"]                       # counted once, on the first piece
    rel = store.one(select(claims).where(claims.c.local_id == "r1"))["rel"]
    assert rel["to"] == "c1.1"                                       # the relation follows the main fact
    # asked once: nothing asked again
    assert split.split_story(store, r, sid) == {"asked": 0, "split": 0, "pieces": 0} and r.calls == 1
    # matching: the shared fact is ONE statement, both outlets behind it
    match.match_story(store, None, sid)
    shared = [c for c in store.rows(select(canonical).where(canonical.c.story_id == sid))
              if c["text"] == "Repo rate is the rate at which the RBI lends money to banks."]
    assert len(shared) == 1
    members = store.rows(select(claims.c.article_id).where(claims.c.canonical_id == shared[0]["id"]))
    assert sorted(m["article_id"] for m in members) == sorted(aids)


def test_split_check_and_quantities():
    from nishpaksh.frames import numbers
    from nishpaksh.narrative import MAX_PARTS
    from nishpaksh.split import _pieces_ok
    assert numbers("raised by 25 basis points to 5.50 per cent") == [0.25, 5.5]
    assert numbers("by 0.25 percent") == [0.25]
    o = "Police said two men were arrested in Patna and 5 kg of ganja was seized from them."
    assert _pieces_ok(o, ["Police said two men were arrested in Patna.", "Police said 5 kg of ganja was seized from them."])
    assert not _pieces_ok(o, ["Police said two men were arrested in Patna.", "Police said 6 kg of ganja was seized."])  # a number changed
    assert not _pieces_ok(o, ["Police said two men were arrested.", "Police said 5 kg of ganja was seized."])           # a name lost
    assert not _pieces_ok("The court did not grant bail and listed the case for Friday.",
                          ["The court granted bail.", "The court listed the case for Friday."])                       # a "not" lost
    assert not _pieces_ok(o, [o, o, o, o, o])                                                                         # more than 4
    # a list split apart is one fact with a list, not separate facts (story 13970)
    emi = "The increase in the repo rate will lead to higher EMIs for home loans, car loans and personal loans."
    assert not _pieces_ok(emi, ["The increase in the repo rate will lead to higher EMIs for home loans.",
                                "The increase in the repo rate will lead to higher EMIs for car loans and personal loans."])
    assert MAX_PARTS == 3


def test_paraphrases_are_proposed_topic_by_topic_story_13970():
    """Owner, Oct 8 2026: after the split, 13970 still said "rate cuts were off the table" and "there is no option
    for interest rate cuts" separately, and the decision as "by 0.25 percent" and "by 25 basis points to 5.50
    per cent". One call sorts statements into topics; per topic one call proposes same / covers pairs. They are
    proposals only (consolidate.py decides them by code and the twice-asked question); kept by text, cached."""
    from nishpaksh import dupes
    from nishpaksh.router import LLMResult
    texts = {10: "The RBI increased the repo rate by 0.25 percent.",
             11: "The RBI raised the repo rate by 25 basis points to 5.50 per cent.",
             12: "Fixed rate loans are not directly affected by repo rate increases.",
             13: "Malhotra said rate cuts were off the table.",
             14: "Malhotra said there is no option for interest rate cuts in the near term.",
             15: "The RBI raised its GDP growth forecast to 7.1 per cent."}

    class Model:
        def __init__(self):
            self.prompts = []

        def call(self, tier, prompt, **kw):
            self.prompts.append(prompt)
            lines = dict((int(n), t) for n, t in re.findall(r"^(\d+)\. (.*)$", prompt, re.M))
            num = {t: n for n, t in lines.items()}
            if "Sort them into TOPICS" in prompt:
                return LLMResult("", {"topics": [[num[texts[10]], num[texts[11]], num[texts[12]]],
                                                 [num[texts[13]], num[texts[14]]]]}, "m", [], 1)   # 15 left out
            data = {"same": [], "covers": []}
            if texts[13] in num:
                data["same"] = [[num[texts[13]], num[texts[14]]]]
            if texts[10] in num:
                data["covers"] = [[num[texts[11]], num[texts[10]]]]
            return LLMResult("", data, "m", [], 1)
    m, cache = Model(), {}
    same, covers = dupes.propose(m, texts, cache)
    assert same == {(13, 14)} and covers == [(11, 10)]
    assert len(m.prompts) == 3                                  # topics + two topics (15 alone: not asked)
    # the same statements again: nothing asked
    m2 = Model()
    assert dupes.propose(m2, texts, cache) == (same, covers) and m2.prompts == []
    # ids changed after a merge, texts the same: the cached answer still finds the pair
    moved = {i + 100: t for i, t in texts.items()}
    same3, covers3 = dupes.propose(Model(), moved, cache)
    assert same3 == {(113, 114)} and covers3 == [(111, 110)]


def test_perspectives_keep_every_other_stages_work_story_16000(store):
    """Oct 9 2026: perspectives.analyze_story rewrote the story's analysis keeping a fixed list of fields, with
    "consolidated" on it and the review's results off it: covered lines, updates, doubtful disputes and the
    cached answers were wiped every run, and the writer's own review then saw the story as done (16000 repeated
    one hearing three times). Doubtful disputes block green, so this could also show a statement as established."""
    from nishpaksh import perspectives
    from nishpaksh.run import run
    _seed(store)
    run(store=store, backend=FakeBackend(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    sid = store.rows(select(stories.c.id).order_by(stories.c.id))[0]["id"]
    an = dict(store.one(select(stories.c.analysis).where(stories.c.id == sid))["analysis"] or {})
    work = {"covered": {"5": 7}, "updates": {"3": 4}, "doubtful_conflicts": [8, 9], "same_checks": {"k": True},
            "conflict_checks": {"k": "both_true"}, "dupe_checks": {"t1": [["a"]]}, "context_checks": {"x": []}}
    store.exec(update(stories).where(stories.c.id == sid).values(analysis={**an, **work}))
    perspectives.analyze_story(store, sid)
    after = store.one(select(stories.c.analysis).where(stories.c.id == sid))["analysis"]
    assert {k: after.get(k) for k in work} == work


def test_fragments_are_joined_or_refused_story_13792():
    """Oct 9 2026, story 13792: the lead was "An unnamed source said on Thursday." (its content returned as a separate
    piece and lost), and "... to make arrests on Thursday," was followed by "And tax officers will no longer ...".
    Pieces are joined back (coloured parts when they cite different statements); a fragment left alone is refused;
    a speaker's surname twice in one sentence is a repeated name."""
    from nishpaksh.narrative import _check_paragraphs, _join_fragments
    by = {1: _item(1, "The 57th GST Council meeting approved a set of measures aimed at simplifying compliance",
                   speaker="an unnamed source"),
          2: _item(2, "The GST Council approved the removal of the power of GST tax officials to make arrests on Thursday"),
          3: _item(3, "Tax officers will no longer have the power to arrest taxpayers under the existing GST system"),
          4: _item(4, "There was no discussion regarding MDR in the GST Council meeting", speaker="Nirmala Sitharaman"),
          5: _item(5, "GST rates have not been changed", speaker="Nirmala Sitharaman")}
    lead = [{"text": "The 57th GST Council meeting approved a set of measures aimed at simplifying compliance,", "ids": [1]},
            {"text": "an unnamed source said on Thursday.", "ids": [1]}]
    joined = _join_fragments(lead)
    assert len(joined) == 1 and joined[0]["text"].endswith("an unnamed source said on Thursday.") and "parts" not in joined[0]
    two = _join_fragments([{"text": "The GST Council approved the removal of the power of GST tax officials to make "
                                     "arrests on Thursday,", "ids": [2]},
                           {"text": "and tax officers will no longer have the power to arrest taxpayers under the "
                                    "existing GST system.", "ids": [3]}])
    assert len(two) == 1 and [p["ids"] for p in two[0]["parts"]] == [[2], [3]]
    # a fragment with nothing to join is refused, never published
    paras, failed, rejected = _check_paragraphs([[{"text": "And is held in New Delhi on Thursday", "ids": [2]}]], by, set(), [])
    assert rejected == 1 and failed[0]["reason"] == "not a full sentence"
    # a lower-case slip after a finished sentence is only capitalised
    assert _join_fragments([{"text": "Done.", "ids": [2]}, {"text": "police said so.", "ids": [3]}])[1]["text"] == "Police said so."
    # the speaker's surname twice in one sentence
    paras, failed, rejected = _check_paragraphs([[
        {"text": "Finance Minister Nirmala Sitharaman said there was no discussion regarding MDR in the GST Council "
                 "meeting, and Sitharaman said GST rates have not been changed.", "ids": [4, 5]}]], by, set(), [])
    assert rejected == 1 and failed[0]["reason"] == "repeats a name"


def test_paragraphs_are_shaped_and_past_schedules_left_out_story_13792():
    """Owner, Oct 9 2026 (story 13792): seven one-sentence paragraphs, one of 14 sentences; "The 57th GST
    Council meeting is scheduled to take place ... on Thursday, October 8" written after the meeting."""
    from nishpaksh.compose import drop_past_schedules
    from nishpaksh.narrative import shape_paragraphs
    by = {i: _item(i, f"Statement {i} about subject{i % 3}") for i in range(1, 40)}
    s = lambda i, t=None: {"text": t or f"Subject{i % 3} fact number {i}.", "ids": [i]}  # noqa: E731
    paras = [[s(1)], [s(2)], [s(3)], [s(4)], [s(5)], [s(6)], [s(7)]]
    out, keys = shape_paragraphs(paras, ["news"] + ["explained"] * 6, by)
    assert len(out[0]) == 1 and keys[0] == "news"                    # the lead stays as written
    assert all(2 <= len(p) <= 4 for p in out[1:]) and sum(len(p) for p in out) == 7
    long = [s(i) for i in range(10, 24)]
    out, keys = shape_paragraphs([long], ["say"], by)
    assert len(out) >= 3 and all(2 <= len(p) <= 5 for p in out) and sum(len(p) for p in out) == 14
    # a sentence leaning on the one before never opens a paragraph
    lean = [s(i) for i in range(10, 16)] + [s(16, "He added that subject1 fact 16 stands.")] + [s(i) for i in range(17, 20)]
    out, _ = shape_paragraphs([lean], ["say"], by)
    assert not any(p[0]["text"].startswith("He added") for p in out)
    # scheduled for a day that has passed: left out; a decision that day: kept
    now = dt.datetime(2026, 10, 9, 3, 49)
    p = {"undated": [], "established": [], "context": [], "timeline": [],
         "contested": [{"id": 1, "text": "The 57th GST Council meeting is scheduled to take place at Bharat Mandapam "
                                         "in New Delhi on Thursday, October 8, 2026."},
                       {"id": 2, "text": "The GST Council approved the removal of the power to make arrests."},
                       {"id": 3, "text": "Counting of votes will take place on October 12."},
                       {"id": 4, "text": "The bench is scheduled to hear the case."}]}           # no date: kept
    assert drop_past_schedules(p, now) == 1 and [i["id"] for i in p["contested"]] == [2, 3, 4]


def test_sections_are_checked_by_code_and_filled_for_live_articles(store):
    """The site's sections (owner, Oct 9 2026): five primary, five secondary under each. Code keeps only listed
    keys, at most two of each; a secondary brings its primary; a secondary under the wrong primary is dropped.
    Live articles from before sections get theirs on a desk run; only the section is added."""
    from nishpaksh import categories as C
    from nishpaksh.config import load_yaml
    assert len(C.SECTIONS) == 5 and all(len(v) == 5 for v in C.SECTIONS.values())
    assert C.parse(["justice/courts", "politics/elections"]) == {"primary": ["justice", "politics"],
                                                                "secondary": ["courts", "elections"]}
    assert C.parse(["Life"]) == {"primary": ["life"], "secondary": []}               # unsure: the primary alone
    assert C.parse(["defence"]) == {"primary": ["world"], "secondary": ["defence"]}   # its primary is known
    assert C.parse(["business/courts", "World / Indians abroad"]) == {"primary": ["world"],
                                                                     "secondary": ["indians-abroad"]}
    assert C.parse(["life/health", "justice/crime", "business/money"])["primary"] == ["life", "justice"]
    assert C.parse(["sports"]) is None and C.parse(None) is None
    assert C.labels("hi")["secondary"]["sport-films"] == "खेल और फ़िल्में"

    _seed(store)
    _run_twice(store)
    row = store.rows(select(published))[0]
    old_en = {k: v for k, v in row["payload_en"].items() if k != "category"}
    store.exec(update(published).where(published.c.story_id == row["story_id"]).values(
        payload_en=old_en, payload_hi={k: v for k, v in row["payload_hi"].items() if k != "category"}))
    router = Router(load_yaml("models.yaml")["tiers"], FakeBackend(), store)
    assert C.fill_live(store, router) == [row["story_id"]]
    after = store.one(select(published).where(published.c.story_id == row["story_id"]))
    assert after["payload_en"]["category"]["primary"] == ["life", "justice"]
    assert {k: v for k, v in after["payload_en"].items() if k != "category"} == old_en
    assert after["payload_hi"]["category"] == after["payload_en"]["category"]
    assert C.fill_live(store, router) == []                  # asked once


def test_lead_is_the_best_reported_fact_story_13792():
    """Owner, Oct 9 2026: the most-reported fact leads; a different fact of the day may follow it. A shared name
    is not "the same act": every "the GST Council approved ..." line had borrowed every other's outlets."""
    from nishpaksh.news import lead_news

    def it(i, t, outs, start=None):
        return {"id": i, "text": t, "kind": "event", "role": "core", "verdict": "unverified",
                "sources": [{"outlet": o, "url": f"{o}{i}"} for o in outs], "time": {"start": start} if start else None}
    items = [it(1, "The 57th GST Council meeting approved a set of measures aimed at simplifying compliance and "
                   "speeding up refunds.", ["Mint"], "2026-10-08T12:00"),
             it(2, "The GST Council approved the removal of the power of GST tax officials to make arrests.",
                ["Mint", "ET", "Hindu"]),
             it(3, "The GST Council approved a set of changes to GST rules.", ["NDTV"])]
    lead = lead_news(items)
    assert lead[0] == 2 and lead[1:] in ([], [1])


def test_separate_is_written_only_when_the_statements_say_it():
    """Owner, Oct 9 2026 (story 16000): "In a separate case" printed for the story's own case; the word
    must be the outlets', the Related events heading says the rest."""
    from nishpaksh.narrative import _no_separate
    assert _no_separate("In a separate case, Sonia Kumar filed a petition.", "Sonia Kumar filed a petition.") \
        == "Sonia Kumar filed a petition."
    assert _no_separate("Separately, the court heard the plea.", "") == "The court heard the plea."
    assert _no_separate("Police arrested two men in a separate incident.", "") == "Police arrested two men."
    kept = "In a separate case, the court heard the plea."
    assert _no_separate(kept, "In a separate case, the court heard a plea.") == kept
    assert _no_separate("The two cases are separate.", "") == "The two cases are separate."


def test_one_outlet_line_never_leads_over_a_fact_more_outlets_tell():
    """Story 13058 (Oct 9 2026): "Nana Patekar ... won millions of hearts" (one outlet, "won" read as an act)
    led a story whose inauguration three outlets told."""
    from nishpaksh.news import pick_news
    src = lambda *o: [{"outlet": x, "url": x} for x in o]
    items = [
        {"id": 1, "kind": "event", "text": "Nana Patekar was an extraordinary artist who won millions of hearts.",
         "sources": src("A")},
        {"id": 2, "kind": "event", "text": "Prime Minister Narendra Modi inaugurated the India Mobile Congress in New Delhi.",
         "sources": src("A", "B", "C")},
        {"id": 3, "kind": "event", "text": "There are more than 5.5 lakh 5G base stations in India.",
         "sources": src("B", "C", "D", "E")},
    ]
    assert pick_news(items)[0] == 2

# ---------------------------------------------------------------- outlets outside India (owner, Oct 9 2026)

def test_state_media_are_a_government_speaking_not_an_outlet():
    """Xinhua, Global Times and a paper carrying "(Xinhua)" copy are one voice: China's. PIB is the Union
    government. None counts as an independent outlet."""
    from nishpaksh.wire import independent
    arts = [dict(id=1, outlet="Global Times", url="https://www.globaltimes.cn/a"),
            dict(id=2, outlet="Xinhua", url="https://english.news.cn/b"),
            dict(id=3, outlet="Gulf News", url="https://gulfnews.com/c", agency="Xinhua"),
            dict(id=4, outlet="Dawn", url="https://www.dawn.com/d"),
            dict(id=5, outlet="PIB", url="https://pib.gov.in/e"),
            dict(id=6, outlet="The Hindu", url="https://www.thehindu.com/f")]
    g = independence_groups(arts)
    assert g[1] == g[2] == g[3] == "gov:china" and g[5] == "gov:union"
    assert len(independent(g)) == 2              # Dawn and The Hindu


def test_region_of_outlets():
    from nishpaksh.ownership import region_of
    assert region_of("Dawn", "https://www.dawn.com/x") == "world"
    assert region_of("BBC Hindi", "https://www.bbc.com/hindi/x") == "world"
    assert region_of("The Hindu", "https://www.thehindu.com/x") == "india"
    assert region_of("PIB", "https://pib.gov.in/x") == "india"
    assert region_of("Lokmat", "https://www.lokmat.com/x") == "india"       # found by search, .com
    assert region_of("Some Paper", "https://paper.co.uk/x") == "world"      # another country's domain


def test_world_articles_are_embedded_only_when_they_can_join_a_story():
    """worldgate: India named, names shared with an Indian headline, or with 2+ other world outlets."""
    from nishpaksh.worldgate import hold
    t = NOW - dt.timedelta(hours=1)

    def art(i, outlet, url, title, feed=1, emb=None):
        return dict(id=i, outlet=outlet, url=url, lang="en", title=title, text="", feed_id=feed,
                    embedding=emb, published_at=t)
    arts = [
        art(1, "The Hindu", "https://www.thehindu.com/1", "Shehbaz Sharif meets Erdogan in Ankara", emb=[1]),
        art(2, "Dawn", "https://www.dawn.com/2", "India, Pakistan trade charges at UN"),           # (a)
        art(3, "Dawn", "https://www.dawn.com/3", "Sharif, Erdogan sign defence pact in Ankara"),   # (b)
        art(4, "Dawn", "https://www.dawn.com/4", "Karachi traffic police launch helmet drive"),     # held
        art(5, "The Guardian", "https://www.theguardian.com/5", "Macron names Lecornu as prime minister"),
        art(6, "DW", "https://rss.dw.com/6", "France: Macron reappoints Lecornu"),
        art(7, "NPR", "https://www.npr.org/7", "Lecornu back as Macron's prime minister"),         # (c) 5, 6, 7
        art(8, "Dawn", "https://www.dawn.com/8", "Lahore court bails Qureshi", feed=None),        # found by search
    ]
    assert hold(arts, NOW) == {4}


def test_a_story_no_indian_outlet_covers_needs_global_impact(store):
    """Rule 1: Indian coverage decides. Rule 3: three independent world outlets and two "yes" to the
    global-impact question; an "unsure" or one "no" keeps it out."""
    import nishpaksh.priority as P

    def world_story(title, outlets):
        sid = store.insert_returning_id(stories, dict(created_at=NOW, updated_at=NOW, dirty=True, qualifies=False,
                                                      signature=title))
        for k, (o, dom) in enumerate(outlets):
            store.exec(insert(articles).values(url=f"https://{dom}/{abs(hash(title)) % 10**6}-{k}", outlet=o, lang="en",
                                               title=f"{title} ({o})", text="t " * 300, text_source="full",
                                               published_at=NOW - dt.timedelta(hours=1), fetched_at=NOW - dt.timedelta(hours=1),
                                               story_id=sid, extract_failures=0))
        return sid
    three = [("The Guardian", "www.theguardian.com"), ("DW", "www.dw.com"), ("NPR", "www.npr.org")]
    war = world_story("Israel strikes Iran oil terminal", three)
    local = world_story("Texas shooting kills three", three)
    unsure = world_story("Sudan ceasefire talks", three)
    china = world_story("Beijing hosts trade fair", [("Xinhua", "english.news.cn"), ("Global Times", "www.globaltimes.cn"),
                                                       ("CGTN", "www.cgtn.com"), ("Dawn", "www.dawn.com")])
    indian = _story_with_sources(store, "Parliament passes bill", 3)
    asked = []

    class Desk(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            import re as _re
            rows = _re.findall(r"^(\d+)\. (.*)$", prompt, _re.M)
            if "OUTSIDE the country" in prompt:
                asked.append([h for _, h in rows])
                ans = lambda h: "yes" if "Iran" in h else "unsure" if "Sudan" in h else "no"
                return json.dumps({"results": [{"n": int(n), "answer": ans(h)} for n, h in rows]}), [], 30
            if "each shown by the headlines" in prompt:
                return json.dumps({"results": [{"n": int(n), "score": 4, "filler": False} for n, _ in rows]}), [], 30
            return super().generate(model, prompt, json_mode, grounded)
    assert P.rank_new(store, _router(store, Desk())) == 2           # the war and the Indian story
    q = P.queue(store, size=10)
    assert set(q) == {war, indian}
    assert len(asked) == 2 and len(asked[1]) == 1                   # asked again only for the first "yes"
    an = store.one(select(stories.c.analysis).where(stories.c.id == unsure))["analysis"]
    assert an["world"]["global"] is False
    assert china not in q   # three state outlets are one voice: not three independent outlets


# ---------------------------------------------------------------- Domains and Sport (owner, Oct 9 2026)

def test_sections_have_domains_as_a_third_level():
    from nishpaksh.categories import labels, normalize, parse
    assert parse(["politics/protests", "business/domains/hr"]) == {
        "primary": ["politics", "business"], "secondary": ["protests", "domains"], "tertiary": ["hr"]}
    assert parse(["operations"]) == {"primary": ["business"], "secondary": ["domains"], "tertiary": ["operations"]}
    assert parse(["business/domains"]) == {"primary": ["business"], "secondary": []}   # which domain? the primary alone
    assert parse(["business/domains/sport"]) is None
    # live articles keep their place: Jobs and Tech are now HR and Analytics
    assert normalize({"primary": ["business"], "secondary": ["tech"]}) == {
        "primary": ["business"], "secondary": ["domains"], "tertiary": ["analytics"]}
    lab = labels("en")
    assert lab["tree"]["business"] == ["economy", "companies", "markets", "money", "domains"]
    assert lab["tree3"]["domains"] == ["hr", "finance", "marketing", "analytics", "operations"]


def test_beat_stories_get_reserved_reading_places(store, monkeypatch):
    """4 of the 16 places go to the best Domains/Sport stories; places they do not use go back."""
    import nishpaksh.priority as P
    monkeypatch.setattr(P, "SETTINGS", dataclasses.replace(P.SETTINGS, prep_queue=3, prep_beat=1))
    gen = [_story_with_sources(store, f"Parliament story {k}", 3) for k in range(3)]
    sport = _story_with_sources(store, "Cricket final result", 3)

    class Rater(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            import re as _re
            rows = _re.findall(r"^(\d+)\. (.*)$", prompt, _re.M)
            if "desk of a newspaper" in prompt:        # the desk question is its own call (Oct 9 2026)
                return json.dumps({"results": [{"n": int(n), "desk": "sport" if "Cricket" in h else "none"}
                                               for n, h in rows]}), [], 30
            if "each shown by the headlines" in prompt:
                return json.dumps({"results": [{"n": int(n), "score": 2 if "Cricket" in h else 5, "filler": False}
                                               for n, h in rows]}), [], 30
            return super().generate(model, prompt, json_mode, grounded)
    P.rank_new(store, _router(store, Rater()))
    q = P.queue(store)
    assert sport in q and len(q) == 3 and len(set(q) & set(gen)) == 2
    monkeypatch.setattr(P, "SETTINGS", dataclasses.replace(P.SETTINGS, prep_queue=3, prep_beat=1))
    store.exec(update(stories).where(stories.c.id == sport).values(analysis={}))
    assert set(P.queue(store)) == set(gen)             # no beat story rated: its place goes back


def test_the_desk_keeps_one_seat_for_domains_or_sport(store, monkeypatch):
    """Two seats for any story, one only for a Domains/Sport story; empty when none is ready."""
    from nishpaksh import desk
    import nishpaksh.compose as C
    gen = [_story_with_sources(store, f"General {k}", 3) for k in range(4)]
    sport = _story_with_sources(store, "Hockey semi-final", 3)
    for sid in gen + [sport]:
        store.exec(update(stories).where(stories.c.id == sid).values(
            analysis={"priority": {"score": 5 if sid != sport else 2, "beat": "sport" if sid == sport else None}}))
    order = gen + [sport]
    monkeypatch.setattr(desk, "ready", lambda store, now=None: list(order))
    done = []

    def fake_publish(store_, router, sid):
        done.append(sid)
        store_.exec(insert(published).values(story_id=sid, version=1, updated_at=NOW, headline_en="h", payload_en={}))
        return True
    monkeypatch.setattr(C, "publish_story", fake_publish)
    import nishpaksh.consolidate as K, nishpaksh.verify as V
    monkeypatch.setattr(K, "consolidate_story", lambda *a, **k: None)
    monkeypatch.setattr(V, "base_verdicts", lambda *a, **k: None)
    st = desk.work(store, None, now=NOW)
    assert done == gen[:2] + [sport] and st["beat_seat"] == [sport]
    # next hour, nothing of a beat ready: two general articles and the seat stays empty
    order[:] = gen[2:]
    st = desk.work(store, None, now=NOW + dt.timedelta(hours=1))
    assert st["published"] == gen[2:] and st["beat_seat"].startswith("empty")


def test_a_line_every_report_gives_one_speaker_keeps_that_speaker(store):
    """Story 13058 (Oct 9 2026): reading gave "Nana Patekar was an extraordinary artist" to Modi, and the
    article printed it as a plain fact. An outlet named as the source is never a speaker."""
    from nishpaksh import compose
    from nishpaksh.db import update as upd
    sid, cid = _origin_story(store, [("Outlet A", None, "Prime Minister Narendra Modi"),
                                     ("Outlet B", None, "Prime Minister Narendra Modi")])
    sid2, cid2 = _origin_story(store, [("Outlet A", None, "Outlet B"), ("Outlet B", None, "Outlet B")])
    sid3, cid3 = _origin_story(store, [("Outlet A", None, "Prime Minister Narendra Modi"), ("Outlet B", None, None)])
    for s in (sid, sid2, sid3):
        store.exec(upd(stories).where(stories.c.id == s).values(qualifies=True))
    def speaker(s, c):
        p = compose.build_payload(store, None, s)
        items = [i for k in ("undated", "established", "contested", "context") for i in p.get(k) or []]
        items += [i for t in p.get("timeline") or [] for i in t]
        return next(i for i in items if i["id"] == c).get("speaker")
    assert speaker(sid, cid) == "Prime Minister Narendra Modi"
    assert speaker(sid2, cid2) is None
    assert speaker(sid3, cid3) is None


def test_reading_drops_a_person_the_report_does_not_name():
    """Story 16197 (Oct 9 2026): the Hindi report names Chief Justice सूर्यकांत; reading wrote "Chief Justice
    Sanjiv Khanna (referred to as Chief Justice Suryakant in the text)" from the model's own memory."""
    from nishpaksh.extract import ground
    src = ("चीफ जस्टिस सूर्यकांत, जस्टिस जॉयमाल्या बागची और जस्टिस वी. मोहना की खंडपीठ ने कानून पर सवाल उठाए। "
           "सीनियर एडवोकेट कपिल सिब्बल, DGP तदाशा मिश्रा, अश्विनी वैष्णव, जेडी वेंस, गौड़ा")
    item = lambda i, t: {"id": i, "text": t}
    ex = {"events": [item("e1", "The bench led by Chief Justice Sanjiv Khanna (referred to as Chief Justice Suryakant "
                                "in the text) questioned the rules."),
                     item("e2", "Chief Justice Surya Kant, Justice Joymalya Bagchi and Justice V Mohana heard the case."),
                     item("e3", "Senior Advocate Kapil Sibal appeared for Jharkhand."),
                     item("e4", "Notice was issued to DGP Tadasha Mishra's office."),
                     item("e5", "Union Minister Ashwini Vaishnaw and US Vice President JD Vance spoke."),
                     item("e6", "Justice Gowda spoke."),
                     item("e7", "The court (as the article calls it) adjourned.")],
          "claims": [], "context": [],
          "relations": [{"from": "e1", "to": "e2", "type": "before"}, {"from": "e2", "to": "e3", "type": "before"}]}
    assert ground(ex, src) == 2
    assert [i["id"] for i in ex["events"]] == ["e2", "e3", "e4", "e5", "e6"]
    assert ex["relations"] == [{"from": "e2", "to": "e3", "type": "before"}]


def test_attribution_follows_the_statements_speaker_not_the_last_one():
    """Story 16197 (Oct 9 2026): after Kapil Sibal, "The Centre alleged ..." became "..., the advocate alleged";
    after a line naming Mishra, "The Supreme Court stated ..." became "..., she said"; story 13058: Scindia's
    figures became "..., Modi said"."""
    from nishpaksh.style import Refs, people, vary_attribution
    from nishpaksh.voice import roles
    sp = {1: "Kapil Sibal", 2: "Centre", 3: "petitioner", 4: "Supreme Court", 5: "Prime Minister Narendra Modi",
          6: "Union Minister Jyotiraditya Scindia"}
    P = [[{"text": "Senior advocate Kapil Sibal argued that the Centre is targeting Jharkhand.", "ids": [1]},
          {"text": "The Centre alleged that the Jharkhand government violated Supreme Court directions.", "ids": [2]}],
         [{"text": "Jharkhand appointed Tadasha Mishra as DGP one day before her retirement, the petitioner said.", "ids": [3]},
          {"text": "She said that Mishra's appointment is in violation of the Prakash Singh judgment.", "ids": [4]}],
         [{"text": "Prime Minister Narendra Modi said 5G reached every district.", "ids": [5]},
          {"text": "He said one GB of data cost 268 rupees in 2014.", "ids": [6]}]]
    text = " ".join(s["text"] for p in P for s in p)
    names = people(text, list(sp.values()))
    refs = Refs(names, {"Narendra Modi": "he", "Tadasha Mishra": "she"}, roles(text, names))
    vary_attribution(P, refs, lambda s: [sp[i] for i in s["ids"]])
    t = [s["text"] for p in P for s in p]
    assert "advocate alleged" not in t[1] and "Centre" in t[1]
    assert not t[3].startswith("She") and "Supreme Court" in t[3]
    assert "Modi" not in t[5] and "he said" not in t[5].lower()


def test_validator_refuses_a_wrong_or_doubled_speaker():
    from nishpaksh import voice
    assert voice.wrong_speaker("The Jharkhand government violated the directions, the advocate alleged.",
                               ["Centre"], "The Centre alleged that the Jharkhand government violated", {}) == "advocate"
    assert voice.wrong_speaker("The appointment is in violation of the judgment, she said.", ["Supreme Court"],
                               "The Supreme Court stated that", {"Tadasha Mishra": "she"}) == "she"
    assert voice.wrong_speaker("The Central government alleged that the rules permit it.", ["Centre"], "x", {}) is None
    assert voice.wrong_speaker("The bench said the rule appeared to contradict the judgment.", ["Supreme Court"], "x", {}) is None
    assert voice.wrong_speaker("The paper will come within a month, the minister said.", ["Ashwini Vaishnaw"], "x", {}) is None
    assert voice.wrong_speaker("The rules are unfair, he said.", ["Kapil Sibal"], "x", {"Kapil Sibal": "he"}) is None
    assert voice.double_attribution("Solicitor General Tushar Mehta argued that the Centre argued that the rules changed.",
                                    "The Centre argued that the rules changed.")
    assert not voice.double_attribution("Police said that the accused claimed that he was innocent.",
                                        "Police said that the accused claimed that he was innocent.")


def test_one_speakers_argument_is_told_together():
    """Story 16197 (Oct 9 2026): the Centre's two lines sat in two paragraphs with other speakers between."""
    from nishpaksh.narrative import group_speakers
    by_id = {1: {"speaker": "Centre"}, 2: {"speaker": "Centre"}, 3: {"speaker": "petitioner"},
             4: {"speaker": "Central government"}, 5: {"speaker": "Kapil Sibal"}, 6: {}}
    for k, v in by_id.items():
        v.update(id=k, text="x", verdict="unverified")
    s = lambda i, t="A line.": {"text": t, "ids": [i]}
    paras = [[s(5)], [s(1), s(3)], [s(4), s(6, "He added more."), s(2)]]
    out, keys = group_speakers(paras, ["say"] * 3, by_id)
    assert [[x["ids"][0] for x in p] for p in out] == [[5], [1, 2], [3, 4, 6]] or \
        [[x["ids"][0] for x in p] for p in out] == [[5], [1, 4, 6, 2], [3]]
    assert keys == ["say"] * len(out)
