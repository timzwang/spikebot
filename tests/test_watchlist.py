import asyncio

from spikebot import main as m
from spikebot import watchlist as wl
from spikebot.sources import JoinLimiter, TwitchChatPool, _parse_join, _parse_privmsg_chan


def stream(login, viewers, game="Grand Theft Auto VI", lang="en"):
    return {"user_login": login, "user_name": login.title(), "viewer_count": viewers,
            "game_name": game, "language": lang, "id": f"s{login}", "user_id": f"u{login}",
            "started_at": "2026-11-19T05:00:00Z", "title": "GTA 6 DAY 1"}


S = wl.settings({"discovery": {"enabled": True, "min_viewers": 1000, "max_channels": 3}})


def test_settings_env_override():
    s = wl.settings({"discovery_mode": "true", "discovery_max_channels": "25",
                     "discovery": {"exclude": ["XQC"]}})
    assert s["enabled"] and s["max_channels"] == 25 and s["exclude"] == ["xqc"]
    assert not wl.settings({})["enabled"]


def test_eligible_filters_category_viewers_language_exclude():
    s = wl.settings({"discovery": {"min_viewers": 1000, "exclude": ["bad"]}})
    assert wl.eligible(stream("a", 5000), s)
    assert not wl.eligible(stream("a", 500), s)
    assert not wl.eligible(stream("a", 5000, game="Grand Theft Auto V"), s)
    assert not wl.eligible(stream("a", 5000, lang="es"), s)
    assert not wl.eligible(stream("bad", 5000), s)


def test_plan_adds_biggest_first_respects_cap_and_fixed():
    top = [stream("small", 1500), stream("big", 90000), stream("lacy", 50000),
           stream("mid", 20000), stream("tiny", 10), stream("other", 1200)]
    add, drop = wl.plan({}, top, {"lacy"}, S, now=0)
    assert [t["user_login"] for t in add] == ["big", "mid", "small"]
    assert drop == []


def test_plan_drops_only_after_grace_period():
    watched = {"gone": 0.0, "stay": 0.0}
    top = [stream("stay", 5000)]
    wl.seen(watched, top, S, now=1000)
    assert watched == {"gone": 0.0, "stay": 1000}
    add, drop = wl.plan(watched, top, set(), S, now=1000)
    assert drop == []  # 1000 s < drop_after_s 1800
    add, drop = wl.plan(watched, top, set(), S, now=1900)
    assert drop == ["gone"]


def test_plan_counts_kept_channels_against_cap():
    watched = {"a": 0.0, "b": 0.0}
    top = [stream("a", 9000), stream("b", 8000), stream("c", 7000), stream("d", 6000)]
    add, _ = wl.plan(watched, top, set(), S, now=10)
    assert [t["user_login"] for t in add] == ["c"]


def test_alert_gate_ranks_and_caps_no_programme():
    g = wl.AlertGate(max_per_hour=2, window_s=3600)
    assert g.decide(5.0, False, 0) == wl.Decision(True, 1, 1)
    assert g.decide(3.0, False, 10) == wl.Decision(True, 2, 2)
    # cap reached: a weaker no-programme moment is held back...
    assert g.decide(4.0, False, 20).send is False
    # ...a programme streamer always goes through, ranked against everything
    assert g.decide(1.0, True, 30) == wl.Decision(True, 4, 4)
    # ...and a no-programme moment beating every sent one still gets through
    assert g.decide(6.0, False, 40) == wl.Decision(True, 1, 5)
    # an hour later the window has emptied
    assert g.decide(0.5, False, 3700).send is True


def test_irc_parsers():
    line = ":bob!bob@bob.tmi.twitch.tv PRIVMSG #KaiCenat :NO WAY 😭"
    assert _parse_privmsg_chan(line) == ("kaicenat", "bob", "NO WAY 😭")
    assert _parse_privmsg_chan("@badge=1 " + line) == ("kaicenat", "bob", "NO WAY 😭")
    assert _parse_privmsg_chan(":tmi.twitch.tv 001 justinfan1 :Welcome") is None
    assert _parse_join(":justinfan123!justinfan123@justinfan123.tmi.twitch.tv JOIN #lacy") == "lacy"
    assert _parse_join(":someone!x@x JOIN #lacy") is None


def test_pool_shards_and_removes():
    pool = TwitchChatPool(per_conn=2)
    for n in ["a", "b", "c"]:
        pool.add(n, lambda *a: None)
    assert len(pool.conns) == 2 and pool.channels() == {"a", "b", "c"}
    pool.remove("b")
    assert pool.channels() == {"a", "c"} and "b" not in pool.subs
    pool.add("d", lambda *a: None)  # fills the free slot instead of a new socket
    assert len(pool.conns) == 2


def test_join_limiter_waits_when_budget_used():
    t = [0.0]
    lim = JoinLimiter(per_window=2, window_s=1.0, clock=lambda: t[0])

    async def go():
        await lim.acquire()
        await lim.acquire()
        task = asyncio.create_task(lim.acquire())
        await asyncio.sleep(0.01)
        assert not task.done()
        t[0] = 1.5
        await asyncio.wait_for(task, 2)
    asyncio.run(go())


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, key, text, **kw):
        self.sent.append(text)


def make_bot(tmp_path):
    cfg = {"channels": [{"platform": "twitch", "name": "lacy", "programme": "Lacy"}],
           "data_dir": str(tmp_path / "data"),
           "discovery": {"enabled": True, "min_viewers": 1000, "max_channels": 2,
                         "max_alerts_per_hour": 1}}
    bot = m.Bot(cfg)
    bot.notifier = FakeNotifier()
    return bot


def test_bot_discovery_adds_and_drops_channels(tmp_path):
    bot = make_bot(tmp_path)
    top = [stream("lacy", 80000), stream("kaicenat", 90000), stream("xqc", 40000), stream("s", 2000)]
    asyncio.run(bot.discovery_tick(top, now=0))
    assert set(bot.discovered) == {"kaicenat", "xqc"}
    key = ("twitch", "kaicenat")
    assert bot.meta[key]["programme"] is None and bot.meta[key]["discovered"]
    assert bot.pool.channels() == {"kaicenat", "xqc"}  # lacy joins via chat_tasks()
    assert "Kaicenat" in bot.notifier.sent[0]
    # xqc leaves the GTA category; dropped after the grace period
    asyncio.run(bot.discovery_tick([stream("kaicenat", 90000)], now=2000))
    assert set(bot.discovered) == {"kaicenat"} and ("twitch", "xqc") not in bot.detectors
    bot.drop_discovered("lacy")  # config channels are never dropped
    assert ("twitch", "lacy") in bot.detectors


def test_bot_alert_cap_and_rank(tmp_path):
    bot = make_bot(tmp_path)
    asyncio.run(bot.discovery_tick([stream("kaicenat", 90000), stream("xqc", 40000)], now=0))
    sent = []

    async def alert(rec):
        sent.append(rec)
    bot.alert = alert

    async def no_vod(*a):
        return None
    bot.vod_link = no_vod
    from spikebot.detector import Spike

    def spike(chan, score):
        return Spike("twitch", chan, 100.0, 105.0, 120.0, 20.0, 2.0, 5.0, 300, 200, 0.5, 3,
                     score, 70.0, 130.0)
    asyncio.run(bot.handle(("twitch", "kaicenat"), spike("kaicenat", 8.0)))
    asyncio.run(bot.handle(("twitch", "xqc"), spike("xqc", 5.0)))      # over the cap of 1
    asyncio.run(bot.handle(("twitch", "lacy"), spike("lacy", 4.0)))    # programme: always
    assert [r["channel"] for r in sent] == ["kaicenat", "lacy"]
    assert sent[1]["rank"] == 3 and sent[1]["rank_of"] == 3


def test_live_count_is_accurate(tmp_path, monkeypatch):
    """Regression: the stats panel said 2/35 live while far more were live."""
    cfg = {"channels": [{"platform": "twitch", "name": "lacy", "programme": "Lacy"},
                        {"platform": "kick", "name": "adinross", "programme": "Adin"},
                        {"platform": "kick", "name": "blocked", "programme": None}],
           "data_dir": str(tmp_path / "data"),
           "discovery": {"enabled": True, "min_viewers": 1000, "max_channels": 5}}
    bot = m.Bot(cfg)
    bot.notifier = FakeNotifier()
    now = 1_800_000_000.0
    asyncio.run(bot.discovery_tick([stream("kaicenat", 90000), stream("xqc", 40000)], now=now))
    # discovered streams count as live straight away, before any poll
    assert bot.live_count(now) == (2, 3)

    class Helix:
        async def stream_states(self, logins):
            return {l: {"live": l in ("lacy", "kaicenat", "xqc"), "category": "Grand Theft Auto VI"}
                    for l in logins}
    bot.helix = Helix()

    async def kick_info(slug):
        if slug == "blocked":
            raise RuntimeError("HTTP 403 (Cloudflare)")
        return {"livestream": {"is_live": True, "categories": [{"name": "Grand Theft Auto VI"}]}}
    monkeypatch.setattr(m, "kick_channel_info", kick_info)
    monkeypatch.setattr(m.time, "time", lambda: now + 5)
    asyncio.run(bot.poll_once())
    # lacy + 2 discovered + adinross live; the blocked Kick channel is unchecked (quiet chat)
    assert bot.live_count(now + 10) == (4, 1)
    bot.on_msg(("kick", "blocked"))(now + 8, "u", "LUL")     # busy chat = live while unchecked
    assert bot.live_count(now + 10) == (5, 1)
    snap = bot.stats_snapshot(now + 10)
    assert snap["live"] == 5 and snap["watching"] == 5 and snap["unchecked"] == 1
    # an old lookup doesn't count any more
    assert bot.live_count(now + 10 + m.LIVE_FRESH_S + 1)[1] == 5
