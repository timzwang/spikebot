"""Nudity check: blocked versions never reach review or a platform."""
import shutil
import subprocess
from pathlib import Path

import pytest

from spikebot import safety as SF

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
FRAME = (640, 360)


def det(cls, score, box=(0, 0, 100, 100)):
    return {"class": cls, "score": score, "box": list(box)}


def test_judge_blocks_exposed_and_sustained_sexual_framing_only():
    assert SF.judge([(0.0, [det("FACE_FEMALE", 0.9)], FRAME)])["ok"]
    r = SF.judge([(1.5, [det("FEMALE_BREAST_EXPOSED", 0.7)], FRAME)])
    assert not r["ok"] and "female_breast_exposed" in r["reason"] and "1.5s" in r["reason"]
    # one weak frame is noise, two weak frames block
    assert SF.judge([(0.0, [det("BUTTOCKS_EXPOSED", 0.35)], FRAME)])["ok"]
    assert not SF.judge([(t, [det("BUTTOCKS_EXPOSED", 0.35)], FRAME) for t in (0.0, 0.5)])["ok"]
    # a bikini in the background (small box) is fine; a sustained close-up (twerking) is not
    small = [(t / 2, [det("BUTTOCKS_COVERED", 0.8, (0, 0, 40, 40))], FRAME) for t in range(6)]
    assert SF.judge(small)["ok"]
    big = [(t / 2, [det("BUTTOCKS_COVERED", 0.8, (0, 0, 200, 200))], FRAME) for t in range(3)]
    assert not SF.judge(big)["ok"] and "sexual content" in SF.judge(big)["reason"]


@needs_ffmpeg
def test_blocked_versions_are_deleted_and_never_shown(tmp_path):
    for name in ("a.mp4", "b.mp4"):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc=d=1:s=320x180", str(tmp_path / name)], check=True)
    (tmp_path / "b_prev.mp4").write_bytes(b"x")
    calls = []

    def fake(frame):
        calls.append(frame.shape)
        return [det("FEMALE_GENITALIA_EXPOSED", 0.9)] if len(calls) > 2 else []
    vs = [{"label": "A", "file": "a.mp4"}, {"label": "B", "file": "b.mp4", "preview": "b_prev.mp4"}]
    clean, blocked = SF.screen_variants(vs, tmp_path, detector=fake)
    assert [v["label"] for v in clean] == ["A"] and clean[0]["safety"] == "ok"
    assert blocked[0]["label"] == "B" and "nudity" in blocked[0]["reason"]
    assert not (tmp_path / "b.mp4").exists() and not (tmp_path / "b_prev.mp4").exists()
    assert calls and calls[0][1] == SF.SAMPLE_W


def test_render_round_drops_blocked_versions_and_fails_if_all_are(tmp_path, monkeypatch):
    from spikebot import review as R
    from tests.test_review import FakeAdapter
    store = R.Store(tmp_path)
    item = store.create("tokyo_girl", {"day": "2026-11-19"}, 1_800_000_000, title="TG")
    monkeypatch.setattr(SF, "screen_variants",
                        lambda vs, folder, detector=None: (vs[:1], [{"label": v["label"], "reason": "nudity (x)"} for v in vs[1:]]))
    it = R.render_round(store, item["id"], {}, tmp_path, adapter=FakeAdapter(), seed=1)
    assert [v["label"] for v in it["rounds"][0]["variants"]] == ["A"] and "🔞 Blocked" in it["rounds"][0]["comment"]
    item2 = store.create("tokyo_girl", {"day": "2026-11-19"}, 1_800_000_001, title="TG2")
    monkeypatch.setattr(SF, "screen_variants", lambda vs, folder, detector=None: ([], [{"label": "A", "reason": "nudity (y)"}]))
    it2 = R.render_round(store, item2["id"], {}, tmp_path, adapter=FakeAdapter(), seed=1)
    assert it2["status"] == "failed" and "blocked" in it2["error"]


def test_publisher_never_posts_unchecked_or_nude(tmp_path, monkeypatch):
    from spikebot import review as R
    pub = R.Publisher.__new__(R.Publisher)

    def unavailable(path, **k):
        raise SF.Unavailable("nudity check unavailable: no nudenet")
    monkeypatch.setattr(SF, "scan", unavailable)
    assert "unavailable" in pub.safety_check({"safety": "unchecked"}, tmp_path / "f.mp4")
    assert pub.safety_check({"safety": "ok"}, tmp_path / "f.mp4") is None
    monkeypatch.setattr(SF, "scan", lambda path, **k: {"ok": False, "reason": "nudity (z)"})
    assert pub.safety_check({}, tmp_path / "f.mp4").startswith("🔞")


def test_library_shots_with_nudity_are_never_used():
    from spikebot import tokyo_girl as TG
    lcfg = TG.settings({})
    entry = {"kind": "video", "duration": 30.0, "cuts": [10.0, 20.0], "yavg": [], "w": 1920, "h": 1080,
             "crop": None, "motion": []}
    clean = TG.build_shots("t.mp4", entry, {}, lcfg)
    flagged = TG.build_shots("t.mp4", dict(entry, nsfw=[[15.0, "buttocks_covered"]]), {}, lcfg)
    assert len(flagged) == len(clean) - 1 and all(not (s.start <= 15.0 <= s.end) for s in flagged)
    img = {"kind": "image", "w": 1920, "h": 1080}
    assert TG.build_shots("a.jpg", img, {}, lcfg) and not TG.build_shots("a.jpg", dict(img, nsfw=[[0, "x"]]), {}, lcfg)


def test_frame_flag_is_strict_for_library_footage():
    assert SF.frame_flag([det("BUTTOCKS_EXPOSED", 0.31)], FRAME) == "buttocks_exposed"
    assert SF.frame_flag([det("FEMALE_BREAST_COVERED", 0.7, (0, 0, 200, 200))], FRAME)
    assert SF.frame_flag([det("FEMALE_BREAST_COVERED", 0.7, (0, 0, 30, 30))], FRAME) is None
