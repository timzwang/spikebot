"""Lacy mode: GTA-only filter, titles, layout, BRB detection, thumbnails, daily cap,
submission log, and an end-to-end render with ffmpeg on a synthetic stream.
Run: python -m pytest -q"""
import asyncio
import csv
import random
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from spikebot import editor
from spikebot import lacy as L
from spikebot.editor import Word

HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
UK = L.UK
T = datetime(2026, 11, 19, 6, 0, tzinfo=UK).timestamp()


# ---- config -------------------------------------------------------------------
def test_settings_defaults_and_env_overrides():
    from spikebot.main import apply_env
    s = L.settings({})
    assert s["enabled"] is False and s["max_uploads_per_day"] == 3 and s["auto_upload"] is False
    cfg = apply_env({"lacy": {"max_uploads_per_day": 5, "facecam": {"x": 0.7}}}, [],
                    {"LACY_MODE": "true", "LACY_MAX_UPLOADS_PER_DAY": "2"})
    s = L.settings(cfg)
    assert s["enabled"] is True and s["max_uploads_per_day"] == 2
    assert s["facecam"] == {"x": 0.7, "y": 0.0, "w": 0.25, "h": 0.30}  # merged, not replaced
    assert L.settings({"lacy_mode": "false", "lacy": {"enabled": True}})["enabled"] is False


def test_is_gta_exact_categories_only():
    allowed = L.DEFAULTS["allowed_categories"]
    assert L.is_gta("Grand Theft Auto V", allowed)
    assert L.is_gta("grand theft auto vi ", allowed)
    for c in ("Grand Theft Auto IV", "Just Chatting", "Slots & Casino", "", None,
              "Grand Theft Auto: San Andreas"):
        assert not L.is_gta(c, allowed)


# ---- titles and description ----------------------------------------------------
def test_every_template_obeys_the_title_rules():
    for ts in L.TITLES.values():
        for t in ts:
            for game in ("GTA 5", "GTA 6"):
                title = t.format(game=game)
                assert L.title_ok(title), title
                assert "Lacy" in title


def test_title_rules_reject_negative_or_non_english():
    for bad in ("Lacy gets EXPOSED", "Lacy rage quits GTA", "Lacy's cringe moment",
                "Lacy fails again", "Lacy hate farm", "Chat mocks Lacy", "Lacy drama explained",
                "Lacy no puede creer esto ñ", "Funny GTA moment", "Lacy takes an L in GTA"):
        assert not L.title_ok(bad), bad


def test_make_title_matches_mood_and_never_repeats():
    rng = random.Random(1)
    t = L.make_title(["LMAO", "💀"], "Grand Theft Auto VI", set(), rng)
    assert t in [x.format(game="GTA 6") for x in L.TITLES["funny"]]
    used: set[str] = set()
    for _ in range(40):
        t = L.make_title(["NO WAY"], "Grand Theft Auto V", used, rng)
        assert t not in used and L.title_ok(t)
        used.add(t)


def test_description_has_required_hashtags():
    d = L.description("Lacy can't believe this happened in GTA", "Grand Theft Auto VI", "lacy")
    assert "#Lacy" in d and "#Shorts" in d and "#GTA6" in d
    assert "twitch.tv/lacy" in d
    assert "#GTA5" in L.description("x Lacy", "Grand Theft Auto V", "lacy")


# ---- clip window, layout, detection parsers ------------------------------------
def test_clip_window_trimmed_around_peak():
    rec = {"clip_start": 100.0, "clip_end": 190.0, "peak_time": 150.0}
    s, e = L.clip_window(rec, 59)
    assert e - s == pytest.approx(59) and s >= 100 and e <= 190 and s < 150 < e
    assert L.clip_window({"clip_start": 0.0, "clip_end": 40.0}, 59) == (0.0, 40.0)


def test_layout_is_one_third_facecam_two_thirds_gameplay():
    lay = editor.layout(1920, 1080, {"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.30})
    assert lay.cam_h == 640 and lay.game_h == 1280          # 1/3 facecam, 2/3 gameplay
    cw, ch, cx, cy = lay.cam
    assert cw == 480 and abs(cw / ch - 1080 / 640) < 0.02    # trimmed, never squashed
    assert cy > 0 and cy + ch <= 324                          # trimmed around the box centre
    gw, gh, gx, gy = lay.game
    assert gx + gw <= 1920 and gy + gh <= 1080 and gh == 1080
    assert abs(gw / gh - 1080 / lay.game_h) < 0.01
    # a tall box and a wide box both end up exactly a third of the screen
    for box in ({"x": 0.8, "y": 0.0, "w": 0.2, "h": 0.6}, {"x": 0.5, "y": 0.7, "w": 0.5, "h": 0.2}):
        lay = editor.layout(1920, 1080, box)
        cw, ch, cx, cy = lay.cam
        assert lay.cam_h == 640 and abs(cw / ch - 1080 / 640) < 0.02
        assert cx + cw <= 1920 and cy + ch <= 1080


def test_freeze_and_black_parsers():
    err = ("[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 2.5\n"
           "[freezedetect @ 0x1] lavfi.freezedetect.freeze_end: 6.5\n"
           "[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 30\n")
    assert editor.frozen_seconds(err, 40) == pytest.approx(14.0)
    assert editor.black_seconds("[blackdetect @ 0x2] black_start:1 black_end:4.5 black_duration:3.5") == 3.5


def test_captions_have_lacy_hook_highlight_and_no_watermark():
    words = [Word(0.0, 0.3, "he"), Word(0.35, 0.6, "stole"), Word(0.65, 1.0, "the plane."),
             Word(2.5, 2.8, "unbelievable")]
    ass = editor.build_ass(words, "Lacy can't believe this happened in GTA", 10.0, 600)
    assert "Hook,,0,0,0,,Lacy can't believe this happened in GTA" in ass
    assert "MarginV" in ass and ",636,1" in ass  # hook sits just under the 600px facecam
    assert "{\\c&H00FFFF&}HE{\\c&HFFFFFF&} STOLE THE PLANE." in ass
    assert "UNBELIEVABLE" in ass and ass.count("Cap,,") == 4
    assert "Mark,," not in ass  # no watermark/logo line
    assert "Mark,,0,0,0,,GTA 6 Clipped" in editor.build_ass(words, "Lacy x", 10, 600,
                                                            watermark="GTA 6 Clipped")
    groups = editor.group_words(words)
    assert [len(g) for g in groups] == [3, 1]


def test_captions_that_add_nothing_are_cut():
    react = [Word(0.0, 0.3, "no"), Word(0.35, 0.6, "way"), Word(0.65, 1.0, "bro."),
             Word(1.2, 1.5, "hahaha"), Word(2.5, 2.8, "what")]
    assert not editor.worth_captioning(react)
    assert "Cap,," not in editor.build_ass(react, "Lacy x", 10.0, 600)
    mixed = [Word(0.0, 0.3, "bro"), Word(0.4, 0.6, "haha"), Word(0.7, 0.9, "yeah"),
             Word(2.0, 2.3, "the"), Word(2.4, 2.7, "cops"), Word(2.8, 3.1, "followed"), Word(3.2, 3.5, "me")]
    ass = editor.build_ass(mixed, "Lacy x", 10.0, 600)
    assert "BRO" not in ass and "COPS" in ass   # the filler-only caption is dropped


def test_dhash_and_thumbnail_times():
    flat = bytes([10] * 72)
    ramp = bytes([200 - c * 20 for r in range(8) for c in range(9)])
    assert editor.dhash(flat) == 0
    assert editor.hamming(editor.dhash(flat), editor.dhash(ramp)) > 10
    ts = editor.thumbnail_times(40.0, 20.0)
    assert ts[0] == 20.0 and all(1.0 <= t <= 39.5 for t in ts) and len(set(ts)) == len(ts)


# ---- submissions CSV and daily cap ---------------------------------------------
class FakeYT:
    def __init__(self, thumb_error=None):
        self.uploads, self.thumb_error = [], thumb_error

    def upload(self, video, title, description, tags, privacy):
        assert video.exists()
        self.uploads.append((title, description, tags, privacy))
        return f"vid{len(self.uploads)}"

    def set_thumbnail(self, vid, jpg):
        if self.thumb_error:
            from spikebot.youtube import YouTubeError
            raise YouTubeError(self.thumb_error)


def ready_job(jobs, n, title=None):
    job = {"id": f"20261119-06000{n}", "status": "ready", "clip_start": T, "category": "Grand Theft Auto VI",
           "title": title or f"Lacy's best GTA 6 moment today (part {n})",
           "description": L.description("Lacy x", "Grand Theft Auto VI", "lacy"),
           "tags": L.tags("Grand Theft Auto VI"), "vod_link": "https://www.twitch.tv/videos/1?t=0h10m00s"}
    jobs.save(job)
    for f in ("short.mp4", "thumb.jpg", "preview.mp4"):
        (jobs.dir(job["id"]) / f).write_bytes(b"x")
    return job


def test_upload_logs_csv_and_respects_daily_cap(tmp_path):
    jobs, subs, yt = L.Jobs(tmp_path), L.Submissions(tmp_path / "logs" / "lacy_submissions.csv"), FakeYT()
    lcfg = L.settings({"lacy": {"max_uploads_per_day": 2}})
    for n in (1, 2):
        job = L.upload_job(ready_job(jobs, n), lcfg, jobs, subs, yt, now=T + n)
        assert job["status"] == "uploaded" and job["youtube_url"] == f"https://youtube.com/shorts/vid{n}"
        assert not (jobs.dir(job["id"]) / "short.mp4").exists()  # disk freed
    with pytest.raises(ValueError, match="already 2 Lacy uploads today"):
        L.upload_job(ready_job(jobs, 3), lcfg, jobs, subs, yt, now=T + 3)
    # next UK day is fine again
    L.upload_job(jobs.load("20261119-060003"), lcfg, jobs, subs, yt, now=T + 86400)
    rows = list(csv.DictReader((tmp_path / "logs" / "lacy_submissions.csv").open()))
    assert [r["youtube_url"] for r in rows] == [f"https://youtube.com/shorts/vid{n}" for n in (1, 2, 3)]
    assert rows[0]["uploaded_uk"].startswith("2026-11-19 06:00:01 GMT")
    assert rows[0]["title"] == "Lacy's best GTA 6 moment today (part 1)"
    assert list(rows[0])[:3] == ["uploaded_uk", "youtube_url", "title"]
    title, desc, tags, privacy = yt.uploads[0]
    assert "#Lacy" in desc and "#Shorts" in desc and "Lacy" in tags and privacy == "public"


def test_upload_refuses_bad_title_and_keeps_going_without_thumbnail(tmp_path):
    jobs, subs = L.Jobs(tmp_path), L.Submissions(tmp_path / "s.csv")
    lcfg = L.settings({})
    with pytest.raises(ValueError, match="title rules"):
        L.upload_job(ready_job(jobs, 1, title="Lacy gets exposed"), lcfg, jobs, subs, FakeYT())
    job = L.upload_job(ready_job(jobs, 2), lcfg, jobs, subs, FakeYT(thumb_error="HTTP 403"))
    assert job["status"] == "uploaded" and job["thumbnail_set"] is False
    assert "thumbnail not set" in job["reason"]


# ---- end-to-end render with ffmpeg ---------------------------------------------
def make_stream(path: Path, seconds: int = 12, still: bool = False) -> None:
    """Fake 1920x1080 stream: moving test pattern with webcam-like noise in the
    facecam corner (or one frozen frame = BRB screen)."""
    src = "testsrc2=size=1920x1080:rate=30"
    vf = (["-vf", "trim=end_frame=1,loop=loop=-1:size=1,setpts=N/30/TB"] if still else
          ["-filter_complex", "[0:v]split[a][b];[b]crop=480:324:0:0,noise=alls=40:allf=t[c];"
                              "[a][c]overlay=0:0"])
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src,
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    *vf, "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(path)], check=True)


def job_for(tmp_path, jobs, n=1, category="Grand Theft Auto VI", channel="lacy"):
    job = {"id": f"20261119-0700{n:02d}", "status": "rendering", "channel": channel,
           "programme": "Lacy (Reach pool)", "score": 7.0, "onset": T, "peak_time": T + 5,
           "vibe": ["LMAO"], "category": category, "clip_start": T, "clip_end": T + 12,
           "vod_url": "https://www.twitch.tv/videos/123", "vod_started_at": "2026-11-19T05:00:00Z"}
    jobs.save(job)
    return job


def fake_fetch(src):
    def fetch(url, start, dur, out):
        assert url == "https://www.twitch.tv/videos/123" and start == pytest.approx(3600)
        shutil.copy(src, out)
        return out
    return fetch


def fake_words(lang="en", prob=0.97):
    def tr(path, model):
        return [Word(0.5 + i * 0.4, 0.8 + i * 0.4, w) for i, w in
                enumerate("no way he actually did that".split())], lang, prob
    return tr


@needs_ffmpeg
def test_render_job_makes_vertical_short_with_unique_thumbnails(tmp_path):
    src = tmp_path / "stream.mp4"
    make_stream(src)
    jobs, lcfg = L.Jobs(tmp_path), L.settings({})
    job = L.render_job(job_for(tmp_path, jobs, 1), lcfg, jobs, set(),
                       fetch=fake_fetch(src), transcribe=fake_words(), now=T)
    assert job["status"] == "ready", job["reason"]
    d = jobs.dir(job["id"])
    info = editor.probe(d / "short.mp4")
    assert (info["width"], info["height"]) == (1080, 1920)
    assert (d / "preview.mp4").stat().st_size < 9_500_000
    assert (d / "thumb.jpg").exists() and not (d / "segment.mp4").exists()
    assert "Lacy" in job["title"] and "#Lacy" in job["description"] and "#Shorts" in job["description"]
    assert "Mark,," not in (d / "captions.ass").read_text()  # no logo in Lacy mode
    assert job["vod_link"] == "https://www.twitch.tv/videos/123?t=1h00m00s"
    # a second clip of the same footage must not reuse a thumbnail
    job2 = L.render_job(job_for(tmp_path, jobs, 2), lcfg, jobs, {job["title"]},
                        fetch=fake_fetch(src), transcribe=fake_words(), now=T)
    if job2["status"] == "ready":
        h1, h2 = int(job["thumb_hash"], 16), int(job2["thumb_hash"], 16)
        assert editor.hamming(h1, h2) >= lcfg["thumb_min_distance"]
        assert job2["title"] != job["title"]
    else:
        assert "earlier thumbnail" in job2["reason"]
    assert len(jobs.used_hashes()) == (2 if job2["status"] == "ready" else 1)


@needs_ffmpeg
def test_render_job_skips_brb_screen(tmp_path):
    src = tmp_path / "brb.mp4"
    make_stream(src, still=True)
    jobs = L.Jobs(tmp_path)
    job = L.render_job(job_for(tmp_path, jobs), L.settings({}), jobs, set(),
                       fetch=fake_fetch(src), transcribe=fake_words(), now=T)
    assert job["status"] == "skipped" and "BRB/away" in job["reason"]
    assert not (jobs.dir(job["id"]) / "short.mp4").exists()


@needs_ffmpeg
def test_render_job_skips_non_english(tmp_path):
    src = tmp_path / "stream.mp4"
    make_stream(src, seconds=6)
    jobs = L.Jobs(tmp_path)
    job = L.render_job(job_for(tmp_path, jobs), L.settings({}), jobs, set(),
                       fetch=fake_fetch(src), transcribe=fake_words("es", 0.9), now=T)
    assert job["status"] == "skipped" and "not English" in job["reason"]


def test_render_job_guards(tmp_path):
    jobs = L.Jobs(tmp_path)

    def boom(*a):
        raise AssertionError("must not download")
    for kw, why in ((dict(category="Just Chatting"), "not GTA"),
                    (dict(channel="kaicenat"), "not the Lacy programme")):
        job = L.render_job(job_for(tmp_path, jobs, **kw), L.settings({}), jobs, set(),
                           fetch=boom, transcribe=boom, now=T)
        assert job["status"] == "skipped" and why in job["reason"]


# ---- Bot wiring: category check at spike time ----------------------------------
def make_bot(tmp_path, monkeypatch, category, live=True):
    import spikebot.main as m
    posts = []

    async def fake_post(url, payload):
        posts.append((url, payload))
    monkeypatch.setattr(m, "post_discord", fake_post)
    cfg = {"channels": [{"platform": "twitch", "name": "lacy", "programme": "Lacy (Reach pool)"},
                        {"platform": "kick", "name": "adinross", "programme": "Adin Ross"}],
           "require_category_contains": ["Grand Theft Auto"], "min_score_to_alert": 3,
           "data_dir": str(tmp_path), "discord_webhook_url": "https://moments.invalid",
           "twitch_client_id": "id", "twitch_client_secret": "secret", "lacy_mode": "true"}
    bot = m.Bot(cfg)
    calls = []

    class FakeHelix:
        async def stream_state(self, login):
            calls.append(login)
            return {"live": live, "category": category, "started_at": "2026-11-19T05:00:00Z",
                    "title": "GTA 6 day 1", "stream_id": "s1", "user_id": "u1"}

        async def stream_vod(self, user_id, stream_id):
            return None
    bot.helix = FakeHelix()
    return bot, posts, calls


def lacy_spike(score=7.0):
    from spikebot.detector import Spike
    s = datetime(2026, 11, 19, 6, 0, tzinfo=UK).timestamp()
    return Spike("twitch", "lacy", onset=s, peak_time=s + 8, end=s + 20, peak_rate=40.0,
                 baseline=5.0, zscore=8.0, messages=500, unique_users=300, hype_share=0.6,
                 clip_mentions=10, score=score, clip_start=s - 30, clip_end=s + 15,
                 sample=["LMAO"], vibe=["LMAO"])


@pytest.mark.parametrize("category,queued,status", [
    ("Grand Theft Auto VI", True, "candidate"),
    ("Grand Theft Auto V", True, "candidate"),
    ("Grand Theft Auto IV", False, "filtered_category"),
    ("Just Chatting", False, "filtered_category"),
])
def test_bot_checks_lacy_category_at_spike_time(tmp_path, monkeypatch, category, queued, status):
    bot, posts, calls = make_bot(tmp_path, monkeypatch, category)
    bot.state[("twitch", "lacy")] = {"live": True, "category": "Grand Theft Auto VI"}  # stale poll
    asyncio.run(bot.handle(("twitch", "lacy"), lacy_spike()))
    assert calls == ["lacy"]  # Helix Get Streams asked at the spike
    jobs = bot.lacy.jobs.all()
    assert bool(jobs) == queued
    assert bot.store.db.execute("select status from spikes").fetchone()[0] == status
    if queued:
        j = jobs[0]
        assert j["category"] == category and j["status"] == "queued"
        assert j["clip_end"] - j["clip_start"] <= 59 and j["ready_at"] > j["clip_end"]


def test_bot_lacy_needs_polled_category_to_be_gta_too(tmp_path, monkeypatch):
    bot, _, _ = make_bot(tmp_path, monkeypatch, "Grand Theft Auto VI")
    bot.state[("twitch", "lacy")] = {"live": True, "category": "Slots & Casino"}
    asyncio.run(bot.handle(("twitch", "lacy"), lacy_spike()))
    assert bot.lacy.jobs.all() == []


def test_bot_lacy_low_score_and_other_channels_untouched(tmp_path, monkeypatch):
    bot, posts, calls = make_bot(tmp_path, monkeypatch, "Grand Theft Auto VI")
    asyncio.run(bot.handle(("twitch", "lacy"), lacy_spike(score=1.0)))
    assert bot.lacy.jobs.all() == []
    from spikebot.detector import Spike
    k = Spike("kick", "adinross", onset=T, peak_time=T, end=T, peak_rate=9.0, baseline=1.0,
              zscore=5.0, messages=99, unique_users=50, hype_share=0.5, clip_mentions=1,
              score=6.0, clip_start=T - 30, clip_end=T + 15)
    bot.state[("kick", "adinross")] = {"live": True, "category": "Grand Theft Auto VI"}
    asyncio.run(bot.handle(("kick", "adinross"), k))
    assert calls == ["lacy"]  # no Helix call for Kick, no Lacy job
    assert bot.lacy.jobs.all() == []
    assert any("adinross" in str(p) for _, p in posts)  # normal #moments alert still sent


def test_lacy_mode_off_keeps_old_behaviour(tmp_path, monkeypatch):
    import spikebot.main as m
    bot = m.Bot({"channels": [{"platform": "twitch", "name": "lacy", "programme": "Lacy"}],
                 "data_dir": str(tmp_path)})
    assert bot.lacy is None


def test_review_card_has_approve_command_and_checklist():
    from spikebot.messages import lacy_review, lacy_uploaded
    job = {"id": "20261119-060000", "title": "Wait for Lacy's reaction", "category": "Grand Theft Auto VI",
           "duration": 45.0, "score": 7.0, "onset": T, "vod_link": "https://www.twitch.tv/videos/1?t=1h"}
    d = lacy_review(job, 1, 3)["embeds"][0]["description"]
    assert "spikebot.lacy approve 20261119-060000" in d and "1/3" in d and "spoiler" in d
    up = lacy_uploaded(job | {"youtube_url": "https://youtube.com/shorts/abc"})
    assert up["embeds"][0]["url"] == "https://youtube.com/shorts/abc"


@pytest.mark.parametrize("outcome,expect", [("review", None), ("failed", "status"), ("skipped", None)])
def test_worker_routes_results(tmp_path, monkeypatch, outcome, expect):
    # LACY_AUTO_UPLOAD is ignored now: the worker only renders; approval happens in Discord
    lm = L.LacyMode({"lacy": {"enabled": True, "auto_upload": True}}, tmp_path)
    assert lm.cfg["auto_upload"] is False
    job = L.new_job({"onset": T, "score": 7.0, "clip_start": T - 30, "clip_end": T + 15,
                     "programme": "Lacy"}, {"category": "Grand Theft Auto VI"}, lm.cfg)
    job["ready_at"] = 0
    lm.jobs.save(job)
    ran = []

    async def fake_run(*args, timeout=3600):
        ran.append(args[0])
        j = lm.jobs.load(job["id"])
        j["status"] = outcome
        lm.jobs.save(j)
        return 0, ""
    monkeypatch.setattr(lm, "_run", fake_run)
    got = []

    async def status(j):
        got.append(("status", j["status"]))

    asyncio.run(lm._process(job["id"], None, status))
    assert [k for k, _ in got] == ([expect] if expect else [])
    assert ran == ["render"]
