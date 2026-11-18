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
  - no channel logo, watermark or channel name (the optional "logo" intro fades in only
    Rockstar's official GTA VI logo from logo_file, then fades away)
  - text follows the variant's text_level: every card, the opening card only, or none
  - exports/tokyo_girl/<date>_<time>_<kind>_<story>.mp4 plus a .txt with the IG
    and TikTok captions

CLI (run from /opt/spikebot):
  python -m spikebot.tokyo_girl fetch       download official media (best effort)
  python -m spikebot.tokyo_girl scan        analyse the media folder (shots, tags)
  python -m spikebot.tokyo_girl make [-n N] [--story ID]
  python -m spikebot.tokyo_girl daily       what the timer runs: fetch if due, top up to per_day
  python -m spikebot.tokyo_girl stories     list the text stories and which are live today
  python -m spikebot.tokyo_girl trend [--song NAME] [-n N]
                                            edits cut to the trending sounds (not Tokyo Girl)
"""
from __future__ import annotations

import argparse
import hashlib
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
W, H, FPS = 1080, 1920, 30   # 30 fps = the trailers' own rate; 60 doubled every trailer frame and stuttered on TikTok (#39)

DEFAULTS = {
    "enabled": True,
    "per_day": 4,                       # edits made per UK day by `daily` (launch week: 4)
    "bpm": 120,
    "min_s": 15,
    "max_s": 30,
    "media_dir": "data/rockstar_media",   # shared official Rockstar library (any mode may read it)
    # official GTA VI logo (transparent PNG from Rockstar's own media) for the "logo" intro;
    # kept outside media_dir so it is never picked as a shot. Missing file = no intro.
    "logo_file": "data/brand/gta6_logo.png",
    # songs (only measured; edits stay silent). Tokyo Girl edits use ONLY the song named by
    # "song" (default tokyo-girl -> data/music/tokyo-girl.*); trending sounds in the same
    # folder are never used here. No such file = the fixed `bpm` grid.
    "music_dir": "data/music",
    # renderer: "auto" = Remotion (motion design) when installed, else ffmpeg; "ffmpeg"; "remotion"
    "engine": "auto",
    "song": "tokyo-girl",
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
    "caption_ig": "{hook} 🎵 Tokyo Girl by @PassiveBand #gta6 #gtavi #vicecity",
    "caption_tiktok": "{hook} 🎵 Tokyo Girl by @Passive #gta6 #gtavi #vicecity",
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
        for k in ("media_dir", "export_dir", "state_file", "logo_file", "music_dir"):
            p = Path(out[k])
            out[k] = str(p if p.is_absolute() else base / p)
    out["media_dir"] = str(migrate_media_dir(Path(out["media_dir"])))
    return out


LEGACY_MEDIA_DIR = Path("data/tokyo_girl/media")


def has_media(folder: Path) -> bool:
    return folder.is_dir() and any(p.is_file() and p.suffix.lower() in VIDEO_EXT | IMAGE_EXT
                                   for p in folder.rglob("*"))


def migrate_media_dir(media: Path) -> Path:
    """Old configs say data/tokyo_girl/media. Use the shared data/rockstar_media
    instead, unless the old folder already holds media."""
    if media.parts[-3:] != LEGACY_MEDIA_DIR.parts or has_media(media):
        return media
    return media.parent.parent / "rockstar_media"


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


# Post captions for the edits (Tim, 18 Nov 2026: the old ones "sound terrible"). Short,
# lowercase, how people actually caption GTA edits: no "Theory:", no forced question, at most
# one emoji. Picked by how far we are from launch; {days_text} = "2 days".
EDIT_LINES = {
    "pre": ["vice city in {days_text} 🌴", "{days_text} left and i'm not ready", "13 years for this",
            "the wait is almost over", "{days_text}. clear your schedule", "leonida is calling",
            "counting down the hours at this point", "this trailer still gives me chills"],
    "launch": ["it's finally here", "vice city is open", "13 years. it's here", "see you in leonida",
               "gta 6 day one 🌴", "the wait is over"],
    "post": ["vice city hits different at night", "leonida never gets old", "this game is unreal",
             "can't stop playing this", "one more mission they said", "nothing feels like vice city"],
    "any": ["vice city at night", "leonida looks unreal", "the detail in this game", "jason and lucia",
            "this city 🌴", "rockstar really did that", "golden hour in vice city"],
}


def edit_line(story: "Story", today: date, salt: str = "") -> str:
    """The caption line for an edit: natural and short, chosen per story/day so the three
    versions and the next days differ."""
    d = days_to_launch(today)
    phase = "pre" if d > 0 else "launch" if d == 0 else "post"
    pool = EDIT_LINES[phase] + (EDIT_LINES["any"] if phase != "launch" else [])
    i = int(hashlib.sha1(f"{story.id}|{today.isoformat()}|{salt}".encode()).hexdigest(), 16) % len(pool)
    return fill(pool[i], today)


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
    c = tg_captions(story, today, lcfg)
    return c["instagram"] + "\n" + c["tiktok"] + "\n"


def caption_file(story: Story, today: date, lcfg: dict) -> str:
    c = tg_captions(story, today, lcfg)
    return f"IG: {c['instagram']}\nTikTok: {c['tiktok']}\n"


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
    motion: float | None = None     # mean frame-to-frame change (low = calm, ambient shot)

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


def measure_motion(path: Path) -> list[list[float]]:
    """How much the picture changes, 4 times a second (mean grey-level difference between
    frames): low values are calm, ambient shots. Cheap: tiny greyscale frames."""
    with tempfile.TemporaryDirectory() as tmp:
        editor.run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(path.resolve()),
                    "-vf", "fps=4,scale=160:-2,format=gray,tblend=all_mode=difference,signalstats,"
                    "metadata=print:key=lavfi.signalstats.YAVG:file=motion.txt", "-f", "null", "-"],
                   timeout=1800, cwd=Path(tmp))
        f = Path(tmp) / "motion.txt"
        text = f.read_text(errors="replace") if f.exists() else ""
    return [[round(t, 2), round(float(d["lavfi.signalstats.YAVG"]), 2)]
            for t, d in parse_meta(text) if "lavfi.signalstats.YAVG" in d]


def build_shots(rel: str, entry: dict, override: dict, lcfg: dict) -> list[Shot]:
    """Turn a cached analysis + tags.json override into usable shots (pure)."""
    if override.get("skip"):
        return []
    tags = sorted(set(override.get("tags") or []) | set(tags_for(rel)))
    bad = [t for t, _ in entry.get("nsfw") or []]
    if entry["kind"] == "image":
        if bad:   # nudity / sexual content: never used
            return []
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
        if any(s - 1.0 <= t <= e + 1.0 for t in bad):   # nudity / sexual content nearby
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
        # skip the first/last half second: the cut itself shows up as a big change
        mv = [m for t, m in entry.get("motion") or [] if s + 0.5 <= t < e - 0.5]
        out.append(Shot(rel, "video", round(s, 3), round(e, 3), sorted(stags), entry.get("crop"),
                        entry["w"], entry["h"], round(statistics.mean(mv), 2) if mv else None))
    return out


def nudity_scan(path: Path, entry: dict, rel: str) -> None:
    """Once per file: the times showing nudity / sexual framing (spikebot/safety.py), stored as
    entry["nsfw"]; build_shots never uses a shot that contains one. Without NudeNet nothing is
    stored, so it runs again next time."""
    from . import safety
    try:
        log.info("nudity check of %s (once)", rel)
        entry["nsfw"] = safety.flag_times(path, fps=1.0)
        if entry["nsfw"]:
            log.warning("%s: %d frame(s) with nudity/sexual content, those shots are never used",
                        rel, len(entry["nsfw"]))
    except safety.Unavailable as e:
        log.error("%s: %s", rel, e)
    except Exception as e:  # noqa: BLE001 - try again next scan
        log.warning("nudity check of %s failed: %s", rel, e)


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

    def scan(self, on_file=None) -> dict:
        """`on_file(i, n, rel)` is called before each file that needs work (a progress bar)."""
        try:
            cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            cache = {}
        out = {}
        files = self.files()
        for i, p in enumerate(files):
            rel = p.relative_to(self.root).as_posix()
            st = p.stat()
            sig = [st.st_size, int(st.st_mtime)]
            cached = rel in cache and cache[rel].get("sig") == sig
            if on_file and (not cached or "nsfw" not in cache[rel]
                            or (cache[rel].get("kind") == "video" and "motion" not in cache[rel])):
                on_file(i, len(files), rel)
            if cached:
                out[rel] = cache[rel]
                if "nsfw" not in cache[rel]:
                    nudity_scan(p, out[rel], rel)
                if cache[rel].get("kind") == "video" and "motion" not in cache[rel]:
                    try:   # files analysed before the motion measure: add it once
                        log.info("measuring motion in %s (once)", rel)
                        out[rel]["motion"] = measure_motion(p)
                    except (editor.EditError, subprocess.TimeoutExpired) as e:
                        log.warning("motion measure failed for %s: %s", rel, e)
                        out[rel]["motion"] = []
                continue
            try:
                if p.suffix.lower() in IMAGE_EXT:
                    info = editor.probe(p)
                    entry = {"kind": "image", "w": info["width"], "h": info["height"]}
                else:
                    log.info("analysing %s (one pass, can take a minute)", rel)
                    entry = analyse_video(p, self.cfg["scene_threshold"])
                    entry["motion"] = measure_motion(p)
            except (editor.EditError, subprocess.TimeoutExpired, ValueError) as e:
                log.warning("skipping %s: %s", rel, e)
                continue
            if entry["w"] < 320 or entry["h"] < 320:
                log.warning("skipping %s: too small (%sx%s)", rel, entry["w"], entry["h"])
                continue
            entry["sig"] = sig
            nudity_scan(p, entry, rel)
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

    def shots(self, entries: dict | None = None, on_file=None) -> list[Shot]:
        entries = self.scan(on_file) if entries is None else entries
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
    lo = min(lo, hi)   # max_s always wins (a trending sound may leave little room)
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


def merge_holds(lens: list[int], hold: int) -> list[int]:
    """Beats per shot with every shot held at least `hold` beats (same total)."""
    out: list[int] = []
    for x in lens:
        if out and out[-1] < hold:
            out[-1] += x
        else:
            out.append(x)
    if len(out) > 1 and out[-1] < hold:
        last = out.pop()
        out[-1] += last
    return out


CALM_TAGS = {"beach", "sunset", "skyline", "city", "ocean", "scenery", "palm", "night", "aerial", "ambient"}


def calm_shot(shots: list[Shot], dur: float, avoid: set[str], uses: dict[str, int]) -> Shot | None:
    """A long, calm trailer clip (low motion, scenery tags) for the logo intro."""
    fits = [s for s in shots if s.kind == "video" and s.length >= dur]
    if not fits:
        return None
    def calm(s: Shot):
        return (s.motion if s.motion is not None else 50.0) - (8.0 if CALM_TAGS & set(s.tags) else 0.0)
    least = min(calm(s) for s in fits)
    calmest = [s for s in fits if calm(s) <= least + 4.0]      # calm first, then variety
    fresh = [s for s in calmest if s.key not in avoid] or calmest
    return min(fresh, key=lambda s: (uses.get(s.key, 0), calm(s)))


def with_intro_shot(plan: Plan, shots: list[Shot], intro_s: float, uses: dict[str, int]) -> Plan:
    """The logo builds over one calm clip: the shots before the drop become one."""
    if intro_s <= 0 or not plan.slots:
        return plan
    k = next((i for i, sl in enumerate(plan.slots) if sl.start + sl.dur >= intro_s - 1e-3), len(plan.slots) - 1)
    end = plan.slots[k].start + plan.slots[k].dur
    shot = calm_shot(shots, end, {sl.shot.key for sl in plan.slots[k + 1:]}, uses)
    if shot is None:
        return plan
    first = Slot(shot, round(shot.start + (shot.length - end) / 2, 3), sum(sl.beats for sl in plan.slots[:k + 1]),
                 0.0, end, 0, flash=False, pan=0)
    return Plan(plan.story, plan.cards, [first] + plan.slots[k + 1:], plan.duration)


def footage_of(plan: Plan) -> list[dict]:
    """The clips of a rendered edit, in order (saved on the variant so a re-render can reuse them)."""
    return [{"key": sl.shot.key, "offset": sl.offset, "pan": sl.pan, "start": round(sl.start, 3),
             "dur": round(sl.dur, 3)} for sl in plan.slots]


def refit(plan: Plan, footage: list[dict], shots: list[Shot], skip: int = 0) -> tuple[Plan, int]:
    """Put the clips of an earlier version back into a new plan, slot by slot in the same order
    (the timing may differ: another sound, pace or length). A clip keeps its spot in the source
    and moves only as far as it must to fill a longer slot; a slot whose clip is gone or too
    short, and any slot past the old clips (a longer edit), keeps the new plan's own pick, so
    clips are added, never repeated. `skip`: leading slots left as planned (a new logo intro's
    calm shot). Returns (plan, slots that got their old clip)."""
    if not footage:
        return plan, 0
    by_key = {s.key: s for s in shots}
    slots, kept = [], 0
    for i, sl in enumerate(plan.slots):
        j = i - skip
        if j < 0 or j >= len(footage):
            slots.append(sl)
            continue
        f = footage[j]
        shot = by_key.get(f.get("key"))
        if shot is None or shot.length < sl.dur - 1e-3:
            slots.append(sl)
            continue
        if shot.kind == "video":
            offset = min(max(float(f.get("offset", shot.start)), shot.start), shot.end - sl.dur)
        else:
            offset = 0.0
        slots.append(Slot(shot, round(offset, 3), sl.beats, sl.start, sl.dur, sl.card, sl.flash,
                          int(f.get("pan") or sl.pan), sl.punch))
        kept += 1
    return Plan(plan.story, plan.cards, slots, plan.duration), kept


def footage_note(base_v: dict | None, spec: dict, kept: int, slots: int) -> str | None:
    """The line on a re-render's card that says whether it kept the clips."""
    if not base_v:
        return None
    name = base_v["label"] if any(c.isdigit() for c in base_v["label"]) else f"{base_v['label']}1"
    if not spec.get("keep_footage"):
        return "New clips (as asked)"
    if not base_v.get("footage"):
        return (f"Couldn't reuse {name}'s clips: it was made before clip lists were saved and its video"
                " couldn't be matched. Feedback on this version keeps its clips")
    how = " (read back from its video)" if base_v.get("recovered") else ""
    old = sum(1 for f in base_v["footage"] if f.get("key"))
    extra = "" if kept >= slots else f"; {slots - kept} new clip(s) where the edit is longer or a clip didn't fit"
    return f"Same clips as {name}{how}: {kept} of {old}" + extra


def find_sound(lib, name: str) -> dict:
    """A sound from the music library by (part of) its name: "impulsive" = Impulsive-2.mp3.
    Raises editor.EditError naming the sounds there are."""
    from . import music
    try:
        return lib.current(name)
    except music.MusicError:
        pass
    want = music.clean_name(name)
    files = lib.files()
    hits = [p for p in files if want and want in music.clean_name(p.stem)]
    if not hits:   # every word of what he typed, in any order
        words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if len(w) > 2]
        hits = [p for p in files if words and all(w in p.stem.lower() for w in words)]
    if hits:
        return lib.get(max(hits, key=lambda p: p.stat().st_mtime))
    names = ", ".join(sorted(p.stem for p in files)) or "none"
    raise editor.EditError(f"no sound called {name!r} in the library (sounds: {names}). Send it in"
                           " #inspiration first")


def plan_clip(story: Story, shots: list[Shot], lcfg: dict, today: date, rng: random.Random,
              uses: dict[str, int] | None = None, pace: str = "normal", transition: str = "flash",
              cards: list[str] | None = None, beats: list[float] | None = None, hold_beats: int = 1,
              intro_s: float = 0.0, intact: bool = False) -> Plan:
    """`beats`: real beat times (seconds from the edit start, from music.grid) so cuts
    follow the song; None = an even grid at lcfg["bpm"]. `hold_beats`: no shot shorter than
    that; `intact`: show the middle of each clip (its main action) instead of a random part;
    `intro_s`: the first seconds are one calm clip (for the logo intro)."""
    if not shots:
        raise editor.EditError("no usable media: run `fetch`, or drop official Rockstar "
                               f"screenshots/trailers into {lcfg['media_dir']}")
    uses = dict(uses or {})
    bpm = lcfg["bpm"]
    beat = beat_s(bpm)
    texts = [fill(c, today) for c in (cards or story.cards)]
    if beats:
        bpm = 60.0 / statistics.median([b - a for a, b in zip(beats, beats[1:])])
        beat = beat_s(bpm)

    def at(i: int) -> float:
        return beats[i] if beats and i < len(beats) else i * beat

    bars = card_bars(len(texts), bpm, lcfg["min_s"], lcfg["max_s"], rng)
    pattern = beat_pattern(sum(bars), rng, pace)
    cards, slots, t, bar_i = [], [], 0, 0
    want = story.tags or ["city"]
    used_here: set[str] = set()
    k = 0
    for ci, (text, nb) in enumerate(zip(texts, bars)):
        cstart = at(t)
        lens = merge_holds([x for b in range(nb) for x in pattern[bar_i + b]], max(1, int(hold_beats)))
        bar_i += nb
        for n_beats in lens:
            dur = at(t + n_beats) - at(t)
            tag = want[k % len(want)]
            k += 1
            shot = choose_shot(shots, tag, dur, used_here, uses, rng)
            used_here.add(shot.key)
            uses[shot.key] = uses.get(shot.key, 0) + 1
            if shot.kind == "video":
                room = shot.length - dur
                offset = shot.start + room * (0.5 if intact else rng.uniform(0.2, 0.8))
            else:
                offset = 0.0
            first_of_card = (at(t) == cstart)
            slots.append(Slot(shot, round(offset, 3), n_beats, round(at(t), 3), dur, ci,
                              flash=first_of_card and ci > 0 and transition == "flash",
                              pan=rng.choice((-1, 1)),
                              punch=first_of_card and transition == "zoom"))
            t += n_beats
        cards.append((cstart, at(t), text))
    plan = Plan(story, cards, slots, at(t))
    return with_intro_shot(plan, shots, intro_s, uses) if intro_s > 0 else plan


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
        style_name = "Ask" if i == len(cards) - 1 and plain(text).rstrip().endswith("?") else "Card"
        lines.append(f"Dialogue: 0,{editor.ass_time(s)},{editor.ass_time(e)},{style_name},,0,0,0,,"
                     f"{pop}{card_markup(text, hi)}")
    return head + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------
MOTION_AMOUNT = {"none": 0.0, "subtle": 0.5, "full": 1.0}


def fit_filter(pre: list[str], flash: bool) -> str:
    """The whole frame, nothing cropped, centred over a blurred, darker copy of itself."""
    head = ",".join(pre) + "," if pre else ""
    tail = ",fade=t=in:st=0:d=0.12:color=white" if flash else ""
    return (f"{head}split[a][b];"
            f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=18:2,"
            f"eq=brightness=-0.12:saturation=0.85,scale={W}:{H}[bg];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS},setsar=1,format=yuv420p{tail}")


def slot_filter(slot: Slot, zoom: float, motion: str = "full", framing: str = "fill",
                extra: float = 0.0) -> str:
    """Cover-crop to 1080x1920 with a slow horizontal pan across the shot (none with
    motion "none"), or the whole frame over a blurred copy with framing "fit"."""
    sh = slot.shot
    vf = []
    sw, shh = sh.w, sh.h
    if sh.crop:
        cw, ch, cx, cy = sh.crop
        vf.append(f"crop={cw}:{ch}:{cx}:{cy}")
        sw, shh = cw, ch
    if framing == "fit":
        return fit_filter(vf, slot.flash)
    s = max(W / sw, H / shh) * zoom
    ow, oh = max(editor.even(sw * s), W), max(editor.even(shh * s), H)
    room = ow - W
    amp = min(160, room) * MOTION_AMOUNT.get(motion, 1.0)
    yroom = oh - H
    if amp == 0:   # no camera movement: a fixed, centred crop (nothing depends on time)
        x, y = f"{room / 2:.1f}", f"{yroom / 2:.1f}"
    else:
        x = f"clip({room / 2:.1f}+{slot.pan * amp:.1f}*(t/{slot.dur:.3f}-0.5),0,{room})"
        y = (f"clip({yroom / 2:.1f}+{-slot.pan * min(80, yroom) * MOTION_AMOUNT.get(motion, 1.0):.1f}"
             f"*(t/{slot.dur:.3f}-0.5),0,{yroom})")
    vf += [f"scale={ow}:{oh}:flags=bicubic", f"crop={W}:{H}:'{x}':'{y}'",
           f"fps={FPS}", "setsar=1", "format=yuv420p"]
    if slot.flash:
        vf.append("fade=t=in:st=0:d=0.12:color=white")
    return ",".join(vf)


CUT_CHECK_THRESHOLD = 0.12   # lower than the library's: dark-to-dark trailer cuts score ~0.2
_cut_cache: dict[tuple, list[float]] = {}


def window_cuts(src: Path, start: float, end: float, threshold: float = CUT_CHECK_THRESHOLD) -> list[float]:
    """Hard cuts inside [start, end] of a source video (absolute seconds). The library's scan
    can miss a cut between two dark shots; this finer check runs on the shot before rendering.
    Never raises: on any failure it reports no cuts."""
    key = (str(src), round(start, 3), round(end, 3), threshold)
    if key in _cut_cache:
        return _cut_cache[key]
    cuts: list[float] = []
    try:
        res = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error",
                              "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src), "-an",
                              "-vf", f"scale=160:-2,select='gt(scene,{threshold})',metadata=print:file=-",
                              "-f", "null", "-"], capture_output=True, text=True, timeout=300)
        cuts = [start + t for t, _ in parse_meta(res.stdout)]
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.warning("cut check failed for %s: %s", src, e)
    _cut_cache[key] = cuts
    return cuts


def clean_plan(plan: Plan, media: Path, tail: float = 0.0, cuts_of=None) -> Plan:
    """Move each trailer clip's window off any hard cut inside it, so no other scene flashes up
    for a few frames. `tail` = extra seconds rendered past a slot (crossfade overlap). A clip
    stays as close to its planned spot as it can; if no clean stretch is long enough it takes
    the longest one (the flash is then as short as possible)."""
    cuts_of = cuts_of or (lambda sh: window_cuts((media / sh.src).resolve(), sh.start, sh.end))
    margin = 0.05
    slots = []
    for i, sl in enumerate(plan.slots):
        if sl.shot.kind != "video":
            slots.append(sl)
            continue
        need = sl.dur + (tail if i < len(plan.slots) - 1 else 0.0)
        inner = [c for c in cuts_of(sl.shot) if sl.shot.start + margin < c < sl.shot.end - margin]
        if not any(sl.offset < c < sl.offset + need for c in inner):
            slots.append(sl)
            continue
        edges = [sl.shot.start] + inner + [sl.shot.end]
        parts = [(a + (margin if a > sl.shot.start else 0), b - (margin if b < sl.shot.end else 0))
                 for a, b in zip(edges, edges[1:])]
        fits = [(a, b) for a, b in parts if b - a >= need]
        if fits:
            a, b = min(fits, key=lambda p: abs(min(max(sl.offset, p[0]), p[1] - need) - sl.offset))
            off = min(max(sl.offset, a), b - need)
        else:
            a, b = max(parts, key=lambda p: p[1] - p[0])
            off = max(sl.shot.start, min(a, sl.shot.end - need))
        log.info("clip %s: moved %.2f -> %.2f s to avoid a scene cut", sl.shot.key, sl.offset, off)
        slots.append(Slot(sl.shot, round(off, 3), sl.beats, sl.start, sl.dur, sl.card, sl.flash, sl.pan, sl.punch))
    return Plan(plan.story, plan.cards, slots, plan.duration)


def render_slot(slot: Slot, media: Path, out: Path, motion: str = "full", framing: str = "fill",
                extra: float = 0.0) -> None:
    """`extra`: seconds rendered past the slot's end (the overlap of a crossfade)."""
    src = str((media / slot.shot.src).resolve())
    # frame count from the slot's edges, so uneven real beats never drift over a whole edit
    frames = round((slot.start + slot.dur) * FPS) - round(slot.start * FPS) + round(extra * FPS)
    still = motion == "none"
    if slot.shot.kind == "video":
        inp = ["-ss", f"{slot.offset:.3f}", "-t", f"{slot.dur + extra + 0.2:.3f}", "-i", src]
        zoom = 1.0 if (still or slot.shot.w / max(slot.shot.h, 1) > 1.2) else 1.08
    else:
        inp = ["-loop", "1", "-framerate", str(FPS), "-t", f"{slot.dur + extra + 0.2:.3f}", "-i", src]
        zoom = 1.0 if still else 1.15
    if slot.punch and not still:
        zoom *= 1.18
    editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inp, "-an",
                "-vf", slot_filter(slot, zoom, motion, framing, extra), "-frames:v", str(frames),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-r", str(FPS),
                str(out)], timeout=300)


def edit_cap(r: dict, lcfg: dict, trend: bool) -> float:
    """Longest an edit may be. Tokyo Girl edits: the `max_s` setting (Tim: 24 s, logo included).
    Trending-sound edits ignore it: only the config's max and the sound itself limit them."""
    if r.get("max_s") and not trend:
        return float(r["max_s"])
    return float(lcfg["max_s"])


def trim_plan(plan: Plan, limit: float) -> Plan:
    """The plan cut to end by `limit` seconds (never runs past the end of the sound)."""
    if plan.duration <= limit + 1e-3:
        return plan
    slots = []
    for sl in plan.slots:
        if sl.start >= limit - 0.25:
            break
        if sl.start + sl.dur > limit:
            sl = Slot(sl.shot, sl.offset, sl.beats, sl.start, round(limit - sl.start, 3), sl.card, sl.flash,
                      sl.pan, sl.punch)
        slots.append(sl)
    cards = [(a, min(b, limit), t) for a, b, t in plan.cards if a < limit - 0.25]
    return Plan(plan.story, cards, slots, round(slots[-1].start + slots[-1].dur, 3) if slots else limit)


def plain_line(today: date) -> str:
    """The one line `text_level: plain` shows: a plain fact, never a question."""
    d = days_to_launch(today)
    if d > 1:
        return f"{d} days until GTA 6"
    if d == 1:
        return "GTA 6 is out tomorrow"
    return "GTA 6 is out today" if d == 0 else "GTA 6 is out now"


def countdown_line(today: date) -> str:
    d = days_to_launch(today)
    return f"{days_text(d)} until GTA 6" if d > 0 else ("GTA 6 is out today" if d == 0 else "GTA 6 is out now")


def cards_for_level(cards: list[tuple[float, float, str]], level: str,
                    intro_s: float = 0.0) -> list[tuple[float, float, str]]:
    """Which text cards to burn in. "full": all; "minimal": the opening card only;
    "none": no text. With a logo intro, text waits until the logo has faded."""
    keep = cards if level == "full" else cards[:1] if level == "minimal" else []
    out = []
    for a, b, t in keep:
        a = max(a, intro_s)
        if b - a >= 0.8:
            out.append((a, b, t))
    return out


LOGO_GROW = 0.35    # how much bigger the logo gets as it bursts away on the drop
LOGO_OUT_S = 0.4    # the burst: quick


def intro_filter(logo_w: int, intro_s: float, out_s: float = LOGO_OUT_S) -> str:
    """The logo builds up gradually until the drop (`intro_s`), then on the drop grows
    quickly while fading to transparent. `logo_w` is its starting width; at its largest it
    is logo_w * (1 + LOGO_GROW)."""
    build = max(0.35, intro_s * 0.85)
    return (f"[1:v]format=rgba,scale=w='{logo_w}*(1+{LOGO_GROW}*clip((t-{intro_s:.3f})/{out_s:.3f},0,1))'"
            f":h=-1:eval=frame,fade=t=in:st=0:d={build:.3f}:alpha=1,"
            f"fade=t=out:st={intro_s:.3f}:d={out_s:.3f}:alpha=1[lg]")


def logo_width(logo_size: float) -> int:
    """Starting width so the logo never gets wider than `logo_size` of the screen."""
    return int(W * float(logo_size) / (1 + LOGO_GROW))


def join_crossfade(names: list[str], plan: Plan, fade_s: float, cwd: Path) -> str:
    """Join the shots with a soft crossfade starting on each cut's beat."""
    inputs = []
    for n in names:
        inputs += ["-i", n]
    graph, last = [], "0:v"
    for k in range(1, len(names)):
        out = f"x{k}"
        graph.append(f"[{last}][{k}:v]xfade=transition=fade:duration={fade_s:.3f}"
                     f":offset={plan.slots[k].start:.3f}[{out}]")
        last = out
    editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
                "-filter_complex", ";".join(graph), "-map", f"[{last}]", "-an", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "16", "-r", str(FPS), "joined.mp4"], timeout=600, cwd=cwd)
    return "joined.mp4"


def fade_of(r: dict, plan: Plan) -> float:
    """Crossfade length (0 = hard cuts)."""
    return float(r.get("fade_s") or 0.45) if r.get("transition") == "fade" and len(plan.slots) > 1 else 0.0


def render_plan(plan: Plan, lcfg: dict, out: Path, grade: str = "", text_style: dict | None = None,
                cards: list[tuple[float, float, str]] | None = None, logo: Path | None = None,
                intro_s: float = 0.0, look: str = "") -> Path:
    """`cards` overrides plan.cards (text level); `logo` + `intro_s` add the logo intro
    (it bursts away exactly at `intro_s`, the drop); `look` is style.look_filter() (grain /
    vignette / cinema bars). `text_style` (style.resolve) also carries motion, framing,
    the crossfade and the logo size."""
    media = Path(lcfg["media_dir"])
    cards = plan.cards if cards is None else cards
    r = text_style or {}
    motion, framing = r.get("motion", "full"), r.get("framing", "fill")
    fade = fade_of(r, plan)
    plan = clean_plan(plan, media, fade)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        names = []
        from . import progress
        job = progress.current()
        for i, slot in enumerate(plan.slots):
            seg = tmpd / f"s{i:03d}.mp4"
            extra = fade if i < len(plan.slots) - 1 else 0.0
            render_slot(slot, media, seg, motion, framing, extra)
            names.append(seg.name)
            job.update(0.8 * (i + 1) / len(plan.slots), detail=f"shot {i + 1}/{len(plan.slots)}")
        job.update(0.85, detail="joining shots, text and colour")
        base_chain = ",".join(x for x in (grade, look) if x) or "null"
        if fade:
            inputs = ["-i", join_crossfade(names, plan, fade, tmpd)]
        else:
            (tmpd / "list.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
            inputs = ["-f", "concat", "-safe", "0", "-i", "list.txt"]
        if logo is not None and intro_s > 0:
            inputs += ["-loop", "1", "-t", f"{intro_s + LOGO_OUT_S:.3f}", "-i", str(Path(logo).resolve())]
            graph = (f"[0:v]{base_chain},eq=brightness=-0.18:saturation=0.55:enable='lt(t,{intro_s:.3f})'[b];"
                     + intro_filter(logo_width(r.get("logo_size") or 0.78), intro_s)
                     + ";[b][lg]overlay=x=(W-w)/2:y=(H-h)/2:eval=frame:eof_action=pass:format=auto[v0]")
        else:
            graph = f"[0:v]{base_chain}[v0]"
        if cards:
            (tmpd / "text.ass").write_text(build_text_ass(cards, lcfg["font"], lcfg["font_size"], text_style),
                                           encoding="utf-8")
            graph += ";[v0]ass=text.ass,format=yuv420p[v]"
        else:
            graph += ";[v0]format=yuv420p[v]"
        part = tmpd / "edit.mp4"
        # -an: no audio stream at all. Tim adds the official sound in the app.
        editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
                    "-filter_complex", graph, "-map", "[v]", "-an", "-r", str(FPS),
                    "-t", f"{plan.duration:.3f}", "-c:v", "libx264", "-preset", "medium",
                    "-crf", str(editor.CRF), "-maxrate", "14M", "-bufsize", "28M", "-pix_fmt", "yuv420p", "-profile:v", "high",
                    "-movflags", "+faststart", part.name], timeout=600, cwd=tmpd)   # a 30 s edit joins in about a minute
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
    h = hook or edit_line(story, today).strip()
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
        from . import music
        src = item.get("source") or {}
        trend = src.get("kind") == "trend"
        default_song = src.get("song") if trend else (self.lcfg.get("song") or music.TOKYO_GIRL_SONG)
        lib = music.Library(self.lcfg["music_dir"])
        out = []
        from . import progress
        job = progress.current()
        for vi, spec in enumerate(specs):
          with job.span(vi / len(specs), (vi + 1) / len(specs)):
            job.update(0.0, f"Version {spec['label']} ({vi + 1}/{len(specs)}): planning")
            r = st.resolve(spec["style"])
            base_v = spec.get("from")
            # the sound: the one Tim asked for in his feedback, else the version's own, else the default
            asked = (spec.get("song") or "").strip()
            own = ((base_v or {}).get("music") or {}).get("song")
            song = None
            if asked:
                song = find_sound(lib, asked)   # a sound that isn't there fails the round (Tim is told why)
            else:
                try:
                    song = lib.current(own or default_song)
                except (music.MusicError, OSError) as e:
                    if trend:   # a trending-sound edit without its sound makes no sense
                        raise editor.EditError(f"trending sound {own or default_song!r} is unavailable: {e}") from e
                    log.warning("song beat map unavailable, using the %s BPM grid: %s", self.lcfg["bpm"], e)
            is_tg_song = not song or music.clean_name(song["name"]) == music.clean_name(
                self.lcfg.get("song") or music.TOKYO_GIRL_SONG)
            shots = self.shots()
            if r["media"] == "clips":   # trailer video only, never screenshots
                shots = [x for x in shots if x.kind == "video"]
                if not shots:
                    raise editor.EditError("no trailer clips in the library (screenshots are switched off):"
                                           " run `fetch` or add official trailers")
            logo = Path(self.lcfg.get("logo_file") or "")
            want_logo = r["intro"] == "logo" and logo.is_file()
            if r["intro"] == "logo" and not want_logo:
                log.warning("logo intro asked for but %s is missing: rendering without it", logo)
            lead_s = r["logo_s"] or None    # how long the logo builds before the drop
            cap = edit_cap(r, self.lcfg, trend)
            plan_cfg, song_start = dict(self.lcfg, max_s=cap, min_s=min(float(self.lcfg["min_s"]), cap)), None
            build_beats = music.BUILD_BEATS
            if song:
                if lead_s and r["intro"] in ("logo", "countdown"):
                    build_beats = max(1, round(lead_s / (60.0 / float(song["bpm"]))))
                song_start, room = music.window(song, float(self.lcfg["min_s"]), cap, build_beats)
                plan_cfg = dict(self.lcfg, max_s=room, min_s=min(float(self.lcfg["min_s"]), room))
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
            if song:   # a short sound can't fit every card: keep the hook and the question
                fit = max(2, int(float(plan_cfg["max_s"]) // bar_s(float(song["bpm"]))))
                if len(cards) > fit:
                    cards = cards[:fit - 1] + cards[-1:]
            pace = spec["style"].get("pace", "normal")
            if r["pace"].get("shot_s"):
                pace = pace_for_shot(r["pace"]["shot_s"], self.lcfg["bpm"])
            beats = music.grid(song, song_start, float(plan_cfg["max_s"]) + 8) if song else None
            intro_s = 0.0
            if want_logo or r["intro"] == "countdown":
                # the build-up ends exactly on the drop (the song's, or the BPM grid's)
                intro_s = beats[build_beats] if beats else (lead_s or bar_s(self.lcfg["bpm"]))
            plan = plan_clip(story, shots, plan_cfg, today, random.Random(seed), state.get("uses"),
                             pace=pace, transition=r["transition"], cards=cards, beats=beats,
                             hold_beats=r["hold_beats"], intro_s=intro_s if want_logo else 0.0,
                             intact=r["hold_beats"] >= 2 or r["motion"] == "none")
            if song:   # the edit starts at song_start: it must end before the sound does
                end = float(song["duration"]) - song_start
                for _ in range(4):
                    if plan.duration <= end + 1e-3 or float(plan_cfg["max_s"]) <= float(plan_cfg["min_s"]):
                        break
                    plan_cfg = dict(plan_cfg, max_s=float(plan_cfg["max_s"]) - bar_s(float(song["bpm"])))
                    plan = plan_clip(story, shots, plan_cfg, today, random.Random(seed), state.get("uses"),
                                     pace=pace, transition=r["transition"], cards=cards, beats=beats,
                                     hold_beats=r["hold_beats"], intro_s=intro_s if want_logo else 0.0,
                                     intact=r["hold_beats"] >= 2 or r["motion"] == "none")
                plan = trim_plan(plan, end)
            kept, recovered = 0, False
            footage = (base_v or {}).get("footage")
            if base_v and spec.get("keep_footage") and not footage:
                footage = self.recover(folder, base_v)   # made before clip lists were saved
                recovered = bool(footage)
            if base_v and spec.get("keep_footage") and footage:
                new_intro = bool(want_logo and intro_s) and (base_v.get("intro") or "none") == "none"
                plan, kept = refit(plan, footage, shots, skip=1 if new_intro else 0)
                if recovered:
                    base_v = dict(base_v, footage=footage, recovered=True)
            stem = f"r{n}_{spec['label']}"
            mp4 = folder / f"{stem}.mp4"
            if r["text_level"] == "plain":
                shown = [(intro_s, plan.duration, plain_line(today))] if plan.duration - intro_s >= 1.5 else []
            else:
                shown = cards_for_level(plan.cards, r["text_level"], intro_s)
            if r["intro"] == "countdown" and intro_s > 0:
                shown = [(0.0, intro_s, countdown_line(today))] + shown
            job.update(0.05, f"Version {spec['label']} ({vi + 1}/{len(specs)}): rendering")
            with job.span(0.05, 0.9):
                engine = self.render_edit(plan, mp4, r, shown, logo if want_logo and intro_s else None, intro_s)
            job.update(0.9, f"Version {spec['label']} ({vi + 1}/{len(specs)}): preview")
            prev = editor.preview(mp4, folder / f"{stem}_preview.mp4", preview_bytes, plan.duration)
            uses = state.setdefault("uses", {})
            for sl in plan.slots:
                uses[sl.shot.key] = uses.get(sl.shot.key, 0) + 1
            state["recent"] = (state.get("recent", []) + [story.id])[-12:]
            hook = plain(fill(texts["hook"], today)) if texts.get("hook") else None
            same_song = ((base_v or {}).get("music") or {}).get("song") == (song or {}).get("name")
            caps = dict(base_v["captions"]) if base_v and not hook and same_song else \
                (tg_captions if (is_tg_song and not trend) else trend_captions)(story, today, self.lcfg, hook)
            title = (base_v["title"] if base_v and not hook
                     else (hook or edit_line(story, today))[:95])
            out.append({"label": spec["label"], "style": spec["style"], "preset": spec.get("preset"),
                        "experimental": spec.get("experimental", False), "story": story.id, "seed": seed,
                        "cards": cards, "title": title,
                        "description": f"{title}\n\nUnofficial fan edit from official Rockstar GTA VI media."
                                       "\n\n#GTA6 #GTAVI #Shorts",
                        "tags": ["GTA 6", "GTA VI", "Shorts", "Vice City"], "hook": title,
                        "captions": caps, "file": mp4.name, "preview": prev.name,
                        "duration": round(plan.duration, 1), "cuts": len(plan.slots) - 1, "silent": True,
                        "text_cards": len(shown), "engine": engine, "footage": footage_of(plan),
                        "footage_note": footage_note(base_v, spec, kept, len(plan.slots)),
                        "intro": r["intro"] if intro_s and (want_logo or r["intro"] == "countdown") else "none",
                        "music": ({"song": song["name"], "start_s": song_start, "bpm": song["bpm"]}
                                  if song else None),
                        "music_note": music.note(song, song_start) if song else None})
        save_state(state_path, state)
        return out

    def recover(self, folder: Path, base_v: dict) -> list[dict]:
        """The clip list of an old version, read back from its video (footage_match)."""
        from . import footage_match, style as st
        for name in (base_v.get("file"), base_v.get("preview")):
            video = Path(folder) / name if name else None
            if video and video.exists():
                break
        else:
            return []
        try:
            lib = Library(self.lcfg)
            index = footage_match.Index(lib.root, lib.scan(),
                                        Path(self.lcfg["data_dir"]) / "tokyo_girl" / "frame_hashes.json")
            framing = st.resolve(base_v.get("style") or {})["framing"]
            out = footage_match.recover(video, index, self.shots(), framing)
        except Exception as e:  # noqa: BLE001 - recovery is best effort: the plan's own clips are used
            log.warning("could not recover the clips of %s: %s", base_v.get("label"), e)
            return []
        return out if any(f.get("key") for f in out) else []

    def sounds(self) -> list[str]:
        """Names of the sounds in the music library (so feedback can switch an edit's sound)."""
        from . import music
        try:
            return sorted(p.stem for p in music.Library(self.lcfg["music_dir"]).files())
        except OSError:
            return []

    def render_edit(self, plan: Plan, mp4: Path, r: dict, shown, logo: Path | None, intro_s: float) -> str:
        """Remotion when available (or forced), ffmpeg otherwise or if Remotion fails.
        Returns the engine that made the file."""
        from . import remotion
        from . import style as st
        engine = self.lcfg.get("engine", "auto")
        plan = clean_plan(plan, Path(self.lcfg["media_dir"]), fade_of(r, plan))
        if engine == "remotion" or (engine == "auto" and remotion.available()):
            try:
                remotion.render(plan, self.lcfg, mp4, r, shown, logo, intro_s)
                return "remotion"
            except (remotion.RemotionError, subprocess.SubprocessError, OSError) as e:
                log.warning("Remotion render failed, using ffmpeg instead: %s", e)
        render_plan(plan, self.lcfg, mp4, st.grade_filter(r["grade"]), r, cards=shown, logo=logo,
                    intro_s=intro_s, look=st.look_filter(r["look"], "tokyo_girl", W, H))
        return "ffmpeg"

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
        if (item.get("source") or {}).get("kind") == "trend":
            export = export.parent / "trend"
        export.mkdir(parents=True, exist_ok=True)
        stem = f"{item['id']}_{v['story']}"
        shutil.copyfile(src, export / f"{stem}.mp4")
        extra = (v.get("music_note") or "").replace("**", "").replace("🎵 ", "")
        (export / f"{stem}.txt").write_text(f"IG: {v['captions']['instagram']}\nTikTok: {v['captions']['tiktok']}\n"
                                            + (f"{extra}\n" if extra else ""), encoding="utf-8")


def trend_captions(story: Story, today: date, lcfg: dict, hook: str | None = None) -> dict[str, str]:
    """Trending-sound edits: the hook + GTA tags. No Tokyo Girl credit; the sound itself is
    added in the app (that's what puts the edit on the sound's page)."""
    h = hook or edit_line(story, today).strip()
    return {"instagram": f"{h} #GTA6 #GTAVI #ViceCity #Rockstar".strip(),
            "tiktok": f"{h} #GTA6 #GTAVI #ViceCity #fyp".strip()}


def trend_used_path(lcfg: dict) -> Path:
    return Path(lcfg["data_dir"]) / "trend" / "used.json"


def pick_rotating(rows: list[dict], used: dict[str, int]) -> dict:
    """The sound used least so far; ties go to the newest (rows are newest first)."""
    return min(enumerate(rows), key=lambda kv: (used.get(kv[1]["file"], 0), kv[0]))[1]


def make_trend(lcfg: dict, songs: list[str] | None = None, per_song: int = 1, now: float | None = None,
               shots: list[Shot] | None = None) -> list[str]:
    """Review items cut to trending sounds (every one in the library except the Tokyo Girl
    song, or just `songs`). Same official trailer footage and style as Tokyo Girl, silent.
    Not counted in Tokyo Girl's per_day. Returns the item ids."""
    from . import music, progress, review
    now = now or time.time()
    dt = datetime.fromtimestamp(now, UK)
    job = progress.current()   # the bot's /trend bar (render_round then adds none of its own)
    job.update(stage="Reading the trending sounds")
    lib = music.Library(lcfg["music_dir"])
    rows = lib.trending(lcfg.get("song") or music.TOKYO_GIRL_SONG)
    used_path = trend_used_path(lcfg)
    used = load_state(used_path)
    if songs == ["auto"]:      # the scheduled run: one sound, rotating through them all
        rows = [pick_rotating(rows, used)] if rows else []
    elif songs:
        want = {music.clean_name(x) for x in songs}
        rows = [r for r in rows if music.clean_name(r["name"]) in want or r["file"] in songs]
    if not rows:
        raise editor.EditError("no trending sounds saved: post a TikTok sound link in #inspiration first")
    store = review.Store(Path(lcfg["data_dir"]))
    cfg, base = lcfg.get("_cfg") or {}, lcfg.get("_base") or Path(".")
    adapter = ReviewAdapter(cfg, base, lcfg, shots)
    ids, k, total = [], 0, len(rows) * per_song
    for row in rows:
        for i in range(per_song):
            title = f"Trending sound edit · {row['name']}" + (f" #{i + 1}" if per_song > 1 else "")
            item = store.create("tokyo_girl", {"day": dt.date().isoformat(), "kind": "trend",
                                               "song": row["file"]}, now + k, title=title)
            if progress.active():
                job.data["title"] = title + (f" ({k + 1}/{total})" if total > 1 else "")
                job.data.setdefault("meta", {})["item"] = item["id"]   # a forced stop cleans this one up
            with job.span(k / total, (k + 1) / total):
                item = review.render_round(store, item["id"], cfg, base, {"kind": "initial"}, now=now + k,
                                           adapter=adapter)
            log.info("trending-sound review item %s (%s): %s", item["id"], row["name"], item["status"])
            used[row["file"]] = used.get(row["file"], 0) + 1
            save_state(used_path, used)
            ids.append(item["id"])
            k += 1
    return ids


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


def ensure_media(lcfg: dict, on_file=None) -> list[Shot]:
    """Usable shots on disk; runs `fetch` once first if there are none."""
    shots = Library(lcfg).shots(on_file=on_file)
    if not shots:
        log.info("no usable media in %s yet, fetching official Rockstar media", lcfg["media_dir"])
        try:
            fetch(lcfg)
        except Exception as e:
            log.warning("fetch failed: %s", e)
        shots = Library(lcfg).shots(on_file=on_file)
    return shots


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
        shots = ensure_media(lcfg)
        if not shots:  # don't fill the review queue with failed items
            log.error("no usable media in %s: run `fetch` by hand and check its log", lcfg["media_dir"])
            return []
        return make_review(lcfg, need, now=now, shots=shots)
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
    dl = sub.add_parser("daily")
    dl.add_argument("--per-day", type=int, default=None, help="at least this many edits today (raises per_day)")
    sub.add_parser("stories")
    dv = sub.add_parser("deliver", help="send exports to Discord again (default: undelivered ones)")
    dv.add_argument("files", nargs="*")
    tr = sub.add_parser("trend", help="edits cut to the trending sounds (one per sound by default)")
    tr.add_argument("--song", action="append", help="only this sound (repeatable); 'auto' = next in rotation")
    tr.add_argument("-n", type=int, default=1, help="edits per sound")
    mk = sub.add_parser("make")
    mk.add_argument("-n", type=int, default=1)
    mk.add_argument("--story")
    mk.add_argument("--seed", type=int)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from . import progress
    progress.arm_cancel()   # /cancel stops one edit; the rest of a daily/trend batch carries on
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
        shots = ensure_media(lcfg)
        if not shots:
            sys.exit(f"no usable media in {lcfg['media_dir']}: `fetch` found nothing (check the log above), "
                     "or drop official Rockstar screenshots/trailers there")
        if lcfg["review"] and not args.story:
            from . import review
            store = review.Store(Path(lcfg["data_dir"]))
            failed = 0
            for item_id in make_review(lcfg, args.n, shots=shots):
                item = store.load(item_id)
                if item.get("status") == "failed":
                    failed += 1
                    print(f"review item {item_id} FAILED: {item.get('error') or 'see the log above'}")
                else:
                    print(f"review item {item_id} (approve it in Discord #ready-to-post)")
            if failed:
                sys.exit(1)
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
    elif args.cmd == "trend":
        job = progress.current()   # the bot's bar for this /trend (a no-op when run by hand)
        job.update(0.0, "Checking the trailer library")

        def checking(i, n, rel):
            job.update(i / max(1, n), "Checking the trailer library (new files: nudity check, motion)",
                       f"{i + 1}/{n} · {Path(rel).name}")
        try:
            with job.span(0.0, 0.1):
                shots = ensure_media(lcfg, on_file=checking)
            if not shots:
                sys.exit(f"no usable media in {lcfg['media_dir']}")
            with job.span(0.1, 1.0):
                ids = make_trend(lcfg, args.song, args.n, shots=shots)
        except progress.Cancelled:
            print("trend edits cancelled")
            return
        from . import review
        store = review.Store(Path(lcfg["data_dir"]))
        failed = 0
        for item_id in ids:
            item = store.load(item_id)
            ok = item.get("status") != "failed"
            failed += not ok
            print(f"review item {item_id} ({item['source']['song']}): "
                  + ("cancelled" if item.get("status") == "cancelled"
                     else "ready for #ready-to-post" if ok else f"FAILED: {item.get('error')}"))
        if failed:
            sys.exit(1)
    elif args.cmd == "daily":
        if args.per_day:
            lcfg["per_day"] = max(int(lcfg["per_day"]), args.per_day)
        for p in daily(lcfg):
            print(p)


if __name__ == "__main__":
    main()
