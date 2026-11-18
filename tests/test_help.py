from __future__ import annotations

from spikebot import help as h


def test_every_command_has_a_category_and_usage():
    keys = {k for k, _, _ in h.CATEGORIES}
    for c in h.COMMANDS:
        assert c["cat"] in keys, c["name"]
        assert c["what"] and c["how"], c["name"]


def test_full_list_has_every_category_heading():
    text = h.help_text()
    for _, heading, _ in h.CATEGORIES:
        assert heading in text
    assert "/preset combine" in text and "Approve A/B/C" in text


def test_category_filter():
    text = h.help_text("presets")
    assert "/preset apply" in text and "/feedback" not in text and "Presets" in text


def test_search_matches_names_text_and_tags():
    assert [c["name"] for c in h.select(search="stop")] == ["/cancel", "Cancel (progress bar)"]
    assert "/preset combine" in [c["name"] for c in h.select(search="/preset colour")]
    assert h.select(search="CAPTION")       # case-insensitive
    assert all(c["cat"] == "presets" for c in h.select("presets", "preset"))


def test_no_match_says_so():
    assert "Nothing matches" in h.help_text(search="zzqx")


def test_chunks_fit_discord_and_keep_everything():
    text = h.help_text()
    parts = h.chunks(text, limit=600)
    assert len(parts) > 1 and all(len(p) <= 600 for p in parts)
    assert all(not p.rstrip().endswith("__") for p in parts)   # a heading never ends a message
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_help_lists_every_registered_slash_command():
    from discord import Client, Intents, app_commands

    from spikebot.discord_bot import ReviewBot

    tree = app_commands.CommandTree(Client(intents=Intents.none()))
    ReviewBot.register_commands(object.__new__(ReviewBot), tree)
    names = {c.name for c in tree.get_commands()}
    assert "help" in names
    assert names <= h.SLASH, f"missing from spikebot/help.py: {names - h.SLASH}"
    group = tree.get_command("preset")
    listed = {c["name"] for c in h.COMMANDS}
    assert {f"/preset {c.name}" for c in group.commands} <= listed
