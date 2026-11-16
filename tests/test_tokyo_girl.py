"""Tokyo Girl mode: stories, beat grid, shot picking, captions, daily quota, fetch
guards, and an end-to-end silent render with ffmpeg.
Run: python -m pytest -q"""
import json
import random
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from spikebot import tokyo_girl as TG

HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
TODAY = date(2026, 11, 16)


def cfg(tmp_path: Path, **kw) -> dict:
    return TG.settings({"tokyo_girl": kw}, tmp_path)


def img_shot(name: str, tags: list[str]) -> TG.Shot:
    return TG.Shot(name, "image", 0.0, float("inf"), tags, None, 1920, 1080)


# ---- config -----------------------------------------------------------------
def test_settings_defaults_env_and_paths(tmp_path):
    from spikebot.main import apply_env
    s = TG.settings({})
    assert s["per_day"] == 2 and s["bpm"] == 120 and (s["min_s"], s["max_s"]) == (15, 30)
    assert s["enabled"] is True
    c = apply_env({"tokyo_girl": {"per_day": 5}}, [], {"TOKYO_GIRL_PER_DAY": "3", "TOKYO_GIRL_MODE": "false"})
    s = TG.settings(c, tmp_path)
    assert s["per_day"] == 3 and s["enabled"] is False
    assert s["export_dir"] == str(tmp_path / "exports/tokyo_girl")
    assert TG.settings({"tokyo_girl": {"per_day": 4}})["per_day"] == 4


def test_caption_templates_are_exact():
    s = TG.settings({})
    story = next(x for x in TG.STORIES if x.id == "leonida")
    assert TG.caption_file(story, TODAY, s) == (
        "IG: Leonida looks insane. Which spot are you visiting first? Song: Tokyo Girl by @PassiveBand #GTA6\n"
        "TikTok: Leonida looks insane. Which spot are you visiting first? Song: Tokyo Girl by @Passive #GTA6\n")


# ---- stories ------------------------------------------------------------------
def test_story_bank_rules():
    assert len({s.id for s in TG.STORIES}) == len(TG.STORIES)
    for s in TG.STORIES:
        assert s.kind in TG.KINDS, s.id
        assert 3 <= len(s.cards) <= 5, s.id
        assert s.cards[-1].endswith("?"), s.id          # ends on a question
        assert TG.plain(TG.fill(s.caption, TODAY)).endswith("?"), s.id
        for c in s.cards + [s.caption]:
            text = TG.plain(TG.fill(c, TODAY))
            assert re.fullmatch(r"[A-Za-z0-9 '\"!?.,:&#/-]+", text), (s.id, text)  # no emoji/odd glyphs
        for c in s.cards:
            assert len(TG.plain(TG.fill(c, TODAY))) <= 40, (s.id, c)          # readable
            assert c.count("*") % 2 == 0, (s.id, c)
        for word in ("leak", "ending", "dies", "killed", "betray"):           # no spoilers/leaks
            assert word not in " ".join(s.cards).lower(), (s.id, word)


def test_story_dates():
    by = {s.id: s for s in TG.STORIES}
    assert TG.eligible(by["countdown"], TODAY) and not TG.eligible(by["countdown"], date(2026, 11, 19))
    assert TG.fill(by["countdown"].cards[0], TODAY) == "GTA 6 drops in *3 days*"
    assert TG.fill(by["countdown"].cards[0], date(2026, 11, 18)) == "GTA 6 drops in *1 day*"
    assert not TG.eligible(by["finally-out"], TODAY) and TG.eligible(by["finally-out"], date(2026, 11, 19))
    assert not TG.eligible(by["finally-out"], date(2026, 12, 20))
    d = TODAY
    while d < date(2027, 6, 1):  # always enough variety, before and long after launch
        live = [s for s in TG.STORIES if TG.eligible(s, d)]
        assert len(live) >= 12 and len({s.kind for s in live}) == 4, d
        d += timedelta(days=9)


def test_pick_story_avoids_recent_and_last_kind():
    rng = random.Random(1)
    recent = ["partners"]
    for _ in range(30):
        s = TG.pick_story(TODAY, recent, rng)
        assert s.id not in recent and TG.eligible(s, TODAY)
        assert s.kind != "couple" or len(recent) > 1
        recent = (recent + [s.id])[-12:]
    with pytest.raises(ValueError):
        TG.pick_story(TODAY, [], rng, "nope")


# ---- beat grid ------------------------------------------------------------------
@pytest.mark.parametrize("bpm", [120, 128, 100])
@pytest.mark.parametrize("n", [3, 4, 5])
def test_card_bars_fit_15_to_30s(bpm, n):
    for seed in range(20):
        bars = TG.card_bars(n, bpm, 15, 30, random.Random(seed))
        total = sum(bars) * TG.bar_s(bpm)
        assert 15 <= total <= 30 and min(bars) >= 1 and bars[-1] >= 2


def test_plan_cuts_on_the_beat(tmp_path):
    s = cfg(tmp_path)
    shots = [img_shot(f"city{i}.jpg", ["city"]) for i in range(5)] + \
            [img_shot(f"car{i}.jpg", ["cars"]) for i in range(5)] + \
            [img_shot(f"jl{i}.jpg", ["couple", "jason", "lucia"]) for i in range(5)]
    for story in TG.STORIES:
        if not TG.eligible(story, TODAY):
            continue
        p = TG.plan_clip(story, shots, s, TODAY, random.Random(story.id))
        assert 15 <= p.duration <= 30 and p.duration % 2.0 == 0         # whole bars at 120 BPM
        t = 0.0
        for sl in p.slots:
            assert sl.start == pytest.approx(t) and (sl.start / 0.5).is_integer()  # on the beat
            assert sl.beats in (1, 2, 3, 4)
            t += sl.dur
        assert t == pytest.approx(p.duration)
        assert [c[2] for c in p.cards] == [TG.fill(c, TODAY) for c in story.cards]
        for (s0, e0, _), (s1, _, _) in zip(p.cards, p.cards[1:]):
            assert e0 == s1 and (s1 / 2.0).is_integer()                # text changes on a bar
        assert p.cards[-1][1] - p.cards[-1][0] >= 4                      # time to read the question
        flashes = [sl for sl in p.slots if sl.flash]
        assert len(flashes) == len(p.cards) - 1 and all(sl.start in {c[0] for c in p.cards} for sl in flashes)
        if story.kind == "couple":
            couple = sum("couple" in sl.shot.tags for sl in p.slots)
            assert couple >= len(p.slots) / 4, story.id


def test_plan_prefers_unused_footage(tmp_path):
    s = cfg(tmp_path)
    shots = [img_shot(f"c{i}.jpg", ["city"]) for i in range(80)]
    story = next(x for x in TG.STORIES if x.id == "leonida")
    p = TG.plan_clip(story, shots, s, TODAY, random.Random(3))
    keys = [sl.shot.key for sl in p.slots]
    assert len(set(keys)) == len(keys)  # plenty of footage: no repeats inside one edit
    uses = {k: 5 for k in keys}
    p2 = TG.plan_clip(story, shots, s, TODAY, random.Random(4), uses)
    assert not any(sl.shot.key in uses for sl in p2.slots)  # 50 fresh shots left: all fresh


def test_plan_needs_media(tmp_path):
    with pytest.raises(TG.editor.EditError):
        TG.plan_clip(TG.STORIES[0], [], cfg(tmp_path), TODAY, random.Random(1))


# ---- library ----------------------------------------------------------------------
def test_tags_from_file_names():
    assert TG.tags_for("rockstar/Jason_Lucia_beach_01.jpg") == ["city", "couple", "jason", "lucia"]
    assert TG.tags_for("GTA_VI_Trailer_2_trailer_VQRLujxTm3c.mp4") == ["trailer"]
    assert TG.tags_for("sports-car-chase.png") == ["cars"]
    assert "cars" not in TG.tags_for("cartel_boss.jpg")


def test_build_shots_trims_dark_and_excluded(tmp_path):
    s = cfg(tmp_path, trailer_head_s=5, trailer_tail_s=10)
    entry = {"kind": "video", "w": 1920, "h": 1080, "duration": 60.0, "crop": [1920, 800, 0, 140],
             "cuts": [3.0, 12.0, 12.1, 20.0, 30.0, 40.0, 52.0],
             "yavg": [[t / 2, 5.0 if 40 <= t / 2 < 52 else 90.0] for t in range(120)]}
    shots = TG.build_shots("t/Trailer_1_trailer_x.mp4", entry,
                           {"exclude": [[21, 25]], "ranges": [{"start": 31, "end": 33, "tags": ["cars"]}]}, s)
    spans = [(sh.start, sh.end) for sh in shots]
    assert spans == [(5.0, 11.9), (12.1, 19.9), (30.1, 39.9)]   # head, 0.1s margins, 12.0/12.1 merged
    assert all(sh.crop == [1920, 800, 0, 140] for sh in shots)    # 40-52 dark, 52+ in tail, 20-30 excluded
    assert shots[2].tags == ["cars", "trailer"] and shots[0].tags == ["trailer"]
    assert TG.build_shots("x.mp4", entry, {"skip": True}, s) == []


def test_parse_meta():
    text = "frame:0    pts:0       pts_time:0\nlavfi.signalstats.YAVG=12.5\nframe:1 pts:512 pts_time:0.5\nlavfi.signalstats.YAVG=80\n"
    assert TG.parse_meta(text) == [(0.0, {"lavfi.signalstats.YAVG": "12.5"}), (0.5, {"lavfi.signalstats.YAVG": "80"})]


# ---- text --------------------------------------------------------------------------
def test_text_has_no_logo_or_watermark():
    ass = TG.build_text_ass([(0, 4, "GTA 6 drops in *3 days*"), (4, 10, "Day one or reviews?")], "DejaVu Sans", 104)
    events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 2 and events[1].split(",")[3] == "Ask"
    assert r"{\c&H00E5FF&}3 days{\c&HFFFFFF&}" in events[0] and "*" not in ass
    assert "Mark" not in ass and "@" not in ass


# ---- fetch guards ------------------------------------------------------------------
def test_only_official_hosts():
    assert TG.host_allowed("https://www.rockstargames.com/VI/media")
    assert TG.host_allowed("https://media-rockstargames-com.akamaized.net/a/b.jpg")
    assert TG.host_allowed("https://www.youtube.com/watch?v=QdBZY2fkU-0")
    for bad in ("https://www.twitch.tv/videos/1", "https://kick.com/adinross", "https://evilrockstargames.com/x",
                "https://gtaleaks.example/vi.mp4"):
        assert not TG.host_allowed(bad), bad


def test_page_media_parsing():
    html = ('<img src="https://media-rockstargames-com.akamaized.net/rockstar-games/img/vi/jason_lucia_01.jpg">'
            '"https:\\/\\/media-rockstargames-com.akamaized.net\\/x\\/vice_city_02.webp"'
            '<img src="https://media-rockstargames-com.akamaized.net/x/logo_vi.png">'
            '<iframe src="https://www.youtube.com/embed/QdBZY2fkU-0"></iframe>')
    imgs, yt = TG.page_media(html)
    assert [Path(u).name for u in imgs] == ["jason_lucia_01.jpg", "vice_city_02.webp"]
    assert yt == ["QdBZY2fkU-0"]


# ---- daily quota ----------------------------------------------------------------------
def test_daily_tops_up_to_per_day(tmp_path, monkeypatch):
    s = cfg(tmp_path, per_day=2)
    shots = [img_shot(f"c{i}.jpg", ["city", "cars", "couple"]) for i in range(30)]
    monkeypatch.setattr(TG.Library, "shots", lambda self, entries=None: shots)
    monkeypatch.setattr(TG, "fetch", lambda lcfg: None)
    monkeypatch.setattr(TG, "render_plan", lambda plan, lcfg, out: out.parent.mkdir(parents=True, exist_ok=True)
                        or out.write_bytes(b"x"))
    now = datetime(2026, 11, 16, 6, 50, tzinfo=TG.UK).timestamp()
    first = TG.daily(s, now)
    assert len(first) == 2 and TG.daily(s, now + 3600) == []         # idempotent the same day
    names = sorted(p.name for p in Path(s["export_dir"]).iterdir())
    assert len(names) == 4 and all(n.startswith("2026-11-16_0650-") for n in names)
    txt = Path(s["export_dir"]) / first[0].name.replace(".mp4", ".txt")
    lines = txt.read_text().splitlines()
    assert lines[0].startswith("IG: ") and lines[0].endswith("Song: Tokyo Girl by @PassiveBand #GTA6")
    assert lines[1].startswith("TikTok: ") and lines[1].endswith("Song: Tokyo Girl by @Passive #GTA6")
    stories = json.loads(Path(s["state_file"]).read_text())["recent"]
    assert len(set(stories)) == 2
    assert len(TG.daily(s, now + 86400)) == 2                        # next UK day
    assert TG.daily(TG.settings({"tokyo_girl": {"enabled": False}}, tmp_path), now + 2 * 86400) == []


# ---- end to end -----------------------------------------------------------------------
@needs_ffmpeg
def test_render_is_silent_vertical_and_on_length(tmp_path):
    media = tmp_path / "data/tokyo_girl/media"
    media.mkdir(parents=True)
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    subprocess.run(ff + ["-f", "lavfi", "-i", "gradients=s=1920x1080:seed=2", "-frames:v", "1",
                         str(media / "vice_city_night.jpg")], check=True)
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=12", "-f", "lavfi", "-i", "sine=d=12",
                         "-vf", "pad=1280:720:0:0", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                         str(media / "car_trailer.mp4")], check=True)
    s = cfg(tmp_path, min_s=15, max_s=16, trailer_head_s=0, trailer_tail_s=0)
    out = TG.make(s, 1, story_id="partners", seed=7)[0]
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                        "-show_entries", "format=duration", "-of", "json", str(out)],
                       capture_output=True, text=True, check=True)
    j = json.loads(p.stdout)
    assert [st["codec_type"] for st in j["streams"]] == ["video"]     # no audio track at all
    assert (j["streams"][0]["width"], j["streams"][0]["height"]) == (1080, 1920)
    assert float(j["format"]["duration"]) == pytest.approx(16.0, abs=0.05)
    assert out.with_suffix(".txt").read_text().startswith("IG: Jason & Lucia: partners in crime")
