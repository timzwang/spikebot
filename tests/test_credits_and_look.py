"""Tim, 24 Nov: API credits, dark stream clips, text that blends in, images in trailer edits,
captions that don't match the footage, comparison fades."""
from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from spikebot import ai, editor, messages, shot_labels
from spikebot import style as S
from spikebot import tokyo_girl as TG


class FakeClaude:
    def __init__(self, answer: dict, model: str | None = None, usage: dict | None = None):
        self.answer, self.model, self.usage, self.requests = answer, model, usage, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        return SimpleNamespace(stop_reason="end_turn", model=self.model or kw["model"],
                               usage=SimpleNamespace(**(self.usage or {"input_tokens": 1000, "output_tokens": 200})),
                               content=[SimpleNamespace(type="text", text=json.dumps(self.answer))])


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    for var in ("SPIKEBOT_AI_MODEL", "SPIKEBOT_FEEDBACK_MODEL", "SPIKEBOT_COPY_MODEL", "SPIKEBOT_CHEAP_MODEL",
                "SPIKEBOT_AI_DAILY_USD"):
        monkeypatch.delenv(var, raising=False)


# ---- API spend ------------------------------------------------------------------------
def test_haiku_tasks_skip_the_fallback_params_and_sonnet_keeps_them(tmp_path):
    c = FakeClaude({"ok": 1})
    ai.ask_json("sys", "x", {"type": "object"}, base=tmp_path, client=c, task="impact")
    assert c.requests[0]["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in c.requests[0] and "betas" not in c.requests[0]
    ai.ask_json("sys", "x", {"type": "object"}, base=tmp_path, client=c, task="copy")
    assert c.requests[1]["model"] == "claude-sonnet-5-5" and c.requests[1]["fallbacks"] == "default"


def test_every_request_is_logged_with_its_cost(tmp_path):
    (tmp_path / ".env").write_text("SPIKEBOT_AI_DAILY_USD=10\n")
    c = FakeClaude({"ok": 1}, usage={"input_tokens": 1_000_000, "output_tokens": 100_000})
    ai.ask_json("sys", "x", {"type": "object"}, base=tmp_path, client=c, task="copy")
    ai.ask_json("sys", "x", {"type": "object"}, base=tmp_path, client=c, task="impact")
    day = ai.usage_on(tmp_path)
    assert day["tasks"]["copy"]["calls"] == 1 and day["tasks"]["copy"]["usd"] == pytest.approx(3.0)   # $2 + $1
    assert day["tasks"]["impact"]["usd"] == pytest.approx(0.15)                                       # Haiku
    assert day["usd"] == pytest.approx(3.15)
    line = ai.usage_line(tmp_path)
    assert line.startswith("🤖 **Claude API**: $3.15 today of $10.00 budget") and "titles 1× $3.00" in line
    assert (tmp_path / "data" / "ai_usage.json").exists()


def test_daily_budget_stops_background_first_and_never_blocks_tim(tmp_path):
    (tmp_path / ".env").write_text("SPIKEBOT_AI_DAILY_USD=1.00\n")
    assert ai.daily_budget(tmp_path) == 1.0
    ai.record(tmp_path, "copy", "claude-sonnet-5-5", SimpleNamespace(input_tokens=300_000, output_tokens=0))  # $0.60
    with pytest.raises(ai.AIError, match="budget"):
        ai.check_budget(tmp_path, "labels")        # background work stops at half
    with pytest.raises(ai.AIError):
        ai.ask_json("s", "x", {}, base=tmp_path, client=FakeClaude({}), task="rules")
    ai.check_budget(tmp_path, "impact")            # clip work still runs
    ai.record(tmp_path, "copy", "claude-sonnet-5-5", SimpleNamespace(input_tokens=250_000, output_tokens=0))
    with pytest.raises(ai.AIError):
        ai.check_budget(tmp_path, "copy")
    ai.check_budget(tmp_path, "feedback")          # Tim's own requests always run
    ai.check_budget(tmp_path, "study")
    ai.check_budget(None, "labels")                # no config dir: no ledger, no limit


def test_budget_from_config_json_and_zero_means_no_limit(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"ai": {"daily_usd": 0}}))
    assert ai.daily_budget(tmp_path) == 0.0
    ai.record(tmp_path, "labels", "claude-haiku-5-5", SimpleNamespace(input_tokens=10**9, output_tokens=0))
    ai.check_budget(tmp_path, "labels")


def test_daily_summary_shows_the_ai_spend():
    out = messages.summary([], [], 1_800_000_000.0, ai_line="🤖 **Claude API**: $0.21 today")
    assert "$0.21 today" in out["embeds"][0]["description"]


def test_shot_labels_keep_answered_batches_and_ask_only_the_missing_once(tmp_path, monkeypatch):
    entry = {"kind": "video", "duration": 40.0, "cuts": [float(i) for i in range(1, 40)]}   # 40 shots
    monkeypatch.setattr(shot_labels, "tile", lambda path, a, b, out: b"jpg")
    calls = []

    def ask(content):
        n = sum(1 for c in content if c.get("type") == "image")
        calls.append(n)
        skip = {3} if len(calls) == 1 else set()         # the first answer leaves one shot out
        return {"shots": [{"i": j, "tags": ["night"], "action": 0.2, "calm": 0.5, "impact_end": 0.1,
                           "subject_x": 0.5, "subject_w": 0.3} for j in range(n) if j not in skip]}
    rows = shot_labels.label_file(tmp_path / "x.mp4", entry, ask=ask, workers=1)
    assert len(rows) == 40 and calls == [16, 16, 8, 1]   # only the missing shot is asked again
    assert all(r[2]["tags"] == ["night"] for r in rows)


def test_a_few_unanswered_shots_get_neutral_labels(tmp_path, monkeypatch):
    entry = {"kind": "video", "duration": 40.0, "cuts": [float(i) for i in range(1, 40)]}
    monkeypatch.setattr(shot_labels, "tile", lambda path, a, b, out: b"jpg")

    def ask(content):   # shot 0 of every batch is never answered
        n = sum(1 for c in content if c.get("type") == "image")
        return {"shots": [{"i": j, "tags": [], "action": 0, "calm": 0, "impact_end": 0, "subject_x": 0.5,
                           "subject_w": 0.3} for j in range(1, n)]}
    rows = shot_labels.label_file(tmp_path / "x.mp4", entry, ask=ask, workers=1)
    missing = [r for r in rows if r[2].get("missing")]
    assert 0 < len(missing) <= 4
    entry["labels"] = {"v": shot_labels.VERSION, "shots": rows}
    a, b, _ = missing[0]
    assert shot_labels.for_range(entry, a, b) is None   # treated as unlabelled


# ---- stream clips: never too dark ----------------------------------------------------------
def test_stream_grade_never_darkens_and_lifts_dark_footage():
    moody = S.resolve({"grade": "moody", "custom": {"brightness": -0.2, "contrast": 1.5}})["grade"]
    g = S.stream_grade(moody, 0.5)
    assert g["brightness"] == 0.0 and g["contrast"] == S.STREAM_MAX_CONTRAST and "gamma" not in g
    dark = S.stream_grade(S.GRADES["natural"], 0.12)
    assert 1.4 < dark["gamma"] <= S.STREAM_MAX_GAMMA
    assert ":gamma=" in S.grade_filter(dark)
    assert "gamma" not in S.stream_grade(S.GRADES["natural"], None)
    assert "vignette" not in S.look_filter({"vignette": True}, "lacy")
    assert "vignette" in S.look_filter({"vignette": True}, "tokyo_girl")


def test_stream_clips_never_explore_dark_grades_but_pins_still_work(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    for seed in range(12):
        for v in S.pick_variants(prof, "lacy", random.Random(seed)):
            assert v["style"]["grade"] in S.STREAM_GRADES
    prof.pin("grade", "moody")
    assert prof.pinned("lacy")["grade"] == "moody"


def test_mean_luma_reads_dark_and_bright(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    for name, colour in (("dark", "0x202020"), ("bright", "0xC0C0C0")):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c={colour}:s=160x90:d=2", "-pix_fmt", "yuv420p", str(tmp_path / f"{name}.mp4")],
                       check=True)
    assert editor.mean_luma(tmp_path / "dark.mp4") < 0.2 < 0.6 < editor.mean_luma(tmp_path / "bright.mp4")


# ---- text over the picture gets a background -------------------------------------------------
def _style_line(ass: str, name: str) -> list[str]:
    line = next(x for x in ass.splitlines() if x.startswith(f"Style: {name},"))
    return line.split(",")


def test_stream_captions_over_gameplay_sit_on_a_box():
    words = [editor.Word(0.0, 0.4, "this"), editor.Word(0.4, 0.8, "is"), editor.Word(0.8, 1.2, "wild")]
    cap = _style_line(editor.build_ass(words, "Lacy hook", 3.0, 640, style=S.resolve({})), "Cap")
    assert cap[15] == "3" and cap[6] == editor.box_colour(editor.TEXT_BOX)   # BorderStyle 3 = box
    off = _style_line(editor.build_ass(words, "Lacy hook", 3.0, 640, style=S.resolve({"custom": {"text_box": 0}})),
                      "Cap")
    assert off[15] == "1"
    band = _style_line(editor.build_ass(words, "Lacy hook", 3.0, 0, style=S.resolve({}), frame_h=1350,
                                        picture=(200, 1100)), "Cap")
    assert band[15] == "1"   # on the black band below the picture: just the outline


def test_trailer_cards_over_the_picture_sit_on_a_box():
    cards = [(0.0, 2.0, "Vice City at *night*")]
    over = _style_line(TG.build_text_ass(cards, "x", 80, dict(S.resolve({}), framing="box45")), "Card")
    assert over[15] == "3"
    band = _style_line(TG.build_text_ass(cards, "x", 80, dict(S.resolve({}), framing="stack", position="high")),
                       "Card")
    assert band[15] == "1"
    assert editor.box_colour(1.0) == "&H00000000" and editor.box_colour(0.0) == "&HFF000000"


# ---- trailer edits: video only, captions that match the footage -------------------------------
def test_trailer_edits_never_use_images():
    assert S.resolve({"media": "mixed"})["media"] == "clips"
    assert S.dims("tokyo_girl")["media"] == ("clips",)


def shot(i: int, vt: list[str]) -> TG.Shot:
    return TG.Shot(f"t{i}.mp4", "video", 0.0, 20.0, ["city"], labels={"tags": vt})


def test_night_line_only_with_night_footage():
    story = TG.STORIES[0]
    today = date(2026, 11, 24)
    night = [shot(i, ["night", "city"]) for i in range(8)]
    day = [shot(i + 10, ["beach"]) for i in range(8)]
    plan_n = TG.Plan(story, [], [TG.Slot(s, 0.0, 2, i * 2.0, 2.0, 0, False, 1) for i, s in enumerate(night[:6])], 12.0)
    plan_d = TG.Plan(story, [], [TG.Slot(s, 0.0, 2, i * 2.0, 2.0, 0, False, 1) for i, s in enumerate(day[:6])], 12.0)
    assert TG.line_for_footage("vice city at night", plan_n, story, today, "A") == "vice city at night"
    other = TG.line_for_footage("vice city at night", plan_d, story, today, "A")
    assert "night" not in other and other in [TG.fill(x, today) for x in TG._line_pool(today)]
    # a library without night clips never gets the night line
    for salt in "ABCDEFGH":
        line = TG.pick_line(story, today, salt, day)
        assert "night" not in TG.line_tags(TG._raw_of(line, today) or "")


def test_the_line_steers_footage_choice():
    pool = [shot(i, ["beach"]) for i in range(10)] + [shot(20 + i, ["night"]) for i in range(10)]
    prefs = TG.Prefs(theme={"night"})
    got = [TG.choose_shot(pool, "city", 2.0, [], {}, random.Random(i), prefs).labels["tags"] for i in range(10)]
    assert all(t == ["night"] for t in got)


# ---- one-off settings requests (Tim, 25 Nov: 4 clips a streamer, stream clips first) ---------------
def test_config_request_writes_whitelisted_settings_once(tmp_path):
    import os
    from spikebot import main as M
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"discord_webhook_url": "secret", "clips": {"max_renders_per_day": 10},
                                    "tokyo_girl": {"per_day": 4}}))
    os.chmod(cfg_path, 0o600)
    (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=x\nTOKYO_GIRL_PER_DAY=4\n")
    reqs = tmp_path / "reqs.json"
    reqs.write_text(json.dumps([
        {"id": "c1", "action": "config", "set": {"clips.max_renders_per_day": 4, "tokyo_girl.per_day": 1,
                                                 "review.trend.times": ["18:40"], "discord_webhook_url": "evil",
                                                 "impact.min_score": 99}},
        {"id": "old", "action": "config", "until": "2026-01-01T00:00:00+00:00", "set": {"ai.daily_usd": 5}},
        {"id": "t1", "action": "trend"}]))
    data = tmp_path / "data"
    got = M.apply_config_requests(cfg_path, data, reqs, now=1_800_000_000.0)
    assert got == ['clips.max_renders_per_day=4', 'tokyo_girl.per_day=1', 'review.trend.times=["18:40"]']
    cfg = json.loads(cfg_path.read_text())
    assert cfg["clips"]["max_renders_per_day"] == 4 and cfg["tokyo_girl"]["per_day"] == 1
    assert cfg["review"]["trend"]["times"] == ["18:40"]
    assert cfg["discord_webhook_url"] == "secret" and "impact" not in cfg       # not whitelisted / out of range
    assert "ai" not in cfg                                                      # expired request
    assert oct(cfg_path.stat().st_mode & 0o777) == "0o600"
    env = (tmp_path / ".env").read_text()
    assert "TOKYO_GIRL_PER_DAY=1" in env and "DISCORD_BOT_TOKEN=x" in env       # .env wins over config: updated
    assert sorted(json.loads((data / "deploy_requests.json").read_text())) == ["c1", "old"]   # trend left for the bot
    assert M.apply_config_requests(cfg_path, data, reqs, now=1_800_000_000.0) == []         # once only


def test_shipped_requests_and_new_defaults():
    from spikebot import lacy as L, main as M, review as R
    reqs = json.loads((Path(M.__file__).with_name("deploy_requests.json")).read_text())
    for r in (r for r in reqs if r.get("action") == "config"):
        for k, v in r["set"].items():
            M.config_value(k, v)   # every shipped value passes the whitelist
    assert L.DEFAULTS["max_renders_per_day"] == 4
    assert TG.DEFAULTS["per_day"] == 1 and TG.DEFAULTS["compare_per_day"] == 0
    assert R.DEFAULTS["trend"]["times"] == ["18:40"]


def test_board_lists_stream_clips_before_edits():
    from spikebot import discord_bot as DB
    rnd = {"n": 1, "status": "posted", "variants": [{"label": "A"}]}
    items = [{"id": "tg", "mode": "tokyo_girl", "status": "open", "expires": 100, "rounds": [rnd]},
             {"id": "clip", "mode": "lacy", "status": "open", "expires": 500, "rounds": [rnd]}]
    assert [it["id"] for it in DB.board_items(items)][0] == "clip"
