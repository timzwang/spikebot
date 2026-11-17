"""Song beat maps: tempo, drop, start point, and Tokyo Girl cuts on the real beats."""
from __future__ import annotations

import datetime as dt
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from spikebot import music as M
from spikebot import tokyo_girl as TG

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
BPM = 128
BEAT = 60 / BPM


def song(path: Path, drop: float = 8 * 60 / 128 * 2, seconds: float = 30) -> Path:
    """Kick on every beat at 128 BPM: quiet build-up, then loud (with bass) from the drop."""
    expr = (f"(if(lt(mod(t,{BEAT}),0.05),sin(2*PI*90*t),0)+0.02*sin(2*PI*440*t))"
            f"*if(lt(t,{drop}),0.12,1)+if(gte(t,{drop}),0.5*sin(2*PI*55*t),0)")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"aevalsrc='{expr}':s=22050:d={seconds}", str(path)], check=True)
    return path


@needs_ffmpeg
def test_analyse_finds_tempo_and_drop(tmp_path):
    drop = 16 * BEAT          # 7.5 s
    row = M.Library(tmp_path / "music").add(song(tmp_path / "Hit Song.wav", drop), "Hit Song")
    assert row["name"] == "hit-song" and (tmp_path / "music/hit-song.wav").exists()
    assert row["bpm"] == pytest.approx(BPM, rel=0.04) or row["bpm"] == pytest.approx(BPM / 2, rel=0.04)
    assert row["drop_s"] == pytest.approx(drop, abs=0.3)
    start = M.start_for(row)
    assert row["drop_s"] - start == pytest.approx(4 * BEAT * (128 / row["bpm"] if row["bpm"] < 100 else 1), abs=0.3)
    # cached: the second read doesn't re-analyse
    assert M.Library(tmp_path / "music").current() == row
    assert "start it at" in M.note(row, start)


def test_grid_is_relative_and_extended():
    row = {"beats": [1.0, 1.5, 2.0, 2.5, 3.0], "drop_s": 2.0, "bpm": 120}
    assert M.start_for(row, build_beats=2) == 1.0
    g = M.grid(row, 1.5, need_s=4)
    assert g[:4] == [0.0, 0.5, 1.0, 1.5] and g[-1] >= 4 + 4 * 0.5 - 1e-9


def test_plan_follows_real_beats():
    beats = [0.0]
    for i in range(200):          # uneven beats: alternating 0.45 / 0.5 s
        beats.append(round(beats[-1] + (0.45 if i % 2 else 0.5), 3))
    shots = [TG.Shot(f"s{i}.jpg", "image", 0.0, float("inf"), ["city"], None, 1920, 1080) for i in range(12)]
    story = next(x for x in TG.STORIES if x.id == "partners")
    lcfg = TG.settings({"tokyo_girl": {"min_s": 15, "max_s": 20}})
    plan = TG.plan_clip(story, shots, lcfg, dt.date(2026, 11, 20), random.Random(3), beats=beats)
    on = set(beats)
    assert all(sl.start in on and round(sl.start + sl.dur, 3) in on for sl in plan.slots)
    assert plan.duration in on and 15 - 1 <= plan.duration <= 20 + 1
    assert all(c[0] in on for c in plan.cards)


# ---- sound links ---------------------------------------------------------------------------
def test_sound_links_are_recognised_and_named():
    page = "https://www.tiktok.com/music/Tokyo-Girl-7301234567890123456?lang=en"
    assert M.is_sound_page(page) and not M.is_sound_page("https://www.tiktok.com/@a/video/1")
    assert M.sound_title(page) == "Tokyo Girl"
    short = {"https://vm.tiktok.com/ZMabc/": page}
    links = M.song_links("new trend https://vm.tiktok.com/ZMabc/ https://www.tiktok.com/@a/video/1",
                         resolve=lambda u: short.get(u, u))
    assert links == [("https://vm.tiktok.com/ZMabc/", page)]          # the video is not a song
    hinted = M.song_links("sound: https://www.tiktok.com/@a/video/1", resolve=lambda u: u)
    assert hinted == [("https://www.tiktok.com/@a/video/1",) * 2]
    assert M.song_links("https://evil.example/music/x-12345678901", resolve=lambda u: u) == []


def test_play_url_unescapes_and_bad_page_explains_the_fallback(tmp_path, monkeypatch):
    html = '{"music":{"title":"x","playUrl":"https:\\u002F\\u002Fsf16.tiktokcdn.com\\u002Fobj\\u002Fa.mp3"}}'
    assert M.play_url(html) == "https://sf16.tiktokcdn.com/obj/a.mp3"
    assert M.play_url("<html>nothing</html>") is None

    def no_file(url, folder):
        raise M.MusicError("TikTok didn't hand over the sound file")
    monkeypatch.setattr(M, "_fetch_sound_page", no_file)
    with pytest.raises(M.MusicError, match="sound:"):
        M.fetch("https://www.tiktok.com/music/Tokyo-Girl-7301234567890123456", tmp_path)


@needs_ffmpeg
def test_add_from_url_keeps_only_the_song(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    src = song(tmp_path / "src.mp3")

    def fake_fetch(url, folder):
        out = folder / "sound.mp3"
        out.write_bytes(src.read_bytes())
        return out, "Tokyo Girl"
    monkeypatch.setattr(M, "fetch", fake_fetch)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "res.json"
    assert M.main(["--config", "config.json", "add", "--url", "https://www.tiktok.com/music/x-1234567890",
                   "--json", str(out)]) == 0
    import json
    res = json.loads(out.read_text())
    assert res["ok"] and res["name"] == "tokyo-girl" and abs(res["bpm"] - BPM) < 3 and res["tokyo_girl"]
    music_dir = next(p.parent for p in tmp_path.rglob("tokyo-girl.mp3"))
    assert [p.name for p in music_dir.iterdir() if p.is_dir()] == []      # temp download removed


@needs_ffmpeg
def test_trending_sound_never_replaces_the_tokyo_girl_song(tmp_path):
    import os
    lib = M.Library(tmp_path / "music")
    tg = lib.add(song(tmp_path / "a.wav"), "Tokyo Girl")
    trend = lib.add(song(tmp_path / "b.wav", seconds=20), "original sound - someone")
    later = (tmp_path / "music" / f"{trend['file']}").stat().st_mtime + 100
    os.utime(tmp_path / "music" / trend["file"], (later, later))       # the trend is the newest file
    assert lib.current()["name"] == trend["name"]
    song_name = TG.settings({}).get("song")
    assert song_name == M.TOKYO_GIRL_SONG and lib.current(song_name)["name"] == tg["name"]
    with pytest.raises(M.MusicError):
        lib.current("not-there")


def test_sound_meta_names_original_sounds_by_author():
    html = '{"music":{"id":"1","title":"original sound","authorName":"gta\\u002Fedits"}}'
    assert M.sound_meta(html) == "original sound - gta/edits"
    assert M.sound_meta('{"title":"Tokyo Girl","authorName":"Passive"}') == "Tokyo Girl"
    assert M.sound_meta("<html></html>") == ""


@needs_ffmpeg
def test_same_name_never_overwrites_a_different_sound(tmp_path):
    lib = M.Library(tmp_path / "music")
    a = lib.add(song(tmp_path / "a.wav"), "original sound")
    b = lib.add(song(tmp_path / "b.wav", seconds=20), "original sound")
    again = lib.add(song(tmp_path / "c.wav"), "original sound")          # identical to a
    assert (a["name"], b["name"], again["name"]) == ("original-sound", "original-sound-2", "original-sound")
    assert len(lib.files()) == 2
    tg = lib.add(song(tmp_path / "t.wav", seconds=25), "Tokyo Girl", replace=True)
    tg2 = lib.add(song(tmp_path / "t2.wav", seconds=22), "Tokyo Girl", replace=True)
    assert tg["name"] == tg2["name"] == "tokyo-girl" and tg2["duration"] < tg["duration"]


def test_window_keeps_the_edit_inside_the_sound():
    row = {"duration": 20.0, "drop_s": 16.0, "beats": [i * 0.5 for i in range(40)]}
    start, room = M.window(row, 8, 30)
    assert start + room <= 20.0 and room >= 8               # drop too late: start earlier
    long = {"duration": 120.0, "drop_s": 30.0, "beats": [i * 0.5 for i in range(240)]}
    assert M.window(long, 15, 30) == (M.start_for(long), 30)
    assert "<https://x>" in M.note(dict(long, name="s", bpm=120, url="https://x"), 28.0)
