"""Tests for the Discord message layouts and the vibe words. Run: python -m pytest -q"""
import asyncio
from datetime import datetime

from spikebot.detector import vibe_word
from spikebot.messages import (GREY, ORANGE, RED, UK, escape, moment, plain, score_colour,
                               status, vod_offset)

T = datetime(2026, 11, 19, 5, 12, tzinfo=UK).timestamp()


def rec(**kw):
    r = {"platform": "kick", "channel": "adinross", "programme": "Adin Ross", "score": 7.2,
         "peak_rate": 24.0, "baseline": 3.0, "unique_users": 180, "clip_mentions": 12,
         "onset": T, "stream_offset_start": "01:02:03", "category": "Grand Theft Auto VI",
         "sample": ["NO WAY", "LMAO"], "vibe": ["NO WAY", "LMAO", "💀"], "vod_url": None}
    return r | kw


def test_vibe_word_merges_variants():
    assert vibe_word("LOOOOL") == vibe_word("lol") == "LOL"
    assert vibe_word("lmfaooo") == "LMAO"
    assert vibe_word("no  way") == vibe_word("NOOO WAY") == "NO WAY"
    assert vibe_word("monkas") == "monkaS"
    assert vibe_word("💀") == "💀"


def test_spike_carries_vibe():
    from spikebot.detector import DetectorConfig, SpikeDetector
    det = SpikeDetector("kick", "x", DetectorConfig(warmup_s=30))
    t = 1_800_000_000.0
    spike = None
    for s in range(200):
        n = 40 if 100 <= s < 110 else 1
        for i in range(n):
            det.add_message(t + s + i / n, f"u{s}_{i}", "LOOOL NO WAY" if n > 1 else "hi")
        spike = det.tick(t + s + 1) or spike
    assert spike and sorted(spike.vibe[:2]) == ["LOL", "NO WAY"]


def test_vod_offset_formats():
    assert vod_offset(3723, "twitch") == "?t=1h02m03s"
    assert vod_offset(3723, "kick") == "?t=3723"
    assert vod_offset(-5, "kick") == "?t=0"


def test_score_colour_bands():
    assert score_colour(2.9) == GREY
    assert score_colour(3) == ORANGE
    assert score_colour(5.1) == RED


def test_moment_with_vod():
    p = moment(rec(vod_url="https://kick.com/adinross/videos/u1?t=3723"), "Adin Ross")
    e = p["embeds"][0]
    assert e["title"] == "🔥 Adin Ross · 8.0x chat spike · score 7.2"
    assert e["url"] == "https://kick.com/adinross/videos/u1?t=3723"
    assert e["color"] == RED
    d = e["description"]
    assert "[▶ Watch moment](https://kick.com/adinross/videos/u1?t=3723)** · 01:02:03 into stream" in d
    assert "**Vibe** NO WAY · LMAO · 💀" in d
    assert "Adin Ross · Kick · Grand Theft Auto VI · <t:" in d
    assert p["allowed_mentions"] == {"parse": []}
    assert "Thu 19 Nov" not in d and "05:12 GMT" in plain(p)


def test_moment_without_vod_or_programme():
    p = moment(rec(vod_url=None, programme=None, vibe=[], score=3.4), "lacy")
    d = p["embeds"][0]["description"]
    assert "[▶ Open stream](https://kick.com/adinross)** · VOD not ready, moment at 01:02:03" in d
    assert "No programme: check permission" in d
    assert '**Vibe** "NO WAY" · "LMAO"' in d  # falls back to repeated messages
    assert p["embeds"][0]["color"] == ORANGE


def test_moment_escapes_chat_markdown():
    p = moment(rec(vibe=[], sample=["**bold** @everyone"]), "x")
    assert "\\*\\*bold\\*\\*" in p["embeds"][0]["description"]
    assert escape("a_b") == "a\\_b"


def test_status_colour_and_plain_text():
    p = status("disconnected", f"down since <t:{int(T)}:t>")
    assert p["embeds"][0]["color"] == RED
    assert plain(p) == "down since 05:12 GMT"


def test_post_discord_wraps_text(monkeypatch):
    import spikebot.main as m
    sent = []

    class FakeResp:
        status_code = 204
        text = ""

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, timeout):
            sent.append(json)
            return FakeResp()
    monkeypatch.setattr(m, "AsyncSession", FakeSession)
    asyncio.run(m.post_discord("https://x.invalid", "hello"))
    asyncio.run(m.post_discord("https://x.invalid", status("start", "hi")))
    assert sent[0] == {"content": "hello", "allowed_mentions": {"parse": []}}
    assert sent[1]["embeds"][0]["description"] == "hi"


def test_bot_moment_gets_vod_link_at_clip_start(tmp_path, monkeypatch):
    import spikebot.main as m
    from spikebot.detector import Spike
    posts = []

    async def fake_post(url, payload):
        posts.append((url, payload))

    async def fake_vod(slug, stream_id):
        assert (slug, stream_id) == ("adinross", 99)
        return {"url": "https://kick.com/adinross/videos/u1", "started_at": "2026-11-19T05:00:00Z"}
    monkeypatch.setattr(m, "post_discord", fake_post)
    monkeypatch.setattr(m, "kick_stream_vod", fake_vod)
    bot = m.Bot({"channels": [{"platform": "kick", "name": "adinross", "programme": "Adin Ross",
                               "display": "Adin Ross"}],
                 "data_dir": str(tmp_path), "discord_webhook_url": "https://moments.invalid"})
    key = ("kick", "adinross")
    start = m.parse_time("2026-11-19T05:00:00Z")
    bot.state[key] = {"live": True, "started_at": "2026-11-19T05:00:00Z",
                      "category": "Grand Theft Auto VI", "stream_id": 99}
    spike = Spike("kick", "adinross", onset=start + 3630, peak_time=start + 3640, end=start + 3660,
                  peak_rate=20.0, baseline=2.0, zscore=9.0, messages=300, unique_users=200,
                  hype_share=0.5, clip_mentions=5, score=8.0, clip_start=start + 3600,
                  clip_end=start + 3645, sample=["NO WAY"], vibe=["NO WAY"])
    asyncio.run(bot.handle(key, spike))
    (url, p), = posts
    assert url == "https://moments.invalid"
    assert p["embeds"][0]["url"] == "https://kick.com/adinross/videos/u1?t=3600"
    assert p["embeds"][0]["title"].startswith("🔥 Adin Ross · 10.0x")
    assert bot.store.candidates_since(0)[0]["vod_url"].endswith("?t=3600")
