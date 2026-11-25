"""The editor rules Tim asked for that needed new code (GitHub issues #40-#84, rule-request):
settings, footage choice, clean two-clip crossfades, end fade, glitch check, the Rockstar Games
Presents intro, vision labels, clip reuse from a named version, and the stream-clip lead-in and
facecam check."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

from spikebot import ai, editor, safety, shot_info, shot_labels
from spikebot import feedback as fb
from spikebot import review as R
from spikebot import style as S
from spikebot import tokyo_girl as TG

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
TODAY = date(2026, 11, 16)
FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]


def vshot(src="t.mp4", start=0.0, end=None, tags=("city",), **kw) -> TG.Shot:
    end = start + 4.0 if end is None else end
    sh = TG.Shot(src, "video", start, end, list(tags), None, 1920, 1080, kw.pop("motion", None))
    for k, v in kw.items():
        setattr(sh, k, v)
    return sh


def plan_of(shots_durs, fade=0.0) -> TG.Plan:
    t, slots = 0.0, []
    for sh, d in shots_durs:
        slots.append(TG.Slot(sh, sh.start, 2, t, d, 0, False, 1))
        t += d
    return TG.Plan(TG.STORIES[0], [], slots, t)


# ---- settings ---------------------------------------------------------------------------------
NEW = ("logo_fade_s", "end_fade_s", "intro_mix", "unique_clips", "crop_safe", "action_first", "ambient_bias",
       "palette_match", "impact_ending", "opener_rotation", "footage_match", "glitch_check", "sharpen",
       "lead_in_s", "require_facecam", "streamer_only", "variety")


def test_every_new_setting_is_real_and_in_the_menu():
    menu = ai.settings_menu()
    for k in NEW:
        assert k in S.CUSTOM_KEYS and k in S.SETTING_INFO and k in menu, k
        assert any(k in keys for _, _, keys in S.CATEGORIES), k
    assert "rockstar_presents" in S.dims("trend")["intro"] and "rockstar_presents" in menu
    assert "rockstar_presents" not in S.dims("lacy").get("intro", ())
    assert S.valid_modes("unique_clips", 1) == ["tokyo_girl", "trend"]
    assert S.valid_modes("lead_in_s", 2) == ["lacy"] and S.valid_modes("require_facecam", 1) == ["lacy"]
    assert S.valid_modes("sharpen", 0.5) == ["lacy", "tokyo_girl", "trend"]
    with pytest.raises(ValueError):
        S.set_value(S.default_style("lacy"), "lead_in_s", "9", "lacy")


def test_resolve_defaults_and_values():
    r = S.resolve(S.default_style("tokyo_girl"))
    assert (r["end_fade_s"], r["logo_fade_s"], r["opener_rotation"]) == (3.0, 0.4, 10)
    assert r["unique_clips"] and r["crop_safe"] and r["footage_match"] and r["glitch_check"]
    assert not r["action_first"] and not r["impact_ending"] and r["ambient_bias"] == 0 and r["palette_match"] == 0
    assert r["lead_in_s"] == 0 and r["require_facecam"] and r["look"]["sharpen"] == 0
    st = dict(S.default_style("trend"), intro="rockstar_presents",
              custom={"logo_fade_s": 0.2, "end_fade_s": 0, "action_first": 1, "sharpen": 0.6, "unique_clips": 0})
    r = S.resolve(st)
    assert r["intro"] == "rockstar_presents" and r["logo_fade_s"] == 0.2 and r["end_fade_s"] == 0
    assert r["action_first"] and not r["unique_clips"] and "unsharp" in S.look_filter(r["look"], "tokyo_girl")
    assert S.sharpen_filter(0) == "" and "unsharp" in S.sharpen_filter(1)


def test_built_in_lines_cover_the_always_on_rules():
    text = "\n".join(S.BUILT_IN)
    for words in ("exactly the two neighbouring clips", "B1", "strip club", "left until GTA 6 releases",
                  "fade it out with the picture", "#inspiration references", "never drift off the beat"):
        assert words in text, words
    assert TG.countdown_line(date(2026, 11, 16)) == "3 days left until GTA 6 releases"


# ---- shot_info: colour, framing, duplicates ---------------------------------------------------
def test_crop_window_finds_the_subject_and_flags_cut_off_shots():
    left = [300, 300, 200, 50, 30, 20, 20, 20, 20, 20, 10, 10]
    focus, inside, peak_out = shot_info.frame_window(left, shot_info.crop_share(16 / 9))
    assert focus < 0.3 and inside > 0.6
    even = [83] * 12
    assert shot_info.frame_window(even, 0.316)[0] == pytest.approx(0.5, abs=0.05)
    edges = [400, 50, 10, 10, 10, 10, 10, 10, 10, 10, 50, 400]     # two subjects at opposite edges
    entry = {"looks": {"rows": [[t / 2, 100, 0, 0, edges, "0" * 16] for t in range(8)]}}
    st = shot_info.shot_stats(entry, 0.0, 4.0, 16 / 9)
    assert not shot_info.crop_ok(st)
    centred = {"looks": {"rows": [[t / 2, 100, 0, 0, [10] * 4 + [230] * 4 + [10] * 4, "0" * 16] for t in range(8)]}}
    st2 = shot_info.shot_stats(centred, 0.0, 4.0, 16 / 9)
    assert st2["focus"] == 0.5 and shot_info.crop_ok(st2)
    assert shot_info.crop_ok(None) and not shot_info.crop_ok(None, subject_w=0.9)
    assert shot_info.subject_focus(0.5, 0.2, 16 / 9) == 0.5 and shot_info.subject_focus(0.1, 0.1, 16 / 9) <= shot_info.crop_share(16 / 9) / 2 + 1e-9   # hard left (4:5 crop)


def test_palette_and_duplicate_footage():
    assert shot_info.colour_distance([100, 5, 5], [100, 5, 5]) == 0
    far = shot_info.colour_distance([100, 30, -20], [60, -25, 20])
    assert far > shot_info.palette_limit(1.0) and far < shot_info.palette_limit(0.0) + 100
    h = [0x0F0F0F0F0F0F0F0F, 0x00FF00FF00FF00FF, 0x123456789ABCDEF0]
    near = [x ^ 0b101 for x in h]                 # 2 bits off: the same picture re-encoded
    other = [0xFFFFFFFF00000000, 0xAAAAAAAAAAAAAAAA, 0x5555555555555555]
    g = shot_info.dup_groups([("a@1", h), ("b@7", near), ("c@2", other), ("d", [])])
    assert g["b@7"] == "a@1" and g["c@2"] == "c@2" and g["d"] == "d"


@needs_ffmpeg
def test_measure_reads_colour_and_detail(tmp_path):
    f = tmp_path / "v.mp4"
    subprocess.run(FF + ["-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=3", "-pix_fmt", "yuv420p", str(f)],
                   check=True)
    m = shot_info.measure(f)
    assert m["v"] == shot_info.VERSION and len(m["rows"]) >= 5
    t, y, u, v, strips, h = m["rows"][0]
    assert len(strips) == 12 and len(h) == 16 and 0 < y < 255


# ---- vision labels -----------------------------------------------------------------------------
def fake_vision(by_index):
    """A fake model: labels shot i with by_index(i) (shot numbers are global via the text blocks)."""
    calls = []

    def ask(content):
        calls.append(content)
        n = sum(1 for c in content if c.get("type") == "image")
        return {"shots": [dict({"i": j, "tags": [], "action": 0.2, "calm": 0.5, "impact_end": 0.1,
                                "subject_x": 0.5, "subject_w": 0.3}, **by_index(len(calls), j)) for j in range(n)]}
    ask.calls = calls
    return ask


@needs_ffmpeg
def test_labels_are_made_once_and_cached(tmp_path):
    f = tmp_path / "trailer.mp4"
    subprocess.run(FF + ["-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=6", "-pix_fmt", "yuv420p", str(f)],
                   check=True)
    entry = {"kind": "video", "duration": 6.0, "cuts": [2.0, 4.0]}
    tags = {0: ["rockstar_presents", "ocean", "skyline"], 1: ["twerking"], 2: ["car", "driving_away"]}
    ask = fake_vision(lambda call, j: {"tags": tags[j], "impact_end": 0.9 if j == 2 else 0.1})
    assert shot_labels.ensure(f, entry, "trailer.mp4", None, ask=ask, now=1000.0)
    assert len(ask.calls) == 1 and entry["labels"]["v"] == shot_labels.VERSION
    assert not shot_labels.ensure(f, entry, "trailer.mp4", None, ask=ask, now=2000.0)   # cached
    assert shot_labels.for_range(entry, 2.2, 3.8)["tags"] == ["twerking"]
    assert shot_labels.presents_segments(entry) == [(0.0, 2.0, "scenery")]
    # a failure is retried only after RETRY_S
    e2 = {"kind": "video", "duration": 6.0, "cuts": []}

    def boom(content):
        raise shot_labels.LabelError("down")
    assert not shot_labels.ensure(f, e2, "x", None, ask=boom, now=5000.0) and e2["labels_tried"] == 5000.0
    assert not shot_labels.needs_labels(e2, now=5000.0 + 60) and shot_labels.needs_labels(e2, now=5000.0 + 25 * 3600)


def test_black_presents_card_is_told_apart():
    entry = {"labels": {"v": 1, "shots": [[0, 2, {"tags": ["rockstar_presents", "black_card", "title_card"]}],
                                          [5, 9, {"tags": ["rockstar_presents", "ocean", "skyline"]}]]}}
    assert shot_labels.presents_segments(entry) == [(0, 2, "black"), (5, 9, "scenery")]


def labelled_entry(rows, dur=20.0, cuts=(4.0, 8.0, 12.0, 16.0)):
    return {"kind": "video", "w": 1920, "h": 1080, "duration": dur, "cuts": list(cuts), "yavg": [], "crop": None,
            "labels": {"v": 1, "shots": rows}}


def test_build_shots_drops_unsafe_and_intro_cards_and_adds_story_tags(tmp_path):
    rows = [[0, 4, {"tags": ["rockstar_presents", "black_card"]}], [4, 8, {"tags": ["stripclub"]}],
            [8, 12, {"tags": ["car", "driving_away"], "action": 0.8, "impact_end": 0.9}],
            [12, 16, {"tags": ["ocean"], "calm": 0.9}], [16, 20, {"tags": ["rockstar_presents", "ocean"]}]]
    s = TG.settings({"tokyo_girl": {"trailer_head_s": 0, "trailer_tail_s": 0}}, tmp_path)
    shots = TG.build_shots("t.mp4", labelled_entry(rows), {}, s)
    assert [round(x.start) for x in shots] == [8, 12]
    assert "cars" in shots[0].tags and shots[0].impact() >= 0.9 and shots[1].calm() == 0.9
    lib = TG.Library(s)
    pres = lib.presents_shots({"t.mp4": labelled_entry(rows)})
    assert len(pres) == 1 and pres[0].start == pytest.approx(16.05) and pres[0].kind == "video"


# ---- footage choice ------------------------------------------------------------------------------
def prefs(**kw) -> TG.Prefs:
    return TG.Prefs(**kw)


def test_unique_clips_never_repeats_footage_even_from_another_file():
    a = vshot("t1.mp4", 0, 4, hashes=[1, 2, 3])
    a_copy = vshot("extended.mp4", 30, 34, hashes=[1, 2, 3])     # same footage, other file
    b = vshot("t1.mp4", 5, 9, hashes=[0xFFFF, 0xFFFF0000, 0xF0F0F0F0F0F0])
    shots = [a, a_copy, b]
    p = prefs(unique=True, groups=TG.footage_groups(shots))
    rng = random.Random(1)
    first = TG.choose_shot(shots, "city", 2.0, [], {}, rng, p)
    second = TG.choose_shot(shots, "city", 2.0, [first.key], {}, rng, p)
    assert TG.group_of(p, second.key) != TG.group_of(p, first.key)
    third = TG.choose_shot(shots, "city", 2.0, [first.key, second.key], {}, rng, p)   # last resort only
    assert third.key in (first.key, second.key) and TG.group_of(p, third.key) == TG.group_of(p, first.key)


def test_relevance_crop_safety_palette_and_ambient():
    filler = vshot(start=0, labels={"tags": ["title_card"], "calm": 0.9})
    good = vshot(start=10, labels={"tags": ["action"], "calm": 0.1})
    rng = random.Random(3)
    for _ in range(10):
        assert TG.choose_shot([filler, good], "city", 2, [], {}, rng, prefs(relevant=True)).key == good.key
    cut_off = vshot(start=20, crop_ok=False)
    fine = vshot(start=30, crop_ok=True)
    assert all(TG.choose_shot([cut_off, fine], "city", 2, [], {}, rng, prefs(crop_safe=True)) is fine
               for _ in range(10))
    anchor = vshot(start=40, yuv=[120, 20, -10])
    near = vshot(start=50, yuv=[118, 18, -8])
    far = vshot(start=60, yuv=[40, -30, 40])
    assert all(TG.choose_shot([near, far], "city", 2, [], {}, rng, prefs(palette=1.0), anchor) is near
               for _ in range(10))
    calm = vshot(start=70, labels={"tags": ["scenery"], "calm": 1.0})
    busy = vshot(start=80, labels={"tags": ["action"], "calm": 0.0})
    picks = [TG.choose_shot([calm, busy], "city", 2, [], {}, random.Random(i), prefs(ambient=1.0)).key
             for i in range(60)]
    assert picks.count(calm.key) > 45


def test_action_first_and_impact_ending():
    quiet = [vshot(start=i * 10, end=i * 10 + 5, labels={"tags": [], "action": 0.1}) for i in range(5)]
    loud = vshot(start=100, end=105, labels={"tags": ["action"], "action": 0.95})
    plan = plan_of([(s, 2.0) for s in quiet] + [(loud, 2.0)])
    out = TG.action_order(plan)
    assert out.slots[0].shot is loud and [sl.start for sl in out.slots] == [sl.start for sl in plan.slots]
    assert len({sl.shot.key for sl in out.slots}) == 6                 # still every clip once
    walk = vshot(start=200, end=206, labels={"tags": ["walking_away"], "impact_end": 0.8})
    ended = TG.impact_end(plan, quiet + [loud, walk], {})
    assert ended.slots[-1].shot is walk and ended.slots[-1].offset == pytest.approx(204.0)
    assert TG.impact_end(plan, quiet + [loud], {}) is plan                     # nothing impactful: unchanged


def test_opener_rotation_and_prefs_from_style(tmp_path):
    shots = [vshot(f"c{i}.mp4", 0, 6) for i in range(6)]
    r = S.resolve(dict(S.default_style("tokyo_girl"), custom={"opener_rotation": 3}))
    p = TG.prefs_for(r, shots, openers=["x", "c0.mp4@0.0", "c1.mp4@0.0", "c2.mp4@0.0"])
    assert p.openers == {"c0.mp4@0.0", "c1.mp4@0.0", "c2.mp4@0.0"} and p.unique and p.crop_safe and p.relevant
    s = TG.settings({"tokyo_girl": {"min_s": 8, "max_s": 8}}, tmp_path)
    for seed in range(8):
        plan = TG.plan_clip(TG.STORIES[0], shots, s, TODAY, random.Random(seed), prefs=p)
        assert plan.slots[0].shot.key not in p.openers
    fit = TG.prefs_for(dict(r, framing="fit"), shots)
    assert not fit.crop_safe


# ---- crossfades, holds, end fade, glitches --------------------------------------------------------
def test_each_crossfade_blends_only_two_clips():
    durs = [2.0, 0.3, 0.5, 1.0, 0.25, 2.0]
    plan = TG.cut_fades(plan_of([(vshot(start=i * 10, end=i * 10 + 5), d) for i, d in enumerate(durs)]), 0.45)
    assert plan.slots[0].fade_in == 0
    for prev, sl in zip(plan.slots, plan.slots[1:]):
        assert sl.fade_in <= max(prev.dur, sl.dur) / 2 + 1e-9 or sl.fade_in == pytest.approx(2 / TG.FPS)
        assert sl.fade_in <= sl.dur and sl.fade_in <= prev.dur       # over before the next one starts
        assert abs(sl.fade_in * TG.FPS - round(sl.fade_in * TG.FPS)) < 1e-6 and sl.fade_in >= 2 / TG.FPS - 1e-9
    assert TG.tails(plan)[:-1] == [sl.fade_in for sl in plan.slots[1:]] and TG.tails(plan)[-1] == 0


def test_a_clip_never_runs_into_another_scene():
    sh = vshot(start=10.0, end=14.0)
    sl = TG.Slot(sh, 12.5, 2, 0.0, 1.0, 0, False, 1)
    other = TG.Slot(vshot(start=0, end=5), 0.0, 2, 1.0, 1.0, 0, False, 1, fade_in=0.4)
    out = TG.clean_plan(TG.Plan(TG.STORIES[0], [], [sl, other], 2.0), Path("."), cuts_of=lambda s: [])
    assert out.slots[0].hold is None                     # 12.5 + 1.4 fits inside the shot
    sl2 = TG.Slot(sh, 13.2, 2, 0.0, 1.0, 0, False, 1)
    out = TG.clean_plan(TG.Plan(TG.STORIES[0], [], [sl2, other], 2.0), Path("."), cuts_of=lambda s: [])
    assert out.slots[0].hold == pytest.approx(0.8)       # runs out at 14.0: the last frame is held
    cut = TG.clean_plan(TG.Plan(TG.STORIES[0], [], [TG.Slot(vshot(start=10, end=12.2), 10.0, 2, 0.0, 2.1, 0,
                                                            False, 1), other], 3.1), Path("."),
                        cuts_of=lambda s: [11.0])
    a = cut.slots[0]
    assert a.hold is not None and a.offset + a.hold <= 11.0 or a.offset >= 11.0


def test_glitches_are_cuts_the_plan_does_not_have():
    shots = [vshot(start=i * 10, end=i * 10 + 5) for i in range(4)]
    plan = TG.cut_fades(plan_of([(s, 2.0) for s in shots]), 0.45)
    assert TG.glitches([], plan) == []
    assert TG.glitches([3.0], plan) == [3.0]                       # mid-fade hard cut = broken fade
    hard = plan_of([(s, 2.0) for s in shots])
    assert TG.glitches([2.0, 4.02, 5.1], hard) == [5.1]           # planned cuts are fine, a stray one isn't
    assert TG.glitches([1.5], hard, also=[1.5]) == []
    spare = vshot("spare.mp4", 0, 6)
    fixed = TG.fix_glitches(hard, [5.1], shots + [spare], {}, None, keep=set())
    assert fixed.slots[2].shot is spare
    kept = TG.fix_glitches(hard, [5.1], shots + [spare], {}, None, keep={shots[2].key})
    assert kept.slots[2].shot is shots[2] and kept.slots[2].hold == pytest.approx(1.1 - 1 / TG.FPS, abs=1e-3)


def test_end_fade_trim_and_sound_note():
    assert TG.end_fade({"end_fade_s": 3}, 20) == 3 and TG.end_fade({"end_fade_s": 3}, 6) == 2
    assert TG.end_fade({"end_fade_s": 0}, 20) == 0 and TG.end_fade({}, 20) == 0
    plan = plan_of([(vshot(start=i * 10, end=i * 10 + 5), 2.0) for i in range(6)])
    assert len(TG.trim_to(plan, 4).slots) == 4 and TG.trim_to(plan, 4).duration == 8.0
    assert TG.trim_to(plan, 2, min_s=6.0) is plan
    assert "fade the sound out over the last 3 s" in TG.sound_note("🎵 start at 0:12", 3.0)
    assert TG.sound_note("x", 0) == "x"


def test_presents_intro_holds_its_last_frame():
    pres = vshot("t2.mp4", 6.0, 9.0, tags=["presents"])
    plan = plan_of([(vshot(start=i * 10, end=i * 10 + 5), 1.0) for i in range(8)])
    out = TG.with_intro_shot(plan, [], 4.0, {}, shot=pres)
    first = out.slots[0]
    assert first.shot is pres and first.offset == 6.0 and first.dur == 4.0 and first.hold == 3.0
    assert out.slots[1].start == 4.0


def test_remotion_props_carry_fades_holds_focus_and_end_fade(tmp_path):
    from spikebot import remotion
    sh = vshot(start=0, end=5, subject_x=0.2)
    plan = TG.cut_fades(plan_of([(sh, 2.0), (vshot(start=10, end=15), 2.0)]), 0.45)
    plan.slots[0].hold = 1.5
    r = S.resolve(dict(S.default_style("tokyo_girl"), transition="fade", framing="fill",
                       custom={"logo_fade_s": 0.2}))
    plan = TG.crop_safe_focus(plan, r)       # no face known: the crop goes to the subject
    props, _ = remotion.props_for(plan, {"font_size": 104}, r, [], tmp_path / "logo.png", 1.0, tmp_path)
    a, b = props["shots"]
    assert a["hold"] == 1.5 and a["tail"] == b["fadeIn"] > 0 and a["fadeIn"] == 0 and a["focus"] == [0.2, 0.5]
    assert b["focus"] is None
    assert props["endFade"] == pytest.approx(4.0 / 3, abs=1e-3) and props["logo"]["out"] == 0.2   # a third of 4 s


# ---- rendering (ffmpeg) -----------------------------------------------------------------------------
@needs_ffmpeg
def test_fade_edit_renders_clean_and_fades_to_black(tmp_path):
    media = tmp_path / "data/rockstar_media"
    media.mkdir(parents=True)
    for i, src in enumerate(["testsrc2=s=1280x720", "smptehdbars=s=1280x720", "mandelbrot=s=1280x720"]):
        subprocess.run(FF + ["-f", "lavfi", "-i", src, "-t", "8", "-r", "30", "-pix_fmt", "yuv420p",
                             str(media / f"t{i}.mp4")], check=True)
    s = TG.settings({"tokyo_girl": {"trailer_head_s": 0, "trailer_tail_s": 0, "min_luma": 0}}, tmp_path)
    shots = [vshot(f"t{i}.mp4", 0.5, 7.5) for i in range(3)]
    for sh in shots:
        sh.w, sh.h = 1280, 720
    plan = plan_of([(shots[0], 2.0), (shots[1], 0.4), (shots[2], 2.0), (shots[0], 1.6)])
    r = S.resolve(dict(S.default_style("tokyo_girl"), transition="fade", custom={"fade_s": 0.6, "end_fade_s": 1.5,
                                                                                  "sharpen": 0.5}))
    out = tmp_path / "e.mp4"
    TG.render_plan(plan, s, out, text_style=r, cards=[], look=S.look_filter(r["look"], "tokyo_girl"))
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                                   str(out)], capture_output=True, text=True, check=True).stdout)
    assert float(j["format"]["duration"]) == pytest.approx(6.0, abs=0.05)
    prepared = TG.prepare(plan, r, media, cuts_of=lambda sh: [])
    assert TG.glitches(TG.edit_cuts(out), prepared) == []        # every change is a planned soft fade
    last = subprocess.run(FF + ["-sseof", "-0.05", "-i", str(out), "-frames:v", "1", "-vf", "scale=8:8,format=gray",
                                "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    assert max(last) < 20                                          # faded to black


@needs_ffmpeg
def test_held_slot_renders_its_full_length(tmp_path):
    media = tmp_path / "m"
    media.mkdir()
    subprocess.run(FF + ["-f", "lavfi", "-i", "testsrc2=s=640x360", "-t", "3", "-r", "30", "-pix_fmt", "yuv420p",
                         str(media / "a.mp4")], check=True)
    sh = vshot("a.mp4", 0.0, 2.9)
    sh.w, sh.h = 640, 360
    slot = TG.Slot(sh, 2.0, 2, 0.0, 2.0, 0, False, 1, hold=0.9)
    TG.render_slot(slot, media, tmp_path / "s.mp4", extra=0.3)
    n = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", str(tmp_path / "s.mp4")],
                       capture_output=True, text=True, check=True).stdout.strip()
    assert int(n) == round(2.3 * TG.FPS)


# ---- clip reuse from a named version -----------------------------------------------------------
def test_footage_reference_from_words_or_file_name():
    assert fb.footage_ref("USE ALL THE CLIPS FROM version B1 AND STOP ADDING NEW ONES") == \
        {"label": "B", "round": 1, "item": None, "exact": False}
    assert fb.footage_ref("Use the SAME clips as in B1, keep the logo") ["label"] == "B"
    link = "https://cdn.discordapp.com/attachments/1/2/B_tokyogirl-20261118-004557.mp4?ex=6a"
    assert fb.footage_ref(f"use all the clips from {link}") == \
        {"label": "B", "round": None, "item": "tokyogirl-20261118-004557", "exact": True}
    assert fb.footage_ref("make it darker") is None
    item = {"rounds": [{"variants": [{"label": "A"}, {"label": "B", "x": 1}, {"label": "C"}]},
                       {"variants": [{"label": "B2", "x": 2}]}]}
    assert fb.find_version(item, fb.footage_ref("clips from b1"))["x"] == 1
    assert fb.find_version(item, fb.footage_ref("clips from B2"))["x"] == 2
    assert fb.find_version(item, {"label": "B2", "exact": True})["x"] == 2
    assert fb.find_version(item, fb.footage_ref("clips from C4")) is None


def test_named_version_is_found_in_this_or_another_item(tmp_path):
    store = R.Store(tmp_path)
    old = store.create("tokyo_girl", {"day": "2026-11-18"}, now=datetime(2026, 11, 18, 0, 45, 57).timestamp())
    with store.edit(old["id"]) as it:
        it["rounds"] = [{"variants": [{"label": "B", "footage": [{"key": "a@1.0"}]}]}]
    cur = store.create("tokyo_girl", {"day": "2026-11-19"})
    v, where = R.named_version(store, store.load(cur["id"]), f"use the clips from B_{old['id']}.mp4")
    assert v["footage"] == [{"key": "a@1.0"}] and where == store.dir(old["id"])
    assert R.named_version(store, store.load(cur["id"]), "darker please") is None


# ---- stream clips: lead-in and the real-facecam check -----------------------------------------------
def test_lead_in_starts_earlier_and_on_the_sentence():
    ws = [editor.Word(3.1, 3.4, "So"), editor.Word(3.5, 3.9, "clingy"), editor.Word(6.0, 6.4, "wow"),
          editor.Word(9.0, 9.4, "no")]
    base = editor.keep_segments(ws, 40.0, 10, peak=10.0, max_gap_s=None, payoff=0.5)
    assert base[0][0] == 5.0                                      # "So clingy" is cut off
    lead = editor.keep_segments(ws, 40.0, 10, peak=10.0, max_gap_s=None, payoff=0.5, lead_in=1.6)
    assert lead[0][0] == pytest.approx(3.1 - 0.25) and lead[0][1] == base[0][1]   # back to "So"


def test_facecam_face_share_and_profile_switch(tmp_path):
    face = [{"class": "FACE_MALE", "score": 0.8, "box": [0, 0, 10, 10]}]
    assert safety.face_share([face, [], face, face]) == 0.75 and safety.face_share([]) == 0.0
    assert safety.face_share([[{"class": "FACE_FEMALE", "score": 0.2}]]) == 0.0
    from spikebot import lacy as L
    store = R.Store(tmp_path)
    assert L.facecam_required(store) and L.facecam_required(None)
    prof = store.profile()
    prof.add_entry("setting", ["lacy"], "require_facecam", 0, said="facecam check off", source="test")
    prof.save()
    assert L.facecam_required(store) is False


@needs_ffmpeg
def test_real_facecam_with_a_fake_detector(tmp_path, monkeypatch):
    f = tmp_path / "s.mp4"
    subprocess.run(FF + ["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=4", "-pix_fmt", "yuv420p", str(f)],
                   check=True)
    monkeypatch.undo()   # the conftest stub: use the real function with fake detectors
    box = {"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.3}
    faces = lambda crop: [{"class": "FACE_MALE", "score": 0.9, "box": [0, 0, 5, 5]}]
    assert safety.real_facecam(f, box, samples=4, detector=faces) is True
    assert safety.real_facecam(f, box, samples=4, detector=lambda crop: []) is False


# ---- end to end: the review path ------------------------------------------------------------------
@needs_ffmpeg
def test_presents_intro_and_reusing_another_versions_clips(tmp_path, monkeypatch):
    media = tmp_path / "data/rockstar_media"
    media.mkdir(parents=True)
    srcs = ["gradients=s=1280x720:seed=1:speed=0.01", "testsrc2=s=1280x720", "smptehdbars=s=1280x720",
            "mandelbrot=s=1280x720", "gradients=s=1280x720:seed=7:type=radial:speed=0.02"]
    for i, src in enumerate(srcs):
        subprocess.run(FF + ["-f", "lavfi", "-i", src, "-t", "12", "-r", "30", "-pix_fmt", "yuv420p",
                             str(media / f"trailer_{i}.mp4")], check=True)

    def vision(content):   # trailer_0 is the "Rockstar Games Presents" shot over the ocean
        n = sum(1 for c in content if c.get("type") == "image")
        return {"shots": [{"i": j, "tags": [], "action": 0.5, "calm": 0.5, "impact_end": 0.2,
                           "subject_x": 0.5, "subject_w": 0.3} for j in range(n)]}
    first = {"done": False}

    def labels(content):
        out = vision(content)
        if not first["done"]:
            first["done"] = True
            out["shots"][0]["tags"] = ["rockstar_presents", "ocean", "skyline"]
        return out
    monkeypatch.setattr(TG.Library, "label_ask", staticmethod(labels))
    cfg = {"tokyo_girl": {"min_s": 8, "max_s": 12, "per_day": 1, "engine": "ffmpeg", "trailer_head_s": 0,
                          "trailer_tail_s": 0, "min_luma": 0}, "data_dir": "data"}
    s = TG.settings(cfg, tmp_path)
    now = datetime(2026, 11, 18, 18, 0, tzinfo=TG.UK).timestamp()
    TG.save_state(Path(s["state_file"]), {"last_fetch": now})
    store = R.Store(tmp_path / "data")
    prof = store.profile()
    prof.add_entry("setting", ["tokyo_girl"], "intro", "rockstar_presents", said="Rockstar intro", source="test")
    prof.save()
    item = store.load(TG.daily(s, now)[0])
    lib = json.loads((media / "library.json").read_text())
    pres_file = next(rel for rel, e in lib.items() if shot_labels.presents_segments(e))
    a = R.variant_of(item, 1, "A")
    assert a["intro"] == "rockstar_presents" and a["footage"][0]["key"].startswith(pres_file)
    assert not any(f["key"].startswith(pres_file) for f in a["footage"][1:])    # only ever the intro
    keys = [f["key"] for f in a["footage"]]
    body = keys[1:]   # unique_clips: a clip repeats only once all 4 usable clips are in, never back to back
    assert len(set(body[:4])) == min(4, len(body)) and all(x != y for x, y in zip(body, body[1:]))
    with store.edit(item["id"]) as it:
        R.request(it, "feedback", now + 2, round=1, label="B", notes="use all the clips from A1, nothing new")
    item = R.render_round(store, item["id"], cfg, tmp_path, now=now + 3)
    b2 = item["rounds"][-1]["variants"][0]
    got = [f["key"] for f in b2["footage"]]
    assert got == keys[:len(got)] and len(got) >= len(keys) - 1     # A's clips, A's order, none added
    assert b2["footage_note"].startswith("Same clips as A")


@pytest.mark.remotion
def test_remotion_renders_held_frames_two_clip_fades_and_end_fade(tmp_path, monkeypatch):
    from spikebot import remotion as RM
    from tests.test_remotion import CHROME
    if not (RM.available() and shutil.which("ffmpeg")):
        pytest.skip("needs node + remotion/node_modules")
    if CHROME:
        monkeypatch.setenv("REMOTION_BROWSER", CHROME)
    media = tmp_path / "media"
    media.mkdir()
    for i, src in enumerate(["testsrc2=s=1280x720", "smptehdbars=s=1280x720"]):
        subprocess.run(FF + ["-f", "lavfi", "-i", src, "-t", "6", "-r", "30", "-pix_fmt", "yuv420p",
                             str(media / f"t{i}.mp4")], check=True)
    a, b = vshot("t0.mp4", 0.5, 5.5), vshot("t1.mp4", 0.5, 2.0)
    for sh in (a, b):
        sh.w, sh.h = 1280, 720
    plan = plan_of([(a, 1.5), (b, 2.0), (a, 1.0)])
    r = S.resolve(dict(S.default_style("tokyo_girl"), transition="fade", custom={"end_fade_s": 1.0, "sharpen": 0.5}))
    plan = TG.prepare(plan, r, media, cuts_of=lambda sh: [])
    assert plan.slots[1].hold is not None                 # b runs out before its fade tail ends: held
    out = tmp_path / "out.mp4"
    RM.render(plan, {"media_dir": str(media), "data_dir": str(tmp_path)}, out, r, [])
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                                   str(out)], capture_output=True, text=True, check=True).stdout)
    assert float(j["format"]["duration"]) == pytest.approx(4.5, abs=0.1)
    last = subprocess.run(FF + ["-sseof", "-0.05", "-i", str(out), "-frames:v", "1", "-vf", "scale=8:8,format=gray",
                                "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    assert max(last) < 24


def test_plan_from_footage_keeps_every_clip_on_the_new_beats():
    shots = [vshot(f"c{i}.mp4", 0, 6) for i in range(5)]
    footage = [{"key": s.key, "offset": 1.0, "dur": d, "pan": 1} for s, d in zip(shots, [1.0, 2.0, 0.5, 1.5, 1.0])]
    footage.insert(2, {"key": "gone.mp4@0.0", "dur": 1.0})
    base = plan_of([(shots[0], 2.0), (shots[1], 2.0)])
    beats = [i * 0.5 for i in range(40)]
    plan, kept = TG.plan_from_footage(base, footage, shots, beats, 120)
    assert kept == 5 and [sl.shot.key for sl in plan.slots] == [s.key for s in shots]
    assert all(abs(sl.start * 2 - round(sl.start * 2)) < 1e-6 for sl in plan.slots)   # on the beat
    assert [sl.dur for sl in plan.slots] == [1.0, 2.0, 0.5, 1.5, 1.0] and plan.duration == 6.0


def test_card_shows_intro_and_glitch_notes():
    from spikebot import discord_bot as D
    item = {"id": "tokyogirl-1", "mode": "tokyo_girl", "source": {"day": "2026-11-19"}}
    v = {"label": "A", "duration": 20, "silent": True, "intro_note": "No Rockstar shot: used the logo intro",
         "glitch_note": "Glitch check: fixed 1 stray cut(s)"}
    p = D.round_payload(item, {"n": 1, "kind": "initial", "variants": [v]})
    text = p["embeds"][0]["fields"][0]["value"]   # one column per version
    assert "⚠️ No Rockstar shot" in text and "🛠️ Glitch check" in text



# ---- #85 streamer's voice only, #86 footage variety -------------------------------------------------
def test_captions_keep_only_the_streamers_voice(tmp_path, monkeypatch):
    from spikebot import lacy as L
    ws = [editor.Word(0.0, 0.3, "bro"), editor.Word(0.35, 0.6, "look"), editor.Word(2.0, 2.4, "Lucia,"),
          editor.Word(2.45, 2.9, "we"), editor.Word(2.95, 3.3, "go"), editor.Word(5.0, 5.4, "no"), editor.Word(5.5, 5.9, "way")]
    assert L.phrase_lines(ws) == [[0, 1], [2, 3, 4], [5, 6]]
    seen = []

    def fake(name, game, rows, base=None, client=None):
        seen.append(rows)
        return [1]                                  # "Lucia, we go" is a cutscene line
    monkeypatch.setattr(ai, "available", lambda base=None: True)
    monkeypatch.setattr(ai, "game_lines", fake)
    ad = L.ReviewAdapter({}, tmp_path, lcfg={"channel": "lacy"})
    job = {"id": "lacy-1", "channel": "lacy", "platform": "twitch", "category": "Grand Theft Auto VI"}
    out = ad.streamer_words(job, tmp_path, ws)
    assert [w.text for w in out] == ["bro", "look", "no", "way"] and seen[0][1][2] == "Lucia, we go"
    assert [w.text for w in ad.streamer_words(job, tmp_path, ws)] == ["bro", "look", "no", "way"]
    assert len(seen) == 1                           # cached in speakers.json
    monkeypatch.setattr(ai, "available", lambda base=None: False)
    assert ad.streamer_words(job, tmp_path / "new", ws) == ws      # no key: every line stays
    assert S.resolve(S.default_style("lacy"))["streamer_only"] and S.valid_modes("streamer_only", 1) == ["lacy"]


def test_variety_avoids_recent_clips_and_busy_trailers():
    t2 = [vshot("trailer2.mp4", i * 10, i * 10 + 5) for i in range(4)]
    ext = [vshot("extended_look.mp4", i * 10, i * 10 + 5) for i in range(8)]
    shots = t2 + ext
    edits = [[t2[0].key, t2[1].key, t2[2].key], [t2[3].key, t2[0].key]]
    recent, load = TG.recent_load(shots, edits, 0.7)
    assert t2[0].key in recent and load["trailer2.mp4"] > load["extended_look.mp4"] == 0
    r = S.resolve(S.default_style("trend"))
    p = TG.prefs_for(r, shots, edits=edits)
    assert p.variety == 0.7 and p.recent == recent
    picks = {TG.choose_shot(shots, "city", 2.0, [], {}, random.Random(i), p).src for i in range(20)}
    assert picks == {"extended_look.mp4"}            # the trailer used in every recent edit is rested
    off = TG.prefs_for(dict(r, variety=0.0), shots, edits=edits)
    assert not off.recent and off.variety == 0
    assert TG.recent_load(shots, None, 0.7) == (set(), {})
