"""/rules: the numbered rules list, categories, switching entries off/on (several at once,
from the menus, from buttons on /feedback replies and review cards)."""
from __future__ import annotations

import asyncio
import random

from spikebot import discord_bot as DB
from spikebot import review as R
from spikebot import style as S

from .test_review import FakeInteraction, bot_for, new_item


class Inter(FakeInteraction):
    """Records follow-ups with their options and edits of the original message."""
    def __init__(self, values=None, uid=42):
        super().__init__(uid)
        self.data = {"values": values or []}
        self.sent, self.edited = [], None

        class F:
            async def send(self_, content=None, **kw):
                self.sent.append((content, kw))
        self.followup = F()

    async def edit_original_response(self, **kw):
        self.edited = kw


def profile_with_rules(store) -> S.Profile:
    prof = store.profile()
    prof.pin("grade", "moody")                                                    # 1
    prof.add_rule("no text, cut faster, payoff early", "", "all",
                  [{"mode": "all", "setting": "text_level", "value": "none"},    # 2
                   {"mode": "lacy", "setting": "pace", "value": "fast"}],       # 3
                  ["Payoff in the first 3 seconds"], ["Licensed music"])        # 4, 5
    prof.pin("grade", "warm")                                                     # 6
    prof.save()
    return prof


def test_rules_payload_groups_by_category_with_numbers():
    prof = S.Profile.__new__(S.Profile)
    prof.data = S._blank()
    prof.pin("grade", "moody")
    prof.add_rule("x", "", "lacy", [{"mode": "lacy", "setting": "text_level", "value": "none"}],
                  ["Payoff in the first 3 seconds"], ["Licensed music"])
    prof.pin("grade", "warm")
    prof.set_enabled([3], False)
    p = DB.rules_payload(prof.entries())
    titles = [e["title"] for e in p["embeds"]]
    assert titles == ["🎨 Colour & look", "🔤 Text & captions", "📝 Notes (the editor can't apply these yet)",
                      "🚫 Asked for, not possible yet"]
    look = p["embeds"][0]["description"]
    assert "`#1` ✅ grade = moody _(replaced by #5)_" in look and "`#5` ✅ grade = warm" in look
    assert "`#2` ✅ text_level = none (Lacy)" in p["embeds"][1]["description"]
    assert "`#3` ⛔ ~~Payoff in the first 3 seconds (Lacy)~~" in p["embeds"][2]["description"]
    assert "4 on · 1 off" in p["content"]
    off, on = p["rows"]
    assert off[0]["custom_id"] == DB.cid("rules_off", "-", 0) and off[0]["max"] == 4
    assert [o["value"] for o in off[0]["options"]] == ["5", "4", "2", "1"]          # newest first
    assert on[0]["custom_id"].startswith("rv|rules_on|") and on[0]["options"][0]["value"] == "3"
    assert DB.rules_payload([])["rows"] == [] and "No rules yet" in DB.rules_payload([])["content"]


def test_long_lists_fit_discord_limits():
    prof = S.Profile.__new__(S.Profile)
    prof.data = S._blank()
    for i in range(160):
        prof.add_rule("x", "", "all", [], [f"Note number {i} " + "x" * 40], [])
    prof.set_enabled(range(1, 21), False)
    p = DB.rules_payload(prof.entries())
    assert sum(len(e["description"]) for e in p["embeds"]) <= DB.RULES_BUDGET
    assert "older entries not shown" in p["content"]
    assert len(p["rows"]) <= 5 and all(len(r[0]["options"]) <= 25 for r in p["rows"])
    assert p["rows"][0][0]["placeholder"].startswith("Turn off #136–#160")


def test_rules_command_disables_several_and_menus_toggle(tmp_path):
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    profile_with_rules(store)
    inter = Inter()
    asyncio.run(b.rules_command(inter, disable="2, 3-4, 99"))
    assert inter.response.done                                       # answered Discord at once
    text, kw = inter.sent[-1]
    assert "⛔ Turned off #2, #3, #4" in text and "No entry #99" in text
    prof = store.profile()
    assert "text_level" not in prof.pinned("lacy") and prof.pinned("lacy")["grade"] == "warm"
    view = kw["view"]
    assert {c.custom_id.split("|")[1] for c in view.children} == {"rules_off", "rules_on"}

    # pick two in the "Turn off" menu, then one in "Turn back on"
    i2 = Inter(["6", "1"])
    asyncio.run(b.on_button(i2, DB.parse_cid(DB.cid("rules_off", "-", 0))))
    assert "Turned off #6, #1" in i2.edited["content"] and "grade" not in store.profile().pinned("lacy")
    i3 = Inter(["1"])
    asyncio.run(b.on_button(i3, DB.parse_cid(DB.cid("rules_on", "-", 0))))
    assert store.profile().pinned("lacy")["grade"] == "moody"            # #1 back on, #6 still off
    stranger = Inter(["1"], uid=7)
    asyncio.run(b.on_button(stranger, DB.parse_cid(DB.cid("rules_off", "-", 0))))
    assert stranger.edited is None and store.profile().entry(1)["on"]


def test_turn_off_buttons_on_feedback_reply_and_review_card(tmp_path):
    store, item = new_item(tmp_path)
    b, reg = bot_for(tmp_path, store)
    profile_with_rules(store)
    rows = DB.entry_buttons([2, 3], {2: True, 3: False})
    assert [x["label"] for x in rows[0]] == ["Turn off #2", "Turn on #3"]
    flipped = DB.flip_rows(rows, 2, False)
    assert flipped[0][0]["label"] == "Turn on #2" and flipped[0][1] == rows[0][1]

    # a feedback re-render round remembers which entries it added and shows their buttons
    with store.edit(item["id"]) as it:
        it["rounds"][0]["entries"] = [2, 3]
    rnd = store.load(item["id"])["rounds"][0]
    p = DB.round_payload(store.load(item["id"]), rnd, entry_on={2: True, 3: True})
    assert [x["label"] for x in p["rows"][-1]] == ["Turn off #2", "Turn off #3"]
    assert p["rows"][-1][0]["custom_id"] == DB.cid("rule", item["id"], rnd["n"], "2")

    class Msg:
        components = []
    inter = Inter()
    inter.message = Msg()
    asyncio.run(b.on_button(inter, DB.parse_cid(DB.cid("rule", "-", 0, "2"))))
    assert not store.profile().entry(2)["on"] and "view" in inter.edited and "Turned off #2" in inter.sent[-1][0]
    inter2 = Inter()
    inter2.message = Msg()
    asyncio.run(b.on_button(inter2, DB.parse_cid(DB.cid("rule", "-", 0, "2"))))
    assert store.profile().entry(2)["on"]


def test_feedback_round_records_its_entries(tmp_path):
    store, item = new_item(tmp_path)
    with store.edit(item["id"]) as it:
        R.request(it, "feedback", round=1, label="A", notes="no captions")
    from .test_review import FakeAdapter
    it = R.render_round(store, item["id"], {}, tmp_path, adapter=FakeAdapter(), seed=2)
    rnd = it["rounds"][-1]
    assert rnd["entries"] and store.profile().entry(rnd["entries"][0])["setting"] == "text_level"
    assert "Saved to /rules as #" in rnd["comment"]


def test_experimental_variant_avoids_rejected_options(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    for opt in S.GRADES:
        if opt != "natural":
            prof._bump("grade", opt, "shown", 1)
    for _ in range(3):
        prof._bump("grade", "natural", "rejected")          # never shown much, but turned down
    for seed in range(10):
        c = S.pick_variants(prof, "lacy", random.Random(seed))[2]
        assert c["experimental"] and c["style"]["grade"] != "natural"


def test_errors_are_reported_instead_of_interaction_failed(tmp_path, caplog):
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    fresh = Inter()
    asyncio.run(b.report_error(fresh, ValueError("bad entry"), "/rules"))
    assert fresh.response.sent[-1][0] == "⚠️ /rules failed: ValueError: bad entry"
    assert fresh.response.sent[-1][1]["ephemeral"] and "/rules failed" in caplog.text
    late = Inter()
    late.response.done = True                                        # already deferred
    asyncio.run(b.report_error(late, RuntimeError("boom"), "button rules_off"))
    assert late.sent[-1] == ("⚠️ button rules_off failed: RuntimeError: boom", {"ephemeral": True})


def test_rules_reply_works_with_real_discord_objects(tmp_path):
    """The view and embeds discord.py would send are built without errors."""
    import discord
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    profile_with_rules(store)
    inter = Inter()
    asyncio.run(b.rules_command(inter))
    text, kw = inter.sent[-1]
    assert isinstance(kw["view"], discord.ui.View) and all(isinstance(e, discord.Embed) for e in kw["embeds"])
    assert len(kw["view"].to_components()) == 1


def test_rules_falls_back_when_discord_answers_500(tmp_path):
    import discord
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    profile_with_rules(store)

    class Resp:
        status, reason = 500, "Internal Server Error"

    def refuse(n):
        calls = []

        async def send(content=None, **kw):
            text = content
            calls.append(kw)
            if len(calls) <= n:
                raise discord.DiscordServerError(Resp(), "500: Internal Server Error")
            inter.sent.append((text, kw))
        return send, calls

    async def go(n):
        global inter
        inter = Inter()
        send, calls = refuse(n)
        inter.followup.send = send
        orig = asyncio.sleep
        asyncio.sleep = lambda s: orig(0)
        try:
            await b.rules_command(inter)
        finally:
            asyncio.sleep = orig
        return inter, calls
    global inter
    i, calls = asyncio.run(go(1))                    # a blip: the retry gets the full list through
    assert len(calls) == 2 and "view" in i.sent[-1][1]
    i, calls = asyncio.run(go(2))                    # menus refused: list without them
    assert "view" not in i.sent[-1][1] and "Menus unavailable" in i.sent[-1][0]
    i, calls = asyncio.run(go(3))                    # embeds refused too: a text file
    f = i.sent[-1][1]["file"]
    assert f.filename == "rules.txt"
    body = f.fp.read().decode()
    assert "#3 ON  pace = fast (Lacy)" in body and "#5 ON  Licensed music" in body
    assert DB.plain_category({"kind": "setting", "setting": "pace"}) == "Pace & cuts"
