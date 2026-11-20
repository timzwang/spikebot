"""Channel descriptions (the line under each channel name)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from spikebot import discord_bot as D


# ---- channel descriptions ------------------------------------------------------------------
class Ch:
    def __init__(self, id, name, topic=None):
        self.id, self.name, self.topic, self.edits = id, name, topic, []

    async def edit(self, topic=None):
        self.edits.append(topic)
        self.topic = topic


def test_topic_plan_by_id_and_name():
    rcfg = {"channel_id": 1, "tiktok_channel_id": 2, "inspiration_channel_id": 3}
    chans = [Ch(1, "ready-to-post"), Ch(2, "tiktok-queue"), Ch(3, "insp"), Ch(4, "📈-daily-summary"),
             Ch(5, "status", D.CHANNEL_TOPICS["status"]), Ch(6, "moments"), Ch(7, "announcements"),
             Ch(8, "general"), Ch(9, "tiktok-status-old")]
    plan = {ch.id: t for ch, t in D.topic_plan(chans, rcfg)}
    assert plan[1] == D.CHANNEL_TOPICS["channel_id"]
    assert plan[3] == D.CHANNEL_TOPICS["inspiration_channel_id"]
    assert plan[4] == D.CHANNEL_TOPICS["daily-summary"]
    assert 5 not in plan            # already right: no edit (Discord rate-limits topic changes)
    assert 6 in plan and 7 in plan and 8 not in plan and 9 not in plan
    assert all(len(t) <= 1024 for t in D.CHANNEL_TOPICS.values())


def test_ensure_topics_skips_forbidden_channels():
    class NoPerm(Ch):
        async def edit(self, topic=None):
            raise RuntimeError("403 Missing Permissions")
    good, bad = Ch(6, "moments"), NoPerm(7, "announcements")
    guild = SimpleNamespace(text_channels=[good, bad])
    bot = SimpleNamespace(rcfg={"guild_id": 9}, client=SimpleNamespace(get_guild=lambda i: guild))
    n = asyncio.run(D.ReviewBot.ensure_topics(bot))
    assert n == 1 and good.topic == D.CHANNEL_TOPICS["moments"]
