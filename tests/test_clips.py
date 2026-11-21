"""Stream clips for every programme channel (Adin Ross, Seal...), not just Lacy:
per-streamer profiles, names in titles/captions, Kick category + VOD handling,
waiting for Kick VODs, and the caption rules (clips always captioned, trailer edits never).
Run: python -m pytest -q tests/test_clips.py"""
import asyncio
import random
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from spikebot import editor, instagram as IG, lacy as L, review as R, style as S
from spikebot.editor import Word

UK = L.UK
T = datetime(2026, 11, 19, 6, 0, tzinfo=UK).timestamp()
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
CHANNELS = [{"platform": "kick", "name": "adinross", "programme": "Adin Ross clipping programme"},
            {"platform": "kick", "name": "sealstats", "programme": "Seal (Content Rewards)"},
            {"platform": "twitch", "name": "lacy", "programme": "Lacy (Reach pool)"},
            {"platform": "twitch", "name": "kaicenat", "programme": None}]


def test_profiles_cover_programme_channels_only():
    p = L.profiles({"channels": CHANNELS, "lacy_mode": "true", "min_score_to_alert": 3})
    assert set(p) == {("kick", "adinross"), ("kick", "sealstats"), ("twitch", "lacy"), ("twitch", "kaicenat")}
    assert p[("twitch", "kaicenat")]["name"] == "Kai Cenat"          # no programme, cleared by Tim (clips.allow)
    assert ("twitch", "kaicenat") not in L.profiles({"channels": CHANNELS, "clips": {"allow": []}})   # not cleared
    adin = p[("kick", "adinross")]
    assert adin["name"] == "Adin Ross" and adin["hashtag"] == "AdinRoss" and adin["platform"] == "kick"
    assert adin["submissions_csv"] == "logs/adinross_submissions.csv" and adin["min_score"] == 3
    assert L.is_gta("GTA V", adin["allowed_categories"]) and L.is_gta("Grand Theft Auto VI", adin["allowed_categories"])
    assert not L.is_gta("Slots & Casino", adin["allowed_categories"])
    assert p[("kick", "sealstats")]["name"] == "Seal"
    assert p[("twitch", "lacy")]["submissions_csv"] == "logs/lacy_submissions.csv"
    # switches: CLIPS_MODE off keeps only Lacy; Lacy off keeps the others
    assert set(L.profiles({"channels": CHANNELS, "lacy_mode": "true", "clips_mode": "false"})) == {("twitch", "lacy")}
    assert ("twitch", "lacy") not in L.profiles({"channels": CHANNELS})
    # per-streamer overrides and the config display name
    chans = [dict(CHANNELS[0], display="ADIN")]
    q = L.profiles({"channels": chans, "clips": {"max_uploads_per_day": 5,
                                                 "streamers": {"adinross": {"facecam": {"x": 0.75}}}}})
    a = q[("kick", "adinross")]
    assert a["name"] == "ADIN" and a["max_uploads_per_day"] == 5
    assert a["facecam"] == {"x": 0.75, "y": 0.0, "w": 0.25, "h": 0.30}
    # a job of a channel that lost its programme has no profile
    with pytest.raises(ValueError):
        L.profile_for({"channels": CHANNELS, "clips": {"allow": []}}, {"platform": "twitch", "channel": "kaicenat"})
    assert L.profile_for({"channels": CHANNELS}, {"channel": "lacy"})["name"] == "Lacy"   # old Lacy job


def test_titles_descriptions_and_captions_name_the_streamer():
    t = L.make_title(["LMAO"], "Grand Theft Auto VI", set(), random.Random(1), name="Adin Ross")
    assert "Adin Ross" in t and "Lacy" not in t and L.title_ok(t, "Adin Ross") and not L.title_ok(t)
    d = L.description(t, "Grand Theft Auto VI", "adinross", "Adin Ross", "kick")
    assert "https://kick.com/adinross" in d and "#AdinRoss" in d and "live on Kick" in d and "Lacy" not in d
    assert L.tags("Grand Theft Auto VI", "Adin Ross", "kick")[:3] == ["Adin Ross", "Adin Ross GTA 6", "Adin Ross GTA"]
    assert "GTA 6 gameplay" in L.tags("Grand Theft Auto VI", "Adin Ross", "kick")
    job = {"id": "adinross-20261119-060000", "streamer": "Adin Ross", "platform": "kick", "channel": "adinross",
           "category": "Grand Theft Auto VI", "vibe": ["LMAO"], "title": t}
    hook = IG.pick_hook(job, set())
    assert "Adin Ross" in hook and "{name}" not in hook
    caps = IG.captions(job, hook)
    assert "kick.com/adinross" in caps["instagram"] and "#AdinRoss" in caps["tiktok"] and "Lacy" not in caps["tiktok"]
    assert L.crosspost_caption(job).endswith("#AdinRoss #GTA6 #GTA")
    # old Lacy jobs (no streamer/platform) read exactly as before
    old = {"id": "20261119-060000", "channel": "lacy", "category": "Grand Theft Auto VI", "vibe": []}
    assert "#Lacy" in IG.captions(old, "Lacy did not see this coming")["tiktok"]
    assert "twitch.tv/lacy" in IG.captions(old, "x")["instagram"]


def test_new_job_ids_and_render_guards(tmp_path):
    prof = L.profiles({"channels": CHANNELS})[("kick", "adinross")]
    rec = {"onset": T, "clip_start": T - 30, "clip_end": T + 15, "score": 6.0, "programme": "Adin Ross clipping programme",
           "peak_time": T + 5, "vibe": ["W"]}
    job = L.new_job(rec, {"category": "Grand Theft Auto VI", "stream_id": 7}, prof)
    assert job["id"] == "adinross-20261119-060000" and job["platform"] == "kick" and job["streamer"] == "Adin Ross"
    jobs = L.Jobs(tmp_path)

    def boom(*a):
        raise AssertionError("must not download")
    # a job file pointing at another channel is refused whatever it says
    bad = dict(job, id="x1", channel="kaicenat", vod_url="https://kick.com/x", vod_started_at="2026-11-19T05:00:00Z")
    out = L.render_job(bad, prof, jobs, set(), fetch=boom, transcribe=boom, now=T)
    assert out["status"] == "skipped" and "not a clipping-programme channel" in out["reason"]
    nov = dict(job, id="x2")
    out = L.render_job(nov, prof, jobs, set(), fetch=boom, transcribe=boom, now=T)
    assert out["status"] == "failed" and "no Kick VOD" in out["reason"]


def test_kick_spike_queues_a_clip_after_a_fresh_kick_check(tmp_path, monkeypatch):
    import spikebot.main as m
    from spikebot.detector import Spike

    async def fake_post(url, payload):
        pass

    async def fake_info(slug):
        assert slug == "adinross"
        return {"livestream": {"is_live": True, "id": 77, "start_time": "2026-11-19 05:00:00",
                               "categories": [{"name": category}], "session_title": "GTA 6 DAY 1"}}

    async def no_vod(slug, stream_id):
        return None
    monkeypatch.setattr(m, "post_discord", fake_post)
    monkeypatch.setattr(m, "kick_channel_info", fake_info)
    monkeypatch.setattr(m, "kick_stream_vod", no_vod)
    cfg = {"channels": CHANNELS[:3], "require_category_contains": ["Grand Theft Auto"], "min_score_to_alert": 3,
           "data_dir": str(tmp_path)}
    spike = Spike("kick", "adinross", onset=T, peak_time=T + 5, end=T + 10, peak_rate=40.0, baseline=4.0,
                  zscore=8.0, messages=400, unique_users=250, hype_share=0.6, clip_mentions=5, score=7.0,
                  clip_start=T - 30, clip_end=T + 15, vibe=["LMAO"])
    for category, queued in (("Grand Theft Auto VI", True), ("Slots & Casino", False)):
        bot = m.Bot(dict(cfg, data_dir=str(tmp_path / category[:4])))
        assert bot.lacy and bot.lacy.applies(("kick", "adinross")) and not bot.lacy.applies(("twitch", "lacy"))
        asyncio.run(bot.handle(("kick", "adinross"), spike))
        jobs = bot.lacy.jobs.all()
        assert bool(jobs) == queued
        if queued:
            j = jobs[0]
            assert j["platform"] == "kick" and j["channel"] == "adinross" and j["stream_id"] == 77
            assert j["id"].startswith("adinross-") and j["programme"] == "Adin Ross clipping programme"


def test_missing_kick_vod_waits_instead_of_failing(tmp_path):
    mode = L.LacyMode({"channels": CHANNELS[:1]}, tmp_path)
    job = {"id": "adinross-20261119-060000", "status": "queued", "channel": "adinross", "platform": "kick",
           "ready_at": T, "created": T}
    mode.jobs.save(job)
    seen = []

    async def go():
        mode.queue = asyncio.Queue()

        async def no_vod(job, helix):
            return None
        mode.find_vod = no_vod
        mode._later = lambda job_id, delay: asyncio.sleep(0, seen.append((job_id, delay)))
        await mode._process(job["id"], None, None)
        await asyncio.sleep(0)
    orig = L.time.time
    L.time.time = lambda: T + 60
    try:
        asyncio.run(go())
    finally:
        L.time.time = orig
    assert mode.jobs.load(job["id"])["status"] == "waiting_vod"
    assert seen == [(job["id"], L.VOD_RETRY_S)]


def test_renders_that_hit_the_pyav_crash_are_retried_once():
    crashed = {"status": "failed", "created": T, "reason": "render crashed: ... unexpected keyword argument 'metadata_errors'"}
    assert L.retry_after_fix(crashed, now=T + 3600)
    assert not L.retry_after_fix(crashed | {"retried": T}, now=T + 3600)
    assert not L.retry_after_fix(crashed, now=T + 3 * 86400)
    assert not L.retry_after_fix(crashed | {"reason": "no Twitch VOD"}, now=T + 60)


# ---- caption rules ---------------------------------------------------------------------
def test_trailer_edits_never_get_text_and_clips_always_get_captions(tmp_path):
    assert S.enforce({"text_level": "full"}, "trend") == {"text_level": "none"}
    assert S.enforce({"text_level": "none"}, "lacy") == {"text_level": "full"}
    # a pin or a feedback asking for the opposite can't break it
    store = R.Store(tmp_path)
    prof = store.profile()
    prof.pin("text_level", "full", "tokyo_girl")
    prof.save()
    item = {"id": "tg1", "mode": "tokyo_girl", "source": {}, "rounds": []}
    specs, _ = R.specs_for(item, {"kind": "initial"}, store, random.Random(1))
    assert {sp["style"]["text_level"] for sp in specs} == {"none"}
    clip = {"id": "c1", "mode": "lacy", "source": {}, "rounds": []}
    specs, _ = R.specs_for(clip, {"kind": "initial"}, store, random.Random(1))
    assert all(sp["style"]["text_level"] in ("full", "minimal") for sp in specs)


# ---- end to end: an Adin Ross clip becomes three captioned variants ----------------------
@needs_ffmpeg
def test_adin_ross_job_becomes_three_captioned_variants(tmp_path, monkeypatch):
    from tests.test_lacy import make_stream
    from spikebot import ai
    asked = []

    def fake_copy(name, game, transcript, vibe=None, n=3, base=None, client=None, pronouns="he/him"):
        asked.append(transcript)
        return [{"title": "Adin Ross hits the jump of his life in GTA 6", "caption": "adin ross actually landed it 😭"},
                {"title": "Adin Ross can't believe chat saw this", "caption": "this is so cringe adin ross"},  # banned word
                {"title": "No name in this title at all", "caption": "adin ross and chat"}]               # no name
    monkeypatch.setattr(ai, "available", lambda base=None: True)
    monkeypatch.setattr(ai, "write_clip_copy", fake_copy)
    src = tmp_path / "stream.mp4"
    make_stream(src, seconds=12)
    cfg = {"channels": CHANNELS[:1], "data_dir": str(tmp_path)}
    prof = L.profiles(cfg)[("kick", "adinross")]
    jobs, store = L.Jobs(tmp_path), R.Store(tmp_path)
    job = {"id": "adinross-20261119-060000", "status": "rendering", "channel": "adinross", "platform": "kick",
           "streamer": "Adin Ross", "programme": "Adin Ross clipping programme", "score": 7.0, "onset": T,
           "peak_time": T + 5, "vibe": ["LMAO"], "category": "Grand Theft Auto VI", "clip_start": T,
           "clip_end": T + 12, "vod_url": "https://kick.com/adinross/videos/abc", "vod_started_at": "2026-11-19 05:00:00"}
    jobs.save(job)

    def fetch(url, start, dur, out):
        assert url == "https://kick.com/adinross/videos/abc" and start == pytest.approx(3600)
        shutil.copy(src, out)
        return out

    def tr(path, model):
        return [Word(0.4 + i * 0.5, 0.8 + i * 0.5, w) for i, w in
                enumerate("bro no way he actually hit that jump chat look at this".split())], "en", 0.98
    job = L.render_job(job, prof, jobs, set(), fetch=fetch, transcribe=tr, now=T, review_store=store,
                       cfg=cfg, base=tmp_path)
    assert job["status"] == "review", job.get("reason")
    assert job["vod_link"] == "https://kick.com/adinross/videos/abc?t=3600"
    item = store.load(job["review_item"])
    assert item["source"]["streamer"] == "Adin Ross" and item["source"]["platform"] == "kick"
    vs = item["rounds"][0]["variants"]
    assert len(vs) == 3
    assert len(asked) == 1 and "jump" in asked[0]                  # written from the transcript, once
    assert vs[0]["title"] == "Adin Ross hits the jump of his life in GTA 6"
    assert vs[0]["hook"] == "adin ross actually landed it 😭"
    assert "cringe" not in vs[1]["hook"] and "Adin Ross" in vs[2]["title"]   # bad ones fall back to templates
    for v in vs:
        assert L.title_ok(v["title"], "Adin Ross") and "Lacy" not in v["title"]
        assert "#AdinRoss" in v["description"] and "Adin Ross" in v["captions"]["instagram"]
        assert v["style"]["text_level"] in ("full", "minimal")
        info = editor.probe(store.dir(item["id"]) / v["file"])
        # no webcam found in the synthetic stream: no fake facecam panel, the whole frame in 4:5
        assert (info["width"], info["height"]) == (1080, 1350) and v["frame"] == "4:5"
    from spikebot.discord_bot import mode_label
    assert mode_label(item)[0] == "Adin Ross"
    # re-renders from the review queue find the streamer's profile on their own
    with store.edit(item["id"]) as it:
        R.request(it, "feedback", round=1, label="A", notes="hook: Adin Ross did not see this coming")
    item = R.render_round(store, item["id"], cfg, tmp_path, now=T + 60, adapter=R.adapter_for("lacy", cfg, tmp_path))
    assert item["rounds"][1]["variants"][0]["title"] == "Adin Ross did not see this coming"


def test_failed_kick_lookup_still_alerts_but_makes_no_clip(tmp_path, monkeypatch):
    import spikebot.main as m
    from spikebot.detector import Spike
    posts = []

    async def fake_post(url, payload):
        posts.append(url)

    async def down(slug):
        raise RuntimeError("Cloudflare said no")

    async def no_vod(slug, stream_id):
        return None
    monkeypatch.setattr(m, "post_discord", fake_post)
    monkeypatch.setattr(m, "kick_channel_info", down)
    monkeypatch.setattr(m, "kick_stream_vod", no_vod)
    bot = m.Bot({"channels": CHANNELS[:1], "require_category_contains": ["Grand Theft Auto"],
                 "data_dir": str(tmp_path), "discord_webhook_url": "https://moments.invalid"})
    bot.state[("kick", "adinross")] = {"live": True, "category": "Grand Theft Auto VI"}
    spike = Spike("kick", "adinross", onset=T, peak_time=T + 5, end=T + 10, peak_rate=40.0, baseline=4.0,
                  zscore=8.0, messages=400, unique_users=250, hype_share=0.6, clip_mentions=5, score=7.0,
                  clip_start=T - 30, clip_end=T + 15)
    asyncio.run(bot.handle(("kick", "adinross"), spike))
    assert posts == ["https://moments.invalid"] and bot.lacy.jobs.all() == []


def test_bigger_sample_around_the_spike():
    prof = L.profiles({"channels": CHANNELS})[("kick", "adinross")]
    rec = {"onset": T, "clip_start": T - 30, "clip_end": T + 15, "peak_time": T + 5, "score": 6.0,
           "programme": "x", "vibe": []}
    a, b = L.sample_window(rec, prof)
    assert (a, b) == (T - 60, T + 45)                         # 30 s more each side
    long = dict(rec, clip_start=T - 200, clip_end=T + 200)
    a, b = L.sample_window(long, prof)
    assert b - a == prof["sample_s"] and a < T + 5 < b       # capped, around the peak


def test_facecam_is_the_face_that_stays_put():
    from spikebot import safety as SF
    size = (1920, 1080)
    cam = {"class": "FACE_MALE", "score": 0.9, "box": [1700, 60, 100, 100]}       # webcam top right
    frames = [([cam] + ([{"class": "FACE_FEMALE", "score": 0.8, "box": [200 + 90 * i, 500, 60, 60]}] if i % 2 else []),
               size) for i in range(8)]                                          # a game character moving
    box = SF.facecam_from_faces(frames)
    assert box and 0.8 < box["x"] < 0.95 and box["y"] < 0.1 and box["x"] + box["w"] <= 1.0
    assert SF.facecam_from_faces([([], size)] * 8) is None
    # the auto box becomes the layout of that clip
    lay = editor.layout(1920, 1080, box)
    assert lay.cam[2] > 1400
    prof = L.profiles({"channels": CHANNELS})[("kick", "adinross")]
    assert prof["facecam_auto"] is True
    fixed = L.profiles({"channels": CHANNELS, "clips": {"streamers": {"adinross": {"facecam": {"x": 0.7}}}}})
    assert fixed[("kick", "adinross")]["facecam_auto"] is False
