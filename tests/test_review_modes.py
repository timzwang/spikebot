"""End-to-end: Lacy and Tokyo Girl render 3 real review variants with ffmpeg."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from spikebot import editor
from spikebot import lacy as L
from spikebot import review as R
from spikebot import style as S
from spikebot import tokyo_girl as TG
from tests.test_lacy import T, fake_fetch, job_for, make_stream

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def words(n=24):
    # speech with a long silence in the middle (jump-cut material)
    out = []
    t = 0.3
    for i in range(n):
        out.append(editor.Word(round(t, 2), round(t + 0.3, 2), f"word{i}"))
        t += 0.4 if i != 10 else 3.0
    return out


# ---- pure pacing helpers -------------------------------------------------------------------
def test_keep_segments_window_and_jump_cuts():
    ws = words()
    full = editor.keep_segments(ws, 40.0, 59, peak=20.0, max_gap_s=None)
    assert full == [(0.0, 40.0)]
    win = editor.keep_segments(ws, 40.0, 10, peak=20.0, max_gap_s=None, payoff=0.5)
    assert win == [(15.0, 25.0)]
    cut = editor.keep_segments(ws, 13.0, 13, peak=1.0, max_gap_s=0.5)
    assert len(cut) == 2 and editor.cut_length(cut) < 13.0   # the 3 s silence is cut out
    remapped = editor.remap_words(ws, cut)
    assert remapped[0].start == pytest.approx(ws[0].start)
    assert all(b.start >= a.start for a, b in zip(remapped, remapped[1:]))
    # the moments around the chat peak are always kept even without words
    quiet = editor.keep_segments([], 30.0, 30, peak=15.0, max_gap_s=0.5)
    assert quiet == [(12.0, 18.0)]


def test_caption_style_and_end_card():
    r = S.resolve({"font": "montserrat", "size": "xl", "position": "middle", "colour": "lime",
                   "ending": "cta", "pace": "fast", "custom": {"hook_s": 3}})
    ass = editor.build_ass(words(6), "Lacy hook", 10.0, 600, watermark=None, style=r)
    assert "Style: Cap,Montserrat,109," in ass and "Mark,," not in ass.split("[Events]")[1]
    assert "0:00:03.00,Hook" in ass.replace("Dialogue: 1,0:00:00.00,", "")  # hook only 3 s
    assert "Follow for daily Lacy GTA clips" in ass
    assert "\\c&H3AFF7C&" in ass   # lime highlight (BGR)
    assert editor.caption_margin("low", 600, 84) == 420
    assert editor.caption_margin("high", 600, 84) > editor.caption_margin("middle", 600, 84) > 420


# ---- Lacy end to end -------------------------------------------------------------------------
@needs_ffmpeg
def test_lacy_job_becomes_three_reviewed_variants(tmp_path):
    src = tmp_path / "stream.mp4"
    make_stream(src, seconds=12)
    jobs, lcfg = L.Jobs(tmp_path), L.settings({})
    store = R.Store(tmp_path)

    def tr(path, model):
        return words(14), "en", 0.97
    job = L.render_job(job_for(tmp_path, jobs, 1), lcfg, jobs, set(), fetch=fake_fetch(src), transcribe=tr,
                       now=T, review_store=store, cfg={"data_dir": str(tmp_path)}, base=tmp_path)
    assert job["status"] == "review", job.get("reason")
    d = jobs.dir(job["id"])
    assert (d / "segment.mp4").exists() and (d / "words.json").exists()   # kept for re-renders
    item = store.load(job["review_item"])
    assert item["status"] == "open" and item["mode"] == "lacy"
    vs = item["rounds"][0]["variants"]
    assert [v["label"] for v in vs] == ["A", "B", "C"]
    assert len({v["title"] for v in vs}) == 3 and all(L.title_ok(v["title"]) for v in vs)
    assert all("Lacy" in v["captions"]["instagram"] and v["captions"]["tiktok"] for v in vs)
    for v in vs:
        info = editor.probe(store.dir(item["id"]) / v["file"])
        assert (info["width"], info["height"]) == (1080, 1920) and info["audio"]
        assert (store.dir(item["id"]) / v["preview"]).stat().st_size < 3_000_000
        assert "#Lacy" in v["description"] and "#Shorts" in v["description"]
    # Feedback re-render from the kept segment
    with store.edit(item["id"]) as it:
        R.request(it, "feedback", round=1, label="A", notes="bigger text. hook: Lacy did not see this coming")
    item = R.render_round(store, item["id"], {"data_dir": str(tmp_path)}, tmp_path, now=T + 60,
                          adapter=L.ReviewAdapter({"data_dir": str(tmp_path)}, tmp_path, lcfg, jobs, set()))
    v2 = item["rounds"][1]["variants"][0]
    assert v2["label"] == "A2" and v2["title"] == "Lacy did not see this coming"
    # a hook that breaks the Lacy rules is refused, not rendered
    with store.edit(item["id"]) as it:
        R.request(it, "feedback", round=2, label="A2", notes="hook: this is so cringe")
    item = R.render_round(store, item["id"], {"data_dir": str(tmp_path)}, tmp_path, now=T + 90,
                          adapter=L.ReviewAdapter({"data_dir": str(tmp_path)}, tmp_path, lcfg, jobs, set()))
    assert item["request"]["status"] == "failed" and "clip title rules" in item["request"]["error"]
    # closing the item frees the segment
    L.ReviewAdapter({"data_dir": str(tmp_path)}, tmp_path, lcfg, jobs).finish(item, "rejected")
    assert not (d / "segment.mp4").exists() and jobs.load(job["id"])["status"] == "rejected"


# ---- Tokyo Girl end to end ------------------------------------------------------------------
@needs_ffmpeg
def test_tokyo_girl_review_items_are_silent_and_different(tmp_path):
    media = tmp_path / "data/rockstar_media"
    media.mkdir(parents=True)
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for i in range(3):
        subprocess.run(ff + ["-f", "lavfi", "-i", f"gradients=s=1920x1080:seed={i}", "-frames:v", "1",
                             str(media / f"vice_city_{i}.jpg")], check=True)
    s = TG.settings({"tokyo_girl": {"min_s": 8, "max_s": 16, "per_day": 1}, "data_dir": "data"}, tmp_path)
    assert s["review"] is True
    now = datetime(2026, 11, 16, 6, 50, tzinfo=TG.UK).timestamp()
    TG.save_state(Path(s["state_file"]), {"last_fetch": now})
    ids = TG.daily(s, now)
    assert len(ids) == 1 and TG.daily(s, now + 60) == []          # one item a day (per_day=1)
    store = R.Store(tmp_path / "data")
    item = store.load(ids[0])
    vs = item["rounds"][0]["variants"]
    assert len({v["story"] for v in vs}) == 3
    assert len({v["style"]["pace"] for v in vs}) == 3
    for v in vs:
        p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                            "-of", "json", str(store.dir(item["id"]) / v["file"])],
                           capture_output=True, text=True, check=True)
        streams = json.loads(p.stdout)["streams"]
        assert [x["codec_type"] for x in streams] == ["video"]   # silent
        assert v["silent"] and v["captions"]["instagram"].endswith("#vicecity")
    # approving copies the edit to exports/tokyo_girl
    with store.edit(item["id"]) as it:
        R.approve(it, 1, "B", R.settings({}), set())
    R.make_final(store, store.load(item["id"]))
    TG.ReviewAdapter({}, tmp_path, s).finish(store.load(item["id"]), "done")
    out = list(Path(s["export_dir"]).glob("*.mp4"))
    assert len(out) == 1 and out[0].with_suffix(".txt").read_text().startswith("IG: ")


@needs_ffmpeg
def test_trending_sound_edits_use_each_sound_and_not_tokyo_girl(tmp_path):
    from spikebot import discord_bot as DB
    from spikebot import music as M
    from tests.test_music import song
    media = tmp_path / "data/rockstar_media"
    media.mkdir(parents=True)
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for i in range(3):
        subprocess.run(ff + ["-f", "lavfi", "-i", f"gradients=s=1920x1080:seed={i}", "-frames:v", "1",
                             str(media / f"vice_city_{i}.jpg")], check=True)
    s = TG.settings({"tokyo_girl": {"min_s": 8, "max_s": 16}, "data_dir": "data"}, tmp_path)
    lib = M.Library(s["music_dir"])
    lib.add(song(tmp_path / "tg.wav", seconds=30), "Tokyo Girl", replace=True)
    trend = lib.add(song(tmp_path / "t.wav", drop=8.0, seconds=12), "original sound - someone")
    lib.set_meta(trend["file"], url="https://www.tiktok.com/music/original-sound-7300000000000000000")
    now = datetime(2026, 11, 17, 18, 0, tzinfo=TG.UK).timestamp()
    ids = TG.make_trend(s, now=now)
    assert len(ids) == 1                                    # the Tokyo Girl song is never a trend
    store = R.Store(tmp_path / "data")
    item = store.load(ids[0])
    assert item["status"] == "open" and item["source"]["song"] == trend["file"]
    for v in item["rounds"][0]["variants"]:
        assert v["music"]["song"] == trend["name"]
        assert v["music"]["start_s"] + v["duration"] <= trend["duration"] + 0.5   # fits inside the sound
        assert "Tokyo Girl" not in v["captions"]["tiktok"] and "tiktok.com/music" in v["music_note"]
    assert DB.mode_label(item)[0] == "Trending sound"
    card = DB.round_payload(item, item["rounds"][0])
    assert "Trending sound" in card["content"]
    assert all(trend["name"] in e["description"] and "start" in e["description"] for e in card["embeds"])
    assert TG.items_on(TG.load_state(Path(s["state_file"])), "2026-11-17") == 0   # not Tokyo Girl's quota


@needs_ffmpeg
def test_tokyo_girl_feedback_keeps_the_clips_and_switches_the_sound(tmp_path, monkeypatch):
    """Tim: "same clips as B1, switch the song to impulsive" -> B2 has B1's clips in B1's order,
    cut to Impulsive; feedback on B2 keeps both. Nothing about it is saved as a lasting rule."""
    from spikebot import ai
    from spikebot import music as M
    from tests.test_music import song
    monkeypatch.setattr(ai, "available", lambda base=None: False)   # the rule parser, no API
    media = tmp_path / "data/rockstar_media"
    media.mkdir(parents=True)
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for i in range(8):
        subprocess.run(ff + ["-f", "lavfi", "-i", f"gradients=s=1920x1080:seed={i}", "-frames:v", "1",
                             str(media / f"vice_city_{i}.jpg")], check=True)
    cfg = {"tokyo_girl": {"min_s": 8, "max_s": 16, "per_day": 1}, "data_dir": "data"}
    s = TG.settings(cfg, tmp_path)
    lib = M.Library(s["music_dir"])
    lib.add(song(tmp_path / "tg.wav", seconds=30), "Tokyo Girl", replace=True)
    imp = lib.add(song(tmp_path / "imp.wav", drop=6.0, seconds=30), "Impulsive")
    now = datetime(2026, 11, 18, 18, 0, tzinfo=TG.UK).timestamp()
    TG.save_state(Path(s["state_file"]), {"last_fetch": now})
    store = R.Store(tmp_path / "data")
    item = store.load(TG.daily(s, now)[0])
    b1 = R.variant_of(item, 1, "B")
    assert b1["music"]["song"] == "tokyo-girl" and b1["footage"]
    keys = [f["key"] for f in b1["footage"]]

    with store.edit(item["id"]) as it:
        R.request(it, "feedback", now, round=1, label="B",
                  notes="Use the exact same clips as B1 and switch the song to impulsive")
    item = R.render_round(store, item["id"], cfg, tmp_path, now=now + 1)
    assert (item.get("request") or {}).get("status") == "done", item.get("request")
    b2 = item["rounds"][-1]["variants"][0]
    assert b2["label"] == "B2" and b2["music"]["song"] == imp["name"]
    n = min(len(keys), len(b2["footage"]))
    assert [f["key"] for f in b2["footage"]][:n] == keys[:n]
    assert b2["footage_note"].startswith("Same clips as B1")
    assert "Tokyo Girl" not in b2["captions"]["tiktok"]
    assert "Sound: impulsive" in item["rounds"][-1]["comment"]
    assert not [e for e in store.profile().entries() if e["kind"] in ("note", "unsupported")]

    with store.edit(item["id"]) as it:        # next feedback: keeps the sound and the clips
        R.request(it, "feedback", now + 2, round=2, label="B2", notes="warmer colours")
    item = R.render_round(store, item["id"], cfg, tmp_path, now=now + 3)
    b3 = item["rounds"][-1]["variants"][0]
    assert b3["music"]["song"] == imp["name"] and b3["style"]["grade"] == "warm"
    assert [f["key"] for f in b3["footage"]] == [f["key"] for f in b2["footage"]][:len(b3["footage"])] or \
        b3["footage_note"].startswith("Same clips as B2")


def test_feedback_reads_footage_and_sound_requests():
    from spikebot import feedback as fb
    assert fb.footage_of("use the exact same clips as B1") == "keep"
    assert fb.footage_of("different clips please") == "new" and fb.footage_of("darker") == "default"
    assert fb.song_of("switch song to impulsive") == "impulsive"
    assert fb.song_of("use the impulsive sound") == "impulsive" and fb.song_of("darker") is None


@needs_ffmpeg
def test_clips_of_an_old_edit_are_read_back_from_its_video(tmp_path):
    """Versions made before clip lists were saved: the clips are matched from the video."""
    media = tmp_path / "data/rockstar_media"
    media.mkdir(parents=True)
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    srcs = ["mandelbrot=s=640x360", "testsrc2=s=640x360", "gradients=s=640x360:seed=3:speed=0.02",
            "gradients=s=640x360:seed=9:speed=0.02:type=radial", "smptehdbars=s=640x360"]
    for i, src in enumerate(srcs):
        subprocess.run(ff + ["-f", "lavfi", "-i", src, "-t", "12", "-r", "25", "-pix_fmt", "yuv420p",
                             str(media / f"trailer_{i}.mp4")], check=True)
    cfg = {"tokyo_girl": {"min_s": 8, "max_s": 12, "per_day": 1, "engine": "ffmpeg", "trailer_head_s": 0,
                          "trailer_tail_s": 0, "min_luma": 0}, "data_dir": "data"}
    s = TG.settings(cfg, tmp_path)
    now = datetime(2026, 11, 18, 18, 0, tzinfo=TG.UK).timestamp()
    TG.save_state(Path(s["state_file"]), {"last_fetch": now})
    store = R.Store(tmp_path / "data")
    item = store.load(TG.daily(s, now)[0])
    v = R.variant_of(item, 1, "A")
    truth = [f["key"] for f in v["footage"]]
    old = dict(v)
    old.pop("footage")
    ad = TG.ReviewAdapter(cfg, tmp_path, s)
    got = ad.recover(store.dir(item["id"]), old)
    assert got, "nothing recovered"
    srcs_truth = [k.split("@")[0] for k in truth]
    srcs_got = [f["src"] for f in got if f.get("key")]
    def at(rows, t):   # which file is on screen at edit time t
        return next((r.get("src") or (r["key"] or "").split("@")[0] for r in rows
                     if r["start"] <= t < r["start"] + r["dur"]), None)
    times = [f["start"] + f["dur"] / 2 for f in v["footage"]]
    hits = sum(1 for t in times if at(got, t) == at(v["footage"], t))
    assert hits >= len(times) * 0.7, (srcs_truth, srcs_got)   # synthetic footage: real trailers match better
    assert abs(len(got) - len(truth)) <= 3
