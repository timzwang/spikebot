"""Tokyo Girl mode: silent 9:16 campaign edits from official GTA 6 media, for
Tim to post by hand on TikTok / Instagram with the official "Tokyo Girl" sound.

Sources are only Rockstar's own GTA VI trailers and screenshots
(rockstargames.com/VI/media and the Rockstar Games YouTube channel). Never stream
clips, never voices or dialogue: every export has NO audio track at all.

Each edit:
  - 1080x1920, 30 fps, 15-30 s (whole bars, so it ends on the beat)
  - cuts land on a 120 BPM grid (0.5 s per beat): mixes of 1/2/4-beat shots,
    a white flash on every new text card, slow pans so stills don't sit dead
  - Vice City views, cars, Jason & Lucia (tags from file names / tags.json)
  - big burned-in text telling a hook/story (news, theory, meme or couple joke),
    always ending on a question to pull comments
  - no logo, watermark or channel name
  - exports/tokyo_girl/<date>_<time>_<kind>_<story>.mp4 plus a .txt with the IG
    and TikTok captions

CLI (run from /opt/spikebot):
  python -m spikebot.tokyo_girl fetch       download official media (best effort)
  python -m spikebot.tokyo_girl scan        analyse the media folder (shots, tags)
  python -m spikebot.tokyo_girl make [-n N] [--story ID]
  python -m spikebot.tokyo_girl daily       what the timer runs: fetch if due, top up to per_day
  python -m spikebot.tokyo_girl stories     list the text stories and which are live today
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import delivery, editor

log = logging.getLogger("tokyo_girl")
UK = ZoneInfo("Europe/London")
LAUNCH = date(2026, 11, 19)  # GTA 6 release day
W, H, FPS = 1080, 1920, 30

DEFAULTS = {
    "enabled": True,
    "per_day": 2,                       # edits made per UK day by `daily`
    "bpm": 120,
    "min_s": 15,
    "max_s": 30,
    "media_dir": "data/tokyo_girl/media",
    "export_dir": "exports/tokyo_girl",
    "state_file": "data/tokyo_girl/state.json",
    "sources": [
        "https://www.rockstargames.com/VI/media",
        "https://www.youtube.com/watch?v=QdBZY2fkU-0",   # GTA VI Trailer 1 (Rockstar Games)
        "https://www.youtube.com/watch?v=VQRLujxTm3c",   # GTA VI Trailer 2 (Rockstar Games)
    ],
    "refetch_days": 7,
    "trailer_head_s": 6.0,              # skip Rockstar logo intro
    "trailer_tail_s": 15.0,             # skip end cards / logo / release date card
    "min_luma": 28,                     # darker shots (black, title-on-black) are skipped
    "scene_threshold": 0.3,
    "font": "DejaVu Sans",
    "font_size": 104,
    "caption_ig": "{hook} Song: Tokyo Girl by @PassiveBand #GTA6",
    "caption_tiktok": "{hook} Song: Tokyo Girl by @Passive #GTA6",
}

TRUE = {"1", "true", "yes", "on"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
# Only official Rockstar hosts. YouTube downloads are also filtered to the
# Rockstar Games channel, so a stray link can't pull in fan or leaked footage.
ALLOWED_HOSTS = ("rockstargames.com", "media-rockstargames-com.akamaized.net",
                 "youtube.com", "youtu.be")


def settings(cfg: dict, base: Path | None = None) -> dict:
    """defaults < config.json "tokyo_girl" block < TOKYO_GIRL_* (main.apply_env).
    Relative paths are resolved against `base` (the config file's folder)."""
    out = json.loads(json.dumps(DEFAULTS))
    out.update(cfg.get("tokyo_girl") or {})
    if cfg.get("tokyo_girl_mode") not in (None, ""):
        out["enabled"] = str(cfg["tokyo_girl_mode"]).strip().lower() in TRUE
    if cfg.get("tokyo_girl_per_day") not in (None, ""):
        out["per_day"] = int(cfg["tokyo_girl_per_day"])
    out["delivery"] = delivery.settings(cfg, base)  # #ready-to-post, shared by every mode
    from . import review
    out["review"] = review.settings(cfg)["enabled"]  # True: 3 variants wait for approval
    data_dir = Path(cfg.get("data_dir", "data"))
    out["data_dir"] = str(data_dir if data_dir.is_absolute() or base is None else base / data_dir)
    out["_cfg"], out["_base"] = cfg, base
    if not 1 <= out["min_s"] <= out["max_s"]:
        raise ValueError("tokyo_girl: need 1 <= min_s <= max_s")
    if base is not None:
        for k in ("media_dir", "export_dir", "state_file"):
            p = Path(out[k])
            out[k] = str(p if p.is_absolute() else base / p)
    return out


def beat_s(bpm: float) -> float:
    return 60.0 / bpm


def bar_s(bpm: float) -> float:
    return 4 * beat_s(bpm)


# --------------------------------------------------------------------------
# Stories: on-screen text. *word* = yellow highlight. Last card is the question.
# days = (19 Nov 2026 - today); `days` range limits when a story can be used.
# --------------------------------------------------------------------------
@dataclass
class Story:
    id: str
    kind: str                    # news / theory / meme / couple
    caption: str                 # the <hook> in the IG/TikTok caption
    cards: list[str]
    days: tuple[int, int] | None = None   # (min, max) days to launch, None = any time
    tags: list[str] = field(default_factory=list)  # preferred footage


ANY, PRE, WEEK, POST, FRESH = None, (1, 9999), (1, 6), (-99999, 0), (-14, 0)
CITY, CARS, COUPLE = ["city", "cars", "couple"], ["cars", "city"], ["couple", "jason", "lucia", "city"]

STORIES = [
    # news
    Story("countdown", "news", "GTA 6 drops in {days_text}. Day one or waiting for reviews?",
          ["GTA 6 drops in *{days_text}*", "Vice City. Leonida. *Thursday.*",
           "Jason & Lucia are almost here", "Day one or waiting for reviews?"], WEEK, CITY),
    Story("until-vice", "news", "{days_text} until Vice City. Where are you playing it?",
          ["*{days_text}* until Vice City", "13 years after GTA 5", "This is what we waited for",
           "PS5 or Xbox: where are you playing it?"], PRE, CITY),
    Story("clear-weekend", "news", "GTA 6 lands Thursday 19 November. Who's taking the day off?",
          ["The countdown is basically over", "GTA 6: *Thursday 19 November*",
           "Clear your whole weekend", "Who's taking the day off for it?"], WEEK, CITY),
    Story("finally-out", "news", "GTA 6 is finally out. What's the first thing you did?",
          ["GTA 6 is *finally* out", "Vice City is back after 24 years", "And it looks unreal",
           "What's the first thing you did?"], FRESH, CITY),
    Story("one-more", "news", "GTA 6 week one: just one more mission. What time did you sleep?",
          ["GTA 6 week one be like:", "\"Just one more mission\"", "*4am.*",
           "What time did you finally sleep?"], FRESH, CARS),
    Story("leonida", "news", "Leonida looks insane. Which spot are you visiting first?",
          ["Leonida looks *insane*", "Beaches. Swamps. Neon.", "Rockstar did not hold back",
           "Which spot are you visiting first?"], ANY, CITY),
    Story("duo", "news", "Meet the duo running Vice City. Jason or Lucia: who's the brains?",
          ["Meet the duo running Vice City", "Jason *and* Lucia", "Ride or die energy",
           "Jason or Lucia: who's the brains?"], ANY, COUPLE),
    Story("glow-up", "news", "Vice City 2002 vs 2026. Did you play the original?",
          ["Vice City in GTA 6", "vs the one we got in *2002*", "Glow up of the century",
           "Did you play the original?"], ANY, CITY),
    Story("detail", "news", "The detail in GTA 6 is unreal. What did you notice?",
          ["The detail in GTA 6", "is *genuinely* unreal", "Pause on any frame",
           "What detail did you notice first?"], ANY, CITY),
    # theory (speculation only, from official trailers; no leaks, no plot spoilers)
    Story("lucia-boss", "theory", "Theory: Lucia is the real boss. Agree or disagree?",
          ["*Theory:* Lucia is the real boss", "Jason drives", "Lucia makes the plan",
           "Agree or disagree?"], ANY, COUPLE),
    Story("every-car", "theory", "Theory: every car in the trailers is drivable. Which one are you taking first?",
          ["*Theory:* every trailer car is drivable", "Count how many there are",
           "Rockstar shows nothing by accident", "Which car are you stealing first?"], ANY, CARS),
    Story("hidden-best", "theory", "Theory: the trailers hid the best part. What do you hope they kept secret?",
          ["*Theory:* the trailers hid the best part", "Rockstar never shows everything",
           "The real surprises are in-game", "What do you hope they kept secret?"], ANY, CITY),
    Story("biggest-city", "theory", "Theory: Vice City is the biggest GTA city yet. Bigger than Los Santos?",
          ["*Theory:* biggest GTA city yet", "Just look at that skyline",
           "And that's only part of Leonida", "Bigger than Los Santos?"], ANY, CITY),
    # meme
    Story("live-here", "meme", "Me after 1 hour in Vice City. Who else is moving to Leonida?",
          ["Me after 1 hour in Vice City:", "*\"I live here now\"*", "Rent: unpaid",
           "Who else is moving to Leonida?"], ANY, CITY),
    Story("nobody-3am", "meme", "Me in GTA 6 at 3am. Law-abiding or chaos player?",
          ["Nobody:", "Me in GTA 6:", "*Speeding through Vice City at 3am*",
           "Law-abiding or chaos player?"], ANY, CARS),
    Story("free-roam", "meme", "GTA 6 day one: ignore every mission. Story first or free roam first?",
          ["GTA 6 players on day one:", "Ignore every mission", "Just drive around *looking at stuff*",
           "Story first or free roam first?"], ANY, CARS),
    Story("driving-test", "meme", "My GTA 6 driving test. Rate your GTA driving 1-10?",
          ["My GTA 6 driving test:", "Red lights? *Suggestions.*", "Sidewalk? *Shortcut.*",
           "Rate your GTA driving 1-10?"], ANY, CARS),
    Story("sunrise", "meme", "POV: just one quick mission. How late did you stay up?",
          ["POV: \"just one quick mission\"", "It's now *sunrise* in Vice City", "And in real life",
           "How late did you stay up?"], POST, CITY),
    Story("first-list", "meme", "Things I'm doing first in GTA 6. What's #1 on your list?",
          ["First things I'm doing in GTA 6:", "1. Steal the fastest car", "2. Drive to the beach",
           "3. Forget the story exists", "What's #1 on your list?"], ANY, CARS),
    # couple jokes (Jason & Lucia)
    Story("partners", "couple", "Jason & Lucia: partners in crime, literally. Couple goals or red flag?",
          ["Jason & Lucia", "Partners in crime. *Literally.*", "Couple goals or red flag?"], ANY, COUPLE),
    Story("date-heist", "couple", "Her: let's go on a date. Me: Vice City heist. Who'd be your Lucia?",
          ["Her: let's go on a date", "Me: *Vice City heist*", "Jason & Lucia energy",
           "Who'd be your Lucia?"], ANY, COUPLE),
    Story("getaway-look", "couple", "Find someone who looks at you like Lucia looks at a getaway car. Who's your ride or die?",
          ["Find someone who looks at you", "like Lucia looks at a *getaway car*",
           "Who's your ride or die?"], ANY, COUPLE),
    Story("one-job", "couple", "Jason: babe it's one job. Would you trust Jason with the plan?",
          ["Jason: \"babe, it's one job\"", "The job:", "*A full Vice City heist*",
           "Would you trust Jason with the plan?"], ANY, COUPLE),
    Story("date-night", "couple", "Date night in Vice City. Would you date a Jason or a Lucia?",
          ["Date night in Vice City:", "Dinner? No.", "*A robbery* and a sunset drive",
           "Would you date a Jason or a Lucia?"], ANY, COUPLE),
    Story("you-drive", "couple", "Every duo playing GTA 6: babe, you drive. Who drives in your duo?",
          ["Every duo playing GTA 6:", "\"Babe, you drive\"", "*Crashes in 4 seconds*",
           "Who drives in your duo?"], ANY, COUPLE),
    Story("teamwork", "couple", "Lucia picks the target, Jason picks the car. Which one are you?",
          ["Lucia picking the target", "Jason picking the getaway car", "*Teamwork.*",
           "Which one are you in a heist?"], ANY, COUPLE),
]
KINDS = ("news", "theory", "meme", "couple")


def days_to_launch(today: date) -> int:
    return (LAUNCH - today).days


def days_text(days: int) -> str:
    return "1 day" if days == 1 else f"{days} days"


def eligible(story: Story, today: date) -> bool:
    if story.days is None:
        return True
    lo, hi = story.days
    return lo <= days_to_launch(today) <= hi


def fill(text: str, today: date) -> str:
    return text.replace("{days_text}", days_text(max(days_to_launch(today), 0)))


def plain(text: str) -> str:
    return text.replace("*", "")


def pick_story(today: date, recent: list[str], rng: random.Random,
               story_id: str | None = None) -> Story:
    """A story that is live today, not used recently, preferably a different kind
    from the last edit."""
    if story_id:
        for s in STORIES:
            if s.id == story_id:
                return s
        raise ValueError(f"no story {story_id!r} (see: python -m spikebot.tokyo_girl stories)")
    live = [s for s in STORIES if eligible(s, today)]
    by_id = {s.id: s for s in STORIES}
    last_kind = by_id[recent[-1]].kind if recent and recent[-1] in by_id else None
    fresh = [s for s in live if s.id not in recent]
    pool = [s for s in fresh if s.kind != last_kind] or fresh
    if not pool:  # everything used: take the one used longest ago
        order = {sid: i for i, sid in enumerate(recent)}
        pool = sorted(live, key=lambda s: order.get(s.id, -1))[:1]
    return rng.choice(pool)


def captions(story: Story, today: date, lcfg: dict) -> str:
    hook = plain(fill(story.caption, today)).strip()
    return (lcfg["caption_ig"].format(hook=hook).strip() + "\n"
            + lcfg["caption_tiktok"].format(hook=hook).strip() + "\n")


def caption_file(story: Story, today: date, lcfg: dict) -> str:
    ig, tt = captions(story, today, lcfg).splitlines()
    return f"IG: {ig}\nTikTok: {tt}\n"


# --------------------------------------------------------------------------
# Media library: shots from trailers, stills from screenshots
# --------------------------------------------------------------------------
TAG_WORDS = {
    "jason": {"jason", "duval"},
    "lucia": {"lucia", "caminos"},
    "couple": {"couple", "duo", "jasonandlucia", "jasonlucia", "luciaandjason"},
    "city": {"vice", "city", "vicecity", "beach", "skyline", "ocean", "downtown", "leonida",
             "keys", "sunset", "night", "port", "grassrivers", "kalaga", "ambrosia",
             "gellhorn", "street", "streets", "neon", "marina", "aerial", "landscape", "place",
             "places"},
    "cars": {"car", "cars", "drive", "driving", "chase", "highway", "bike", "motorcycle",
             "boat", "race", "racing", "truck", "vehicle", "vehicles", "convertible"},
    "trailer": {"trailer"},
}


def tags_for(name: str) -> list[str]:
    low = name.lower()
    tokens = set(re.split(r"[^a-z0-9]+", low))
    out = {t for t, words in TAG_WORDS.items() if tokens & words}
    if "jason" in low:
        out.add("jason")
    if "lucia" in low:
        out.add("lucia")
    if {"jason", "lucia"} <= out:
        out.add("couple")
    return sorted(out)


@dataclass
class Shot:
    src: str                 # path relative to media_dir
    kind: str                # video / image
    start: float
    end: float               # inf for stills
    tags: list[str]
    crop: list[int] | None = None   # letterbox crop w, h, x, y in the source
    w: int = 0
    h: int = 0

    @property
    def key(self) -> str:
        return f"{self.src}@{self.start:.1f}" if self.kind == "video" else self.src

    @property
    def length(self) -> float:
        return self.end - self.start


def parse_meta(text: str) -> list[tuple[float, dict]]:
    """ffmpeg `metadata=print` output -> [(pts_time, {key: value})]."""
    out: list[tuple[float, dict]] = []
    for line in text.splitlines():
        m = re.search(r"pts_time:(-?[\d.]+)", line)
        if m:
            out.append((float(m.group(1)), {}))
        elif "=" in line and out:
            k, v = line.split("=", 1)
            out[-1][1][k.strip()] = v.strip()
    return out


def analyse_video(path: Path, threshold: float = 0.3) -> dict:
    """One decode pass: scene cuts, brightness twice a second and letterbox bars."""
    info = editor.probe(path)
    with tempfile.TemporaryDirectory() as tmp:
        graph = ("[0:v]split=3[a][b][c];"
                 f"[a]scale=320:-2,select='gt(scene,{threshold})',metadata=print:file=scenes.txt[o1];"
                 "[b]fps=2,scale=160:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=yavg.txt[o2];"
                 "[c]fps=1,cropdetect=limit=24:round=2:reset=1,metadata=print:file=crop.txt[o3]")
        editor.run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(path.resolve()),
                    "-filter_complex", graph, "-map", "[o1]", "-f", "null", "-",
                    "-map", "[o2]", "-f", "null", "-", "-map", "[o3]", "-f", "null", "-"],
                   timeout=1800, cwd=Path(tmp))
        read = lambda n: (Path(tmp) / n).read_text(errors="replace") if (Path(tmp) / n).exists() else ""
        cuts = [t for t, _ in parse_meta(read("scenes.txt"))]
        yavg = [[round(t, 2), round(float(d["lavfi.signalstats.YAVG"]), 1)]
                for t, d in parse_meta(read("yavg.txt")) if "lavfi.signalstats.YAVG" in d]
        crops = []
        for _, d in parse_meta(read("crop.txt")):
            try:
                c = [int(d[f"lavfi.cropdetect.{k}"]) for k in ("w", "h", "x", "y")]
            except (KeyError, ValueError):
                continue
            if c[0] >= info["width"] * 0.5 and c[1] >= info["height"] * 0.3:
                crops.append(c)
    crop = None
    if crops:
        med = [int(statistics.median(c[i] for c in crops)) for i in range(4)]
        if med[0] * med[1] < 0.97 * info["width"] * info["height"]:
            crop = [editor.even(med[0]), editor.even(med[1]), editor.even_pos(med[2]), editor.even_pos(med[3])]
    return {"kind": "video", "w": info["width"], "h": info["height"], "duration": info["duration"],
            "cuts": [round(c, 3) for c in cuts], "yavg": yavg, "crop": crop}


def build_shots(rel: str, entry: dict, override: dict, lcfg: dict) -> list[Shot]:
    """Turn a cached analysis + tags.json override into usable shots (pure)."""
    if override.get("skip"):
        return []
    tags = sorted(set(override.get("tags") or []) | set(tags_for(rel)))
    if entry["kind"] == "image":
        return [Shot(rel, "image", 0.0, math.inf, tags, None, entry["w"], entry["h"])]
    dur = entry["duration"]
    lo, hi = lcfg["trailer_head_s"], dur - lcfg["trailer_tail_s"]
    bounds = [0.0] + sorted(c for c in entry["cuts"] if 0 < c < dur) + [dur]
    merged = [bounds[0]]
    for b in bounds[1:]:
        if b - merged[-1] >= 0.25:
            merged.append(b)
    merged[-1] = dur
    excl = override.get("exclude") or []
    ranges = override.get("ranges") or []
    out = []
    for s, e in zip(merged, merged[1:]):
        s, e = max(s + 0.1, lo), min(e - 0.1, hi)  # margin keeps the neighbour shot out
        if e - s < 0.55:
            continue
        if any(s < b and e > a for a, b in excl):
            continue
        ys = [y for t, y in entry["yavg"] if s <= t < e]
        if not ys and entry["yavg"]:
            ys = [min(entry["yavg"], key=lambda p: abs(p[0] - (s + e) / 2))[1]]
        if ys and statistics.mean(ys) < lcfg["min_luma"]:
            continue
        stags = set(tags)
        for r in ranges:
            if s < r["end"] and e > r["start"]:
                stags |= set(r.get("tags") or [])
        out.append(Shot(rel, "video", round(s, 3), round(e, 3), sorted(stags), entry.get("crop"),
                        entry["w"], entry["h"]))
    return out


class Library:
    """Everything under media_dir. Analyses are cached in library.json (by size and
    mtime) so only new files are decoded; tags.json can be edited any time."""

    def __init__(self, lcfg: dict):
        self.cfg = lcfg
        self.root = Path(lcfg["media_dir"])
        self.cache_file = self.root / "library.json"
        self.tags_file = self.root / "tags.json"

    def files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(p for p in self.root.rglob("*")
                      if p.is_file() and p.suffix.lower() in VIDEO_EXT | IMAGE_EXT
                      and not any(part.startswith(".") for part in p.relative_to(self.root).parts))

    def scan(self) -> dict:
        try:
            cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            cache = {}
        out = {}
        for p in self.files():
            rel = p.relative_to(self.root).as_posix()
            st = p.stat()
            sig = [st.st_size, int(st.st_mtime)]
            if rel in cache and cache[rel].get("sig") == sig:
                out[rel] = cache[rel]
                continue
            try:
                if p.suffix.lower() in IMAGE_EXT:
                    info = editor.probe(p)
                    entry = {"kind": "image", "w": info["width"], "h": info["height"]}
                else:
                    log.info("analysing %s (one pass, can take a minute)", rel)
                    entry = analyse_video(p, self.cfg["scene_threshold"])
            except (editor.EditError, subprocess.TimeoutExpired, ValueError) as e:
                log.warning("skipping %s: %s", rel, e)
                continue
            if entry["w"] < 320 or entry["h"] < 320:
                log.warning("skipping %s: too small (%sx%s)", rel, entry["w"], entry["h"])
                continue
            entry["sig"] = sig
            out[rel] = entry
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(out, indent=1), encoding="utf-8")
        return out

    def overrides(self) -> dict:
        try:
            return json.loads(self.tags_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as e:
            log.warning("tags.json is not valid JSON, ignoring it: %s", e)
            return {}

    def shots(self, entries: dict | None = None) -> list[Shot]:
        entries = self.scan() if entries is None else entries
        ov = self.overrides()
        out: list[Shot] = []
        for rel, entry in entries.items():
            o = ov.get(rel) or ov.get(Path(rel).name) or {}
            out += build_shots(rel, entry, o, self.cfg)
        return out


# --------------------------------------------------------------------------
# Plan: cards on whole bars, cuts on beats, footage per slot
# --------------------------------------------------------------------------
@dataclass
class Slot:
    shot: Shot
    offset: float      # seconds into the source (videos)
    beats: int
    start: float       # seconds into the edit
    dur: float
    card: int
    flash: bool
    pan: int           # +1 / -1 direction of the slow pan
    punch: bool = False  # zoom-cut: tighter framing on the first shot of a card


@dataclass
class Plan:
    story: Story
    cards: list[tuple[float, float, str]]   # start, end, text
    slots: list[Slot]
    duration: float


BAR_PATTERNS = {  # beats per shot inside one 4-beat bar
    "open": [[2, 2], [2, 2], [4], [2, 1, 1]],
    "mid": [[1, 1, 2], [2, 1, 1], [1, 1, 1, 1], [2, 2], [1, 1, 1, 1], [1, 3]],
    "end": [[4], [2, 2]],
}


def card_bars(n_cards: int, bpm: float, min_s: float, max_s: float, rng: random.Random) -> list[int]:
    """Bars per card: 2 each (4 s at 120 BPM, enough to read), 3 for the question,
    padded to at least min_s and maybe a bar longer, never over max_s."""
    bs = bar_s(bpm)
    lo, hi = math.ceil(min_s / bs - 1e-9), math.floor(max_s / bs + 1e-9)
    if hi < n_cards:
        raise ValueError("max_s too short for this many cards")
    bars = [2] * n_cards
    bars[-1] = 3
    while sum(bars) > hi:
        i = max(range(n_cards), key=lambda k: bars[k])
        bars[i] -= 1
    while sum(bars) < lo:
        i = min(range(n_cards), key=lambda k: (bars[k], k != 0))
        bars[i] += 1
    if sum(bars) < hi and rng.random() < 0.5:
        bars[0] += 1  # sometimes a longer hook
    return bars


PACE_PATTERNS = {
    "slow": {"open": [[4], [2, 2]], "mid": [[2, 2], [4], [2, 2], [3, 1]], "end": [[4]]},
    "normal": BAR_PATTERNS,
    "fast": {"open": [[1, 1, 2], [2, 1, 1], [1, 1, 1, 1]], "mid": [[1, 1, 1, 1], [1, 1, 2], [2, 1, 1]],
             "end": [[2, 2], [1, 1, 2]]},
}


def pace_for_shot(shot_s: float, bpm: float) -> str:
    """A preset's average shot length -> the nearest pacing pattern."""
    beats = shot_s / beat_s(bpm)
    return "fast" if beats < 1.6 else ("normal" if beats < 2.6 else "slow")


def beat_pattern(n_bars: int, rng: random.Random, pace: str = "normal") -> list[list[int]]:
    pats = PACE_PATTERNS.get(pace, BAR_PATTERNS)
    out = []
    for b in range(n_bars):
        kind = "open" if b == 0 else ("end" if b == n_bars - 1 else "mid")
        out.append(list(rng.choice(pats[kind])))
    return out


def plan_clip(story: Story, shots: list[Shot], lcfg: dict, today: date, rng: random.Random,
              uses: dict[str, int] | None = None, pace: str = "normal", transition: str = "flash",
              cards: list[str] | None = None) -> Plan:
    if not shots:
        raise editor.EditError("no usable media: run `fetch`, or drop official Rockstar "
                               f"screenshots/trailers into {lcfg['media_dir']}")
    uses = dict(uses or {})
    bpm = lcfg["bpm"]
    beat = beat_s(bpm)
    texts = [fill(c, today) for c in (cards or story.cards)]
    bars = card_bars(len(texts), bpm, lcfg["min_s"], lcfg["max_s"], rng)
    pattern = beat_pattern(sum(bars), rng, pace)
    cards, slots, t, bar_i = [], [], 0, 0
    want = story.tags or ["city"]
    used_here: set[str] = set()
    k = 0
    for ci, (text, nb) in enumerate(zip(texts, bars)):
        cstart = t * beat
        for _ in range(nb):
            for j, beats in enumerate(pattern[bar_i]):
                dur = beats * beat
                tag = want[k % len(want)]
                k += 1
                shot = choose_shot(shots, tag, dur, used_here, uses, rng)
                used_here.add(shot.key)
                uses[shot.key] = uses.get(shot.key, 0) + 1
                if shot.kind == "video":
                    room = shot.length - dur
                    offset = shot.start + room * rng.uniform(0.2, 0.8)
                else:
                    offset = 0.0
                first_of_card = (t * beat == cstart)
                slots.append(Slot(shot, round(offset, 3), beats, round(t * beat, 3), dur, ci,
                                  flash=first_of_card and ci > 0 and transition == "flash",
                                  pan=rng.choice((-1, 1)),
                                  punch=first_of_card and transition == "zoom"))
                t += beats
            bar_i += 1
        cards.append((cstart, t * beat, text))
    return Plan(story, cards, slots, t * beat)


def choose_shot(shots: list[Shot], tag: str, dur: float, used_here: set[str],
                uses: dict[str, int], rng: random.Random) -> Shot:
    fits = [s for s in shots if s.length >= dur]
    if not fits:
        raise editor.EditError(f"no shot is {dur:.1f}s long")
    for pool in ([s for s in fits if tag in s.tags and s.key not in used_here],
                 [s for s in fits if s.key not in used_here],
                 fits):
        if pool:
            least = min(uses.get(s.key, 0) for s in pool)
            return rng.choice([s for s in pool if uses.get(s.key, 0) <= least + 1])
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# Text (ASS, libass)
# --------------------------------------------------------------------------
def card_markup(text: str, highlight: str = "&H00E5FF&") -> str:
    t = editor.ass_escape(text)
    return re.sub(r"\*(.+?)\*", rf"{{\\c{highlight}}}\1{{\\c&HFFFFFF&}}", t).replace("*", "")


TEXT_POS = {"high": (8, 520), "middle": (5, 0), "low": (2, 560)}  # ASS alignment, MarginV


def build_text_ass(cards: list[tuple[float, float, str]], font: str, size: int,
                   style: dict | None = None) -> str:
    """Big centred text, upper middle (clear of TikTok/IG buttons), pops in on the
    beat. The last card (the question) sits in a dark box so it stands out.
    `style` (style.resolve output) changes font, size, position and colours."""
    from .style import ass_colour
    r = style or {}
    if r.get("font"):
        font = r["font"]
    size = int(r.get("text_px") or size * float(r.get("scale") or 1.0))
    q = int(size * 1.05)
    align, margin = TEXT_POS.get(r.get("position") or "high", TEXT_POS["high"])
    if r.get("text_y") is not None:
        align, margin = 2, int(max(400, min(H - 300, H * (1 - float(r["text_y"])) - size)))
    main = ass_colour(r.get("text_colour") or "#FFFFFF")
    hi = "&H" + ass_colour(r.get("highlight") or "#FFE500")[4:] + "&"
    outline = int(r["outline"]) if r.get("outline") is not None else 9
    shadow = int(r["shadow"]) if r.get("shadow") is not None else 4
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Card,{font},{size},{main},{main},&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},{align},90,90,{margin},1
Style: Ask,{font},{q},&H00FFFFFF,&H00FFFFFF,&H40000000,&H40000000,-1,0,0,0,100,100,0,0,3,22,0,{align},90,90,{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    pop = r"{\fad(60,0)\fscx72\fscy72\t(0,140,\fscx100\fscy100)}"
    lines = []
    for i, (s, e, text) in enumerate(cards):
        style_name = "Ask" if i == len(cards) - 1 else "Card"
        lines.append(f"Dialogue: 0,{editor.ass_time(s)},{editor.ass_time(e)},{style_name},,0,0,0,,"
                     f"{pop}{card_markup(text, hi)}")
    return head + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------
def slot_filter(slot: Slot, zoom: float) -> str:
    """Cover-crop to 1080x1920 with a slow horizontal pan across the shot."""
    sh = slot.shot
    vf = []
    sw, shh = sh.w, sh.h
    if sh.crop:
        cw, ch, cx, cy = sh.crop
        vf.append(f"crop={cw}:{ch}:{cx}:{cy}")
        sw, shh = cw, ch
    s = max(W / sw, H / shh) * zoom
    ow, oh = max(editor.even(sw * s), W), max(editor.even(shh * s), H)
    room = ow - W
    amp = min(160, room)
    x = f"clip({room / 2:.1f}+{slot.pan * amp:.1f}*(t/{slot.dur:.3f}-0.5),0,{room})"
    yroom = oh - H
    y = f"clip({yroom / 2:.1f}+{-slot.pan * min(80, yroom):.1f}*(t/{slot.dur:.3f}-0.5),0,{yroom})"
    vf += [f"scale={ow}:{oh}:flags=bicubic", f"crop={W}:{H}:'{x}':'{y}'",
           f"fps={FPS}", "setsar=1", "format=yuv420p"]
    if slot.flash:
        vf.append("fade=t=in:st=0:d=0.12:color=white")
    return ",".join(vf)


def render_slot(slot: Slot, media: Path, out: Path) -> None:
    src = str((media / slot.shot.src).resolve())
    frames = round(slot.dur * FPS)
    if slot.shot.kind == "video":
        inp = ["-ss", f"{slot.offset:.3f}", "-t", f"{slot.dur + 0.2:.3f}", "-i", src]
        zoom = 1.0 if slot.shot.w / max(slot.shot.h, 1) > 1.2 else 1.08
    else:
        inp = ["-loop", "1", "-framerate", str(FPS), "-t", f"{slot.dur + 0.2:.3f}", "-i", src]
        zoom = 1.15
    if slot.punch:
        zoom *= 1.18
    editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inp, "-an",
                "-vf", slot_filter(slot, zoom), "-frames:v", str(frames),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-r", str(FPS),
                str(out)], timeout=300)


def render_plan(plan: Plan, lcfg: dict, out: Path, grade: str = "", text_style: dict | None = None) -> Path:
    media = Path(lcfg["media_dir"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        names = []
        for i, slot in enumerate(plan.slots):
            seg = tmpd / f"s{i:03d}.mp4"
            render_slot(slot, media, seg)
            names.append(seg.name)
        (tmpd / "list.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
        (tmpd / "text.ass").write_text(build_text_ass(plan.cards, lcfg["font"], lcfg["font_size"], text_style),
                                       encoding="utf-8")
        part = tmpd / "edit.mp4"
        # -an: no audio stream at all. Tim adds the official sound in the app.
        editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", "list.txt", "-vf", (grade + "," if grade else "") + "ass=text.ass", "-an", "-r", str(FPS),
                    "-t", f"{plan.duration:.3f}", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "19", "-maxrate", "9M", "-bufsize", "18M", "-pix_fmt", "yuv420p", "-profile:v", "high",
                    "-movflags", "+faststart", part.name], timeout=1800, cwd=tmpd)
        shutil.move(str(part), out)
    return out


# --------------------------------------------------------------------------
# State, daily quota, make
# --------------------------------------------------------------------------
def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


def made_on(export_dir: Path, day: str) -> int:
    return len(list(export_dir.glob(f"{day}_*.mp4"))) if export_dir.exists() else 0


def make(lcfg: dict, n: int = 1, story_id: str | None = None, now: float | None = None,
         shots: list[Shot] | None = None, seed: int | None = None) -> list[Path]:
    now = now or time.time()
    dt = datetime.fromtimestamp(now, UK)
    today = dt.date()
    state_path = Path(lcfg["state_file"])
    state = load_state(state_path)
    shots = Library(lcfg).shots() if shots is None else shots
    export = Path(lcfg["export_dir"])
    out = []
    for i in range(n):
        rng = random.Random(seed + i if seed is not None else f"{now}-{i}")
        story = pick_story(today, state.get("recent", []), rng, story_id)
        plan = plan_clip(story, shots, lcfg, today, rng, state.get("uses"))
        stem = f"{dt:%Y-%m-%d}_{dt:%H%M}-{i + 1}_{story.kind}_{story.id}"
        mp4 = export / f"{stem}.mp4"
        log.info("making %s (%.0fs, %d cuts)", mp4.name, plan.duration, len(plan.slots))
        render_plan(plan, lcfg, mp4)
        (export / f"{stem}.txt").write_text(caption_file(story, today, lcfg), encoding="utf-8")
        uses = state.setdefault("uses", {})
        for s in plan.slots:
            uses[s.shot.key] = uses.get(s.shot.key, 0) + 1
        state["recent"] = (state.get("recent", []) + [story.id])[-12:]
        state.setdefault("made", []).append({"file": mp4.name, "story": story.id,
                                             "uk": dt.strftime("%Y-%m-%d %H:%M %Z"),
                                             "seconds": plan.duration})
        state["made"] = state["made"][-500:]
        save_state(state_path, state)
        out.append(mp4)
        deliver_safe(lcfg, mp4, now)
    return out


# --------------------------------------------------------------------------
# Review variants (spikebot/review.py): 3 versions per edit, Tim approves one
# --------------------------------------------------------------------------
def tg_captions(story: Story, today: date, lcfg: dict, hook: str | None = None) -> dict[str, str]:
    h = hook or plain(fill(story.caption, today)).strip()
    return {"instagram": lcfg["caption_ig"].format(hook=h).strip(),
            "tiktok": lcfg["caption_tiktok"].format(hook=h).strip()}


def pick_variant_story(today: date, recent: list[str], kind: str | None, taken: set[str],
                       rng: random.Random, hook_style: str | None = None) -> Story:
    from .style import hook_style as classify
    live = [s for s in STORIES if eligible(s, today) and s.id not in taken]
    if not live:
        live = [s for s in STORIES if eligible(s, today)]
    pool = [s for s in live if s.kind == kind] or live
    fresh = [s for s in pool if s.id not in recent] or pool
    if hook_style:
        fresh = [s for s in fresh if classify(plain(fill(s.cards[0], today))) == hook_style] or fresh
    return rng.choice(fresh)


class ReviewAdapter:
    """Renders Tokyo Girl review variants: each has its own story (hook wording),
    pacing pattern, colour grade, text style and transition. Always silent."""

    def __init__(self, cfg: dict, base: Path, lcfg: dict | None = None, shots: list[Shot] | None = None):
        self.cfg, self.base = cfg, base
        self.lcfg = lcfg or settings(cfg, base)
        self._shots = shots

    def shots(self) -> list[Shot]:
        if self._shots is None:
            self._shots = Library(self.lcfg).shots()
        return self._shots

    def render(self, item: dict, specs: list[dict], n: int, folder: Path, preview_bytes: int) -> list[dict]:
        from . import style as st
        today = date.fromisoformat(item["source"]["day"])
        state_path = Path(self.lcfg["state_file"])
        state = load_state(state_path)
        taken = {v["story"] for r in item["rounds"] for v in r["variants"]} if n > 1 else set()
        out = []
        for spec in specs:
            r = st.resolve(spec["style"])
            base_v = spec.get("from")
            texts = spec.get("texts") or {}
            if base_v:
                story = next(x for x in STORIES if x.id == base_v["story"])
                seed = base_v["seed"]
            else:
                rng0 = random.Random(f"{item['id']}-{n}-{spec['label']}")
                story = pick_variant_story(today, state.get("recent", []), spec["style"].get("hook"),
                                           taken, rng0, r.get("hook_style"))
                seed = rng0.randrange(1_000_000)
            taken.add(story.id)
            cards = list(base_v["cards"]) if base_v and base_v.get("cards") else list(story.cards)
            if texts.get("hook"):
                cards[0] = texts["hook"]
            pace = spec["style"].get("pace", "normal")
            if r["pace"].get("shot_s"):
                pace = pace_for_shot(r["pace"]["shot_s"], self.lcfg["bpm"])
            plan = plan_clip(story, self.shots(), self.lcfg, today, random.Random(seed), state.get("uses"),
                             pace=pace, transition=r["transition"], cards=cards)
            stem = f"r{n}_{spec['label']}"
            mp4 = folder / f"{stem}.mp4"
            render_plan(plan, self.lcfg, mp4, st.grade_filter(r["grade"]), r)
            prev = editor.preview(mp4, folder / f"{stem}_preview.mp4", preview_bytes, plan.duration)
            uses = state.setdefault("uses", {})
            for sl in plan.slots:
                uses[sl.shot.key] = uses.get(sl.shot.key, 0) + 1
            state["recent"] = (state.get("recent", []) + [story.id])[-12:]
            hook = plain(fill(texts["hook"], today)) if texts.get("hook") else None
            caps = dict(base_v["captions"]) if base_v and not hook else tg_captions(story, today, self.lcfg, hook)
            title = (base_v["title"] if base_v and not hook
                     else (hook or plain(fill(story.caption, today)))[:95])
            out.append({"label": spec["label"], "style": spec["style"], "preset": spec.get("preset"),
                        "experimental": spec.get("experimental", False), "story": story.id, "seed": seed,
                        "cards": cards, "title": title,
                        "description": f"{title}\n\nUnofficial fan edit from official Rockstar GTA VI media."
                                       "\n\n#GTA6 #GTAVI #Shorts",
                        "tags": ["GTA 6", "GTA VI", "Shorts", "Vice City"], "hook": title,
                        "captions": caps, "file": mp4.name, "preview": prev.name,
                        "duration": round(plan.duration, 1), "cuts": len(plan.slots) - 1, "silent": True})
        save_state(state_path, state)
        return out

    def validate(self, item: dict, v: dict) -> None:
        if not (3 <= len(v["title"]) <= 100):
            raise ValueError("the YouTube title must be 3-100 characters")
        v["description"] = f"{v['title']}\n\nUnofficial fan edit from official Rockstar GTA VI media.\n\n#GTA6 #GTAVI #Shorts"

    def finish(self, item: dict, status: str) -> None:
        """Approved edits are also kept in exports/tokyo_girl/ (with the caption .txt)."""
        if status != "done":
            return
        from . import review
        store = review.Store(Path(self.lcfg["data_dir"]))
        src = review.final_path(store, item)
        if not src.exists():
            return
        a = item["approved"]
        v = review.variant_of(item, a["round"], a["label"])
        export = Path(self.lcfg["export_dir"])
        export.mkdir(parents=True, exist_ok=True)
        stem = f"{item['id']}_{v['story']}"
        shutil.copyfile(src, export / f"{stem}.mp4")
        (export / f"{stem}.txt").write_text(f"IG: {v['captions']['instagram']}\nTikTok: {v['captions']['tiktok']}\n",
                                            encoding="utf-8")


def make_review(lcfg: dict, n: int = 1, now: float | None = None, shots: list[Shot] | None = None) -> list[str]:
    """Make n review items (3 variants each). Returns their ids."""
    from . import review
    now = now or time.time()
    dt = datetime.fromtimestamp(now, UK)
    store = review.Store(Path(lcfg["data_dir"]))
    cfg, base = lcfg.get("_cfg") or {}, lcfg.get("_base") or Path(".")
    adapter = ReviewAdapter(cfg, base, lcfg, shots)
    ids = []
    for i in range(n):
        item = store.create("tokyo_girl", {"day": dt.date().isoformat()}, now + i,
                            title=f"Tokyo Girl edit {dt:%d %b} #{i + 1}")
        item = review.render_round(store, item["id"], cfg, base, {"kind": "initial"}, now=now + i, adapter=adapter)
        state_path = Path(lcfg["state_file"])
        state = load_state(state_path)
        state.setdefault("made", []).append({"item": item["id"], "uk": dt.strftime("%Y-%m-%d %H:%M %Z"),
                                             "status": item["status"]})
        state["made"] = state["made"][-500:]
        save_state(state_path, state)
        log.info("Tokyo Girl review item %s: %s", item["id"], item["status"])
        ids.append(item["id"])
    return ids


def items_on(state: dict, day: str) -> int:
    return sum(1 for m in state.get("made", []) if m.get("item") and m.get("uk", "").startswith(day)
               and m.get("status") != "failed")


def daily(lcfg: dict, now: float | None = None) -> list:
    """Top today's edits up to per_day (review items, or exports with review off).
    Safe to run any number of times a day."""
    now = now or time.time()
    if not lcfg["enabled"]:
        log.info("Tokyo Girl mode is off (TOKYO_GIRL_MODE=false)")
        return []
    state = load_state(Path(lcfg["state_file"]))
    if now - state.get("last_fetch", 0) > lcfg["refetch_days"] * 86400:
        try:
            fetch(lcfg)
        except Exception as e:  # never let a fetch problem stop the edits
            log.warning("fetch failed, using media already on disk: %s", e)
        state = load_state(Path(lcfg["state_file"]))
        state["last_fetch"] = now
        save_state(Path(lcfg["state_file"]), state)
    day = datetime.fromtimestamp(now, UK).strftime("%Y-%m-%d")
    if lcfg.get("review"):
        need = lcfg["per_day"] - items_on(load_state(Path(lcfg["state_file"])), day)
        if need <= 0:
            log.info("already %d edits for %s", lcfg["per_day"], day)
            return []
        return make_review(lcfg, need, now=now)
    need = lcfg["per_day"] - made_on(Path(lcfg["export_dir"]), day)
    made: list[Path] = []
    if need <= 0:
        log.info("already %d edits for %s", lcfg["per_day"], day)
    else:
        made = make(lcfg, need, now=now)
    deliver_pending(lcfg, now)  # retry anything Discord/GCS missed last time
    return made


# --------------------------------------------------------------------------
# Delivery to Discord #ready-to-post (spikebot/delivery.py). Never re-encoded.
# --------------------------------------------------------------------------
def deliver(lcfg: dict, mp4: Path, now: float | None = None, post=delivery.post_webhook,
            uploader_factory=delivery.GCSUploader) -> str:
    """Send one export with its captions. Returns attached / link / card / off;
    raises if nothing got through (deliver_pending retries)."""
    state = load_state(Path(lcfg["state_file"]))
    meta = next((m for m in reversed(state.get("made", [])) if m.get("file") == mp4.name), {})
    txt = mp4.with_suffix(".txt")
    lines = txt.read_text(encoding="utf-8").splitlines() if txt.exists() else []
    blocks = [(l.split(":", 1)[0], l.split(":", 1)[1].strip()) for l in lines if ":" in l]
    bits = ([f"{meta['seconds']:g}s"] if meta.get("seconds") else []) + \
           ([meta["story"]] if meta.get("story") else []) + ["silent: add the Tokyo Girl sound in the app"]
    return delivery.send(lcfg["delivery"], mp4, "tokyo_girl", bits, blocks, now=now, post=post,
                         uploader_factory=uploader_factory)


def mark_delivered(lcfg: dict, name: str, how: str) -> None:
    path = Path(lcfg["state_file"])
    state = load_state(path)
    for m in state.get("made", []):
        if m.get("file") == name:
            m["delivered"] = how
    save_state(path, state)


def deliver_safe(lcfg: dict, mp4: Path, now: float | None = None) -> str | None:
    try:
        how = deliver(lcfg, mp4, now)
    except Exception as e:  # delivery must never break making the edits
        log.warning("delivery of %s failed, will retry next run: %s", mp4.name, e)
        return None
    if how != "off":
        mark_delivered(lcfg, mp4.name, how)
        log.info("delivered %s to Discord (%s)", mp4.name, how)
    return how


def deliver_pending(lcfg: dict, now: float | None = None) -> list[str]:
    """Retry recent exports that never reached Discord (last 3 days)."""
    if not lcfg["delivery"].get("webhook_url"):
        return []
    now = now or time.time()
    export = Path(lcfg["export_dir"])
    cutoff = datetime.fromtimestamp(now - 3 * 86400, UK).strftime("%Y-%m-%d")
    done = []
    for m in load_state(Path(lcfg["state_file"])).get("made", []):
        f = export / m.get("file", "")
        if m.get("delivered") or m.get("file", "") < cutoff or not f.is_file():
            continue
        if deliver_safe(lcfg, f, now):
            done.append(f.name)
    return done


# --------------------------------------------------------------------------
# Fetch official media (best effort; the drop folder always works too)
# --------------------------------------------------------------------------
def host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


IMG_RE = re.compile(r"https://media-rockstargames-com\.akamaized\.net/[^\s\"'<>()\\]+?\.(?:jpe?g|png|webp)", re.I)
YT_RE = re.compile(r"(?:youtube\.com/(?:embed/|watch\?v=)|youtu\.be/)([\w-]{11})")


def page_media(html: str) -> tuple[list[str], list[str]]:
    """Image URLs and YouTube ids on a Rockstar page. Thumbnails/icons are skipped."""
    imgs = []
    for u in dict.fromkeys(m.group(0) for m in IMG_RE.finditer(html.replace("\\/", "/"))):
        low = u.lower()
        if any(w in low for w in ("icon", "logo", "favicon", "sprite", "avatar", "thumb", "badge")):
            continue
        imgs.append(u)
    return imgs, list(dict.fromkeys(YT_RE.findall(html)))


def fetch(lcfg: dict) -> None:
    media = Path(lcfg["media_dir"])
    (media / "rockstar").mkdir(parents=True, exist_ok=True)
    (media / "trailers").mkdir(parents=True, exist_ok=True)
    videos: list[str] = []
    for src in lcfg["sources"]:
        if not host_allowed(src):
            log.warning("not an official Rockstar/YouTube host, skipping: %s", src)
            continue
        host = urlparse(src).hostname or ""
        if "youtu" in host:
            videos.append(src)
            continue
        try:
            from curl_cffi import requests as creq
            r = creq.get(src, impersonate="chrome", timeout=30)
            r.raise_for_status()
        except Exception as e:
            log.warning("could not load %s: %s", src, e)
            continue
        imgs, yt = page_media(r.text)
        log.info("%s: %d images, %d videos", src, len(imgs), len(yt))
        videos += [f"https://www.youtube.com/watch?v={v}" for v in yt]
        for u in imgs:
            name = re.sub(r"[^\w.-]+", "_", Path(urlparse(u).path).name)[-120:]
            dest = media / "rockstar" / name
            if dest.exists():
                continue
            try:
                ri = creq.get(u, impersonate="chrome", timeout=60)
                ri.raise_for_status()
                if len(ri.content) > 30_000:  # real screenshots, not UI bits
                    dest.write_bytes(ri.content)
            except Exception as e:
                log.warning("image failed %s: %s", u, e)
    for v in dict.fromkeys(videos):
        # Video only (no audio needed), max 1080p, Rockstar's own channel only.
        p = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist",
                            "-f", "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b[height<=1080]",
                            "--match-filter", "uploader~='(?i)rockstar'",
                            "--download-archive", str(media / ".yt_archive"),
                            "--restrict-filenames", "-o", str(media / "trailers" / "%(title).80s_trailer_%(id)s.%(ext)s"),
                            v], capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            log.warning("yt-dlp failed for %s: %s", v, p.stderr.strip()[-300:])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def load_cfg(path: str) -> tuple[dict, Path]:
    from .main import apply_env
    import os
    p = Path(path)
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    env_files = list(dict.fromkeys([p.resolve().parent / ".env", Path.cwd() / ".env"]))
    apply_env(cfg, env_files, dict(os.environ))
    return cfg, p.resolve().parent


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m spikebot.tokyo_girl")
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("scan")
    sub.add_parser("daily")
    sub.add_parser("stories")
    dv = sub.add_parser("deliver", help="send exports to Discord again (default: undelivered ones)")
    dv.add_argument("files", nargs="*")
    mk = sub.add_parser("make")
    mk.add_argument("-n", type=int, default=1)
    mk.add_argument("--story")
    mk.add_argument("--seed", type=int)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg, base = load_cfg(args.config)
    lcfg = settings(cfg, base)
    if args.cmd == "fetch":
        fetch(lcfg)
        args.cmd = "scan"
    if args.cmd == "scan":
        shots = Library(lcfg).shots()
        counts: dict[str, int] = {}
        for s in shots:
            for t in s.tags or ["untagged"]:
                counts[t] = counts.get(t, 0) + 1
        print(f"{len(shots)} usable shots in {lcfg['media_dir']}: "
              + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    elif args.cmd == "stories":
        today = datetime.now(UK).date()
        for s in STORIES:
            print(f"{'LIVE' if eligible(s, today) else '    '}  {s.kind:7} {s.id:15} "
                  f"{plain(fill(s.cards[0], today))}  ...  {s.cards[-1]}")
    elif args.cmd == "make":
        if lcfg["review"] and not args.story:
            for item_id in make_review(lcfg, args.n):
                print(f"review item {item_id} (approve it in Discord #ready-to-post)")
        else:
            for p in make(lcfg, args.n, args.story, seed=args.seed):
                print(p)
    elif args.cmd == "deliver":
        if not lcfg["delivery"]["webhook_url"]:
            sys.exit("videos_webhook_url is not set in config.json")
        if args.files:
            for f in args.files:
                p = Path(f) if Path(f).exists() else Path(lcfg["export_dir"]) / Path(f).name
                print(p.name, deliver_safe(lcfg, p) or "FAILED (see log)")
        else:
            print("delivered:", ", ".join(deliver_pending(lcfg)) or "nothing pending")
    elif args.cmd == "daily":
        for p in daily(lcfg):
            print(p)


if __name__ == "__main__":
    main()
