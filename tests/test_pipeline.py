import datetime as dt

import numpy as np
import pytest

from nishpaksh.db import Store, articles, canonical, insert, published, select, source_clusters, stories, story_pairs
from nishpaksh.router import ModelSlot, Router, parse_json
from nishpaksh.timeline import build_timeline
from nishpaksh.wire import independence_groups, jaccard, minhash

from .fixtures import ARTICLES, NOW, FakeBackend


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


def test_global_clusters_emerge_from_roll_call(store):
    from nishpaksh.perspectives import recompute_global
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

def _seed(store):
    for i, a in enumerate(ARTICLES):
        store.exec(insert(articles).values(
            url=f"https://example.com/{i}", feed_id=None, outlet=a["outlet"], lang=a["lang"], role="news",
            title=a["title"], author=a["author"], published_at=NOW - dt.timedelta(hours=2 + i), fetched_at=NOW,
            text=a["text"], text_source="full", agency="PTI" if a["author"] == "PTI" else None,
            minhash=minhash(a["text"]), extract_failures=0))


def test_end_to_end(store):
    from nishpaksh.run import run
    _seed(store)
    backend = FakeBackend()
    stats = run(store=store, backend=backend, time_budget_min=30, ingest_news=False)
    # the second PTI copy is the same source as the first, so it is never sent to a model
    assert stats["extracted"] == len(ARTICLES) - 1

    sts = store.rows(select(stories))
    assert len(sts) == 2                                         # Hindi + English grouped together
    contested = next(s for s in sts if "flyover" in s["signature"])
    consensus = next(s for s in sts if "assembly" in s["signature"])
    assert contested["qualifies"] and contested["analysis"]["mode"] == "story"
    assert not consensus["qualifies"]                            # one perspective only: not published

    pub = store.one(select(published).where(published.c.story_id == contested["id"]))
    en, hi = pub["payload_en"], pub["payload_hi"]
    # 5 articles; the two PTI copies count once -> 4 independent sources
    assert en["counts"] == {"articles": 5, "independent_sources": 4}

    timeline_text = [i["text"] for tier in en["timeline"] for i in tier]
    assert any("collapsed" in t for t in timeline_text)
    assert any("arrested" in t for t in timeline_text)
    assert not any("rain" in t.lower() for t in timeline_text)   # one-sided: not in the timeline
    order = [i["text"] for tier in en["timeline"] for i in tier]
    assert order.index(next(t for t in order if "collapsed" in t)) < order.index(next(t for t in order if "arrested" in t))

    est = {i["text"]: i for i in en["established"]}
    assert any("died" in t or "killed" in t for t in est)        # paraphrases merged and corroborated

    cont = {i["text"]: i for i in en["contested"]}
    sub = cont["The contractor used substandard material"]
    assert sub["verdict"] == "false"                             # both models, primary evidence
    assert sub["check"]["evidence_urls"] == ["https://example.org/order"]
    rain = next(i for t, i in cont.items() if "rain" in t.lower() and i["kind"] == "event")
    assert rain["verdict"] == "unverified"
    rel = next(i for i in en["contested"] if i["kind"] == "relation")
    assert "because of" in rel["text"] and rel["verdict"] == "unverified"

    framing = {f["text"]: f["words"] for f in en["framing"]}
    words = framing["The contractor used substandard material"]
    assert any("shoddy" in w for ws in words.values() for w in ws)
    assert any("साज़िश" in w for ws in words.values() for w in ws)

    assert en["headline"].startswith("Section of Kesarganj")
    assert hi["headline"].startswith("[हिं]") and hi["translation_complete"]

    # second run with nothing new: no reprocessing, no new version
    run(store=store, backend=backend, time_budget_min=30, ingest_news=False)
    assert store.one(select(published).where(published.c.story_id == contested["id"]))["version"] == 1


def test_headline_with_loaded_word_is_rejected(store):
    from nishpaksh.run import run

    class Loaded(FakeBackend):
        def generate(self, model, prompt, json_mode, grounded):
            if "Write one news headline" in prompt:
                return '{"headline": "Shoddy flyover collapses in Kesarganj"}', [], 10
            return super().generate(model, prompt, json_mode, grounded)
    _seed(store)
    run(store=store, backend=Loaded(), time_budget_min=30, ingest_news=False)
    pub = store.rows(select(published))[0]
    assert "shoddy" not in pub["headline_en"].lower()


def test_quota_exhaustion_degrades_safely(store):
    """With no judge quota, nothing is ever marked false."""
    from nishpaksh.run import run

    class NoJudge(FakeBackend):
        def list_models(self):
            return [m for m in super().list_models() if "3.8" not in m and "3.7" not in m]
    _seed(store)
    run(store=store, backend=NoJudge(), time_budget_min=30, ingest_news=False)
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
