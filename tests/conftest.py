import dataclasses

import pytest


@pytest.fixture(autouse=True)
def model_verdicts_on(monkeypatch):
    """Red verdicts are on hold in production (SETTINGS.model_verdicts = False); the tests keep the
    machinery working for when a second model family is available."""
    import nishpaksh.run as run_mod
    monkeypatch.setattr(run_mod, "SETTINGS", dataclasses.replace(run_mod.SETTINGS, model_verdicts=True))
