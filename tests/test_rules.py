"""/rules: the numbered rules list, categories, a Turn off / Turn on button beside every rule
(Components V2 pages), several at once with disable:, buttons on /feedback replies and cards."""
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

        async def send_message(content=None, **kw):     # the direct (non-deferred) answer
            self.response.done = True
            self.sent.append((content, kw))
        self.response.send_message = send_message

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


def small_profile() -> S.Profile:
    prof = S.Profile.__new__(S.Profile)
    prof.data = S._blank()
    prof.pin("grade", "moody")
    prof.add_rule("x", "", "lacy", [{"mode": "lacy", "setting": "text_level", "value": "none"}],
                  ["Payoff in the first 3 seconds"], ["Licensed music"])
    prof.pin("grade", "warm")
    prof.set_enabled([3], False)
    return prof


def test_rules_page_has_a_button_beside_every_rule():
    p = DB.rules_page(small_profile().entries())
    texts = [b["content"] for b in p["blocks"] if b["type"] == "text"]
    assert "4 on · 1 off" in texts[0]
    assert texts[1:] == ["### 🎨 Colour & look", "### 🔤 Text & captions",
                         "### 📝 Notes (being turned into settings, or waiting for new editor code)", "### 🚫 Asked for, not possible yet"]
    rules = {b["n"]: b for b in p["blocks"] if b["type"] == "rule"}
    assert rules[1]["content"] == "**#1** Colour grade: moody · _replaced by #5_\n-# darker, high contrast, a bit cold"
    assert rules[2]["content"] == "**#2** Amount of text: none\n-# no text at all"
    assert {o["value"]: o["default"] for o in rules[2]["scope"]["options"]} == \
        {"lacy": True, "tokyo_girl": False, "trend": False}                     # what it applies to
    assert rules[2]["scope"]["custom_id"] == DB.cid("rulem", "-", 0, "2")
    assert rules[3]["content"] == "~~**#3** Payoff in the first 3 seconds~~ · _off_"
    assert rules[1]["button"]["label"] == "Turn off" and rules[3]["button"]["label"] == "Turn on"
    assert rules[3]["button"]["custom_id"] == DB.cid("rulep", "-", 0, "3")
    assert p["pages"] == 1 and not any(b["type"] == "row" for b in p["blocks"])
    empty = DB.rules_page([])
    assert "No rules yet" in empty["blocks"][0]["content"]


def test_long_lists_are_paged_within_discord_limits(tmp_path):
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    prof = S.Profile.__new__(S.Profile)
    prof.data = S._blank()
    for i in range(60):
        prof.add_rule("x", "", "all", [{"mode": "all", "setting": "grade", "value": "moody"}],
                      [f"Note number {i} " + "cut the dead air " * (i % 6)], [])
    es = prof.entries()
    pages = DB.rules_pages(es)
    assert len(pages) > 1 and sorted(n for pg in pages for _, g in pg for n in g["ns"]) == [e["n"] for e in es]
    for i in range(len(pages)):
        spec = DB.rules_page(es, i)
        view = b.layout(spec)                        # real discord.py: raises past 40 components
        assert view.total_children_count <= 40
        assert sum(len(x["content"]) for x in spec["blocks"] if "content" in x) <= 4000
    nav = DB.rules_page(es, 1)["blocks"][-1]["buttons"]
    assert [x["label"] for x in nav] == ["◀ Prev", f"Page 2/{len(pages)}", "Next ▶"]
    assert nav[0]["custom_id"] == DB.cid("rulesp", "-", 0) and nav[1]["disabled"]
    assert DB.rules_page(es, 99)["page"] == len(pages) - 1


def test_rules_command_answers_at_once_and_buttons_toggle(tmp_path):
    import discord
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    profile_with_rules(store)
    inter = Inter()
    asyncio.run(b.rules_command(inter))
    _, kw = inter.sent[-1]
    assert isinstance(kw["view"], discord.ui.LayoutView)              # sent straight away, no "thinking"
    ids = [c.accessory.custom_id for c in kw["view"].children if isinstance(c, discord.ui.Section)]
    assert ids[0] == DB.cid("rulep", "-", 0, "1") and len(ids) == 5          # 5 a page with their menus
    menus = [c for c in kw["view"].walk_children() if isinstance(c, discord.ui.Select)]
    assert len(menus) == 5 and menus[0].custom_id == DB.cid("rulem", "-", 0, "1")

    i2 = Inter()                                                       # several at once
    asyncio.run(b.rules_command(i2, disable="2, 3-4, 99"))
    assert "Turned off #2, #3, #4" in str(i2.sent[-1][1]["view"].to_components())
    assert "No entry #99" in str(i2.sent[-1][1]["view"].to_components())
    prof = store.profile()
    assert "text_level" not in prof.pinned("lacy") and prof.pinned("lacy")["grade"] == "warm"

    i3 = Inter()                                                       # the button beside #6
    asyncio.run(b.on_button(i3, DB.parse_cid(DB.cid("rulep", "-", 0, "6"))))
    assert not store.profile().entry(6)["on"] and store.profile().pinned("lacy")["grade"] == "moody"
    page = str(i3.edited["view"].to_components())
    assert "Turned off #6" in page and "~~**#6** Colour grade: warm~~" in page
    i4 = Inter()
    asyncio.run(b.on_button(i4, DB.parse_cid(DB.cid("rulep", "-", 0, "6"))))
    assert store.profile().entry(6)["on"]
    i5 = Inter()                                                       # Prev / Next
    asyncio.run(b.on_button(i5, DB.parse_cid(DB.cid("rulesp", "-", 0))))
    assert isinstance(i5.edited["view"], discord.ui.LayoutView)
    stranger = Inter(uid=7)
    asyncio.run(b.on_button(stranger, DB.parse_cid(DB.cid("rulep", "-", 0, "1"))))
    assert stranger.edited is None and store.profile().entry(1)["on"]


def test_turn_off_buttons_on_feedback_reply_and_review_card(tmp_path):
    store, item = new_item(tmp_path)
    b, reg = bot_for(tmp_path, store)
    profile_with_rules(store)
    rows = DB.entry_buttons([2, 3], {2: True, 3: False})
    assert [x["label"] for x in rows[0]] == ["Turn off #2", "Turn on #3"]
    flipped = DB.flip_rows(rows, 2, False)
    assert flipped[0][0]["label"] == "Turn on #2" and flipped[0][1] == rows[0][1]

    # the rules a feedback re-render saved get their own message (button + "applies to" each)
    sub = DB.rules_subset(store.profile().entries(), [2, 3], "Saved")
    rules = [b for b in sub["blocks"] if b["type"] == "rule"]
    assert [r["n"] for r in rules] == [2, 3] and rules[0]["scope"]["custom_id"] == DB.cid("rulem", "2.3", 0, "2")

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
        R.request(it, "feedback", round=1, label="A", notes="no captions from now on")
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
    assert fresh.sent[-1][0] == "⚠️ /rules failed: ValueError: bad entry"
    assert fresh.sent[-1][1]["ephemeral"] and "/rules failed" in caplog.text
    late = Inter()
    late.response.done = True                                        # already deferred
    asyncio.run(b.report_error(late, RuntimeError("boom"), "button rules_off"))
    assert late.sent[-1] == ("⚠️ button rules_off failed: RuntimeError: boom", {"ephemeral": True})


def test_rules_falls_back_to_a_file_when_discord_refuses(tmp_path):
    import discord
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    profile_with_rules(store)

    class Resp:
        status, reason = 500, "Internal Server Error"
    inter = Inter()
    first = []

    async def send_message(content=None, **kw):
        if not first:
            first.append(1)
            raise discord.DiscordServerError(Resp(), "500: Internal Server Error")
        inter.response.done = True
        inter.sent.append((content, kw))
    inter.response.send_message = send_message
    asyncio.run(b.rules_command(inter))
    text, kw = inter.sent[-1]
    assert "as a file" in text and kw["file"].filename == "rules.txt"
    body = kw["file"].fp.read().decode()
    assert "#3 ON  Pace: fast  (short clips, many cuts)" in body and "#5 ON  Licensed music" in body


def test_applies_to_ticks_change_what_a_rule_is_for(tmp_path):
    import discord
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    prof = profile_with_rules(store)          # #2 text_level none for all edits
    assert prof.pinned("trend")["text_level"] == "none"
    i = Inter(["lacy", "tokyo_girl"])
    asyncio.run(b.on_button(i, DB.parse_cid(DB.cid("rulem", "-", 0, "2"))))
    prof = store.profile()
    assert prof.entry(2)["modes"] == ["lacy", "tokyo_girl"] and "text_level" not in prof.pinned("trend")
    assert "now applies to **Lacy, Tokyo Girl**" in str(i.edited["view"].to_components())
    # the ticks offered only where the setting exists; a feedback reply's own rules redraw themselves
    prof.add_entry("setting", "all", "framing", "fit")
    prof.save()
    n = prof.entries()[-1]["n"]
    sub = DB.rules_subset(store.profile().entries(), [2, n], "Saved")
    menus = {blk["n"]: [o["value"] for o in blk["scope"]["options"]] for blk in sub["blocks"] if blk["type"] == "rule"}
    assert menus == {2: ["lacy", "tokyo_girl", "trend"], n: ["tokyo_girl", "trend"]}
    i2 = Inter(["trend"])
    asyncio.run(b.on_button(i2, DB.parse_cid(DB.cid("rulem", f"2.{n}", 0, str(n)))))
    assert store.profile().entry(n)["modes"] == ["trend"] and isinstance(i2.edited["view"], discord.ui.LayoutView)
    i3 = Inter(["lacy"])                                                   # framing can't apply to Lacy
    asyncio.run(b.on_button(i3, DB.parse_cid(DB.cid("rulem", "-", 0, str(n)))))
    assert store.profile().entry(n)["modes"] == ["trend"]


def test_trend_edits_have_their_own_rules():
    prof = S.Profile.__new__(S.Profile)
    prof.data = S._blank()
    old = {"n": 1, "kind": "setting", "mode": "tokyo_girl", "setting": "grade", "value": "moody", "on": True}
    prof.data["entries"].append(old)                       # saved before: Tokyo Girl meant both
    assert prof.pinned("trend")["grade"] == "moody" and prof.pinned("tokyo_girl")["grade"] == "moody"
    prof.add_entry("setting", "trend", "grade", "warm")
    assert prof.pinned("trend")["grade"] == "warm" and prof.pinned("tokyo_girl")["grade"] == "moody"
    assert S.dims("trend") == S.dims("tokyo_girl")
    v = S.pick_variants(prof, "trend", random.Random(1))
    assert {x["style"]["grade"] for x in v} == {"warm"}
    assert R.style_mode({"mode": "tokyo_girl", "source": {"kind": "trend"}}) == "trend"
    assert R.style_mode({"mode": "tokyo_girl", "source": {}}) == "tokyo_girl"


def test_same_rules_are_merged_explained_and_toggled_together(tmp_path):
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    prof = store.profile()
    prof.pin("transition", "cut")                                               # 1
    prof.pin("transition", "cut")                                               # 2 (same again)
    prof.add_rule("x", "", "all", [], ["No  text at the END."], [])            # 3
    prof.add_rule("y", "", "all", [], ["no text at the end"], [])              # 4 (same note)
    prof.pin("grade", "warm")                                                   # 5
    prof.save()
    p = DB.rules_page(prof.entries())
    rules = [x for x in p["blocks"] if x["type"] == "rule"]
    assert len(rules) == 3 and "3 on · 0 off" in p["blocks"][0]["content"]
    cut = next(x for x in rules if x["ns"] == [1, 2])
    assert cut["content"].startswith("**#1 · #2** Transition: cut\n-# a straight cut, no effect")
    assert "saved 2 times" in cut["content"] and cut["button"]["custom_id"] == DB.cid("rulep", "-", 0, "1.2")
    asyncio.run(b.on_button(Inter(), DB.parse_cid(cut["button"]["custom_id"])))    # one click: both off
    assert not store.profile().entry(1)["on"] and not store.profile().entry(2)["on"]
    asyncio.run(b.on_button(Inter(), DB.parse_cid(cut["button"]["custom_id"])))
    assert store.profile().entry(1)["on"] and store.profile().entry(2)["on"]


def test_rules_search_finds_by_words_and_number(tmp_path):
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    prof = profile_with_rules(store)
    es = prof.entries()
    found = lambda q: [x["n"] for x in DB.rules_page(es, 0, "", q)["blocks"] if x["type"] == "rule"]
    assert found("colour") == [1, 6] and found("warm") == [6] and found("#3") == [3]
    assert found("music") == [5] and found("nothing-like-this") == []
    p = DB.rules_page(es, 0, "", "Colour")
    assert "matching “colour”" in p["blocks"][0]["content"]
    assert p["blocks"][2]["button"]["custom_id"] == DB.cid("rulep", "s:colour", 0, "1")
    inter = Inter()
    asyncio.run(b.rules_command(inter, search="warm"))
    assert "Colour grade: warm" in str(inter.sent[-1][1]["view"].to_components())
    i2 = Inter()                                   # a click on a search page keeps the search
    asyncio.run(b.on_button(i2, DB.parse_cid(DB.cid("rulep", "s:warm", 0, "6"))))
    page = str(i2.edited["view"].to_components())
    assert "matching “warm”" in page and "moody" not in page
