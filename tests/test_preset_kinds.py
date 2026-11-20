"""Presets know which kind of video they came from (stream clip / trailer edit), and only
style edits of that kind; channel descriptions."""
from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import pytest

from spikebot import discord_bot as D
from spikebot import inspiration as I
from spikebot import presets as P
from spikebot import style as st


def test_parse_for():
    assert P.parse_for("for: stream clips") == "clips"
    assert P.parse_for("name: x for: Trailer edits, copy: colour") == "edits"
    assert P.parse_for("copy: colour for: tokyo girl name: y") == "edits"
    assert P.parse_for("for: both") == "all"
    assert P.parse_for("for: banana") is None
    assert P.parse_for("love it") is None
    with pytest.raises(ValueError):
        P.kind_name("banana")


def test_guess_kind():
    assert P.guess_kind(True)[0] == "clips"
    assert P.guess_kind(False, on_beat=0.7)[0] == "edits"
    assert P.guess_kind(False, cuts_per_s=0.8)[0] == "edits"
    assert P.guess_kind(False, on_beat=0.1, cuts_per_s=0.2)[0] == "all"
    kind, why = P.guess_kind(None, on_beat=0.9)
    assert kind == "all" and "unavailable" in why


def test_fits_and_old_presets():
    assert P.fits({"for": "clips"}, "lacy") and not P.fits({"for": "clips"}, "tokyo_girl")
    assert P.fits({"for": "edits"}, "trend") and not P.fits({"for": "edits"}, "lacy")
    assert all(P.fits({}, m) for m in ("lacy", "tokyo_girl", "trend"))   # old presets: any edit


def preset(name, kind, pace):
    return {"name": name, "for": kind, "copy": ["pacing"], "settings": {"pacing": {"pace": pace}}}


def test_variant_b_only_uses_presets_of_its_kind(tmp_path):
    prof = st.Profile(tmp_path / "p.json")
    ps = {"clip": preset("clip", "clips", "fast"), "edit": preset("edit", "edits", "slow")}
    for _ in range(5):
        lacy = st.pick_variants(prof, "lacy", random.Random(1), ps)
        tg = st.pick_variants(prof, "tokyo_girl", random.Random(1), ps)
        assert lacy[1]["preset"] == "clip"
        assert tg[1]["preset"] == "edit"
    only_clips = {"clip": ps["clip"]}
    assert st.pick_variants(prof, "trend", random.Random(1), only_clips)[1]["preset"] is None


def test_edit_and_combine_carry_the_kind(tmp_path):
    store = P.Presets(tmp_path)
    store.save(preset("a", "clips", "fast"))
    store.save(preset("b", "edits", "slow"))
    store.save(preset("c", "all", "slow"))
    assert store.edit("a", "for", "trailer")["for"] == "edits"
    assert store.combine("ab", {"pacing": "a", "colour": "b"})["for"] == "edits"
    store.edit("a", "for", "clips")
    assert store.combine("mixed", {"pacing": "a", "colour": "b"})["for"] == "all"
    assert store.combine("ac", {"pacing": "a", "colour": "c"})["for"] == "clips"
    assert "for stream clips" in P.summary(store.load("a"))


def analysis(facecam, on_beat=0.0, cps=0.1):
    return {"facecam": facecam, "duration": 20.0, "size": [1080, 1920], "detector": "ffmpeg",
            "pacing": {"cuts": 4, "avg_shot_s": 4.0, "cuts_per_s": cps, "shortest_s": 1.0, "longest_s": 6.0},
            "music": {"bpm": 120.0, "phase": 0.0, "on_beat": on_beat},
            "colour": {}, "text": {}, "motion": {}, "transitions": {"flash": 0, "fade": 0},
            "framing": {}, "structure": {"hook_s": 2.0, "payoff_s": None, "payoff": None,
                                         "ending": "none", "length_s": 20.0}}


def test_make_preset_guesses_unless_tim_says(monkeypatch):
    monkeypatch.setattr(I, "settings_from", lambda a: {"pacing": {"pace": "fast"}})
    assert I.make_preset("x", ["pacing"], analysis(True), {})["for"] == "clips"
    p = I.make_preset("x", ["pacing"], analysis(False, on_beat=0.8), {})
    assert p["for"] == "edits" and "music" in p["for_why"]
    p = I.make_preset("x", ["pacing"], analysis(True), {}, kind="edits")
    assert p["for"] == "edits" and p["for_why"] == "set by Tim"


def test_webcam_check_never_fails_the_analysis(monkeypatch, tmp_path):
    from spikebot import safety

    def boom(path):
        raise safety.Unavailable("no nudenet")
    monkeypatch.setattr(safety, "find_facecam", boom)
    assert I.webcam(tmp_path / "x.mp4") is None
    monkeypatch.setattr(safety, "find_facecam", lambda p: {"x": 0.7})
    assert I.webcam(tmp_path / "x.mp4") is True


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
