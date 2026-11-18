"""Tests for status alerts and the daily summary. Run: python -m pytest -q"""
import asyncio
from datetime import datetime, timezone

import websockets.exceptions as wse
from websockets.frames import Close

from spikebot.messages import plain
from spikebot.messages import summary as build_summary
from spikebot.sources import _close_code, _pusher_error_code
from spikebot.status import (UK, ConnectionWatch, LiveWatch, Notifier, StatusState,
                             duration, next_summary_time, summary_due_on_start)

K1, K2 = ("kick", "adinross"), ("twitch", "lacy")


def uk_ts(*args) -> float:
    return datetime(*args, tzinfo=UK).timestamp()


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_notifier(tmp_path, clock, url="https://example.invalid/hook"):
    sent = []

    async def poster(u, text):
        sent.append((u, text))
    return Notifier(url, StatusState(tmp_path / "s.json"), poster, clock=clock), sent


# ---- notifier: 30 minute de-duplication ------------------------------------
def test_same_alert_not_repeated_within_30_minutes(tmp_path):
    clock = Clock()
    n, sent = make_notifier(tmp_path, clock)
    assert asyncio.run(n.send("live:kick/adinross", "a"))
    clock.t += 29 * 60
    assert not asyncio.run(n.send("live:kick/adinross", "a"))
    assert asyncio.run(n.send("live:twitch/lacy", "b"))  # different alert still goes out
    clock.t += 61
    assert asyncio.run(n.send("live:kick/adinross", "a"))
    assert [p["embeds"][0]["description"] for _, p in sent] == ["a", "b", "a"]
    assert sent[0][1]["embeds"][0]["color"] == 0x2ECC71  # "live" is green


def test_dedup_survives_restart(tmp_path):
    clock = Clock()
    n, sent = make_notifier(tmp_path, clock)
    asyncio.run(n.send("start", "started"))
    clock.t += 10  # systemd restarts a crashing bot after 10s
    n2, sent2 = make_notifier(tmp_path, clock)
    assert not asyncio.run(n2.send("start", "started"))
    assert sent2 == []


def test_no_url_logs_only(tmp_path):
    n, sent = make_notifier(tmp_path, Clock(), url=None)
    assert asyncio.run(n.send("disk", "low"))
    assert sent == []


def test_corrupt_state_file_starts_fresh(tmp_path):
    (tmp_path / "s.json").write_text("{not json")
    assert StatusState(tmp_path / "s.json").data["sent"] == {}


# ---- connection watch --------------------------------------------------------
def test_disconnect_alert_after_5_minutes_then_reconnect():
    w = ConnectionWatch([K1, K2], now=0)
    w.connected(K1, 1)
    w.connected(K2, 1)
    w.disconnected(K1, 100)
    w.disconnected(K1, 150)  # retries don't reset the outage start
    assert w.due(100 + 299) == []
    assert w.due(100 + 300) == [(K1, 100)]
    assert w.due(100 + 900) == []  # announced once per outage
    assert w.connected(K1, 1000) == 900


def test_short_blip_is_silent():
    w = ConnectionWatch([K1], now=0)
    w.connected(K1, 1)
    w.disconnected(K1, 100)
    assert w.connected(K1, 160) is None  # back within 5 min: no reconnect alert either
    assert w.due(10_000) == []


def test_never_connected_since_startup_counts_as_down():
    w = ConnectionWatch([K1, K2], now=0)
    w.connected(K2, 5)
    assert w.due(301) == [(K1, 0)]


# ---- live / offline ----------------------------------------------------------
def test_live_and_offline_events_with_category():
    w = LiveWatch()
    assert w.update(K1, {"live": False}) is None  # offline at startup: say nothing
    assert w.update(K1, {"live": True, "category": "Grand Theft Auto VI"}) == ("live", "Grand Theft Auto VI")
    assert w.update(K1, {"live": True, "category": "Grand Theft Auto VI"}) is None
    assert w.update(K1, {"live": False, "category": ""}) == ("offline", "Grand Theft Auto VI")
    assert w.update(K1, {"live": False}) is None


def test_already_live_at_startup_is_announced():
    w = LiveWatch()
    assert w.update(K2, {"live": True, "category": ""}) == ("live", "unknown")


# ---- Kick 4001 detection -----------------------------------------------------
def test_pusher_error_code():
    assert _pusher_error_code({"code": 4001, "message": "App key not in cluster"}) == 4001
    assert _pusher_error_code('{"code":4001,"message":"x"}') == 4001
    assert _pusher_error_code({"code": 4200}) == 4200
    assert _pusher_error_code("garbage") is None
    assert _pusher_error_code(None) is None


def test_close_code_from_websocket_error():
    e = wse.ConnectionClosedError(Close(4001, "App key not in cluster"), None)
    assert _close_code(e) == 4001
    assert _close_code(RuntimeError("boom")) is None


# ---- daily summary -----------------------------------------------------------
def test_next_summary_time_is_22_uk_including_bst():
    assert next_summary_time(uk_ts(2026, 11, 19, 21, 59)) == uk_ts(2026, 11, 19, 22, 0)
    assert next_summary_time(uk_ts(2026, 11, 19, 22, 0)) == uk_ts(2026, 11, 20, 22, 0)
    # summer (BST): 22:00 UK is 21:00 UTC
    t = next_summary_time(uk_ts(2027, 7, 1, 12, 0))
    assert datetime.fromtimestamp(t, timezone.utc).hour == 21
    # across the October clock change the next day is 25 hours away
    assert next_summary_time(uk_ts(2027, 10, 30, 22, 30)) == uk_ts(2027, 10, 31, 22, 0)


def test_summary_catch_up_after_downtime():
    assert summary_due_on_start(uk_ts(2026, 11, 19, 23, 10), "2026-11-18")
    assert not summary_due_on_start(uk_ts(2026, 11, 19, 23, 10), "2026-11-19")
    assert not summary_due_on_start(uk_ts(2026, 11, 19, 21, 0), "2026-11-18")


def test_build_summary_counts_and_top3():
    t = uk_ts(2026, 11, 19, 20, 0)
    rows = [
        {"platform": "kick", "channel": "adinross", "score": 4.0, "onset": t, "stream_offset_start": "00:10:00"},
        {"platform": "kick", "channel": "adinross", "score": 9.5, "onset": t + 60, "stream_offset_start": "01:00:00",
         "peak_rate": 30.0, "baseline": 5.0, "vod_url": "https://kick.com/adinross/videos/abc?t=3600"},
        {"platform": "twitch", "channel": "lacy", "score": 6.1, "onset": t + 120, "stream_offset_start": None},
        {"platform": "kick", "channel": "adinross", "score": 3.2, "onset": t + 180, "stream_offset_start": "02:00:00"},
    ]
    p = build_summary(rows, [K1, K2, ("kick", "sealstats")], uk_ts(2026, 11, 19, 22, 0),
                      names={K1: "Adin Ross"})
    text = p["embeds"][0]["description"]
    assert p["embeds"][0]["title"] == "📊 Daily summary"
    assert "**4** moments sent to #moments" in text
    assert "• Adin Ross: 3" in text  # display name used when configured
    assert "• twitch/lacy: 1" in text
    assert "• kick/sealstats: 0" in text  # quiet streamers still listed
    top = text.split("Top 3 by score")[1]
    assert top.index("9.5") < top.index("6.1") < top.index("4.0")
    assert "3.2" not in top
    assert "6.0x" in top and "[▶ VOD](https://kick.com/adinross/videos/abc?t=3600)" in top
    assert "stream time 00:10:00" in top
    assert "score 9.5 · 6.0x · 20:01 GMT" in plain(p)  # Discord timestamps read as UK time in the log


def test_build_summary_empty_day():
    p = build_summary([], [K1], uk_ts(2026, 11, 19, 22, 0))
    assert "No moments today." in p["embeds"][0]["description"]


def test_duration():
    assert duration(42) == "42s"
    assert duration(6 * 60) == "6m"
    assert duration(2 * 3600 + 5 * 60) == "2h 5m"


# ---- wiring inside Bot -------------------------------------------------------
def test_bot_status_wiring(tmp_path, monkeypatch):
    import spikebot.main as m

    posts = []

    async def fake_post(url, text):
        posts.append((url, text))
    monkeypatch.setattr(m, "post_discord", fake_post)
    cfg = {"channels": [{"platform": "kick", "name": "adinross", "programme": "Adin Ross"},
                        {"platform": "twitch", "name": "lacy", "programme": None}],
           "data_dir": str(tmp_path), "discord_webhook_url": "https://moments.invalid",
           "status_webhook_url": "https://status.invalid",
           "summary_webhook_url": "https://summary.invalid"}

    async def scenario():
        bot = m.Bot(cfg)
        bot.notifier.poster = fake_post
        await bot.startup_alert()
        loop = asyncio.create_task(bot.status_loop())
        await asyncio.sleep(0.01)
        kick, twitch = bot.on_status(K1), bot.on_status(K2)
        kick("connected", "")
        twitch("connected", "")
        kick("disconnected", "boom")
        bot.conn.threshold_s = 0          # pretend 5 minutes have passed
        kick("key_error", "34bf7a0ff419a2a775b9")
        await asyncio.sleep(0.05)
        kick("connected", "")
        await asyncio.sleep(0.05)
        bot.state[K2] = {"live": True, "category": "Grand Theft Auto VI", "started_at": None}
        await bot.live_alert(K2, "live", "Grand Theft Auto VI")
        bot.store.save({"platform": "kick", "channel": "adinross", "programme": "Adin Ross",
                        "status": "candidate", "score": 7.5, "onset": __import__("time").time(),
                        "onset_uk": "now", "stream_offset_start": "00:01:00",
                        "stream_offset_end": "00:01:45", "clip_start": 0, "clip_end": 0,
                        "category": "GTA", "title": "t"})
        await bot.send_summary()
        loop.cancel()

    asyncio.run(scenario())
    status = [plain(t) for u, t in posts if u == "https://status.invalid"]
    summary = [plain(t) for u, t in posts if u == "https://summary.invalid"]
    assert not any(u == "https://moments.invalid" for u, _ in posts)  # never mixed into #moments
    assert "started" in status[0]
    assert any("error 4001" in t for t in status)
    assert any("disconnected for" in t and "adinross** (Kick)" in t for t in status)
    assert any("reconnected" in t for t in status)
    assert any("lacy** (Twitch)" in t and "LIVE" in t and "Grand Theft Auto VI" in t for t in status)
    assert all(isinstance(t, dict) and "embeds" in t for u, t in posts)
    assert len(summary) == 1 and "adinross: 1" in summary[0] and "7.5" in summary[0]
    assert (tmp_path / "status_state.json").exists()


def test_cpu_samples_and_daily_summary_line():
    from spikebot import status as st
    from spikebot import messages as M
    data: dict = {}
    t0 = 1_800_000_000.0
    for i in range(288):                                   # a day of 5-minute samples
        st.add_cpu_sample(data, t0 + i * 300, 1.2 if 100 <= i < 130 else 0.3)
    assert len(data["cpu"]) == 288
    st.add_cpu_sample(data, t0 + 30 * 3600, 0.5)            # older than 26 h are dropped
    assert data["cpu"][0][0] >= t0 + 4 * 3600
    rows = [[t0 + i * 300, 1.2 if 100 <= i < 130 else 0.3] for i in range(288)]
    cpu = st.cpu_summary(rows, t0 + 287 * 300)
    assert cpu["busy_s"] == 30 * 300 and cpu["upgrade"] and cpu["peak"] == 1.2
    line = M.cpu_line(cpu, 4)
    assert "(4 cores)" in line and "maxed out for 2h 30m" in line and "bigger VM" in line
    calm = st.cpu_summary([[t0, 0.2], [t0 + 300, 0.4]], t0 + 300)
    assert not calm["upgrade"] and "no upgrade needed" in M.cpu_line(calm)
    assert st.cpu_summary([], t0) is None
    payload = M.summary([], [("kick", "x")], t0 + 287 * 300, cpu=cpu, cores=4)
    assert "Server CPU" in payload["embeds"][0]["description"]
    assert "Server CPU" not in M.summary([], [("kick", "x")], t0)["embeds"][0]["description"]
