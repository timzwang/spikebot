import pytest


@pytest.fixture(autouse=True)
def _no_real_ai(monkeypatch):
    """Tests never call the real Claude API (a developer's key in the environment is ignored)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
