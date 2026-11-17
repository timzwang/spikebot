"""Sound effects: cue placement and mixing under the voice."""
from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

from spikebot import editor
from spikebot import sfx as X

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
LIB = {"whoosh": [Path("w.wav")], "hit": [Path("h.wav")], "riser": [Path("r.wav")]}
LEN = {"w.wav": 0.5, "h.wav": 0.4, "r.wav": 2.0}


def test_cues_on_visible_cuts_and_payoff():
    segs = [(0, 4), (5, 9), (10, 14)]          # cuts at 4 s and 8 s on the cut timeline
    cs = X.cues(segs, 6.0, "zoom", LIB, random.Random(0), 0.4, length_of=lambda p: LEN[p.name])
    kinds = [(c.file.name, round(c.at, 2)) for c in cs]
    assert ("w.wav", 3.7) in kinds and ("w.wav", 7.7) in kinds
    assert ("r.wav", 4.0) in kinds and ("h.wav", 6.0) in kinds
    assert all(c.volume <= 0.4 for c in cs)
    plain = X.cues(segs, 6.0, "cut", LIB, random.Random(0), 0.4, length_of=lambda p: LEN[p.name])
    assert [c.file.name for c in plain] == ["r.wav", "h.wav"]      # jump cuts stay clean
    assert X.cues(segs, 6.0, "zoom", LIB, random.Random(0), 0.0) == []      # sfx 0 = off
    assert X.cues(segs, None, "cut", {"whoosh": [], "hit": [], "riser": []}, random.Random(0)) == []


def test_library_reads_folders(tmp_path):
    (tmp_path / "hit").mkdir()
    (tmp_path / "hit" / "boom.mp3").write_bytes(b"x")
    (tmp_path / "hit" / "notes.txt").write_text("x")
    lib = X.library(tmp_path)
    assert [p.name for p in lib["hit"]] == ["boom.mp3"] and lib["whoosh"] == [] and lib["riser"] == []


@needs_ffmpeg
def test_hit_is_mixed_in(tmp_path):
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    src = tmp_path / "src.mp4"
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6", "-f", "lavfi",
                         "-i", "sine=f=300:d=6,volume=0.05", "-c:v", "libx264", "-preset", "ultrafast",
                         "-c:a", "aac", str(src)], check=True)
    hit = tmp_path / "hit.wav"
    subprocess.run(ff + ["-f", "lavfi", "-i", "sine=f=1000:d=0.4", str(hit)], check=True)
    lay = editor.layout(1280, 720, {"x": 0, "y": 0, "w": 0.3, "h": 0.3})
    out = tmp_path / "out.mp4"
    editor.render_variant(src, out, lay, None, 1280, 720, [(0, 6)], sfx_cues=[X.Cue(hit, 3.0, 0.8)])

    def loud(t):
        p = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(t), "-t", "0.3", "-i", str(out), "-af",
                            "volumedetect", "-vn", "-f", "null", "-"], capture_output=True, text=True)
        return float(p.stderr.split("max_volume:")[1].split("dB")[0])
    assert loud(3.05) > loud(1.0) + 10
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(out)], capture_output=True, text=True).stdout)
    assert dur == pytest.approx(6.0, abs=0.15)
