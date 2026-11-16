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
    assert item["request"]["status"] == "failed" and "Lacy title rules" in item["request"]["error"]
    # closing the item frees the segment
    L.ReviewAdapter({"data_dir": str(tmp_path)}, tmp_path, lcfg, jobs).finish(item, "rejected")
    assert not (d / "segment.mp4").exists() and jobs.load(job["id"])["status"] == "rejected"


# ---- Tokyo Girl end to end ------------------------------------------------------------------
@needs_ffmpeg
def test_tokyo_girl_review_items_are_silent_and_different(tmp_path):
    media = tmp_path / "data/tokyo_girl/media"
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
        assert v["silent"] and v["captions"]["instagram"].endswith("#GTA6")
    # approving copies the edit to exports/tokyo_girl
    with store.edit(item["id"]) as it:
        R.approve(it, 1, "B", R.settings({}), set())
    R.make_final(store, store.load(item["id"]))
    TG.ReviewAdapter({}, tmp_path, s).finish(store.load(item["id"]), "done")
    out = list(Path(s["export_dir"]).glob("*.mp4"))
    assert len(out) == 1 and out[0].with_suffix(".txt").read_text().startswith("IG: ")
