"""Reader features (owner, Oct 9 2026): places, people, notifications, the recap, audio, videos, feed fields."""
import datetime as dt
import json

from nishpaksh import audio, notify, people, places, videos
from nishpaksh.db import (Store, audio_files, audio_requests, follows, insert, notifications, published,
                          push_subscriptions, recaps, select)
from nishpaksh.feed import card


def _store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'r.db'}")
    s.init()
    return s


def _payload(head, sents, cat=None, pl=None, ppl=None, parents=None):
    paras = [[{"text": t, "class": c, "ids": [k]} for k, (t, c) in enumerate(sents)]]
    return {"headline": head, "narrative": {"paragraphs": paras, "section_keys": ["news"]},
            "category": cat or {}, "places": pl or [], "people": ppl or [], "parents": parents or [],
            "counts": {"independent_sources": 4},
            "timeline": [], "undated": [{"id": 1, "kind": "event", "text": sents[0][0], "speaker": None}],
            "contested": [], "established": [], "context": []}


def _publish(store, sid, at, payload, hi=None):
    store.exec(insert(published).values(story_id=sid, version=1, updated_at=at, headline_en=payload["headline"],
                                        headline_hi=hi or payload["headline"], payload_en=payload,
                                        payload_hi=dict(payload, headline=hi or payload["headline"])))


def test_places_are_kept_only_when_the_article_names_them():
    text = "Police in Gurugram arrested two men. The Centre said in New Delhi it would act."
    assert places.confirm(["haryana", "delhi", "kerala"], text) == ["haryana"]       # New Delhi alone is not Delhi
    assert places.confirm(["Delhi"], "A fire in north Delhi killed three.") == ["delhi"]
    assert places.confirm(["uttar pradesh"], "लखनऊ में बारिश") == ["uttar-pradesh"]   # a city, in Hindi
    assert places.confirm(["atlantis"], "Atlantis") == []


def test_people_without_titles_and_counted_by_surname():
    p = _payload("Supreme Court issues notice to Jharkhand DGP Tadasha Mishra",
                 [("The Supreme Court issued a notice to Jharkhand DGP Tadasha Mishra on Friday.", "established"),
                  ("Mishra did not reply, and Authorities said the hearing is next week.", "single")])
    p["undated"] = [{"id": 1, "kind": "event", "text": p["narrative"]["paragraphs"][0][0]["text"], "speaker": None},
                    {"id": 2, "kind": "claim", "text": "Mishra did not reply", "speaker": "Chief Justice Surya Kant"}]
    got = people.from_payload(p)
    assert "Tadasha Mishra" in got and "Supreme Court" in got
    assert "Authorities" not in got and not any("DGP" in g for g in got)
    assert people.matches("mishra", got) and not people.matches("modi", got)


def test_a_new_article_notifies_its_followers_once(tmp_path):
    s = _store(tmp_path)
    t0 = dt.datetime(2026, 10, 9, 10, 0)
    assert notify.queue_articles(s, t0) == 0                  # the first look only sets the starting point
    for reader, kind, key in [("r1", "section", "justice/courts"), ("r2", "place", "haryana"),
                              ("r3", "entity", "mishra"), ("r4", "section", "world"), ("r5", "story", "100"),
                              ("r6", "section", "business/domains/hr")]:
        s.exec(insert(follows).values(reader=reader, kind=kind, key=key))
    _publish(s, 200, t0 + dt.timedelta(minutes=5),
             _payload("Court orders Haryana DGP Tadasha Mishra to appear", [("The court ordered it.", "established"),
                                                                            ("He will appear.", "single")],
                      cat={"primary": ["justice"], "secondary": ["courts"]}, pl=["haryana"], ppl=["Tadasha Mishra"],
                      parents=[{"story_id": 100, "headline": "earlier"}]))
    _publish(s, 201, t0 + dt.timedelta(minutes=6),
             _payload("TCS to cut 12,000 jobs", [("TCS said it would cut jobs.", "established")],
                      cat={"primary": ["business"], "secondary": ["companies", "domains"], "tertiary": ["hr"]}))
    assert notify.queue_articles(s, t0 + dt.timedelta(minutes=10)) == 5
    rows = {(r["reader"], r["ref"]): r for r in s.rows(select(notifications))}
    assert set(rows) == {("r1", "200"), ("r2", "200"), ("r3", "200"), ("r5", "200"), ("r6", "201")}
    assert "Courts" in rows[("r1", "200")]["body"] and "50% established" in rows[("r1", "200")]["body"]
    assert rows[("r5", "200")]["body"].startswith("Update to a story you follow")
    assert notify.queue_articles(s, t0 + dt.timedelta(minutes=20)) == 0     # never twice

    s.exec(insert(push_subscriptions).values(endpoint="https://push/a", reader="r1", p256dh="k", auth="a", lang="en", failures=0))
    s.exec(insert(push_subscriptions).values(endpoint="https://push/gone", reader="r1", p256dh="k", auth="a", lang="en", failures=0))
    sent = []
    st = notify.send_pending(s, t0 + dt.timedelta(minutes=21),
                             pusher=lambda sub, data: (sent.append((sub["endpoint"], data["title"])), 410 if "gone" in sub["endpoint"] else 201)[1])
    assert st["pushed"] == 1 and st["gone"] == 1 and st["notifications"] == 5
    assert [e for e, _ in sent].count("https://push/a") == 1
    assert len(s.rows(select(push_subscriptions))) == 1
    assert all(r["sent_at"] for r in s.rows(select(notifications)))


def test_the_daily_recap_is_made_once_late_in_the_ist_day(tmp_path):
    s = _store(tmp_path)
    s.exec(insert(follows).values(reader="r1", kind="recap", key="daily"))
    day = dt.datetime(2026, 10, 9, 4, 0)                       # 09:30 IST
    _publish(s, 300, day, _payload("One", [("First lead.", "established"), ("Second.", "single")]), hi="एक")
    _publish(s, 301, day + dt.timedelta(hours=5), _payload("Two", [("Lead two.", "developing")]))
    assert notify.make_recap(s, day + dt.timedelta(hours=6)) is None          # 15:30 IST: not yet
    late = dt.datetime(2026, 10, 9, 17, 50)                    # 23:20 IST
    assert notify.make_recap(s, late) == "2026-10-09"
    assert notify.make_recap(s, late + dt.timedelta(minutes=30)) is None      # once a day
    r = s.one(select(recaps))
    assert [x["id"] for x in r["payload_en"]["stories"]] == [300, 301]
    assert r["payload_hi"]["stories"][0]["h"] == "एक"
    assert r["payload_en"]["stories"][0]["lead"][0] == {"t": "First lead.", "c": "established"}
    n = s.one(select(notifications))
    assert n["kind"] == "recap" and n["url"].endswith("#/recap/2026-10-09")


class FakeVoices:
    def __init__(self, n=50):
        self.n = n
        self.calls = []

    def left(self):
        return self.n - len(self.calls)

    def speak(self, text, lang):
        if self.left() <= 0:
            return None
        self.calls.append((text, lang))
        return b"\x00\x00" * (audio.RATE * max(1, len(text) // 100)), "gemini-tts-test"


def test_audio_is_made_once_per_story_and_language_and_its_readers_are_told(tmp_path):
    s = _store(tmp_path)
    p = _payload("Headline here", [("A first sentence.", "established"), ("A second one.", "single")])
    p["narrative"]["paragraphs"].append([{"text": "Background line.", "class": "established"}])
    p["narrative"]["section_keys"] = ["news", "background"]
    _publish(s, 400, dt.datetime(2026, 10, 9, 10), p)
    for reader in ("a", "b"):
        s.exec(insert(audio_requests).values(reader=reader, story_id=400, lang="en", status="queued",
                                             requested_at=dt.datetime(2026, 10, 9, 11)))
    units = audio.article_units("Headline here", p, "en")
    assert [u["text"] for u in units] == ["Headline here", "A first sentence.", "A second one.", "Background.",
                                          "Background line."]
    assert [(u["p"], u["s"]) for u in units] == [(-1, -1), (0, 0), (0, 1), (1, -1), (1, 0)]
    out = tmp_path / "audio"
    v = FakeVoices()
    st = audio.work(s, out, v, encoder=lambda pcm, path: path.write_bytes(b"m4a"))
    assert st["made"] == ["400-en"]
    assert len(v.calls) == 1                                   # one chunk; two requests, one audio
    tm = json.loads((out / "400-en.json").read_text())
    assert [u["t"] for u in tm["units"]] == sorted(u["t"] for u in tm["units"]) and tm["units"][0]["t"] == 0
    assert {r["status"] for r in s.rows(select(audio_requests))} == {"done"}
    assert {(r["reader"], r["kind"], r["ref"]) for r in s.rows(select(notifications))} == {("a", "audio", "400-en"), ("b", "audio", "400-en")}
    assert s.one(select(audio_files))["url"] == "/audio/400-en.m4a"
    # nothing left to do; once the article leaves the live pages, its audio goes
    assert audio.work(s, out, v, encoder=lambda pcm, path: None)["made"] == []
    from nishpaksh.db import delete
    s.exec(delete(published).where(published.c.story_id == 400))
    assert audio.prune(s, out) == 1 and not list(out.glob("400-en.*"))


def test_audio_waits_when_the_day_has_too_few_calls_left(tmp_path):
    s = _store(tmp_path)
    p = _payload("H", [("x " * 400, "established")] * 6)
    _publish(s, 401, dt.datetime(2026, 10, 9, 10), p)
    s.exec(insert(audio_requests).values(reader="a", story_id=401, lang="hi", status="queued", requested_at=dt.datetime(2026, 10, 9, 11)))
    v = FakeVoices(n=1)
    st = audio.work(s, tmp_path / "o", v, encoder=lambda pcm, path: None)
    assert st["made"] == [] and st["waiting"] == 1 and v.calls == []          # no half-made audio
    assert s.one(select(audio_requests))["status"] == "queued"


def test_timing_shares_each_chunk_by_length():
    units = [{"text": "a" * 88, "p": 0, "s": 0}, {"text": "b" * 188, "p": 0, "s": 1}]
    tm = audio.timing([units], [10.0])
    assert tm[0]["t"] == 0 and 3.0 < tm[1]["t"] < 3.5


def test_videos_keep_youtube_order_two_per_channel_both_languages():
    en = [{"id": f"e{k}", "channel": "NDTV" if k < 3 else f"C{k}", "channelId": "ndtv" if k < 3 else f"c{k}"} for k in range(6)]
    hi = [{"id": f"h{k}", "channel": "Aaj Tak", "channelId": "aaj"} for k in range(4)]
    got = [v["id"] for v in videos.pick([en, hi], keep=7)]
    assert got == ["e0", "h0", "e1", "h1", "e3", "e4", "e5"]
    assert videos.is_primary("Sansad TV") and not videos.is_primary("NDTV")


def test_videos_are_fetched_once_for_new_articles(tmp_path):
    s = _store(tmp_path)
    _publish(s, 500, dt.datetime.utcnow(), _payload("Floods in Assam", [("Floods.", "established")]), hi="असम में बाढ़")
    asked = []

    class R:
        def __init__(self, q):
            self.q = q

        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [{"id": {"videoId": self.q[:2] + "1"}, "snippet": {"title": "A &amp; B", "channelTitle": "PIB India",
                                                                                 "channelId": "pib", "publishedAt": "2026-10-09T10:00:00Z"}}]}

    def get(url, params, timeout):
        asked.append(params["relevanceLanguage"])
        return R(params["q"])
    assert videos.fetch_new(s, key="k", get=get) == [500]
    assert asked == ["en", "hi"]
    from nishpaksh.db import videos as vt
    items = s.one(select(vt))["items"]
    assert items[0]["title"] == "A & B" and items[0]["primary"] is True
    assert videos.fetch_new(s, key="k", get=get) == []         # once
    assert videos.fetch_new(s, key="", get=get) == []          # no key: nothing


def test_feed_card_carries_length_places_and_people():
    p = _payload("H", [("One two three.", "established")], pl=["bihar"], ppl=["Nitish Kumar"])
    c = card({"story_id": 1, "updated_at": dt.datetime(2026, 10, 9), "headline_en": "H", "headline_hi": "ह",
              "payload_en": p, "payload_hi": p}, "en")
    assert c["w"] == 3 and c["pl"] == ["bihar"] and c["pp"] == ["Nitish Kumar"]


def test_section_paths_cover_every_level():
    assert notify.section_paths({"primary": ["business"], "secondary": ["domains"], "tertiary": ["hr"]}) == \
        {"business", "business/domains", "business/domains/hr"}
    assert "business/domains/hr" in notify.section_paths({"primary": ["business"], "secondary": ["jobs"]})  # old key
