import pytest


@pytest.fixture(autouse=True)
def _seeded_covers(monkeypatch):
    """Tests build schemes from fixed seeds; keep covers reproducible too."""
    monkeypatch.setenv("RANDOM_COVERS", "0")
