"""Gameplay highlights from the stream audio (no chat spike needed)."""
import asyncio
from datetime import datetime

import numpy as np

from spikebot import highlights as H
from spikebot import lacy as L

UK = L.UK
T = datetime(2026, 11, 19, 6, 0, tzinfo=UK).timestamp()


def loud_audio(seconds=600, shouts=(120.0, 400.0), rate=H.RATE):
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.02, int(seconds * rate)).astype(np.float32)   # talking / game noise
    for s in shouts:
        a, b = int(s * rate), int((s + 3) * rate)
        x[a:b] = rng.normal(0, 0.5, b - a)                              # shouting for 3 s
    return x


def test_peaks_find_the_loud_moments_only():
    env = H.envelope(loud_audio())
    assert len(env) == 1200
    found = H.peaks(env)
    times = sorted(t for t, _ in found)
    assert len(times) == 2 and abs(times[0] - 121) < 3 and abs(times[1] - 401) < 3
    assert H.peaks(H.envelope(np.random.default_rng(2).normal(0, 0.02, 600 * H.RATE).astype(np.float32))) == []


def test_scan_plan_and_pick():
    assert H.plan_scan(0, 100, 1800, 180) is None
    assert H.plan_scan(0, 4000, 1800, 180) == (0, 1800)
    assert H.plan_scan(1800, 4000, 1800, 180) == (1800, 1800)
    assert H.plan_scan(3600, 4000, 1800, 180) == (3600, 220)
    found = [(10.0, 9.0), (500.0, 7.0), (900.0, 5.0)]
    assert H.pick(found, 1000.0, chat_times=[1505.0], hours=0.5, max_per_hour=2, min_gap_s=120) == [(1010.0, 9.0)]


def test_bot_turns_loud_moments_into_clip_jobs(tmp_path, monkeypatch):
    import spikebot.main as m
    cfg = {"channels": [{"platform": "twitch", "name": "kaicenat", "programme": None}], "data_dir": str(tmp_path)}
    bot = m.Bot(cfg)
    key = ("twitch", "kaicenat")
    start = T - 3600
    bot.state[key] = {"live": True, "stream_id": "s1", "user_id": "u1", "category": "Grand Theft Auto VI"}

    async def vod(job, helix):
        return {"url": "https://www.twitch.tv/videos/9", "started_at": "2026-11-19T05:00:00Z"}
    bot.lacy.find_vod = vod
    reads = []

    def read(url, a, dur):
        reads.append((a, dur))
        return loud_audio(int(dur))
    hcfg = H.settings({})
    state = H.State(tmp_path)
    made = asyncio.run(bot.highlights_tick(hcfg, state, T, read_audio=read))
    assert reads == [(2700.0, 720.0)] and len(made) == 1          # from 15 min before we saw it
    job = bot.lacy.jobs.load(made[0])
    assert job["source"] == "audio" and job["channel"] == "kaicenat" and job["vod_url"].endswith("/9")
    assert job["category"] == "Grand Theft Auto VI" and job["id"].endswith("-a")
    # not GTA at that moment: no job
    bot.state[key]["category"] = "Slots & Casino"
    d = state.load()
    for e in d.values():
        e["cats"] = [[T, False, "Slots & Casino"]]
    state.save(d)
    assert asyncio.run(bot.highlights_tick(hcfg, state, T + 1800, read_audio=read)) == []
