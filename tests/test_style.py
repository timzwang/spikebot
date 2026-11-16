"""Variant styles, the learning profile, feedback parsing and presets (pure logic)."""
from __future__ import annotations

import random

import pytest

from spikebot import presets as P
from spikebot import style as S


def profile(tmp_path):
    return S.Profile(tmp_path / "style_profile.json")


# ---- picking variants ---------------------------------------------------------
@pytest.mark.parametrize("mode", ["lacy", "tokyo_girl"])
def test_three_variants_differ_in_grade_pace_text_and_hook(tmp_path, mode):
    vs = S.pick_variants(profile(tmp_path), mode, random.Random(1))
    assert [v["label"] for v in vs] == ["A", "B", "C"]
    assert [v["experimental"] for v in vs] == [False, False, True]
    for dim in ("grade", "pace", "font", "colour", "position", "transition", "hook"):
        assert len({v["style"][dim] for v in vs}) == 3, dim
    assert all(v["style"]["hook"] in S.HOOKS[mode] for v in vs)
    assert ("ending" in vs[0]["style"]) == (mode == "lacy")


def test_learning_biases_variant_a_and_pins_win(tmp_path):
    prof = profile(tmp_path)
    vs = S.pick_variants(prof, "lacy", random.Random(2))
    target = vs[2]  # Tim keeps approving the experimental look
    for _ in range(4):
        prof.shown("lacy", vs)
        prof.decide("approve", "lacy", "x", vs, chosen="C")
    prof.save()
    prof = S.Profile(prof.path)  # survives a reload
    a = S.pick_variants(prof, "lacy", random.Random(3))[0]
    for dim in ("grade", "pace", "font", "colour"):
        assert a["style"][dim] == target["style"][dim], dim
    assert prof.preferred("lacy")["grade"] == target["style"]["grade"]
    prof.pin("font", "roboto")
    prof.pin("hook:lacy", "hype")
    for v in S.pick_variants(prof, "lacy", random.Random(4)):
        assert v["style"]["font"] == "roboto" and v["style"]["hook"] == "hype"
    assert prof.pinned("tokyo_girl").get("hook") is None
    prof.pin("font", "auto")
    assert "font" not in prof.data["pinned"]


def test_reject_and_redo_lower_scores(tmp_path):
    prof = profile(tmp_path)
    vs = S.pick_variants(prof, "lacy", random.Random(5))
    g = vs[0]["style"]["grade"]
    before = prof.score("grade", g)
    prof.decide("reject", "lacy", "x", vs)
    assert prof.score("grade", g) < before
    prof.decide("redo", "lacy", "y", vs)
    assert prof.data["stats"]["grade"][g]["passed"] == 1
    assert [d["action"] for d in prof.data["decisions"]] == ["reject", "redo"]


def test_experimental_tries_least_shown_and_redo_avoids_old_looks(tmp_path):
    prof = profile(tmp_path)
    first = S.pick_variants(prof, "lacy", random.Random(6))
    prof.shown("lacy", first)
    again = S.pick_variants(prof, "lacy", random.Random(7), avoid=[v["style"] for v in first])
    shown_grades = {v["style"]["grade"] for v in first}
    assert again[2]["style"]["grade"] not in shown_grades
    assert again[0]["style"]["grade"] not in shown_grades


def test_variant_b_uses_most_approved_presets_in_rotation(tmp_path):
    prof = profile(tmp_path)
    ps = {n: {"name": n, "copy": ["pacing", "colour"], "settings": {
        "pacing": {"pace": "fast", "shot_s": 0.7}, "colour": {"grade": "moody", "saturation": 0.9}}}
        for n in ("aaa", "bbb", "ccc", "ddd")}
    prof.data["presets"] = {"bbb": {"approved": 5, "shown": 6, "last_used": 10},
                            "ccc": {"approved": 3, "shown": 3, "last_used": 5},
                            "ddd": {"approved": 1, "shown": 9, "last_used": 1}}
    assert prof.top_presets(list(ps)) == ["bbb", "ccc", "ddd"]
    vs = S.pick_variants(prof, "lacy", random.Random(8), ps)
    b = vs[1]
    assert b["preset"] == "ddd"  # of the top 3, used longest ago
    assert b["style"]["pace"] == "fast" and b["style"]["grade"] == "moody"
    assert b["style"]["custom"]["saturation"] == 0.9 and b["style"]["custom"]["shot_s"] == 0.7
    prof.shown("lacy", vs, now=100)
    assert S.pick_variants(prof, "lacy", random.Random(9), ps)[1]["preset"] == "ccc"
    prof.decide("approve", "lacy", "x", vs, chosen="B")
    assert prof.data["presets"]["ddd"]["approved"] == 2


# ---- resolve / filters -------------------------------------------------------------
def test_resolve_and_grade_filter():
    r = S.resolve({"grade": "warm", "pace": "fast", "font": "montserrat", "size": "xl", "colour": "fire",
                   "custom": {"saturation": 1.4, "text_px": 120}})
    assert r["grade"]["saturation"] == 1.4 and r["font"] == "Montserrat" and r["scale"] == 1.3
    assert r["pace"]["max_gap_s"] == 0.5 and r["text_px"] == 120
    f = S.grade_filter(r["grade"])
    assert f.startswith("eq=saturation=1.400") and "colorbalance=rm=0.080" in f
    assert S.grade_filter(S.GRADES["natural"]) == ""
    assert S.ass_colour("#FFE500") == "&H0000E5FF"


# ---- feedback --------------------------------------------------------------------
def test_feedback_notes_become_setting_changes():
    changes, texts, intents, unknown = S.parse_feedback(
        "Bigger text please. Make it faster; more colour. Text in yellow\nhook: Lacy did NOT see this coming."
        " The vibe is off somehow", "lacy")
    assert changes["size"] == "up" and changes["pace"] == "up" and changes["saturation"] == "up"
    assert changes["colour"] == "classic"
    assert texts["hook"] == "Lacy did NOT see this coming."
    assert {"text_bigger", "faster", "more_colour", "new_hook"} <= set(intents)
    assert unknown == ["The vibe is off somehow"]
    style = {"grade": "natural", "pace": "normal", "size": "m", "position": "low", "colour": "ice"}
    new = S.apply_changes(style, changes)
    assert new["size"] == "l" and new["pace"] == "fast" and new["colour"] == "classic"
    assert new["custom"]["saturation"] == pytest.approx(1.15)
    assert style["size"] == "m"  # original untouched


def test_feedback_more_cases():
    c, _, _, _ = S.parse_feedback("too fast, text higher, no flashes, end with a cta", "lacy")
    new = S.apply_changes({"pace": "normal", "position": "low", "transition": "flash", "ending": "none"}, c)
    assert new == {"pace": "slow", "position": "middle", "transition": "cut", "ending": "cta"}
    c, _, _, _ = S.parse_feedback("warmer please, smaller text", "tokyo_girl")
    assert c == {"grade": "warm", "size": "down"}
    c, t, _, _ = S.parse_feedback('use "Who is playing day one?" as the hook', "tokyo_girl")
    assert t["hook"] == "Who is playing day one?"


def test_profile_records_feedback(tmp_path):
    prof = profile(tmp_path)
    prof.feedback("i", "B", "bigger text", ["text_bigger"], {"size": "m"}, {"size": "l"}, "lacy")
    prof.feedback("i", "B", "bigger text again", ["text_bigger"], {"size": "l"}, {"size": "xl"}, "lacy")
    assert prof.data["feedback"]["intents"]["text_bigger"] == 2
    assert prof.score("size", "xl") > prof.score("size", "m")


def test_set_value_validates():
    s = S.set_value({}, "pace", "fast", "lacy")
    s = S.set_value(s, "saturation", "1.3", "lacy")
    s = S.set_value(s, "text_colour", "ffe500", "lacy")
    assert s == {"pace": "fast", "custom": {"saturation": 1.3, "text_colour": "#FFE500"}}
    for setting, value in (("pace", "warp"), ("saturation", "9"), ("text_colour", "yellow"), ("nope", "1")):
        with pytest.raises(ValueError):
            S.set_value({}, setting, value, "lacy")


@pytest.mark.parametrize("text,kind", [
    ("POV: you're in Lacy's chat", "pov"), ("Wait for Lacy's reaction", "wait"),
    ("Day one or waiting for reviews?", "question"), ("GTA 6 drops in 3 days", "countdown"),
    ("Nobody saw this coming", "reveal"), ("Lacy made this look easy", "statement"), ("", "none")])
def test_hook_style(text, kind):
    assert S.hook_style(text) == kind


def test_ending_style():
    assert S.ending_style("Follow for part 2", False) == "cta"
    assert S.ending_style("Would you?", True) == "question"
    assert S.ending_style("", True) == "loop"
    assert S.ending_style("", False) == "none"


# ---- presets ---------------------------------------------------------------------
def test_parse_copy_and_name():
    assert P.parse_copy("love this copy: pacing, color and music") == ["pacing", "colour", "music-sync"]
    assert P.parse_copy("no note") == list(P.PARTS)
    assert P.parse_copy("copy: all") == list(P.PARTS)
    assert P.parse_name("name: Fast Cuts!") == "fast-cuts"
    assert P.clean_name("../../etc") == "etc"  # no path tricks
    with pytest.raises(ValueError):
        P.clean_name("!!!")


def test_preset_store_crud_combine_and_style(tmp_path):
    store = P.Presets(tmp_path / "presets")
    base = {"copy": list(P.PARTS), "settings": {
        "pacing": {"pace": "fast", "shot_s": 0.6}, "colour": {"grade": "vivid", "saturation": 1.6},
        "text": {"position": "middle", "text_px": 96, "text_colour": "#FFFFFF"},
        "transitions": {"transition": "zoom"}, "hook": {"hook_style": "pov"},
        "structure": {"hook_s": 2.0, "payoff": 0.6, "ending": "loop"}, "music-sync": {"on_beat": True}}}
    store.save({"name": "one"} | base)
    store.save({"name": "two"} | base | {"settings": base["settings"] | {"colour": {"grade": "moody"}}})
    assert store.names() == ["one", "two"] and store.unique_name("one") == "one-2"
    store.rename("one", "Fast One")
    assert store.names() == ["fast-one", "two"]
    with pytest.raises(ValueError):
        store.rename("two", "fast-one")
    p = store.edit("two", "pace", "slow")
    assert p["settings"]["pacing"]["pace"] == "slow"
    p = store.edit("two", "copy", "pacing, colour")
    assert p["copy"] == ["pacing", "colour"]
    with pytest.raises(ValueError):
        store.edit("two", "pace", "ludicrous")
    mix = store.combine("mix", {"pacing": "fast-one", "colour": "two"})
    assert mix["copy"] == ["pacing", "colour"]
    s = P.to_style(store.load("mix"), "lacy")
    assert s == {"pace": "fast", "grade": "moody", "custom": {"shot_s": 0.6}}
    full = P.to_style(store.load("fast-one"), "lacy")
    assert full["transition"] == "zoom" and full["ending"] == "none" and full["position"] == "middle"
    assert full["custom"]["hook_style"] == "pov" and full["custom"]["payoff"] == 0.6
    assert "ending" not in P.to_style(store.load("fast-one"), "tokyo_girl")
    assert "copies: pacing, colour" in P.summary(mix)
    store.delete("mix")
    with pytest.raises(ValueError):
        store.load("mix")
