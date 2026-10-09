import dataclasses

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "settle: keep the wait for coverage to settle (editions.py)")


@pytest.fixture(autouse=True)
def two_databases(monkeypatch, tmp_path):
    """Production keeps reader tables and run logs in Supabase and the newsroom in the job's own Postgres
    (db.READER_TABLES, Oct 10 2026): every test runs the same way, each with its own readers' database, so a
    statement sent to the wrong database, or mixing the two, fails here."""
    monkeypatch.setenv("READERS_DATABASE_URL", f"sqlite:///{tmp_path / 'readers.db'}")


@pytest.fixture(autouse=True)
def model_verdicts_on(monkeypatch):
    """Red verdicts are on hold in production (SETTINGS.model_verdicts = False); the tests keep the
    machinery working for when a second model family is available."""
    import nishpaksh.run as run_mod
    monkeypatch.setattr(run_mod, "SETTINGS", dataclasses.replace(run_mod.SETTINGS, model_verdicts=True))


@pytest.fixture(autouse=True)
def no_settle_wait(monkeypatch, request):
    """Articles are written once coverage has settled (3 quiet hours). Tests run on articles fetched
    seconds ago, so the wait is off unless a test is about it (marked `settle`). No archive reads."""
    monkeypatch.setenv("NISHPAKSH_ARCHIVE_URL", "")
    if request.node.get_closest_marker("settle"):
        return
    import nishpaksh.editions as ed
    monkeypatch.setattr(ed, "SETTINGS", dataclasses.replace(ed.SETTINGS, settle_quiet_hours=0))


@pytest.fixture(autouse=True)
def pipeline_then_desk(monkeypatch):
    """In production the writing desk is its own job (desk.py); in tests a pipeline run is followed by
    the desk, so a run ends with what a reader would see."""
    import functools
    import nishpaksh.run as run_mod
    monkeypatch.setattr(run_mod, "run", functools.partial(run_mod.run, then_write=True))
