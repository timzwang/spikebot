"""GTA 5 vs GTA 6 comparison edits (spikebot/compare.py)."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from spikebot import compare as C
from spikebot import review as R
from spikebot import tokyo_girl as TG

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def shot(src, start=0.0, end=8.0):
    return TG.Shot(src, "video", start, end, [], None, 1920, 1080, fps=30.0)


def test_pairs_match_themes_biggest_difference_first():
    v5 = [shot(".gta5/a.mp4"), shot(".gta5/b.mp4"), shot(".gta5/c.mp4"), shot(".gta5/d.mp4")]
    v6 = [shot("x.mp4"), shot("y.mp4"), shot("z.mp4"), shot("w.mp4")]
    labels = {v5[0].key: {"theme": "beach", "quality": 2}, v6[0].key: {"theme": "beach", "quality": 3},
              v5[1].key: {"theme": "aircraft", "quality": 2}, v6[1].key: {"theme": "aircraft", "quality": 3},
              v5[2].key: {"theme": "faces", "quality": 2}, v6[2].key: {"theme": "faces", "quality": 2},
              v5[3].key: {"theme": "police", "quality": 0}, v6[3].key: {"theme": "police", "quality": 3}}
    pairs = C.pick_pairs(v5, v6, labels, 2, 2.0, random.Random(1))
    assert {p[0] for p in pairs} == {"beach", "faces"}        # big-difference themes beat aircraft
    assert all(labels[a.key]["theme"] == labels[b.key]["theme"] for _, a, b in pairs)
    assert "police" not in {p[0] for p in C.pick_pairs(v5, v6, labels, 4, 2.0, random.Random(1))}   # quality 0
    kept = C.pick_pairs(v5, v6, labels, 2, 2.0, random.Random(5),
                        keep=[{"theme": "aircraft", "key5": v5[1].key, "key6": v6[1].key}])
    assert kept[0][0] == "aircraft"                            # a re-render keeps its pairs
    with pytest.raises(Exception):
        C.pick_pairs(v5, v6, {}, 3, 2.0, random.Random(1))


def test_plan_stack_and_sequence_with_labels_and_captions():
    pairs = [("beach", shot(".gta5/a.mp4"), shot("x.mp4")), ("faces", shot(".gta5/b.mp4"), shot("y.mp4"))]
    plan, labels = C.plan_compare(pairs, "stack", 2.5, "GTA 5 vs *GTA 6*", "Which upgrade is the biggest?",
                                  random.Random(1), intro=shot("hero.mp4"))
    assert [sl.shot2 is not None for sl in plan.slots] == [False, True, True]
    assert plan.duration == pytest.approx(1.6 + 5.0)
    assert [c[2] for c in plan.cards] == ["GTA 5 vs *GTA 6*", "Beaches", "Which upgrade is the biggest?"]
    assert [lb["text"] for lb in labels] == ["GTA 5 · 2013", "GTA 6 · 2026"] * 2
    y5, y6 = TG.stack_tops()
    assert labels[0]["y"] == y5 + 24 and labels[1]["y"] == y6 + 24
    seq, lab2 = C.plan_compare(pairs, "sequence", 2.5, "h", "q?", random.Random(1))
    assert len(seq.slots) == 4 and seq.slots[0].shot.src.startswith(".gta5/") and seq.slots[1].shot.src == "x.mp4"
    assert lab2[0]["end"] == pytest.approx(1.0) and lab2[1]["text"].startswith("GTA 6")
    caps = C.captions(["beach", "faces"])
    assert "gta 5 vs gta 6: beaches, faces" in caps["tiktok"] and "#GTA5" in caps["instagram"]


def test_gta5_library_is_hidden_from_gta6_edits(tmp_path):
    s = TG.settings({"data_dir": "data"}, tmp_path)
    c5 = C.gta5_cfg(s)
    assert Path(c5["media_dir"]) == Path(s["media_dir"]) / ".gta5" and not c5["sources"]
    (Path(c5["media_dir"]) / "trailers").mkdir(parents=True)
    (Path(c5["media_dir"]) / "trailers" / "gta5.mp4").write_bytes(b"x")
    assert TG.Library(s).files() == []                          # GTA 6 edits never see GTA 5 footage
    assert C.prefixed([shot("trailers/a.mp4")])[0].src == ".gta5/trailers/a.mp4"
    assert all("exclude" in q for q in C.GTA5_SEARCHES)         # never the PS5 remaster


def make_libraries(tmp_path) -> dict:
    s = TG.settings({"tokyo_girl": {"trailer_head_s": 0, "trailer_tail_s": 0}, "data_dir": "data"}, tmp_path)
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for folder, hue in ((Path(s["media_dir"]) / "trailers", 0), (Path(s["media_dir"]) / ".gta5" / "trailers", 120)):
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            subprocess.run(ff + ["-f", "lavfi", "-i", f"testsrc2=s=640x360:r=30:d=16,hue=h={hue + i * 30}",
                                 "-pix_fmt", "yuv420p", str(folder / f"t{i}.mp4")], check=True)
    return s


def fake_ask(content):
    ids = [c["text"].split()[1] for c in content if c.get("type") == "text" and c["text"].startswith("id ")]
    themes = ["beach", "faces", "city_night"]
    return {"shots": [{"id": i, "theme": themes[k % 3], "quality": 3} for k, i in enumerate(ids)]}


def test_labels_are_cached_and_only_new_shots_are_asked(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(C, "thumb", lambda media, s, width=384: b"\xff\xd8jpg")
    sh = [shot(f"v{i}.mp4") for i in range(5)]
    path = tmp_path / "labels.json"
    labels = C.label_shots(sh, tmp_path, path, ask=lambda c: calls.append(1) or fake_ask(c), batch=2)
    assert len(labels) == 5 and len(calls) == 3
    C.label_shots(sh, tmp_path, path, ask=lambda c: calls.append(1) or fake_ask(c))
    assert len(calls) == 3 and json.loads(path.read_text())[sh[0].key]["theme"] == "beach"


@needs_ffmpeg
def test_comparison_item_renders_three_versions(tmp_path, monkeypatch):
    s = make_libraries(tmp_path)
    real = C.label_shots
    monkeypatch.setattr(C, "label_shots", lambda shots, media, path, base=None, **kw: real(shots, media, path,
                                                                                         ask=fake_ask))
    now = datetime(2026, 11, 19, 9, 0, tzinfo=TG.UK).timestamp()
    ids = C.make(s, 1, now=now)
    store = R.Store(Path(s["data_dir"]))
    item = store.load(ids[0])
    assert item["status"] == "open", item.get("error")
    vs = item["rounds"][0]["variants"]
    assert [v["layout"] for v in vs] == ["stack", "stack", "sequence"]
    for v in vs:
        assert len(v["pairs"]) == 3 and v["title"].startswith("GTA 5 vs GTA 6")
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                                          "-of", "json", str(store.dir(item["id"]) / v["file"])],
                                         capture_output=True, text=True).stdout)["streams"]
        assert [x["codec_type"] for x in info] == ["video"] and (info[0]["width"], info[0]["height"]) == (1080, 1920)
    from spikebot import discord_bot as DB
    assert DB.mode_label(item)[0] == "GTA 5 vs GTA 6"
