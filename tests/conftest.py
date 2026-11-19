import pytest


@pytest.fixture(autouse=True)
def _no_real_ai(monkeypatch):
    """Tests never call the real Claude API (a developer's key in the environment is ignored)
    or a real text-to-speech service (render subprocesses inherit the switch)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SPIKEBOT_VOICE", "off")


@pytest.fixture(autouse=True)
def _ffmpeg_renderer_unless_marked(request, monkeypatch):
    """Tokyo Girl renders use the fast ffmpeg path in tests; tests marked `remotion` use Remotion."""
    if request.node.get_closest_marker("remotion") is None:
        import spikebot.remotion as R
        monkeypatch.setattr(R, "available", lambda root=R.ROOT: False)


def pytest_configure(config):
    config.addinivalue_line("markers", "remotion: renders with Remotion (needs node + remotion/node_modules)")


@pytest.fixture(autouse=True)
def _no_nudity_model_on_test_patterns(request, monkeypatch):
    """The nudity model sees things in synthetic test patterns (mandelbrot, test cards), which
    would randomly drop shots and versions in render tests. Those tests get a clean scan;
    tests/test_safety.py tests the real decision logic with its own fake detector."""
    if request.module.__name__.endswith("test_safety"):
        return
    from spikebot import safety
    monkeypatch.setattr(safety, "flag_times", lambda *a, **k: [])
    real_scan = safety.scan

    def scan(path, fps=safety.SAMPLE_FPS, detector=None):
        return real_scan(path, fps, detector=detector or (lambda frame: []))
    monkeypatch.setattr(safety, "scan", scan)
    monkeypatch.setattr(safety, "find_facecam", lambda *a, **k: None)
    monkeypatch.setattr(safety, "real_facecam", lambda *a, **k: None)   # synthetic streams have no faces


@pytest.fixture(autouse=True)
def _no_real_downloads(monkeypatch):
    """Tests never download from Rockstar / YouTube (a new source makes `daily` fetch at once).
    A test that checks fetching patches TG.fetch itself."""
    from spikebot import tokyo_girl as TG
    monkeypatch.setattr(TG, "fetch", lambda lcfg: None)
