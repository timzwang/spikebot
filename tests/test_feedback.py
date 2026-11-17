"""/feedback rules, text levels, the logo intro and the artistic #inspiration read.
The Claude API is replaced by a fake client; ffmpeg tests need ffmpeg."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from spikebot import ai, editor
from spikebot import feedback as fb
from spikebot import inspiration as I
from spikebot import presets as P
from spikebot import style as S
from spikebot import tokyo_girl as TG

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


class FakeClaude:
    """Stands in for anthropic.Anthropic(): returns the given JSON, records requests."""

    def __init__(self, *answers: dict, stop: str = "end_turn"):
        self.answers, self.stop, self.requests = list(answers), stop, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        text = json.dumps(self.answers.pop(0))
        return SimpleNamespace(stop_reason=self.stop, content=[SimpleNamespace(type="text", text=text)])


def answer(settings=(), notes=(), unsupported=(), summary="ok", hook_text=None) -> dict:
    return {"summary": summary, "settings": list(settings), "notes": list(notes),
            "unsupported": list(unsupported), "hook_text": hook_text}


def row(setting, value, mode="all", scope="always"):
    return {"mode": mode, "setting": setting, "value": value, "scope": scope}


# ---- the API wrapper ----------------------------------------------------------------
def test_ask_json_sends_structured_request_and_handles_refusal():
    c = FakeClaude({"a": 1})
    assert ai.ask_json("sys", "hi", {"type": "object"}, client=c) == {"a": 1}
    req = c.requests[0]
    assert req["model"] == ai.MODEL and req["output_config"]["format"]["type"] == "json_schema"
    assert req["fallbacks"] == "default" and ai.FALLBACK_BETA in req["betas"]
    with pytest.raises(ai.AIError):
        ai.ask_json("sys", "hi", {}, client=FakeClaude({"a": 1}, stop="refusal"))


def test_no_key_means_not_available(tmp_path, monkeypatch):
    assert not ai.available(tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-test\nSPIKEBOT_AI_MODEL=claude-sonnet-5-5\n")
    assert ai.api_key(tmp_path) == "sk-test" and ai.model_name(tmp_path) == "claude-sonnet-5-5"


def test_feedback_uses_sonnet_and_study_uses_opus(tmp_path, monkeypatch):
    monkeypatch.delenv("SPIKEBOT_AI_MODEL", raising=False)
    monkeypatch.delenv("SPIKEBOT_FEEDBACK_MODEL", raising=False)
    assert ai.model_name(tmp_path, "feedback") == "claude-sonnet-5-5"
    assert ai.model_name(tmp_path) == "claude-opus-5-5"
    c = FakeClaude(answer([row("grade", "warm")]))
    ai.interpret_feedback("warmer please", "lacy", base=tmp_path, client=c)
    assert c.requests[0]["model"] == "claude-sonnet-5-5"
    (tmp_path / ".env").write_text("SPIKEBOT_FEEDBACK_MODEL=claude-haiku-5-5\n")
    assert ai.model_name(tmp_path, "feedback") == "claude-haiku-5-5"


# ---- /feedback ------------------------------------------------------------------------
def test_feedback_with_claude_pins_settings_for_every_future_variant(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    c = FakeClaude(answer([row("text_level", "none"), row("intro", "logo", mode="tokyo_girl"),
                           row("grain", "8"), row("transition", "cut"), row("size", "huge")],
                          notes=["Let the gameplay carry the clip"],
                          unsupported=["licensed music in the file"], summary="Clean edits, no captions"))
    rule, problems = fb.learn("the captions are AI slop, if there's no need for a caption don't add one. "
                              "Open trailer edits on the GTA 6 logo", "all", prof, tmp_path, now=1, client=c)
    assert rule["engine"] == "claude" and [e["n"] for e in rule["entries"]] == [1, 2, 3, 4, 5, 6]
    assert any("size" in p for p in problems)                    # invalid value dropped, not pinned
    prof.save()
    prof = S.Profile(tmp_path / "p.json")
    for mode in ("lacy", "tokyo_girl"):
        vs = S.pick_variants(prof, mode, random.Random(1))
        assert {v["style"]["text_level"] for v in vs} == {"none"}
        assert {v["style"]["transition"] for v in vs} == {"cut"}
        assert all(v["style"]["custom"]["grain"] == 8.0 for v in vs)
    assert {v["style"]["intro"] for v in S.pick_variants(prof, "tokyo_girl", random.Random(2))} == {"logo"}
    assert "intro" not in S.pick_variants(prof, "lacy", random.Random(2))[0]["style"]
    assert "Let the gameplay carry the clip" in prof.taste("lacy")
    text = fb.format_rule(rule)
    assert "**#1** text_level = none" in text and "intro = logo (Tokyo Girl)" in text
    assert "licensed music" in text and "not possible yet" in text and "can't apply this yet" in text
    # the next request carries what was learned
    c2 = FakeClaude(answer())
    fb.learn("slower please", "lacy", prof, tmp_path, client=c2)
    assert "- text_level = none" in c2.requests[0]["messages"][0]["content"]


def test_disabling_an_entry_falls_back_to_the_one_before(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    prof.pin("grade", "moody")                                      # #1
    fb.learn("x", "all", prof, client=FakeClaude(answer([row("grade", "warm"), row("vignette", "1")])))
    assert prof.pinned("lacy")["grade"] == "warm" and prof.custom_pins("lacy")["vignette"] == 1.0
    assert prof.set_enabled([2, 3], False) == ([2, 3], [])
    assert prof.pinned("lacy")["grade"] == "moody" and "vignette" not in prof.custom_pins("lacy")
    assert prof.set_enabled([2, 99], True) == ([2], [99])
    assert prof.pinned("lacy")["grade"] == "warm"


def test_auto_clears_a_pin(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    prof.pin("pace", "fast")
    fb.learn("let it learn the pace again", "all", prof, client=FakeClaude(answer([row("pace", "auto")])))
    assert "pace" not in prof.pinned("lacy")


def test_feedback_without_a_key_uses_the_rule_parser(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    rule, _ = fb.learn("Captions are AI slop with no meaning. Put the GTA 6 logo at the start", "tokyo_girl",
                       prof, tmp_path)
    assert rule["engine"] == "rules"
    vs = S.pick_variants(prof, "tokyo_girl", random.Random(0))
    assert {v["style"]["text_level"] for v in vs} == {"none"} and {v["style"]["intro"] for v in vs} == {"logo"}


def test_newest_instruction_wins_whatever_its_mode(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    prof.pin("text_level", "full")
    prof.pin("text_level:lacy", "minimal")                          # newer, Lacy only
    assert prof.pinned("lacy")["text_level"] == "minimal" and prof.pinned("tokyo_girl")["text_level"] == "full"
    prof.pin("text_level", "none")                                  # newer, every mode: wins in Lacy too
    assert prof.pinned("lacy")["text_level"] == "none" and prof.pinned("tokyo_girl")["text_level"] == "none"
    prof.add_rule("lacy sat", "", "lacy", [{"mode": "lacy", "setting": "saturation", "value": 1.4}], [], [])
    prof.add_rule("all sat", "", "all", [{"mode": "all", "setting": "saturation", "value": 1.0}], [], [])
    assert prof.custom_pins("lacy")["saturation"] == 1.0
    prof.set_enabled([3], False)                                    # the "none for all" one
    assert prof.pinned("lacy")["text_level"] == "minimal" and prof.pinned("tokyo_girl")["text_level"] == "full"


def test_old_profiles_are_migrated_to_the_numbered_list(tmp_path):
    import json
    old = {"version": 1, "pinned": {"font": "roboto", "text_level:lacy": "full", "text_level": "none"},
           "custom_pins": {"lacy": {"saturation": 1.4}},
           "rules": [{"id": 1, "ts": 5, "mode": "lacy", "text": "caption everything", "summary": "",
                      "settings": [{"mode": "lacy", "setting": "text_level", "value": "full"}],
                      "notes": [], "unsupported": []},
                     {"id": 2, "ts": 9, "mode": "all", "text": "no text at all", "summary": "",
                      "settings": [{"mode": "all", "setting": "text_level", "value": "none"}],
                      "notes": ["Payoff in the first 3 seconds"], "unsupported": ["licensed music"]}]}
    (tmp_path / "p.json").write_text(json.dumps(old))
    prof = S.Profile(tmp_path / "p.json")
    es = prof.entries()
    assert [(e["n"], e["kind"], e.get("setting")) for e in es] == [
        (1, "setting", "font"), (2, "setting", "saturation"), (3, "setting", "text_level"),
        (4, "setting", "text_level"), (5, "note", None), (6, "unsupported", None)]
    assert prof.pinned("lacy")["text_level"] == "none"              # the newer rule wins now
    assert prof.pinned("lacy")["font"] == "roboto" and prof.custom_pins("lacy")["saturation"] == 1.4
    assert "Payoff in the first 3 seconds" in prof.taste("lacy")


def test_review_feedback_this_edit_vs_always(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    style = S.default_style("lacy")
    c = FakeClaude(answer([row("grade", "moody", scope="this_edit"), row("text_level", "none", mode="lacy")],
                          hook_text="Lacy had no idea what was coming"))
    res = fb.for_variant("darker for this one, and never caption his clips", "lacy", style, prof, tmp_path,
                         item_id="abc", client=c)
    assert res["style"]["grade"] == "moody" and res["style"]["text_level"] == "none"
    assert res["texts"] == {"hook": "Lacy had no idea what was coming"}
    assert res["rule"]["settings"] == [{"mode": "lacy", "setting": "text_level", "value": "none"}]
    assert "grade" not in prof.pinned("lacy") and prof.pinned("lacy")["text_level"] == "none"
    assert "Saved to /rules as #1" in res["comment"] and "only for this edit" in res["comment"]


def test_review_feedback_is_kept_unless_it_says_only_this_one(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    style = S.default_style("lacy") | {"text_level": "full"}
    res = fb.for_variant("no captions", "lacy", style, prof, tmp_path, item_id="a")     # rule parser, no key
    assert res["style"]["text_level"] == "none" and prof.pinned("lacy").get("text_level") == "none"
    res = fb.for_variant("full captions, just this one", "lacy", res["style"], prof, tmp_path, item_id="b")
    assert prof.pinned("lacy").get("text_level") == "none"          # not saved


# ---- text levels ----------------------------------------------------------------------
WORDS = [editor.Word(i * 0.5, i * 0.5 + 0.4, f"w{i}") for i in range(40)]


def dialogue(ass: str) -> list[str]:
    return [line for line in ass.splitlines() if line.startswith("Dialogue")]


def test_lacy_text_levels():
    full = S.resolve(S.default_style("lacy") | {"text_level": "full"})
    mini = S.resolve(S.default_style("lacy") | {"text_level": "minimal"})
    none = S.resolve(S.default_style("lacy") | {"text_level": "none"})
    d_full = dialogue(editor.build_ass(WORDS, "Lacy hook", 20, 600, style=full))
    d_mini = dialogue(editor.build_ass(WORDS, "Lacy hook", 20, 600, style=mini, focus=editor.caption_focus(12.0)))
    assert dialogue(editor.build_ass(WORDS, "Lacy hook", 20, 600, style=none)) == []
    assert ",0:00:20.00,Hook," in d_full[0] and ",0:00:02.50,Hook," in d_mini[0]
    caps = [d for d in d_mini if ",Cap," in d]
    assert caps and all(7.0 <= float(d.split(",")[1].split(":")[2]) <= 14.5 for d in caps)
    assert len(caps) < len([d for d in d_full if ",Cap," in d])
    no_peak = dialogue(editor.build_ass(WORDS, "Lacy hook", 20, 600, style=mini, focus=None))
    assert [d for d in no_peak if ",Cap," in d] == []


def test_tokyo_girl_card_levels():
    cards = [(0.0, 4.0, "a"), (4.0, 8.0, "b"), (8.0, 14.0, "c?")]
    assert TG.cards_for_level(cards, "full") == cards
    assert TG.cards_for_level(cards, "minimal") == [(0.0, 4.0, "a")]
    assert TG.cards_for_level(cards, "none") == []
    assert TG.cards_for_level(cards, "minimal", intro_s=2.0) == [(2.0, 4.0, "a")]
    assert TG.cards_for_level(cards, "minimal", intro_s=3.5) == []   # too short to read: dropped


def test_look_filter():
    assert S.look_filter({}, "lacy") == ""
    f = S.look_filter({"grain": 8, "vignette": True, "letterbox": True}, "tokyo_girl")
    assert "noise=alls=8" in f and "vignette" in f and f.count("drawbox") == 2
    assert "drawbox" not in S.look_filter({"letterbox": True}, "lacy")


@needs_ffmpeg
def test_logo_intro_and_no_text_render(tmp_path):
    media = tmp_path / "data/rockstar_media"
    media.mkdir(parents=True)
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    subprocess.run(ff + ["-f", "lavfi", "-i", "gradients=s=1920x1080:seed=3", "-frames:v", "1",
                         str(media / "vice_city_beach.jpg")], check=True)
    logo = tmp_path / "data/brand/gta6_logo.png"
    logo.parent.mkdir(parents=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=0xff3399@1.0:s=800x500,format=rgba", "-frames:v", "1",
                         str(logo)], check=True)
    s = TG.settings({"tokyo_girl": {"min_s": 15, "max_s": 16}}, tmp_path)
    assert Path(s["logo_file"]) == logo
    shots = TG.Library(s).shots()
    story = next(x for x in TG.STORIES if x.id == "partners")
    import datetime as dt
    plan = TG.plan_clip(story, shots, s, dt.date(2026, 11, 17), random.Random(1))
    out = tmp_path / "out.mp4"
    r = S.resolve(S.default_style("tokyo_girl") | {"text_level": "none", "intro": "logo",
                                                   "custom": {"grain": 6, "letterbox": 1}})
    TG.render_plan(plan, s, out, S.grade_filter(r["grade"]), r, cards=TG.cards_for_level(plan.cards, "none"),
                   logo=logo, intro_s=TG.bar_s(s["bpm"]), look=S.look_filter(r["look"], "tokyo_girl"))
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                                   "-show_entries", "format=duration", "-of", "json", str(out)],
                                  capture_output=True, text=True, check=True).stdout)
    assert [x["codec_type"] for x in j["streams"]] == ["video"]
    assert float(j["format"]["duration"]) == pytest.approx(plan.duration, abs=0.1)

    def pixel(t, x, y):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1",
                              "-vf", f"crop=2:2:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        return tuple(raw[:3])
    r0, g0, b0 = pixel(1.0, 540, 960)       # logo (pink) in the middle during the intro
    assert r0 > 150 and g0 < 120 and b0 > 80
    assert pixel(1.0, 540, 20) == pytest.approx((0, 0, 0), abs=12)   # cinema bar
    r1, g1, b1 = pixel(5.0, 540, 960)       # gone afterwards
    assert not (r1 > 150 and g1 < 120 and b1 > 80 and abs(r1 - 255) < 30)


# ---- #inspiration: the artistic read ---------------------------------------------------
ART = {"summary": "Calm open, one hard drop, no text until the end",
       "what_makes_it_good": ["Opens on a slow logo reveal before any cut", "Text appears once, on the drop"],
       "techniques": [{"name": "logo reveal", "how": "logo fades in over dark footage", "can_copy": True},
                      {"name": "speed ramp", "how": "slows into the drop", "can_copy": False}],
       "text_use": "Almost none: the footage carries it", "opening": "Black, then the logo fades up",
       "settings": [row("text_level", "minimal"), row("intro", "logo", mode="tokyo_girl"),
                    row("letterbox", "1"), row("grade", "moody")],
       "avoid": ["Busy captions"]}


@needs_ffmpeg
def test_inspiration_adds_artistic_notes(tmp_path):
    from tests.test_inspiration import ref_video
    ref = tmp_path / "upload.mp4"
    ref_video(ref)
    out = tmp_path / "out"
    out.mkdir()
    c = FakeClaude(ART)
    res = I.run("ref", list(P.PARTS), tmp_path, out, file=ref, reader=lambda img: [],
                note="love how little text there is", ai_client=c)
    assert res["ok"] and not ref.exists()
    content = c.requests[0]["messages"][0]["content"]
    assert 4 <= sum(1 for b in content if b["type"] == "image") <= 10
    assert any("love how little text" in b.get("text", "") for b in content)
    p = P.Presets(tmp_path / "presets").load("ref")
    assert p["settings"]["look"] == {"text_level": "minimal", "intro": "logo", "letterbox": 1.0}
    assert p["art"]["what_makes_it_good"][0].startswith("Opens on a slow logo")
    assert "Why it works" in res["art"] and "not in our editor yet" in res["art"] and len(res["art"]) < 2000
    sty = P.to_style(p, "tokyo_girl")
    assert sty["text_level"] == "minimal" and sty["intro"] == "logo" and sty["custom"]["letterbox"] == 1.0
    assert "intro" not in P.to_style(p, "lacy")
    assert "★ Opens on a slow logo" in P.summary(p)


@needs_ffmpeg
def test_inspiration_without_key_still_works(tmp_path):
    from tests.test_inspiration import ref_video
    ref = tmp_path / "upload.mp4"
    ref_video(ref, 3)
    out = tmp_path / "out"
    out.mkdir()
    res = I.run("ref", list(P.PARTS), tmp_path, out, file=ref, reader=lambda img: [], base=tmp_path)
    assert res["ok"] and res["art"] is None
