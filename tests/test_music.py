"""Song beat maps: tempo, drop, start point, and Tokyo Girl cuts on the real beats."""
from __future__ import annotations

import datetime as dt
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from spikebot import music as M
from spikebot import tokyo_girl as TG

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
BPM = 128
BEAT = 60 / BPM


def song(path: Path, drop: float = 8 * 60 / 128 * 2, seconds: float = 30) -> Path:
    """Kick on every beat at 128 BPM: quiet build-up, then loud (with bass) from the drop."""
    expr = (f"(if(lt(mod(t,{BEAT}),0.05),sin(2*PI*90*t),0)+0.02*sin(2*PI*440*t))"
            f"*if(lt(t,{drop}),0.12,1)+if(gte(t,{drop}),0.5*sin(2*PI*55*t),0)")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"aevalsrc='{expr}':s=22050:d={seconds}", str(path)], check=True)
    return path


@needs_ffmpeg
def test_analyse_finds_tempo_and_drop(tmp_path):
    drop = 16 * BEAT          # 7.5 s
    row = M.Library(tmp_path / "music").add(song(tmp_path / "Hit Song.wav", drop), "Hit Song")
    assert row["name"] == "hit-song" and (tmp_path / "music/hit-song.wav").exists()
    assert row["bpm"] == pytest.approx(BPM, rel=0.04) or row["bpm"] == pytest.approx(BPM / 2, rel=0.04)
    assert row["drop_s"] == pytest.approx(drop, abs=0.3)
    start = M.start_for(row)
    assert row["drop_s"] - start == pytest.approx(4 * BEAT * (128 / row["bpm"] if row["bpm"] < 100 else 1), abs=0.3)
    # cached: the second read doesn't re-analyse
    assert M.Library(tmp_path / "music").current() == row
    assert "start it at" in M.note(row, start)


def test_grid_is_relative_and_extended():
    row = {"beats": [1.0, 1.5, 2.0, 2.5, 3.0], "drop_s": 2.0, "bpm": 120}
    assert M.start_for(row, build_beats=2) == 1.0
    g = M.grid(row, 1.5, need_s=4)
    assert g[:4] == [0.0, 0.5, 1.0, 1.5] and g[-1] >= 4 + 4 * 0.5 - 1e-9


def test_plan_follows_real_beats():
    beats = [0.0]
    for i in range(200):          # uneven beats: alternating 0.45 / 0.5 s
        beats.append(round(beats[-1] + (0.45 if i % 2 else 0.5), 3))
    shots = [TG.Shot(f"s{i}.jpg", "image", 0.0, float("inf"), ["city"], None, 1920, 1080) for i in range(12)]
    story = next(x for x in TG.STORIES if x.id == "partners")
    lcfg = TG.settings({"tokyo_girl": {"min_s": 15, "max_s": 20}})
    plan = TG.plan_clip(story, shots, lcfg, dt.date(2026, 11, 20), random.Random(3), beats=beats)
    on = set(beats)
    assert all(sl.start in on and round(sl.start + sl.dur, 3) in on for sl in plan.slots)
    assert plan.duration in on and 15 - 1 <= plan.duration <= 20 + 1
    assert all(c[0] in on for c in plan.cards)
