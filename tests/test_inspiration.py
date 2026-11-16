"""#inspiration style analysis: pure maths, OCR with a fake reader, and an end-to-end run."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from spikebot import inspiration as I
from spikebot import presets as P

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_links_and_hosts():
    text = "look https://www.tiktok.com/@x/video/1 and https://evil.example/v.mp4 (https://youtu.be/abcdefghijk)."
    assert I.links(text) == ["https://www.tiktok.com/@x/video/1", "https://youtu.be/abcdefghijk"]
    assert I.platform_of("https://www.instagram.com/reel/x/") == "instagram"
    with pytest.raises(I.InspirationError):
        I.download("https://evil.example/v.mp4", Path("."))


def test_tempo_and_beat_alignment():
    import numpy as np
    hop = I.HOP / I.SR
    env = np.zeros(int(8 / hop))
    for k in range(16):           # clicks every 0.5 s = 120 BPM, starting at 0.25 s
        env[int((0.25 + k * 0.5) / hop)] = 1.0
    bpm, phase = I.tempo(env)
    assert bpm == pytest.approx(120, abs=3) and phase == pytest.approx(0.25, abs=0.03)
    assert I.on_beat_share([0.75, 1.25, 2.25, 3.0], bpm, phase) == 0.75
    assert I.loudest_point(env) is not None
    assert I.tempo(np.zeros(100)) == (0.0, 0.0)


def test_pacing_numbers():
    p = I.pacing([1.0, 1.5, 3.0], 4.0)
    assert p == {"cuts": 3, "avg_shot_s": 1.0, "cuts_per_s": 0.75, "shortest_s": 0.5, "longest_s": 1.5}
    assert [I.pace_name(x) for x in (0.6, 1.5, 3)] == ["fast", "normal", "slow"]


def test_colour_to_grade():
    frames = [{"t": 0, "YAVG": 120, "YLOW": 20, "YHIGH": 240, "SATAVG": 50, "UAVG": 120, "VAVG": 140}] * 3
    c = I.colour(frames)
    assert c["tone"].startswith("warm") and c["saturation"] == 50
    g = I.grade_from(c)
    assert g["saturation"] == pytest.approx(50 / I.REF_SAT, abs=0.01) and g["warmth"] > 0
    assert g["grade"] in ("vivid", "warm", "punchy")
    flat = I.grade_from(I.colour([{"t": 0, "YAVG": 112, "YLOW": 28, "YHIGH": 228, "SATAVG": 32,
                                   "UAVG": 128, "VAVG": 128}]))
    assert flat["grade"] == "natural"
    ys = [{"t": t / 10, "YAVG": 250 if 9 <= t <= 11 else 100} for t in range(30)]
    assert I.transitions(ys, [1.0, 2.5]) == {"flash": 1, "fade": 0}


def test_text_settings_from_ocr():
    t = {"found": True, "y": 0.3, "font_px": 100, "outline": True, "words_per_screen": 3.4,
         "colours": ["#FFFFFF", "#FFE500"]}
    s = I.text_settings(t)
    assert s["position"] == "high" and s["size"] == "l" and s["text_px"] == 100
    assert s["text_colour"] == "#FFFFFF" and s["highlight"] == "#FFE500" and s["words"] == 3
    assert I.text_settings({"found": False}) == {}


def ref_video(path: Path, seconds: int = 6) -> None:
    """Fast cuts every 0.5 s on a 120 BPM click, yellow outlined text near the top."""
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=s=540x960:r=30:d={seconds}",
                    "-f", "lavfi", "-i", f"color=c=0x2060c0:s=540x960:r=30:d={seconds}",
                    "-f", "lavfi", "-i", f"aevalsrc='if(lt(mod(t,0.5),0.03),sin(2*PI*1000*t),0)':s=44100:d={seconds}",
                    "-filter_complex", "[0:v][1:v]blend=all_expr='if(lt(mod(T,1),0.5),A,B)',"
                    f"drawtext=fontfile={font}:text='POV you need this':fontsize=56:fontcolor=yellow:borderw=5:"
                    "x=(w-tw)/2:y=h*0.25[v]", "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset",
                    "ultrafast", "-c:a", "aac", str(path)], check=True)


@needs_ffmpeg
def test_end_to_end_with_fake_ocr(tmp_path):
    ref = tmp_path / "upload.mp4"
    ref_video(ref)

    def reader(img):  # stands in for easyocr.Reader.readtext
        h, w = img.shape[:2]
        y0, y1 = int(h * 0.23), int(h * 0.31)
        return [([[40, y0], [w - 40, y0], [w - 40, y1], [40, y1]], "POV you need this", 0.9)]
    out = tmp_path / "out"
    out.mkdir()
    res = I.run("ref", ["pacing", "colour", "text", "hook", "music-sync"], tmp_path, out, file=ref, reader=reader)
    assert res["ok"], res
    assert not ref.exists()                       # the reference is never kept
    assert Path(res["strip"]).exists()
    p = P.Presets(tmp_path / "presets").load("ref")
    a = p["analysis"]
    assert a["pacing"]["cuts"] >= 4 and a["pacing"]["avg_shot_s"] <= 1.3
    assert a["music"]["bpm"] == pytest.approx(120, abs=6) and a["music"]["on_beat"] >= 0.5
    assert a["text"]["found"] and a["text"]["hook"] == "POV you need this"
    assert p["settings"]["hook"]["hook_style"] == "pov"
    assert p["settings"]["text"]["position"] == "high"
    assert p["settings"]["pacing"]["pace"] in ("fast", "normal")
    assert "Preset `ref` saved" in res["text"] and "never the words" in res["text"]
    assert len(res["text"]) < 2000
    style = P.to_style(p, "tokyo_girl")
    assert style["custom"]["hook_style"] == "pov" and "transition" not in style  # transitions not copied


@needs_ffmpeg
def test_failed_link_asks_for_upload(tmp_path, monkeypatch):
    def boom(url, folder):
        raise I.InspirationError("login required")
    monkeypatch.setattr(I, "download", boom)
    res = I.run("x", list(P.PARTS), tmp_path, tmp_path, url="https://www.instagram.com/reel/abc/")
    assert res == {"ok": False, "error": "login required", "ask_upload": True}
    assert not P.Presets(tmp_path / "presets").names()


@needs_ffmpeg
def test_cli_writes_json(tmp_path, monkeypatch):
    ref = tmp_path / "r.mp4"
    ref_video(ref, 4)
    (tmp_path / "config.json").write_text(json.dumps({"data_dir": "data"}))
    monkeypatch.setattr(I, "read_text", lambda path, duration, reader=None: {"available": False})
    out = tmp_path / "res.json"
    code = I.main(["--config", str(tmp_path / "config.json"), "analyse", "--file", str(ref), "--name", "Cool One",
                   "--copy", "pacing colour", "--json", str(out)])
    res = json.loads(out.read_text())
    assert code == 0 and res["name"] == "cool-one"
    p = P.Presets(tmp_path / "data/presets").load("cool-one")
    assert p["copy"] == ["pacing", "colour"] and "OCR not installed" in res["text"]
