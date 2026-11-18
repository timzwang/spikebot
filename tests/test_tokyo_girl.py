"""Tokyo Girl mode: stories, beat grid, shot picking, captions, daily quota, fetch
guards, and an end-to-end silent render with ffmpeg.
Run: python -m pytest -q"""
import os
import time
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
    assert s["per_day"] == 4 and s["bpm"] == 120 and (s["min_s"], s["max_s"]) == (15, 30)
    assert s["enabled"] is True
    c = apply_env({"tokyo_girl": {"per_day": 5}}, [], {"TOKYO_GIRL_PER_DAY": "3", "TOKYO_GIRL_MODE": "false"})
    s = TG.settings(c, tmp_path)
    assert s["per_day"] == 3 and s["enabled"] is False
    assert s["export_dir"] == str(tmp_path / "exports/tokyo_girl")
    assert TG.settings({"tokyo_girl": {"per_day": 6}})["per_day"] == 6


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
    s["review"] = False  # old flow: straight to exports (review mode is tested in test_review_modes.py)
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
    media = tmp_path / "data/rockstar_media"
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


# ---- delivery to Discord ------------------------------------------------------------
class FakeDiscord:
    def __init__(self, codes=None):
        self.calls, self.codes = [], list(codes or [])

    def __call__(self, url, payload, path, filename=None):
        self.calls.append({"url": url, "payload": payload, "file": path.read_bytes() if path else None,
                           "filename": filename})
        return self.codes.pop(0) if self.codes else 204


class FakeGCS:
    made = []

    def __init__(self, bucket, key_file):
        self.bucket, self.key_file, self.uploaded, self.cleaned = bucket, key_file, None, False
        FakeGCS.made.append(self)

    def upload(self, path, object_name, days):
        self.uploaded, self.object_name = path.read_bytes(), object_name
        return f"https://storage.googleapis.com/{self.bucket}/{object_name}?X-Goog-Signature=abc"

    def cleanup(self, keep_days, now):
        self.cleaned = True
        return 0


def export(tmp_path, size: int, **kw):
    s = cfg(tmp_path, **kw)
    s["delivery"]["webhook_url"] = "https://discord.example/webhook"
    d = Path(s["export_dir"])
    d.mkdir(parents=True)
    mp4 = d / "2026-11-16_0647-1_couple_partners.mp4"
    mp4.write_bytes(bytes(range(256)) * (size // 256))
    story = next(x for x in TG.STORIES if x.id == "partners")
    mp4.with_suffix(".txt").write_text(TG.caption_file(story, TODAY, s))
    TG.save_state(Path(s["state_file"]), {"made": [{"file": mp4.name, "story": "partners", "seconds": 18.0}]})
    return s, mp4


def test_small_edit_is_attached_untouched(tmp_path):
    s, mp4 = export(tmp_path, 2_000_000)
    original = mp4.read_bytes()
    fake = FakeDiscord()
    assert TG.deliver(s, mp4, post=fake, uploader_factory=FakeGCS) == "attached"
    (call,) = fake.calls
    assert call["file"] == original and mp4.read_bytes() == original      # byte-for-byte, no re-encode
    content = call["payload"]["content"]
    assert content.startswith("**Tokyo Girl**") and call["filename"] == mp4.name
    assert "18s" in content and "partners" in content and "2.0 MB" in content
    assert "```Jason & Lucia: partners in crime, literally. Couple goals or red flag? Song: Tokyo Girl by @PassiveBand #GTA6```" in content
    assert "```Jason & Lucia: partners in crime, literally. Couple goals or red flag? Song: Tokyo Girl by @Passive #GTA6```" in content
    assert call["payload"]["allowed_mentions"] == {"parse": []}


def test_big_edit_goes_to_bucket_link(tmp_path):
    FakeGCS.made.clear()
    s, mp4 = export(tmp_path, 12_000_000, gcs_bucket="tg-bucket")
    original = mp4.read_bytes()
    fake = FakeDiscord()
    assert TG.deliver(s, mp4, now=1_800_000_000, post=fake, uploader_factory=FakeGCS) == "link"
    (gcs,) = FakeGCS.made
    assert gcs.uploaded == original and gcs.cleaned and gcs.key_file == str(tmp_path / "gcs-key.json")
    assert gcs.object_name == f"videos/tokyo_girl/{mp4.name}"
    (call,) = fake.calls
    assert call["file"] is None                                             # link only, no attachment
    emb = call["payload"]["embeds"][0]
    assert emb["url"].startswith("https://storage.googleapis.com/tg-bucket/") and "12.0 MB" in emb["title"]
    assert f"<t:{1_800_000_000 + 7 * 86400}:f>" in emb["description"]


def test_discord_413_falls_back_to_link_and_boost_limit(tmp_path):
    s, mp4 = export(tmp_path, 3_000_000, gcs_bucket="b")
    fake = FakeDiscord([413, 204])
    assert TG.deliver(s, mp4, post=fake, uploader_factory=FakeGCS) == "link"
    assert fake.calls[0]["file"] and fake.calls[1]["file"] is None
    s2, mp42 = export(tmp_path / "boosted", 30_000_000, discord_max_mb=50)
    assert TG.deliver(s2, mp42, post=FakeDiscord(), uploader_factory=FakeGCS) == "attached"


def test_big_edit_without_bucket_gets_a_card(tmp_path):
    s, mp4 = export(tmp_path, 12_000_000)
    fake = FakeDiscord()
    assert TG.deliver(s, mp4, post=fake, uploader_factory=FakeGCS) == "card"
    assert mp4.name in fake.calls[0]["payload"]["embeds"][0]["description"]


def test_failed_delivery_is_retried_later(tmp_path, monkeypatch):
    s, mp4 = export(tmp_path, 1_000_000)
    now = datetime(2026, 11, 16, 6, 50, tzinfo=TG.UK).timestamp()
    monkeypatch.setattr(TG.deliver, "__defaults__", (None, FakeDiscord([500]), FakeGCS))
    assert TG.deliver_safe(s, mp4, now) is None                             # Discord down: no crash
    assert "delivered" not in TG.load_state(Path(s["state_file"]))["made"][0]
    assert TG.deliver_pending(s, now + 86400) == [mp4.name]                 # next run sends it
    assert TG.load_state(Path(s["state_file"]))["made"][0]["delivered"] == "attached"
    assert TG.deliver_pending(s, now + 2 * 86400) == []                     # only once


def test_no_webhook_means_off(tmp_path):
    s, mp4 = export(tmp_path, 1000)
    s["delivery"]["webhook_url"] = ""
    assert TG.deliver(s, mp4, post=FakeDiscord()) == "off" and TG.deliver_pending(s) == []


def test_old_tokyo_girl_delivery_names_still_work():
    from spikebot.main import apply_env
    c = apply_env({"tokyo_girl_webhook_url": "https://d/x", "tokyo_girl": {"discord_max_mb": 50}}, [],
                  {"TOKYO_GIRL_GCS_BUCKET": "gs://my-bucket/"})
    d = TG.settings(c, Path("/opt/spikebot"))["delivery"]
    assert d["gcs_bucket"] == "my-bucket" and d["webhook_url"] == "https://d/x"
    assert d["gcs_key_file"] == "/opt/spikebot/gcs-key.json" and d["discord_max_mb"] == 50
    with pytest.raises(ValueError):
        TG.settings({"tokyo_girl": {"gcs_link_days": 8}})


def test_signed_link_is_made_offline_with_a_key(tmp_path):
    pytest.importorskip("google.cloud.storage")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    kf = tmp_path / "key.json"
    kf.write_text(json.dumps({"type": "service_account", "project_id": "p", "private_key_id": "1",
                              "private_key": pem, "client_email": "up@p.iam.gserviceaccount.com",
                              "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token"}))
    up = TG.delivery.GCSUploader("tg-bucket", str(kf))
    blob = up.bucket.blob("tokyo_girl/x.mp4")
    url = blob.generate_signed_url(version="v4", method="GET", expiration=7 * 86400,
                                   response_disposition='attachment; filename="x.mp4"')
    assert url.startswith("https://storage.googleapis.com/tg-bucket/tokyo_girl/x.mp4?")
    assert "X-Goog-Expires=604800" in url and "X-Goog-Signature=" in url


def test_no_media_fetches_once_and_makes_no_failed_items(tmp_path, monkeypatch):
    s = cfg(tmp_path, per_day=2)
    s["review"] = True
    calls = []
    monkeypatch.setattr(TG.Library, "shots", lambda self, entries=None: [])
    monkeypatch.setattr(TG, "fetch", lambda lcfg: calls.append(1))
    now = datetime(2026, 11, 16, 6, 50, tzinfo=TG.UK).timestamp()
    TG.save_state(Path(s["state_file"]), {"last_fetch": now})   # periodic refetch not due
    assert TG.daily(s, now) == []
    assert calls == [1]                                            # fetched because the library was empty
    assert not (Path(s["data_dir"]) / "review" / "items").exists()
    with pytest.raises(SystemExit):
        TG.main(["--config", str(tmp_path / "none.json"), "make"])


def test_legacy_media_dir_moves_to_shared_library(tmp_path):
    s = TG.settings({"tokyo_girl": {"media_dir": "data/tokyo_girl/media"}}, tmp_path)
    assert Path(s["media_dir"]) == tmp_path / "data/rockstar_media"
    old = tmp_path / "data/tokyo_girl/media"
    old.mkdir(parents=True)
    (old / "a.jpg").write_bytes(b"x")                      # old folder in use: keep it
    s = TG.settings({"tokyo_girl": {"media_dir": "data/tokyo_girl/media"}}, tmp_path)
    assert Path(s["media_dir"]) == old
    assert Path(TG.settings({}, tmp_path)["media_dir"]) == tmp_path / "data/rockstar_media"


def test_remotion_watchdog_kills_silent_hang(tmp_path):
    import sys
    from spikebot import remotion
    cmd = [sys.executable, "-c", "print('Rendered 1/10', flush=True); import time; time.sleep(60)"]
    t = time.time()
    with pytest.raises(remotion.RemotionError, match="stalled"):
        remotion.run_with_progress(cmd, tmp_path, dict(os.environ), timeout=120, stall_s=1)
    assert time.time() - t < 15


def test_remotion_concurrency():
    from spikebot import remotion
    assert remotion.concurrency({"remotion_concurrency": 3}) == 3
    assert 1 <= remotion.concurrency({}) <= 4


def test_remotion_memory_options(tmp_path, monkeypatch):
    from spikebot import remotion
    seen = {}

    def fake_run(cmd, cwd, env, timeout):
        seen["cmd"] = cmd
        Path(cmd[4]).write_bytes(b"x")
        return 0, ""
    monkeypatch.setattr(remotion, "available", lambda root=None: True)
    monkeypatch.setattr(remotion, "props_for", lambda *a: ({}, {}))
    monkeypatch.setattr(remotion, "run_with_progress", fake_run)
    monkeypatch.setattr(remotion.subprocess, "run", lambda *a, **k: None)
    ram = tmp_path / "shm"
    lcfg = {"media_dir": str(tmp_path), "data_dir": str(tmp_path), "remotion_tmp": str(ram),
            "remotion_cache_mb": 2048, "remotion_concurrency": 4}
    remotion.render(None, lcfg, tmp_path / "o.mp4", {}, [])
    assert "--offthreadvideo-cache-size-in-bytes=2147483648" in seen["cmd"]
    assert "--concurrency=4" in seen["cmd"]
    assert seen["cmd"][4].startswith(str(ram))


def test_parallel_remotion_renders_share_cpus_and_cache(tmp_path, monkeypatch):
    from spikebot import remotion
    seen = {}
    monkeypatch.setattr(remotion, "available", lambda root=None: True)
    monkeypatch.setattr(remotion, "props_for", lambda *a: ({}, {}))
    monkeypatch.setattr(remotion.subprocess, "run", lambda *a, **k: None)

    def fake_run(cmd, cwd, env, timeout):
        seen["cmd"] = cmd
        Path(cmd[4]).write_bytes(b"x")
        return 0, ""
    monkeypatch.setattr(remotion, "run_with_progress", fake_run)
    work = tmp_path / "w"
    lcfg = {"media_dir": str(tmp_path), "data_dir": str(tmp_path), "remotion_tmp": str(work),
            "remotion_cache_mb": 4096, "remotion_concurrency": 4}
    (work / "active").mkdir(parents=True)
    (work / "active" / f"{os.getpid()}-other").touch()     # another live render
    (work / "active" / "999999999-dead").touch()           # a crashed one: ignored + cleaned
    remotion.render(None, lcfg, tmp_path / "o.mp4", {}, [])
    assert "--concurrency=2" in seen["cmd"]
    assert f"--offthreadvideo-cache-size-in-bytes={2048 * 1024 * 1024}" in seen["cmd"]
    assert not (work / "active" / "999999999-dead").exists()
    assert [f.name for f in (work / "active").iterdir()] == [f"{os.getpid()}-other"]


def _cut_plan(offset, dur=1.0, tail=0.0):
    shot = TG.Shot("t.mp4", "video", 10.0, 14.0, ["x"])
    story = TG.STORIES[0]
    sl = TG.Slot(shot, offset, 2, 0.0, dur, 0, False, 1)
    other = TG.Slot(img_shot("a.jpg", ["x"]), 0.0, 2, dur, 1.0, 0, False, 1)
    return TG.Plan(story, [], [sl, other], dur + 1.0)


def test_clean_plan_moves_clip_off_a_missed_cut():
    cuts = lambda sh: [12.4]
    # clean already: untouched
    assert TG.clean_plan(_cut_plan(10.5), Path("."), cuts_of=cuts).slots[0].offset == 10.5
    # window 11.9-12.9 crosses 12.4: moves to the nearest clean side (before the cut)
    off = TG.clean_plan(_cut_plan(11.9), Path("."), cuts_of=cuts).slots[0].offset
    assert off + 1.0 <= 12.4 and off == pytest.approx(12.4 - 0.05 - 1.0)
    # the crossfade tail counts too
    off = TG.clean_plan(_cut_plan(11.3), Path("."), tail=0.45, cuts_of=cuts).slots[0].offset
    assert off + 1.45 <= 12.4 or off >= 12.4
    # nothing changes for stills
    assert TG.clean_plan(_cut_plan(11.9), Path("."), cuts_of=cuts).slots[1].offset == 0.0


@needs_ffmpeg
def test_window_cuts_finds_a_dark_cut(tmp_path):
    """Two dark shots (the kind the library scan missed) joined at 1.0 s."""
    src = tmp_path / "dark.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1,eq=brightness=-0.35",
                    "-f", "lavfi", "-i", "mandelbrot=s=320x180:r=30,trim=duration=1,eq=brightness=-0.35",
                    "-filter_complex", "[0][1]concat=n=2:v=1", "-pix_fmt", "yuv420p", str(src)], check=True)
    cuts = TG.window_cuts(src, 0.0, 2.0)
    assert len(cuts) == 1 and abs(cuts[0] - 1.0) < 0.1
