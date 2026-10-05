import datetime as dt

import numpy as np
import pytest

from nishpaksh.db import Store, articles, canonical, claims, insert, published, select, source_clusters, stories, story_pairs, update
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
    assert s.wait_time(4000, now) == pytest.approx(10.2, abs=0.01)   # oldest call must age out
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
    # six hours later, with nothing new: the page is re-checked and the collapse is established
    _age_rules(store)
    run(store=store, backend=backend, time_budget_min=30, ingest_news=False, verify_budget=VB)
    pub = store.one(select(published).where(published.c.story_id == contested["id"]))
    assert pub["version"] == 2
    en, hi = pub["payload_en"], pub["payload_hi"]
    # 5 articles; the two PTI copies count once -> 4 independent sources
    assert en["counts"] == {"articles": 5, "independent_sources": 4, "outlets": 5}

    timeline_text = [i["text"] for tier in en["timeline"] for i in tier]
    assert any("collapsed" in t for t in timeline_text)
    assert not any("rain" in t.lower() for t in timeline_text)   # one-sided: not in the timeline
    # four outlets report the arrest, but every one of them got it from the police: one origin
    # (the police and the PWD minister speak for the same state government), so not established
    cont0 = {i["text"]: i for i in en["contested"]}
    arrest = cont0["Police arrested the site engineer"]
    assert arrest["verdict"] == "unverified" and arrest["origins"] == ["gov:uttar pradesh"]
    collapse = next(i for tier in en["timeline"] for i in tier if "collapsed" in i["text"])
    assert collapse["verdict"] == "corroborated" and collapse["n_origins"] >= 2 and collapse["n_outlets"] >= 3

    est = {i["text"]: i for i in en["established"]}
    assert any("died" in t or "killed" in t for t in est)        # paraphrases merged and corroborated

    cont = {i["text"]: i for i in en["contested"]}
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

    # another run with nothing new: no reprocessing, no new version
    run(store=store, backend=backend, time_budget_min=30, ingest_news=False, verify_budget=VB)
    assert store.one(select(published).where(published.c.story_id == contested["id"]))["version"] == 2


def test_headline_with_loaded_word_is_rejected(store):
    from nishpaksh.run import run

    class Loaded(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            if "Write the headline for this news story" in prompt:
                return '{"headline": "Shoddy flyover collapses in Kesarganj"}', [], 10
            return super().generate(model, prompt, json_mode, grounded)
    _seed(store)
    run(store=store, backend=Loaded(), time_budget_min=30, ingest_news=False, verify_budget=VB)
    # the loaded headline is rejected; with only the code fallback left, the new story waits
    assert store.rows(select(published)) == []
    assert store.rows(select(stories.c.dirty).where(stories.c.qualifies.is_(True)))[0]["dirty"] is True


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
    assert len(b.starts) == 4                 # never more than rpm calls in the minute
    assert results.count({"ok": 1}) == 4 and results.count("QuotaExhausted") == 4


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
    assert nar["rejected"] == 4
    # an allegation with a known speaker names the speaker; nothing unconfirmed reads as plain fact
    for para in paras:
        if any(x["class"] in ("unverified", "developing") for x in para):
            assert any(w in " ".join(x["text"].lower() for x in para)
                       for w in ("reportedly", "reports said", "said", "alleg", "according to"))
    assert all(x["class"] == "established" for x in paras[0])            # essay opens with what is settled
    # the red statement's sentence never said "false": rejected, so the statement is listed under the
    # essay in plain words (rejected sentences are dropped, never patched into the prose)
    also = nar["also"]
    false_s = [x for x in sents + also if x["class"] == "false"]
    assert false_s and "substandard" in false_s[0]["text"] and "false" in false_s[0]["text"]
    assert not [x for x in sents if x["class"] == "false"]
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
            for n, a, b in _re.findall(r'(\d+)\. N: "(.*?)" \| S: "(.*?)"', prompt):
                ta = _re.search(r"\[(\w+)\]", a)
                tb = _re.search(r"\[(\w+)\]", b)
                res.append({"n": int(n), "same": bool(ta and tb and ta.group(1) == tb.group(1))})
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


def test_headline_checks_catch_bare_names_and_tacked_on_hedges():
    from nishpaksh.compose import _headline_problem
    src = "kumar was produced before the court in kotdwar"
    assert "bare name" in _headline_problem("Kumar was produced before the court in Kotdwar", ["x"], src, set())
    assert "tack" in _headline_problem("Gym trainer produced before Kotdwar court, reportedly", ["x"], src, set())
    assert "12 words" in _headline_problem(" ".join(["word"] * 15), ["x"], src, set())
    assert _headline_problem("Gym trainer who defended shopkeeper produced before Kotdwar court", ["x"], src, set()) is None


def test_filler_is_never_published(store):
    from nishpaksh import compose
    _seed(store)
    _run_twice(store)
    sid = store.rows(select(published.c.story_id))[0]["story_id"]
    store.exec(update(stories).where(stories.c.id == sid).values(signature="Aaj ka Rashifal"))
    an = dict(store.one(select(stories.c.analysis).where(stories.c.id == sid))["analysis"])
    an.pop("importance", None)
    store.exec(update(stories).where(stories.c.id == sid).values(analysis=an))

    class Horoscope(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            if "Rate how important" in prompt:
                return '{"score": 1, "filler": true, "reason": "horoscope"}', [], 10
            return super().generate(model, prompt, json_mode, grounded)
    from nishpaksh.config import load_yaml
    r = Router(load_yaml("models.yaml")["tiers"], Horoscope(), store)
    r.resolve()
    assert compose.publish_story(store, r, sid) is False
    assert store.rows(select(published).where(published.c.story_id == sid)) == []


def test_a_later_development_links_to_its_story_and_gets_background(store):
    """A bail hearing for the arrested engineer is a development of the flyover collapse: the new page
    links back, opens with the new development, and the old page gains 'what happened next'."""
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
    assert store.rows(select(story_links)) and store.one(select(stories.c.dirty).where(stories.c.id == parent))["dirty"]
    # an unrelated story with no shared name is never linked
    other = store.insert_returning_id(stories, dict(created_at=later, updated_at=later, dirty=False, qualifies=True,
                                                   signature="Monsoon session of Lucknow assembly adjourned"))
    assert threads.find_parents(store, r, other, "Monsoon session of Lucknow assembly adjourned", "") == []
    compose.publish_story(store, r, parent)
    page = store.one(select(published).where(published.c.story_id == parent))["payload_en"]
    assert page["children"] == [] or page["children"][0]["story_id"] == child   # child shows once it is published
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
            {"text": "He added that Ukraine is ready for an energy truce.", "ids": [2]},
            {"text": "He said Russia is not interested in peace talks.", "ids": [3]}]   # Merz's claim, not Sybiha's
    paras, dropped, rejected = _check_paragraphs([para], by, set(), [])
    assert [s["ids"] for s in paras[0]] == [[1], [2]] and rejected == 1 and dropped == [3]


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
    assert re2["also"][0]["ids"] == [2] and "denied" in re2["also"][0]["text"]
    # a new statement that is not minor is material: worth a rewrite when the writer can afford one
    items.append(_item(3, "The state ordered an inquiry", "developing"))
    assert needs_rewrite(re2, _payload(items))


def test_new_story_waits_for_the_writer_and_live_page_keeps_its_essay(store):
    """No writer quota: a new story is not published as stitched sentences; a page already written
    by the writer stays up with its essay. A page that was only ever stitched is taken down."""
    from nishpaksh import compose
    from nishpaksh.router import QuotaExhausted
    _seed(store)
    _run_twice(store)
    row = store.rows(select(published))[0]
    sid = row["story_id"]
    essay = [s["text"] for p in row["payload_en"]["narrative"]["paragraphs"] for s in p]

    class NoWriter(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            if "Write the story below as ONE news article" in prompt:
                raise RuntimeError("429 RESOURCE_EXHAUSTED: GenerateRequestsPerDay")
            return super().generate(model, prompt, json_mode, grounded)
    r = _router(store, NoWriter())
    # a new statement arrives (material): the rewrite fails for quota, the written essay stays
    nar = dict(row["payload_en"]["narrative"], covers=[], hash="changed")
    pe = dict(row["payload_en"], narrative=nar)
    store.exec(update(published).where(published.c.story_id == sid).values(payload_en=pe))
    assert compose.publish_story(store, r, sid)
    kept = store.one(select(published).where(published.c.story_id == sid))["payload_en"]["narrative"]
    assert [s["text"] for p in kept["paragraphs"] for s in p][:2] == essay[:2] and kept.get("recoloured")
    # a page stitched by code (no writer model) is not kept: taken down until the writer can do it
    pe = dict(row["payload_en"], narrative=dict(kept, model=None))
    store.exec(update(published).where(published.c.story_id == sid).values(payload_en=pe))
    assert compose.publish_story(store, r, sid) is False
    assert store.rows(select(published).where(published.c.story_id == sid)) == []
    assert store.one(select(stories.c.dirty).where(stories.c.id == sid))["dirty"] is True
    # so is one written by Flash-Lite
    assert compose._keepable({"model": "gemini-3.5-flash-lite", "paragraphs": [[{}]]}) is False


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
