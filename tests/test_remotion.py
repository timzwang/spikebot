"""Remotion renderer: props built from a plan, and (when Node + the project are installed) a real render."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from spikebot import remotion as R
from spikebot import style as S
from spikebot import tokyo_girl as TG


def plan_of(tmp_path):
    shot = TG.Shot("beach.jpg", "image", 0.0, float("inf"), ["city"], None, 1920, 1080)
    vid = TG.Shot("trailer.mp4", "video", 0.0, 10.0, ["cars"], (1280, 600, 0, 60), 1280, 720)
    slots = [TG.Slot(shot, 0.0, 4, 0.0, 2.0, 0, False, 1),
             TG.Slot(vid, 1.5, 4, 2.0, 2.0, 1, True, -1, punch=True)]
    story = next(x for x in TG.STORIES if x.id == "partners")
    return TG.Plan(story, [(0.0, 2.0, "Jason & Lucia"), (2.0, 4.0, "Couple goals or *red flag?*")], slots, 4.0)


def test_props_from_plan(tmp_path):
    plan = plan_of(tmp_path)
    r = S.resolve(S.default_style("tokyo_girl") | {"grade": "punchy", "position": "middle",
                                                   "custom": {"grain": 6, "letterbox": 1}})
    logo = tmp_path / "logo.png"
    props, files = R.props_for(plan, {"font_size": 104}, r, plan.cards[1:], logo, 1.0, tmp_path)
    assert [s["src"] for s in props["shots"]] == ["m000.jpg", "m001.mp4"]
    assert files["m001.mp4"] == (tmp_path / "trailer.mp4").resolve()
    assert props["shots"][1]["crop"] == [1280, 600, 0, 60] and props["shots"][1]["punch"]
    assert props["logo"] == {"src": "m002.png", "until": 1.0, "out": 0.4, "size": 0.78}
    assert props["cards"] == [{"from": 2.0, "to": 4.0, "text": "Couple goals or *red flag?*", "ask": True}]
    assert props["look"]["saturation"] == 1.3 and props["look"]["letterbox"] and props["look"]["grain"] == 6
    assert props["text"]["y"] == R.TEXT_Y["middle"] and props["duration"] == 4.0
    json.dumps(props)


def test_falls_back_to_ffmpeg_when_remotion_fails(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(R, "available", lambda root=R.ROOT: True)

    def boom(*a, **k):
        raise R.RemotionError("no chrome")
    monkeypatch.setattr(R, "render", boom)
    monkeypatch.setattr(TG, "render_plan", lambda *a, **k: calls.append(a))
    ad = TG.ReviewAdapter({}, tmp_path, TG.settings({}, tmp_path), shots=[])
    r = S.resolve(S.default_style("tokyo_girl"))
    assert ad.render_edit(plan_of(tmp_path), tmp_path / "x.mp4", r, [], None, 0.0) == "ffmpeg" and calls


def _chrome() -> str | None:
    """REMOTION_BROWSER, else a preinstalled headless Chrome (CI sandboxes block Remotion's download)."""
    if os.environ.get("REMOTION_BROWSER"):
        return os.environ["REMOTION_BROWSER"]
    found = sorted(Path("/opt/pw-browsers").glob("chromium_headless_shell-*/chrome-headless-shell-linux64/"
                                                  "chrome-headless-shell")) if Path("/opt/pw-browsers").exists() else []
    return str(found[-1]) if found else None


CHROME = _chrome()


@pytest.mark.remotion
@pytest.mark.skipif(not (R.available() and shutil.which("ffmpeg")), reason="needs node + remotion/node_modules")
def test_real_remotion_render(tmp_path, monkeypatch):
    if CHROME:
        monkeypatch.setenv("REMOTION_BROWSER", CHROME)
    media = tmp_path / "media"
    media.mkdir()
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    subprocess.run(ff + ["-f", "lavfi", "-i", "gradients=s=1920x1080:seed=5", "-frames:v", "1",
                         str(media / "beach.jpg")], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=10", "-f", "lavfi", "-i", "sine=d=10",
                         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(media / "trailer.mp4")],
                   check=True)
    plan = plan_of(tmp_path)
    r = S.resolve(S.default_style("tokyo_girl"))
    out = tmp_path / "out.mp4"
    R.render(plan, {"media_dir": str(media), "data_dir": str(tmp_path)}, out, r, plan.cards)
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                                   "-show_entries", "format=duration", "-of", "json", str(out)],
                                  capture_output=True, text=True, check=True).stdout)
    assert [s["codec_type"] for s in j["streams"]] == ["video"]                  # silent
    assert (j["streams"][0]["width"], j["streams"][0]["height"]) == (1080, 1920)
    assert float(j["format"]["duration"]) == pytest.approx(4.0, abs=0.1)


def test_edits_render_at_60_fps_crf_18():
    """#39: Tokyo Girl / trend edits at 60 fps and CRF 18 in both renderers."""
    from spikebot import editor, remotion, tokyo_girl
    assert tokyo_girl.FPS == remotion.FPS == 60 and remotion.CRF == editor.CRF == 18
