"""Editor rules that used to be "not possible": no camera movement, whole-frame framing,
trailer clips only, held clips, crossfades, a length cap, the logo build-up that bursts on the
drop, a countdown / plain line, and turning Tim's old notes into these settings."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

from spikebot import remotion as RM
from spikebot import review as R
from spikebot import style as S
from spikebot import tokyo_girl as TG

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
TIM_RULES = [("all", "motion", "none"), ("tokyo_girl", "framing", "fit"), ("tokyo_girl", "media", "clips"),
             ("tokyo_girl", "transition", "fade"), ("tokyo_girl", "hold_beats", 2.0),
             ("tokyo_girl", "max_s", 12.0), ("tokyo_girl", "intro", "logo"), ("tokyo_girl", "logo_s", 2.0),
             ("tokyo_girl", "logo_size", 0.6), ("all", "text_level", "none")]


# ---- settings ----------------------------------------------------------------------------
def test_new_settings_exist_and_resolve():
    tg, lacy = S.dims("tokyo_girl"), S.dims("lacy")
    assert tg["motion"] == lacy["motion"] == ("none", "subtle", "full")
    assert tg["framing"] == ("fill", "fit") and tg["media"] == ("clips", "mixed") and "fade" in tg["transition"]
    assert "framing" not in lacy and "fade" not in lacy["transition"] and "plain" not in lacy["text_level"]
    assert "countdown" in tg["intro"] and "plain" in tg["text_level"]
    r = S.resolve(S.default_style("lacy") | {"motion": "none", "transition": "zoom"})
    assert r["motion"] == "none" and r["transition"] == "cut"          # a punch-in zoom is camera movement
    r = S.resolve(S.default_style("tokyo_girl") | {"transition": "fade", "custom": {"max_s": 24, "hold_beats": 2,
                                                                                     "logo_size": 0.6}})
    assert (r["transition"], r["max_s"], r["hold_beats"], r["logo_size"]) == ("fade", 24.0, 2, 0.6)
    ok, bad = S.apply_settings([{"mode": "all", "setting": "framing", "value": "fit"},
                                {"mode": "all", "setting": "max_s", "value": "24"}])
    assert [(o["mode"], o["setting"]) for o in ok] == [("tokyo_girl", "framing"), ("tokyo_girl", "max_s")]
    assert not bad


def test_feedback_words_map_to_the_new_settings():
    ch, _, intents, _ = S.parse_feedback("No camera movement or zooms at all. Never use screenshots. Stop cropping"
                                         " off key details. Softer crossfades between clips.", "tokyo_girl")
    assert ch == {"motion": "none", "media": "clips", "framing": "fit", "transition": "fade"}
    assert {"static", "clips_only", "fit_frame", "crossfade"} <= set(intents)


def test_old_notes_become_settings_once(tmp_path):
    notes = ["HARD RULE: trailer video clips only. Never use screenshots or stills in any Tokyo Girl edit.",
             "STRICT AND PERMANENT: no camera movement of any kind. Every shot must be fully static.",
             "Tokyo Girl edits must never run longer than 24 seconds in total.",
             "Opening intro: build the GTA VI logo gradually over about 4 seconds, then at the beat drop expand it"
             " quickly while fading it to transparent, so it never covers more than about 60% of the screen.",
             "Place the logo intro over an ambient atmosphere trailer clip, not action footage or a screenshot.",
             "Study the creator's inspiration examples and match their style of clip selection and pacing."]
    old = {"rules": [{"id": 1, "ts": 1, "mode": "all", "text": "x", "settings": [], "notes": notes,
                      "unsupported": ["The editor has no setting for total video length or clip start times.",
                                      "Guaranteeing that every clip is semantically relevant to the story."]}]}
    (tmp_path / "p.json").write_text(json.dumps(old))
    prof = S.Profile(tmp_path / "p.json")
    tg = prof.pinned("tokyo_girl") | prof.custom_pins("tokyo_girl")
    assert tg["media"] == "clips" and tg["motion"] == "none" and tg["max_s"] == 24.0
    assert tg["intro"] == "logo" and tg["logo_s"] == 4.0 and tg["logo_size"] == 0.6
    assert prof.pinned("lacy")["motion"] == "none"
    es = {e["n"]: e for e in prof.entries()}
    assert not es[1]["on"] and es[1]["upgraded"].startswith("now #")
    assert not es[5]["on"] and es[5]["upgraded"]                             # ambient intro clip: automatic
    assert es[6]["on"]                                                       # not a setting: stays a note
    assert not es[7]["on"] and es[8]["on"]                                   # length: possible now; relevance: no
    prof.save()
    again = S.Profile(tmp_path / "p.json")
    assert len(again.entries()) == len(prof.entries())                       # only once
    prof.set_enabled([es[1]["n"]], True)


# ---- planning --------------------------------------------------------------------------
def test_merge_holds_keeps_the_total():
    assert TG.merge_holds([1, 1, 2, 1, 1, 1, 1], 2) == [2, 2, 2, 2]
    assert TG.merge_holds([4, 1], 2) == [5]
    assert sum(TG.merge_holds([1, 3, 1, 1, 2], 3)) == 8


def shots():
    calm = TG.Shot("beach.mp4", "video", 0.0, 20.0, ["beach"], None, 1920, 1080, motion=1.5)
    busy = [TG.Shot(f"chase{i}.mp4", "video", 0.0, 12.0, ["cars"], None, 1920, 1080, motion=30.0)
            for i in range(6)]
    return [calm] + busy


def test_plan_holds_clips_keeps_them_intact_and_starts_on_a_calm_clip():
    story = next(x for x in TG.STORIES if x.id == "partners")
    cfg = {"bpm": 120, "min_s": 8, "max_s": 12, "media_dir": "x"}
    plan = TG.plan_clip(story, shots(), cfg, date(2026, 11, 17), random.Random(3), hold_beats=2,
                        intro_s=2.0, intact=True)
    assert plan.duration <= 12.0
    assert plan.slots[0].shot.src == "beach.mp4" and plan.slots[0].dur >= 2.0       # calm clip under the logo
    assert all(sl.beats >= 2 for sl in plan.slots[1:])
    for sl in plan.slots[1:]:                                                        # the middle of each clip
        assert sl.offset == pytest.approx(sl.shot.start + (sl.shot.length - sl.dur) / 2, abs=0.01)
    assert abs(sum(sl.dur for sl in plan.slots) - plan.duration) < 0.01


def test_plain_and_countdown_lines_are_facts_not_questions():
    assert TG.plain_line(date(2026, 11, 17)) == "2 days until GTA 6"
    assert TG.countdown_line(date(2026, 11, 18)) == "1 day until GTA 6"
    assert TG.plain_line(date(2026, 11, 19)) == "GTA 6 is out today"
    assert TG.plain_line(date(2026, 12, 1)) == "GTA 6 is out now"
    ass = TG.build_text_ass([(0, 2, "2 days until GTA 6")], "DejaVu Sans", 100)
    assert ",Card," in ass and ",Ask," not in ass.split("[Events]")[1]             # no question box


def test_ffmpeg_filters_obey_motion_and_framing():
    sl = TG.Slot(shots()[1], 1.0, 2, 0.0, 1.0, 0, False, 1)
    moving = TG.slot_filter(sl, 1.0, "full")
    still = TG.slot_filter(sl, 1.0, "none")
    assert "+160.0*(t/" in moving and "t/" not in still and "crop=1080:1920:'1167.0':'0.0'" in still
    fit = TG.slot_filter(sl, 1.0, "none", "fit")
    assert "force_original_aspect_ratio=decrease" in fit and "boxblur" in fit and "overlay" in fit
    lg = TG.intro_filter(TG.logo_width(0.6), 4.0)
    assert f"scale=w='{int(1080 * 0.6 / 1.35)}*(1+0.35*clip((t-4.000)/0.400,0,1))'" in lg
    assert "fade=t=out:st=4.000:d=0.400" in lg and "fade=t=in:st=0:d=3.400" in lg


def test_remotion_props_carry_the_rules(tmp_path):
    from tests.test_remotion import plan_of
    plan = plan_of(tmp_path)
    style = S.default_style("tokyo_girl") | {"motion": "none", "framing": "fit", "transition": "fade",
                                             "custom": {"logo_size": 0.6, "fade_s": 0.5}}
    props, _ = RM.props_for(plan, {"font_size": 104}, S.resolve(style), [], tmp_path / "logo.png", 4.0, tmp_path)
    assert (props["motion"], props["framing"], props["fade"]) == ("none", "fit", 0.5)
    assert props["logo"]["until"] == 4.0 and props["logo"]["size"] == 0.6 and props["logo"]["out"] == 0.4


# ---- real renders ------------------------------------------------------------------------
def library(tmp_path) -> dict:
    media = tmp_path / "data/rockstar_media"
    media.mkdir(parents=True)
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    subprocess.run(ff + ["-f", "lavfi", "-i", "gradients=s=1920x1080:seed=4", "-frames:v", "1",
                         str(media / "screenshot_vice_city.jpg")], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "gradients=s=1920x1080:speed=0.002:r=30:d=14",
                         "-c:v", "libx264", "-preset", "ultrafast", str(media / "beach_sunset.mp4")], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=14", "-c:v", "libx264",
                         "-preset", "ultrafast", str(media / "car_chase.mp4")], check=True)
    logo = tmp_path / "data/brand/gta6_logo.png"
    logo.parent.mkdir(parents=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=0xff3399@1.0:s=800x400,format=rgba", "-frames:v", "1",
                         str(logo)], check=True)
    s = TG.settings({"tokyo_girl": {"min_s": 8, "max_s": 30, "per_day": 1, "trailer_head_s": 0,
                                    "trailer_tail_s": 0}, "data_dir": "data"}, tmp_path)
    return s


def pin_tims_rules(store):
    prof = store.profile()
    for mode, setting, value in TIM_RULES:
        prof.add_entry("setting", mode, setting, value)
    prof.save()


@needs_ffmpeg
def test_ffmpeg_render_follows_every_rule(tmp_path):
    s = library(tmp_path)
    store = R.Store(Path(s["data_dir"]))
    pin_tims_rules(store)
    now = datetime(2026, 11, 17, 6, 50, tzinfo=TG.UK).timestamp()
    TG.save_state(Path(s["state_file"]), {"last_fetch": now})
    ids = TG.make_review(s, 1, now=now)
    item = store.load(ids[0])
    assert item["status"] == "open", item.get("error")
    used = TG.load_state(Path(s["state_file"]))["uses"]
    assert used and all("@" in k for k in used)                       # trailer clips only, no screenshot
    for v in item["rounds"][0]["variants"]:
        st = v["style"]
        assert (st["motion"], st["framing"], st["media"], st["transition"]) == ("none", "fit", "clips", "fade")
        assert v["intro"] == "logo" and v["text_cards"] == 0
        j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                                       "-show_entries", "format=duration", "-of", "json",
                                       str(store.dir(item["id"]) / v["file"])],
                                      capture_output=True, text=True, check=True).stdout)
        assert [x["codec_type"] for x in j["streams"]] == ["video"]
        assert (j["streams"][0]["width"], j["streams"][0]["height"]) == (1080, 1920)
        assert float(j["format"]["duration"]) <= 12.05                 # max_s, logo intro included


def _chrome():
    from tests.test_remotion import CHROME
    return CHROME


@pytest.mark.remotion
@pytest.mark.skipif(not (RM.available() and shutil.which("ffmpeg")), reason="needs node + remotion/node_modules")
def test_remotion_render_with_still_fit_crossfade_and_logo(tmp_path, monkeypatch):
    if _chrome():
        monkeypatch.setenv("REMOTION_BROWSER", _chrome())
    s = library(tmp_path)
    shots = [x for x in TG.Library(s).shots() if x.kind == "video"]
    story = next(x for x in TG.STORIES if x.id == "partners")
    plan = TG.plan_clip(story, shots, dict(s, max_s=6, min_s=4), date(2026, 11, 17), random.Random(1),
                        hold_beats=2, intro_s=2.0, intact=True)
    style = S.default_style("tokyo_girl") | {"motion": "none", "framing": "fit", "transition": "fade",
                                             "custom": {"logo_size": 0.6}}
    out = tmp_path / "out.mp4"
    RM.render(plan, s, out, S.resolve(style), [], Path(s["logo_file"]), 2.0)
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                                   "-show_entries", "format=duration", "-of", "json", str(out)],
                                  capture_output=True, text=True, check=True).stdout)
    assert [x["codec_type"] for x in j["streams"]] == ["video"]
    assert float(j["format"]["duration"]) == pytest.approx(plan.duration, abs=0.1)


def test_length_cap_is_tokyo_girl_only_and_edits_never_outrun_the_sound():
    r = S.resolve(S.default_style("tokyo_girl") | {"custom": {"max_s": 24}})
    assert TG.edit_cap(r, {"max_s": 30}, trend=False) == 24.0
    assert TG.edit_cap(r, {"max_s": 30}, trend=True) == 30.0              # trending sounds: the sound limits it
    assert TG.edit_cap(S.resolve(S.default_style("tokyo_girl")), {"max_s": 30}, trend=False) == 30.0
    story = next(x for x in TG.STORIES if x.id == "partners")
    plan = TG.plan_clip(story, shots(), {"bpm": 120, "min_s": 8, "max_s": 12, "media_dir": "x"},
                        date(2026, 11, 17), random.Random(2))
    cut = TG.trim_plan(plan, 9.3)
    assert cut.duration <= 9.3 and abs(sum(sl.dur for sl in cut.slots) - cut.duration) < 0.01
    assert all(b <= 9.3 for _, b, _ in cut.cards) and TG.trim_plan(plan, 99) is plan
