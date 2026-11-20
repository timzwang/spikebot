"""#updates channel: the changelog and posting it once (spikebot/updates.py)."""
from __future__ import annotations

import asyncio

from spikebot import updates as U


def test_changelog_is_valid_brief_and_unique():
    rows = U.load()
    assert len(rows) >= 30
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    for r in rows:
        assert r["date"][:4] == "2026" and len(r["title"]) <= 40 and len(r["text"]) <= 160, r["id"]
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)   # newest last


def test_messages_group_by_day_and_cap_at_ten():
    rows = [{"id": f"a{i}", "date": "2026-11-18", "title": f"T{i}", "text": "x"} for i in range(12)]
    rows += [{"id": "b", "date": "2026-11-19", "title": "B", "text": "y"}]
    msgs = U.messages(rows)
    assert [len(m["embeds"]) for m in msgs] == [10, 2, 1]
    assert msgs[0]["content"] == "**📅 Wed 18 Nov**" and msgs[2]["content"] == "**📅 Thu 19 Nov**"
    e = msgs[2]["embeds"][0]
    assert e["title"] == "🆕 B" and e["footer"]["text"] == "Thu 19 Nov"
    assert U.pending(rows, {"a0", "b"})[0]["id"] == "a1"


def test_bot_posts_every_update_once(tmp_path, monkeypatch):
    from spikebot import discord_bot as DB
    sent = []

    class Ch:
        id = 77

        async def send(self, **kw):
            sent.append(kw)
    cfg = {"data_dir": str(tmp_path / "data"), "discord_bot_token": "t", "discord_channel_id": "100"}
    (tmp_path / "config.json").write_text("{}")
    b = DB.ReviewBot(cfg, tmp_path / "config.json")

    async def chan():
        return Ch()
    monkeypatch.setattr(b, "updates_channel", chan)
    n = asyncio.run(b.post_updates())
    assert n == len(U.load()) and sum(len(m["embeds"]) for m in sent) == n
    assert asyncio.run(b.post_updates()) == 0          # never twice, even after a restart
