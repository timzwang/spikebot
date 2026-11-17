import pytest


@pytest.fixture(autouse=True)
def _no_real_ai(monkeypatch):
    """Tests never call the real Claude API (a developer's key in the environment is ignored)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _ffmpeg_renderer_unless_marked(request, monkeypatch):
    """Tokyo Girl renders use the fast ffmpeg path in tests; tests marked `remotion` use Remotion."""
    if request.node.get_closest_marker("remotion") is None:
        import spikebot.remotion as R
        monkeypatch.setattr(R, "available", lambda root=R.ROOT: False)


def pytest_configure(config):
    config.addinivalue_line("markers", "remotion: renders with Remotion (needs node + remotion/node_modules)")
