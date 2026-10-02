import os

from streamlit.testing.v1 import AppTest

from nishpaksh.db import Store, published, select
from nishpaksh.run import run

from .fixtures import FakeBackend
from .test_pipeline import _seed

VB = {"grounded": 5, "judge": 5}
APP = os.path.join(os.path.dirname(__file__), "..", "app", "streamlit_app.py")


def _db(tmp_path):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    s = Store(url)
    s.init()
    _seed(s)
    run(store=s, backend=FakeBackend(), ingest_news=False, verify_budget=VB)
    return url, s


def test_app_renders_list_and_story_in_both_languages(tmp_path, monkeypatch):
    url, s = _db(tmp_path)
    monkeypatch.setenv("DATABASE_URL", url)
    sid = s.rows(select(published.c.story_id))[0]["story_id"]

    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("Kesarganj" in m.value for m in at.markdown)

    at = AppTest.from_file(APP, default_timeout=30)
    at.query_params["story"] = str(sid)
    at.query_params["lang"] = "en"
    at.run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert 'class="np-s false"' in text and "substandard" in text      # red sentence in the story
    assert 'class="np-legend"' in text and 'id="src-1"' in text         # legend and numbered sources

    at = AppTest.from_file(APP, default_timeout=30)
    at.query_params["story"] = str(sid)
    at.query_params["lang"] = "hi"
    at.run()
    assert not at.exception
    assert any("[हिं]" in h.value for h in at.header)
    text = " ".join(m.value for m in at.markdown)
    assert "इस ख़बर को कैसे पढ़ें" in text and "जहाँ ख़बरें अलग-अलग हैं" in text
