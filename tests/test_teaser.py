"""Stream clips' experimental opening (Tim, 19 Nov): the clip's big moment plays first while an
AI voice reads a hook, then the clip itself. Version C tries it unless /style pins `opening`."""
from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

from spikebot import editor
from spikebot import lacy as L
from spikebot import review as R
from spikebot import style as S
from spikebot import voice as V
from tests.test_lacy import T, fake_fetch, job_for, make_stream
from tests.test_review_modes import words

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


# ---- which version gets it ---------------------------------------------------------------
def test_only_the_experimental_version_opens_with_the_teaser(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    vs = S.pick_variants(prof, "lacy", random.Random(1))
    assert [v["style"]["opening"] for v in vs] == ["normal", "normal", "teaser"] and vs[2]["experimental"]
    assert "opening" not in S.pick_variants(prof, "tokyo_girl", random.Random(1))[2]["style"]
    assert S.resolve(vs[2]["style"])["opening"] == "teaser" and S.resolve({})["opening"] == "normal"
    # pinned with /style (or a rule): every version follows it
    prof.pin("opening", "normal")
    assert {v["style"]["opening"] for v in S.pick_variants(prof, "lacy", random.Random(2))} == {"normal"}
    prof.pin("opening", "teaser")
    assert {v["style"]["opening"] for v in S.pick_variants(prof, "lacy", random.Random(3))} == {"teaser"}
    assert S.valid_modes("opening", "teaser") == ["lacy"]
    from spikebot import ai
    assert "opening: normal, teaser" in ai.settings_menu()


# ---- pure helpers -------------------------------------------------------------------------
def test_teaser_length_fits_the_voice():
    assert editor.teaser_length(None) == editor.TEASER_MIN_S
    assert editor.teaser_length(3.0) == pytest.approx(3.0 + editor.VOICE_DELAY_S + 0.45)
    assert editor.teaser_length(9.0) == editor.TEASER_MAX_S


def test_teaser_window_finds_the_reaction_before_the_chat_peak():
    levels = [0.01] * 400                    # 40 s at 0.1 s
    for i in range(160, 175):                # a shout at 16.0-17.5 s; chat peaks at 20 s
        levels[i] = 0.5
    a, b = editor.teaser_window(levels, 20.0, 40.0, 3.0)
    assert b - a == pytest.approx(3.0) and a < 16.7 < b
    # a louder moment outside the reaction window (chat lag) is ignored
    levels[300:310] = [0.9] * 10
    assert editor.teaser_window(levels, 20.0, 40.0, 3.0) == (a, b)
    # no audio levels: just before the peak; never outside the clip
    a2, b2 = editor.teaser_window([], 20.0, 40.0, 3.0)
    assert a2 < 18.0 < b2
    assert editor.teaser_window([], 0.5, 40.0, 3.0)[0] == 0.0
    assert editor.teaser_window([], 39.9, 40.0, 3.0)[1] <= 40.0
    assert editor.teaser_window([], 5.0, 4.0, 3.0) == (1.0, 4.0)     # a short clip: its last 3 s
    assert editor.loudness([0.5] * 3200, rate=16000) == pytest.approx([0.5, 0.5])


def test_captions_move_after_the_teaser_and_it_shows_the_voice_line():
    r = S.resolve({"text_level": "full"})
    plain = editor.build_ass(words(6), "Lacy hook", 10.0, 600, style=r)
    led = editor.build_ass(words(6), "Lacy hook", 10.0, 600, style=r, lead=3.0,
                           lead_text="Lacy thought this was easy")
    assert "Dialogue: 1,0:00:00.00,0:00:03.00,Hook,,0,0,0,,Lacy thought this was easy" in led
    assert "Dialogue: 1,0:00:03.00,0:00:13.00,Hook,,0,0,0,,Lacy hook" in led
    first = lambda ass: next(x for x in ass.splitlines() if ",Cap," in x).split(",")[1]  # noqa: E731
    assert first(plain) == "0:00:00.30" and first(led) == "0:00:03.30"
    m = editor.build_ass(words(6), "Lacy hook", 10.0, 600, style=S.resolve({"text_level": "minimal"}), lead=3.0)
    assert f"0:00:03.00,0:00:0{3 + editor.MINIMAL_HOOK_S:.2f},Hook" in m


def test_voice_line_rules_and_spoken_text():
    assert L.voice_line_ok("Lacy thought this heist was easy... it wasn't.")
    assert not L.voice_line_ok("Lacy fails")                         # too short
    assert not L.voice_line_ok("He thought this heist was easy but it was not")   # no name
    assert not L.voice_line_ok("Lacy gets roasted by chat after this terrible drive")  # banned words
    assert V.spoken("Lacy in GTA 6 🔥 #gta6") == "Lacy in GTA six"
    assert V.spoken("GTA V vs GTA VI") == "GTA five vs GTA six"


def test_speak_falls_back_between_engines_and_never_raises(tmp_path, monkeypatch):
    calls = []

    def broken(text, out, s):
        calls.append("edge")
        raise V.VoiceError("blocked")

    def good(text, out, s):
        calls.append(("google", text))
        out.write_bytes(b"x" * 2000)
        return out
    out = V.speak("Lacy in GTA 6", tmp_path / "vo.mp3", {}, engines={"edge": broken, "google": good})
    assert out == tmp_path / "vo.mp3" and calls == ["edge", ("google", "Lacy in GTA six")]
    assert V.speak("x", tmp_path / "v2.mp3", {}, engines={"edge": broken, "google": broken}) is None
    assert not (tmp_path / "v2.mp3").exists()
    assert V.settings({"clips": {"voice": {"edge_voice": "en-GB-RyanNeural"}}})["edge_voice"] == "en-GB-RyanNeural"
    monkeypatch.setenv("SPIKEBOT_VOICE", "off")      # tests and render subprocesses: never a real service
    assert V.speak("Lacy in GTA 6", tmp_path / "v3.mp3", {}) is None


# ---- end to end ---------------------------------------------------------------------------
def tone(text, out, s):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=330:duration=1.6", "-ac", "1", str(out)], check=True)
    return out


@needs_ffmpeg
def test_version_c_renders_the_teaser_with_the_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "ENGINES", {"edge": tone})
    monkeypatch.delenv("SPIKEBOT_VOICE")
    src = tmp_path / "stream.mp4"
    make_stream(src, seconds=12)
    jobs, lcfg = L.Jobs(tmp_path), L.settings({})
    store = R.Store(tmp_path)
    job = L.render_job(job_for(tmp_path, jobs, 1), lcfg, jobs, set(), fetch=fake_fetch(src),
                       transcribe=lambda p, m: (words(14), "en", 0.97), now=T, review_store=store,
                       cfg={"data_dir": str(tmp_path)}, base=tmp_path)
    assert job["status"] == "review", job.get("reason")
    item = store.load(job["review_item"])
    a, b, c = item["rounds"][0]["variants"]
    assert a["teaser"] is None and b["teaser"] is None
    assert c["teaser"]["voice"] is True and L.title_ok(c["teaser"]["line"])   # no AI key: the title
    assert c["teaser"]["s"] == editor.teaser_length(1.6)
    info = editor.probe(store.dir(item["id"]) / c["file"])
    assert info["audio"] and info["duration"] == pytest.approx(c["duration"], abs=0.25)
    assert not list(store.dir(item["id"]).glob("*_vo.mp3"))     # the voice file is not kept
    assert len(list(store.dir(item["id"]).glob("r1_*.mp4"))) >= 3
    from spikebot import discord_bot as DB
    cols = DB.round_payload(item, item["rounds"][0])["embeds"][0]["fields"]
    assert "🎙️ teaser" in cols[2]["value"] and "teaser" not in cols[0]["value"]
    # a Feedback re-render of C keeps the teaser and its line
    with store.edit(item["id"]) as it:
        R.request(it, "feedback", round=1, label="C", notes="bigger text")
    item = R.render_round(store, item["id"], {"data_dir": str(tmp_path)}, tmp_path, now=T + 60,
                          adapter=L.ReviewAdapter({"data_dir": str(tmp_path)}, tmp_path, lcfg, jobs, set()))
    c2 = item["rounds"][1]["variants"][0]
    assert c2["label"] == "C2" and c2["teaser"]["line"] == c["teaser"]["line"]
