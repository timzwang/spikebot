"""4:5 layouts, TikTok-style static captions (Tim, 20 Nov) and the #ready-to-post board."""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from spikebot import editor as E
from spikebot import discord_bot as D
from spikebot import tokyo_girl as TG

W = E.Word


def test_no_facecam_is_a_whole_frame_4x5():
    lay = E.layout(1920, 1080, None, src_fps=60)
    assert lay.cam is None and lay.out_h == 1350 and lay.game == (1920, 1080, 0, 0)
    assert lay.game_h == 608 and lay.picture == (lay.top, lay.top + 608)
    assert "pad=1080:1350" in lay.filter() and "vstack" not in lay.filter()
    cam = E.layout(1920, 1080, {"x": 0, "y": 0, "w": 0.25, "h": 0.3})
    assert cam.out_h == 1920 and cam.cam_h == 640 and cam.picture is None


def test_captions_static_tiktok_style_and_held_longer():
    words = [W(0.0, 0.4, "this"), W(0.4, 0.8, "is"), W(0.8, 1.2, "crazy"), W(5.0, 5.4, "look"),
             W(5.4, 5.8, "at"), W(5.8, 6.2, "that")]
    ass = E.build_ass(words, "Kai tries the heist", 10, 640, style={"text_level": "full"})
    assert "TikTok Sans" in ass
    assert not re.search(r"\\(fad|move|t\(|fscx)", ass)          # nothing pops, moves or fades
    caps = [l for l in ass.splitlines() if ",Cap," in l]
    assert caps[0].split(",")[1:3] == ["0:00:00.00", "0:00:02.70"]   # last word 1.2 s + 1.5 s hold
    assert caps[0].endswith("This is crazy")                          # whole line, sentence case
    assert caps[1].split(",")[1] == "0:00:05.00"


def test_hold_never_overlaps_the_next_line():
    g1, g2 = [W(0, 0.5, "one")], [W(1.0, 1.5, "two")]
    assert E.caption_times([g1, g2], 10) == [(0, 1.0), (1.0, 3.0)]


def test_minimal_hook_stays_longer():
    ass = E.build_ass([], "Kai tries the heist", 20, 640, style={"text_level": "minimal"})
    hook = next(l for l in ass.splitlines() if ",Hook," in l)
    assert hook.split(",")[2] == "0:00:04.00"   # 2.5 s + 1.5 s to read it


def test_4x5_text_goes_in_the_bands():
    lay = E.layout(1920, 1080, None)
    ass = E.build_ass([W(0, .4, "big"), W(.4, .8, "jump"), W(.8, 1.2, "there")], "Kai tries the heist", 5,
                      0, style={"text_level": "full"}, frame_h=lay.out_h, picture=lay.picture)
    assert "PlayResY: 1350" in ass
    y = int(re.search(r"\\pos\(540,(\d+)\)", ass).group(1))
    assert y >= lay.picture[1]   # captions under the picture, never over the game


def test_no_cta_end_card():
    assert "cta" not in E.END_TEXT
    ass = E.build_ass([], "Kai tries the heist", 10, 640, style={"text_level": "full", "ending": "cta"})
    assert ",End," not in ass


def test_tokyo_girl_is_4x5_and_text_does_not_move():
    assert (TG.W, TG.H) == (1080, 1350)
    ass = TG.build_text_ass([(0, 2, "GTA 6 *is* here"), (2, 4, "Ready?")], "x", 104)
    assert "TikTok Sans" in ass and not re.search(r"\\(fad|t\(|fscx)", ass)
    assert "PlayResY: 1350" in ass
    last = ass.splitlines()[-1]
    assert last.split(",")[2] == "0:00:04.00" and ",Ask," in last


def test_crop_that_cuts_off_text_shows_the_whole_frame():
    sh = TG.Shot("a.mp4", "video", 0, 5, [], w=1920, h=1080, labels={"tags": ["text_overlay"]})
    slot = TG.Slot(sh, 0.0, 4, 0.0, 2.0, 0, False, 1)
    assert TG.slot_layout(slot, "box45") == "fit"
    sh2 = TG.Shot("a.mp4", "video", 0, 5, [], w=1920, h=1080)
    assert TG.slot_layout(TG.Slot(sh2, 0.0, 4, 0.0, 2.0, 0, False, 1), "box45") == "box45"
    sh2.crop_ok = False   # the subject is wider than the 4:5 crop
    assert TG.slot_layout(TG.Slot(sh2, 0.0, 4, 0.0, 2.0, 0, False, 1), "fill") == "fit"
    assert "boxblur" not in TG.fit_filter([], False)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_render_variant_4x5_end_to_end(tmp_path):
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=3",
                    "-f", "lavfi", "-i", "sine=d=3", "-shortest", str(src)], check=True)
    lay = E.layout(1280, 720, None, src_fps=30)
    ass = tmp_path / "c.ass"
    ass.write_text(E.build_ass([W(0.2, 0.6, "big"), W(0.6, 1.0, "jump"), W(1.0, 1.4, "here")], "Kai tries it",
                               3, 0, style={"text_level": "full"}, frame_h=lay.out_h, picture=lay.picture))
    out = E.render_variant(src, tmp_path / "o.mp4", lay, ass, 1280, 720, [(0, 3)])
    info = E.probe(out)
    assert (info["width"], info["height"]) == (1080, 1350)


# ---- #ready-to-post board ------------------------------------------------------------
def item(i: str, exp: float, status: str = "open", rnd_status: str = "posted") -> dict:
    return {"id": i, "mode": "lacy", "status": status, "expires": exp, "title": f"t {i}",
            "source": {"streamer": "Kai Cenat", "platform": "twitch", "category": "Grand Theft Auto VI"},
            "rounds": [{"n": 1, "kind": "initial", "status": rnd_status,
                        "variants": [{"label": L, "title": f"Kai {i} {L}", "duration": 30} for L in "ABC"]}]}


def test_board_lists_waiting_videos_in_one_message():
    items = [item("b", 200), item("a", 100), item("done", 50, status="done"), item("r", 300, rnd_status="rendering")]
    pay = D.board_payload(items, None, 0)
    assert "Ready to review: 2" in pay["content"]
    assert pay["content"].index("`a`") < pay["content"].index("`b`")      # expiring soonest first
    assert pay["opened"] is None and pay["embeds"] == []
    sel = pay["rows"][0][0]
    assert sel["type"] == "select" and [o["value"] for o in sel["options"]] == ["a", "b"]
    assert D.parse_cid(sel["custom_id"])["action"] == "board"


def test_board_expands_one_video_in_place():
    items = [item("a", 100), item("b", 200)]
    pay = D.board_payload(items, "b", 0)
    assert pay["opened"] == "b" and pay["round"] == 1
    assert len(pay["embeds"]) == 1 and len(pay["embeds"][0]["fields"]) == 3
    labels = [b["label"] for row in pay["rows"][1:] for b in row]
    assert {"Approve A", "Approve B", "Approve C", "Reject", "Feedback", "Collapse"} <= set(labels)
    assert len(pay["rows"]) <= 5 and all(len(r) <= 5 for r in pay["rows"])
    assert len(pay["content"]) <= 2000
    # an item that's gone (approved) collapses the board
    assert D.board_payload([item("a", 100)], "b", 0)["opened"] is None


def test_board_caps_the_drop_down():
    items = [item(f"i{k:02d}", k) for k in range(30)]
    pay = D.board_payload(items, None, 0)
    assert len(pay["rows"][0][0]["options"]) == D.BOARD_MAX and "5 more" in pay["content"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_sponsor_logo_top_right_for_the_whole_clip(tmp_path):
    from pathlib import Path
    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1280x720:r=30:d=3",
                    "-f", "lavfi", "-i", "sine=d=3", "-shortest", str(src)], check=True)
    logo = Path(E.__file__).resolve().parent / "brand" / "clipup_logo.png"
    lay = E.layout(1280, 720, None, src_fps=30)
    out = E.render_variant(src, tmp_path / "o.mp4", lay, None, 1280, 720, [(0, 3)], logo=logo, logo_w=320)
    assert E.probe(out)["duration"] == pytest.approx(3, abs=0.2)

    def pixel(t, x, y):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1",
                              "-vf", f"crop=2:2:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        return tuple(raw[:3])
    for t in (0.2, 2.8):                                   # the sign's black body, top right, start to end
        r, g, b = pixel(t, 1080 - E.LOGO_INSET - 300, E.LOGO_INSET + 20)
        assert r < 60 and g < 60 and b < 60


# ---- captions read as sentences: capitals only at a sentence start, no full stop at every pause -------
def _caps(ass: str) -> list[str]:
    return [line.split("\\q2}", 1)[1] for line in ass.splitlines() if ",Cap,," in line]


def test_captions_capitalise_only_where_a_sentence_starts():
    W = E.Word
    ws = [W(0.0, 0.3, "Kai"), W(0.35, 0.6, "really"), W(0.65, 0.9, "needs"), W(0.95, 1.2, "to"),
          W(1.25, 1.5, "get"), W(1.55, 1.8, "away"), W(1.85, 2.1, "fast."), W(2.6, 2.9, "Oh"),
          W(2.95, 3.2, "my"), W(3.25, 3.6, "gosh."), W(4.2, 4.5, "where"), W(4.55, 4.8, "is"),
          W(4.85, 5.1, "the"), W(5.15, 5.4, "car?")]
    caps = _caps(E.build_ass(ws, "", 8.0, 640))
    assert caps == ["Kai really needs", "to get away", "fast.", "Oh my gosh.", "Where is the", "car?"]


def test_stray_full_stops_from_pauses_are_dropped():
    W = E.Word
    ws = [W(0.0, 0.3, "I'm"), W(0.35, 0.6, "gonna."), W(1.4, 1.7, "Steal"), W(1.75, 2.0, "the."),
          W(2.6, 2.9, "Car"), W(2.95, 3.2, "and."), W(3.9, 4.2, "I"), W(4.25, 4.5, "drive.")]
    out = [w.text for w in E.tidy_punctuation(ws)]
    assert out == ["I'm", "gonna", "steal", "the", "car", "and", "I", "drive."]
    caps = _caps(E.build_ass(ws, "", 6.0, 640))
    assert " ".join(caps) == "I'm gonna steal the car and I drive." and caps[1][0].islower()
    assert E.sentence_case("NO WAY") == "NO WAY" and E.sentence_case("WHAT ARE YOU DOING I SAID") == \
        "What are you doing I said"
    assert E.sentence_case("get away", start=False) == "get away"


def test_ai_punctuation_only_changes_punctuation_and_capitals(tmp_path, monkeypatch):
    from spikebot import ai, lacy as L
    W = E.Word
    ws = [W(0.0, 0.3, "Bro"), W(0.35, 0.6, "look."), W(1.4, 1.7, "At"), W(1.75, 2.0, "that.")]
    calls = []

    def fake(words, base=None, client=None):
        calls.append(words)
        return ["Bro,", "look", "at", "THIS!"]          # "THIS" is a different word: Whisper's kept
    monkeypatch.setattr(ai, "available", lambda base=None: True)
    monkeypatch.setattr(ai, "punctuate", fake)
    ad = L.ReviewAdapter({}, tmp_path, lcfg={"channel": "lacy"})
    job = {"id": "lacy-1", "channel": "lacy"}
    assert [w.text for w in ad.punctuated(job, tmp_path, ws)] == ["Bro,", "look", "at", "that."]
    assert [w.text for w in ad.punctuated(job, tmp_path, ws)] == ["Bro,", "look", "at", "that."]
    assert len(calls) == 1                                # cached in punct.json
    monkeypatch.setattr(ai, "punctuate", lambda words, base=None, client=None: ["one"])
    assert ad.punctuated(job, tmp_path / "x", ws) == ws   # wrong word count: Whisper's words
    monkeypatch.setattr(ai, "available", lambda base=None: False)
    assert ad.punctuated(job, tmp_path / "y", ws) == ws   # no key: unchanged


def test_two_webcams_of_a_similar_size_means_no_facecam_panel():
    from spikebot import safety as SF
    size = (1920, 1080)
    host = {"class": "FACE_MALE", "score": 0.9, "box": [60, 380, 90, 90]}       # streamer's cam, left
    guest = {"class": "FACE_MALE", "score": 0.9, "box": [1700, 420, 85, 85]}    # co-streamer shown on the overlay
    assert SF.facecam_from_faces([([host, guest], size)] * 8) is None
    big = {"class": "FACE_MALE", "score": 0.9, "box": [1650, 40, 200, 200]}     # clearly the main webcam
    box = SF.facecam_from_faces([([guest | {"box": [60, 380, 90, 90]}, big], size)] * 8)
    assert box and box["x"] > 0.6          # the big one, not the small cam on the left


def test_swear_words_are_starred_out_on_screen():
    assert E.censor("what the fuck") == "what the f**k"
    assert E.censor("Shit. Nigga, that's crazy") == "S**t. N***a, that's crazy"
    assert E.censor("fucking motherfucker bullshit bitches") == "f**king motherf**ker bulls**t b***hes"
    assert E.censor("Scunthorpe class cockpit Dickens scrap shitake") == "Scunthorpe class cockpit Dickens scrap s**take"
    assert E.censor("you dick") == "you d**k"
    W = E.Word
    ws = [W(0.0, 0.3, "Somebody"), W(0.35, 0.6, "stole"), W(0.65, 0.9, "my"), W(0.95, 1.2, "fucking"),
          W(1.25, 1.5, "car.")]
    assert _caps(E.build_ass(ws, "Lacy said what the fuck", 3.0, 640)) == ["Somebody stole my", "f**king car."]
    assert "what the f**k" in E.build_ass(ws, "Lacy said what the fuck", 3.0, 640)
