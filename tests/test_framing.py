"""Framing of 16:9 trailer clips in the edit (4:5 since 20 Nov: box45 fills the frame), 1:1 and
stacked on black for A/B/C to try, smart fill on close-ups of one person, crops centred on the
subject; real 60 fps only when the footage has it; clips spread over long videos."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from spikebot import remotion as RM
from spikebot import safety
from spikebot import style as S
from spikebot import tokyo_girl as TG

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def shot(src="a.mp4", start=0.0, end=12.0, **kw):
    return TG.Shot(src, "video", start, end, ["city"], None, 1920, 1080, **kw)


def slot(sh, **kw):
    return TG.Slot(sh, sh.start, 2, 0.0, 1.0, 0, False, 1, **kw)


# ---- faces -> subject --------------------------------------------------------------
def test_frame_faces_keeps_confident_faces_as_fractions():
    dets = [{"class": "FACE_FEMALE", "score": 0.8, "box": [100, 50, 64, 80]},
            {"class": "FACE_MALE", "score": 0.2, "box": [0, 0, 10, 10]},
            {"class": "BELLY_EXPOSED", "score": 0.9, "box": [0, 0, 10, 10]}]
    assert safety.frame_faces(dets, (640, 360)) == [[0.206, 0.25, 0.1, 0.222]]


def test_subject_of_finds_one_clear_subject_or_a_group_centre():
    close = [0.7, 0.4, 0.15, 0.3]                       # one big face, right of centre
    entry = {"w": 1920, "h": 1080, "faces": [[t, [close]] for t in range(0, 5)]}
    focus, single = TG.subject_of(entry, 0.0, 4.5)
    assert single and focus == (0.7, 0.4)
    group = {"w": 1920, "h": 1080, "faces": [[t, [[0.3, 0.5, 0.1, 0.1], [0.5, 0.5, 0.1, 0.1]]] for t in range(5)]}
    focus, single = TG.subject_of(group, 0.0, 4.5)
    assert not single and focus == (0.4, 0.5)
    assert TG.subject_of({"faces": []}, 0, 4) == (None, False)
    tiny = {"w": 1920, "h": 1080, "faces": [[t, [[0.5, 0.5, 0.02, 0.03]]] for t in range(5)]}   # a crowd extra
    assert TG.subject_of(tiny, 0.0, 4.5) == (None, False)
    # letterboxed trailer: the focus is measured inside the picture, not the bars
    boxed = {"w": 1920, "h": 1080, "crop": [1920, 800, 0, 140], "faces": [[t, [[0.5, 0.5, 0.15, 0.3]]] for t in range(3)]}
    assert TG.subject_of(boxed, 0.0, 2.5)[0] == (0.5, 0.5)


def test_build_shots_carries_focus_fps_and_the_known_animal_range():
    entry = {"kind": "video", "w": 1920, "h": 1080, "duration": 700.0, "fps": 59.94, "cuts": [460.0, 470.0],
             "yavg": [], "faces": [[t, [[0.8, 0.4, 0.15, 0.3]]] for t in range(461, 470)]}
    cfg = {"trailer_head_s": 6.0, "trailer_tail_s": 15.0, "min_luma": 28}
    out = TG.build_shots("trailers/Grand_Theft_Auto_VI_An_Extended_Look_trailer_x.mp4", entry, {}, cfg)
    animal = next(s for s in out if s.start > 460 and s.end < 471)
    assert "animals" in animal.tags and animal.single and animal.focus == (0.8, 0.4) and animal.fps == 59.94
    assert "animals" not in out[0].tags


# ---- layouts ---------------------------------------------------------------------
def test_slot_layout_smart_fill_and_stack_fallback():
    close, wide = shot(single=True, focus=(0.7, 0.4)), shot()
    assert TG.slot_layout(slot(close), "box45") == "fill"           # a close-up of one person: full screen
    assert TG.slot_layout(slot(close), "box45", smart=False) == "box45"
    assert TG.slot_layout(slot(wide), "box45") == "box45"
    assert TG.slot_layout(slot(close), "stack") == "box45"           # no second clip yet
    assert TG.slot_layout(slot(wide, shot2=shot("b.mp4")), "stack") == "stack"


def test_ffmpeg_graphs_for_each_framing():
    wide = shot()
    box = TG.slot_filter(slot(wide), 1.0, "none", "box45")
    # 4:5 sits on plain black bars, never a blurred copy (Tim, 19 Nov)
    assert "crop=1080:1350:" in box and f"pad=1080:{TG.H}:0:{TG.box_top('box45')}:color=black" in box
    assert "boxblur" not in box and "split" not in box
    assert box.startswith("[0:v]") and box.endswith("[v]")
    sq = TG.slot_filter(slot(wide), 1.0, "none", "box11")
    assert "crop=1080:1080:" in sq and "pad=1080:1350:0:135:color=black" in sq and "boxblur" not in sq
    st = TG.slot_filter(slot(wide, shot2=shot("b.mp4")), 1.0, "none", "stack")
    y1, y2 = TG.stack_tops()
    assert "[1:v]" in st and f"overlay=0:{y1}" in st and f"overlay=0:{y2}" in st and st.count("crop=1080:608:") == 2
    # the crop follows the subject: a face at 80% across moves the window right of centre
    centred = TG.slot_filter(slot(shot()), 1.0, "none", "fill", smart=False)
    right = TG.slot_filter(slot(shot(focus=(0.8, 0.5))), 1.0, "none", "fill", smart=False)
    assert "crop=1080:1350:'660.0':'0.0'" in centred
    x = float(right.split("crop=1080:1350:'")[1].split("'")[0])
    assert x > 660.0
    fps60 = TG.slot_filter(slot(wide), 1.0, "none", "box45", fps=60)
    assert "fps=60" in fps60


def test_text_sits_in_the_band_above_a_boxed_picture():
    r = S.resolve(S.default_style("tokyo_girl") | {"position": "high"})
    assert r["framing"] == "box45" and r["smart_fill"] is True
    ass = TG.build_text_ass([(0, 2, "hi")], "DejaVu Sans", 100, dict(r, framing="box11"))
    assert f",8,90,90,{TG.BAND_MARGIN}," in ass
    full = TG.build_text_ass([(0, 2, "hi")], "DejaVu Sans", 100, r)   # 4:5 box45 fills the frame: no band
    assert ",8,90,90,150," in full


# ---- 60 fps ----------------------------------------------------------------------
def test_edit_is_60_fps_only_when_every_clip_really_is():
    story = TG.STORIES[0]
    fast, slow = shot(fps=59.94), shot("b.mp4", fps=30.0)
    img = TG.Shot("x.jpg", "image", 0.0, float("inf"), [], None, 1920, 1080)
    plan = TG.Plan(story, [], [slot(fast), slot(img)], 2.0)
    assert TG.edit_fps(plan) == 60
    assert TG.edit_fps(TG.Plan(story, [], [slot(fast), slot(slow)], 2.0)) == 30     # never doubled frames
    assert TG.edit_fps(TG.Plan(story, [], [slot(fast, shot2=slow)], 1.0)) == 30
    assert TG.edit_fps(TG.Plan(story, [], [slot(img)], 1.0)) == 30


# ---- variety ---------------------------------------------------------------------
def test_clips_spread_over_a_long_video():
    long = [shot("ext.mp4", t, t + 4.0) for t in range(0, 600, 5)]
    used: set[str] = set()
    rng = random.Random(1)
    picks = []
    for _ in range(8):
        s = TG.choose_shot(long, "city", 2.0, used, {}, rng)
        used.add(s.key)
        picks.append(s.start)
    picks.sort()
    assert all(b - a >= TG.NEAR_S for a, b in zip(picks, picks[1:]))


def test_stacked_slots_get_a_different_second_clip():
    story = next(x for x in TG.STORIES if x.id == "leonida")
    pool = [shot(f"v{i}.mp4", 0.0, 10.0) for i in range(40)]
    plan = TG.plan_clip(story, pool, {"bpm": 120, "min_s": 8, "max_s": 10, "media_dir": "x"},
                        date(2026, 11, 18), random.Random(2))
    plan = TG.with_seconds(plan, pool, random.Random(3))
    keys = [sl.shot.key for sl in plan.slots] + [sl.shot2.key for sl in plan.slots if sl.shot2]
    assert all(sl.shot2 is not None for sl in plan.slots) and len(keys) == len(set(keys))
    foot = TG.footage_of(plan)
    again, kept = TG.refit(TG.plan_clip(story, pool, {"bpm": 120, "min_s": 8, "max_s": 10, "media_dir": "x"},
                                        date(2026, 11, 18), random.Random(9)), foot, pool)
    assert kept and again.slots[0].shot2.key == plan.slots[0].shot2.key        # re-renders keep both clips


# ---- style: defaults, variants, old rules ------------------------------------------------
def test_a_is_4_5_and_b_c_try_square_and_stacked(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    vs = S.pick_variants(prof, "tokyo_girl", random.Random(1))
    assert vs[0]["style"]["framing"] == "box45"
    assert {v["style"]["framing"] for v in vs[1:]} == {"box11", "stack"}
    assert "framing" not in S.pick_variants(prof, "lacy", random.Random(1))[0]["style"]


def test_old_fill_and_fit_rules_are_switched_off_once():
    entries = [{"n": 1, "kind": "setting", "setting": "framing", "value": "fit", "mode": "tokyo_girl", "on": True},
               {"n": 2, "kind": "setting", "setting": "motion", "value": "none", "mode": "all", "on": True}]
    assert S.framing_upgrade(entries) == [1]
    assert entries[0]["on"] is False and entries[1]["on"] is True


# ---- Remotion props ----------------------------------------------------------------
def test_remotion_props_carry_layout_box_focus_and_second_clip(tmp_path):
    story = TG.STORIES[0]
    close = shot("c.mp4", single=True, focus=(0.7, 0.4), fps=60.0)
    wide, other = shot("w.mp4", fps=60.0), shot("o.mp4", fps=60.0)
    plan = TG.Plan(story, [], [slot(close), TG.Slot(wide, 0.0, 2, 1.0, 1.0, 0, False, 1, shot2=other)], 2.0)
    r = S.resolve(S.default_style("tokyo_girl") | {"framing": "stack", "position": "high"})
    props, files = RM.props_for(plan, {"font": "DejaVu Sans"}, r, [], None, 0.0, tmp_path)
    a, b = props["shots"]
    assert props["fps"] == 60 and a["layout"] == "box45" and a["focus"] == [0.7, 0.4]   # stack, no 2nd clip
    assert b["layout"] == "stack" and b["second"]["box"][1] == TG.stack_tops()[1] and len(files) == 4
    assert "TikTokSans-Bold.ttf" in files and props["height"] == 1350
    assert props["text"]["y"] < 0.1                                   # hook in the band above


@needs_ffmpeg
@pytest.mark.parametrize("framing", ["box45", "stack"])
def test_ffmpeg_renders_boxed_and_stacked_edits(tmp_path, framing):
    media = tmp_path / "media"
    media.mkdir()
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for i in range(2):
        subprocess.run(ff + ["-f", "lavfi", "-i", f"testsrc2=s=640x360:r=60:d=3,hue=h={i * 90}",
                             "-pix_fmt", "yuv420p", str(media / f"v{i}.mp4")], check=True)
    a = TG.Shot("v0.mp4", "video", 0.0, 3.0, [], None, 640, 360, fps=60.0)
    b = TG.Shot("v1.mp4", "video", 0.0, 3.0, [], None, 640, 360, fps=60.0)
    plan = TG.Plan(TG.STORIES[0], [(0.0, 1.0, "hello")],
                   [TG.Slot(a, 0.0, 2, 0.0, 1.0, 0, False, 1, shot2=b)], 1.0)
    out = tmp_path / "e.mp4"
    r = S.resolve(S.default_style("tokyo_girl") | {"framing": framing, "position": "high"})
    TG.render_plan(plan, {"media_dir": str(media), "font": "DejaVu Sans", "font_size": 80}, out, text_style=r)
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height,r_frame_rate",
                                      "-of", "json", str(out)], capture_output=True, text=True).stdout)["streams"][0]
    assert (info["width"], info["height"], info["r_frame_rate"]) == (1080, 1350, "60/1")


