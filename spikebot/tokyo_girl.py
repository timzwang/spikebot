"""Tokyo Girl mode: silent 4:5 campaign edits from official GTA 6 media, for
Tim to post by hand on TikTok / Instagram with the official "Tokyo Girl" sound.

Sources are only Rockstar's own GTA VI trailers and screenshots
(rockstargames.com/VI/media and the Rockstar Games YouTube channel). Never stream
clips, never voices or dialogue: every export has NO audio track at all.

Each edit:
  - 1080x1350 (4:5, Tim 20 Nov), 30 fps, 15-30 s (whole bars, so it ends on the beat)
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
import dataclasses
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import delivery, editor, shot_info, shot_labels

log = logging.getLogger("tokyo_girl")
UK = ZoneInfo("Europe/London")
LAUNCH = date(2026, 11, 19)  # GTA 6 release day
W, H, FPS = 1080, 1350, 30   # 4:5 (Tim, 20 Nov: "edits in 4:5, not 9:16"). 30 fps = the trailers' own rate; 60 doubled every trailer frame and stuttered on TikTok (#39)

DEFAULTS = {
    "enabled": True,
    "per_day": 1,                       # edits made per UK day by `daily` (launch week 4; 1 since 25 Nov: stream clips first)
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
    # official videos found by title on Rockstar's own YouTube channel (no fixed link needed);
    # Tim, 18 Nov: the Extended Look has lots of cinematic / ambient shots and an animal part
    "searches": ["Grand Theft Auto VI: An Extended Look"],
    "refetch_days": 7,
    "compare_per_day": 0,               # GTA 5 vs GTA 6 edits made by `daily` (0 = only on demand, /compare)
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
    Story("wildlife", "news", "Leonida's wildlife looks unreal. What are you looking for first?",
          ["Leonida isn't just *Vice City*", "The wildlife is *unreal*", "Swamps. Beaches. *Everything moves.*",
           "What are you looking for first?"], ANY, ["animals", "city"]),
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


# Caption lines that name what's on screen (Tim, 24 Nov: "vice city at night should actually show clips
# of vice city at night"): line -> vision tags (shot_labels) of the footage it needs. The edit's
# footage is picked to show it, and the line is only used when at least LINE_SHARE of the edit's
# clips really do; otherwise a line that fits the footage (or a general one) is used instead.
LINE_TAGS = {
    "vice city hits different at night": {"night"},
    "vice city at night": {"night"},
    "golden hour in vice city": {"sunset"},
    "jason and lucia": {"couple"},
    "this city 🌴": {"city", "skyline", "beach"},
    "vice city in {days_text} 🌴": {"city", "skyline", "beach", "ocean"},
    "leonida looks unreal": {"scenery", "establishing", "ocean", "beach", "skyline", "sunset"},
    "the detail in this game": {"close_up", "character", "interior"},
}
LINE_SHARE = 0.5       # share of the edit's clips that must show what the line names
LINE_MIN_SHOTS = 6     # the library needs this many fitting labelled clips before footage is steered


def line_tags(line: str) -> set[str]:
    """The vision tags a caption line needs (empty = a general line that fits any footage)."""
    return set(LINE_TAGS.get(line, set()))


def footage_share(plan: "Plan", tags: set[str]) -> float:
    """Share of the edit's clips showing any of `tags` (vision labels)."""
    slots = [sl for sl in plan.slots if sl.shot.kind == "video"]
    if not slots or not tags:
        return 0.0
    return sum(1 for sl in slots if sl.shot.vtags & tags) / len(slots)


def pick_line(story: "Story", today: date, salt: str, shots: list["Shot"]) -> str:
    """The caption line for an edit, before it is planned: edit_line, unless it names footage the
    library can't show (too few labelled clips with those tags): then the next general line."""
    raw = _line_pool(today)
    first = edit_line(story, today, salt)
    i = raw.index(_raw_of(first, today)) if _raw_of(first, today) in raw else 0
    for k in range(len(raw)):
        cand = raw[(i + k) % len(raw)]
        need = line_tags(cand)
        if not need or sum(1 for s in shots if s.vtags & need) >= LINE_MIN_SHOTS:
            return fill(cand, today)
    return first


def line_for_footage(line: str, plan: "Plan", story: "Story", today: date, salt: str) -> str:
    """`line` if the edit's footage really shows what it names, else a line whose footage the edit
    does show, else a general line (never "at night" over daytime clips)."""
    raw = _raw_of(line, today)
    if raw is None or not line_tags(raw) or footage_share(plan, line_tags(raw)) >= LINE_SHARE:
        return line
    pool = _line_pool(today)
    h = int(hashlib.sha1(f"{story.id}|{today.isoformat()}|{salt}|fit".encode()).hexdigest(), 16)
    fits = [c for c in pool if line_tags(c) and footage_share(plan, line_tags(c)) >= LINE_SHARE]
    general = [c for c in pool if not line_tags(c)]
    choice = fits or general or pool
    return fill(choice[h % len(choice)], today)


def _line_pool(today: date) -> list[str]:
    d = days_to_launch(today)
    phase = "pre" if d > 0 else "launch" if d == 0 else "post"
    return EDIT_LINES[phase] + (EDIT_LINES["any"] if phase != "launch" else [])


def _raw_of(line: str, today: date) -> str | None:
    """The EDIT_LINES template a filled-in line came from."""
    for raw in _line_pool(today):
        if fill(raw, today) == line:
            return raw
    return None


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
    # where the subject is (fractions of the picture after the letterbox crop), from the faces
    # the library's nudity pass found; None = no face, crop around the centre
    focus: tuple[float, float] | None = None
    single: bool = False            # one clear subject (a close-up): smart fill shows it full screen
    fps: float | None = None        # the source's frame rate (videos)
    subject_x: float = 0.5          # crop_safe: where the subject sits across the frame when no face is known
    crop_ok: bool = True            # a 9:16 crop shows the whole subject (shot_info / vision labels)
    yuv: list[float] | None = None  # mean colour (palette_match)
    hashes: list[int] = field(default_factory=list)   # picture hashes: same footage in another file
    labels: dict | None = None      # vision labels (shot_labels): tags, action, calm, impact_end...

    @property
    def key(self) -> str:
        return f"{self.src}@{self.start:.1f}" if self.kind == "video" else self.src

    @property
    def length(self) -> float:
        return self.end - self.start

    @property
    def vtags(self) -> set[str]:
        return set((self.labels or {}).get("tags") or [])

    def action(self) -> float:
        """0-1: how action-packed (vision label, else the measured motion)."""
        if self.labels:
            return float(self.labels.get("action") or 0)
        return min(1.0, max(0.0, (self.motion or 0.0) / 20.0)) if self.motion is not None else 0.3

    def calm(self) -> float:
        """0-1: how calm / ambient (vision label, else low motion and scenery tags)."""
        if self.labels:
            return float(self.labels.get("calm") or 0)
        base = max(0.0, min(1.0, (15.0 - self.motion) / 12.0)) if self.motion is not None else 0.4
        return min(1.0, base + (0.2 if CALM_TAGS & set(self.tags) else 0.0))

    def impact(self) -> float:
        """0-1: how good a closing shot (walking away, driving into the distance). Needs labels."""
        if not self.labels:
            return 0.0
        bonus = 0.3 if self.vtags & {"walking_away", "driving_away"} else 0.0
        return min(1.0, float(self.labels.get("impact_end") or 0) + bonus)

    def relevant(self) -> bool:
        """Worth showing: not filler, a title card or text over footage (unknown = yes)."""
        from .shot_labels import FILLER
        return not (self.vtags & FILLER)


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
            "fps": info.get("fps"),
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


# Tags for parts of official videos we know (file name pattern -> ranges in seconds). tags.json
# ranges add to these. Tim, 18 Nov: the Extended Look's animal part starts at 7:44.
KNOWN_RANGES = [
    (r"extended_look", [{"start": 464.0, "end": 560.0, "tags": ["animals", "nature", "ambient"]}]),
]


def known_ranges(rel: str) -> list[dict]:
    return [r for rx, rows in KNOWN_RANGES if re.search(rx, rel, re.I) for r in rows]


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
    ranges = known_ranges(rel) + list(override.get("ranges") or [])
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
        lab = shot_labels.for_range(entry, s, e)
        if lab:
            vt = set(lab.get("tags") or [])
            if vt & shot_labels.UNSAFE:          # strip club / twerking / suggestive: never (Tim's rule)
                continue
            if vt & {"rockstar_presents", "black_card"}:   # the intro shot is used only as the intro
                continue
            stags |= {shot_labels.STORY_TAGS[t] for t in vt if t in shot_labels.STORY_TAGS}
        # skip the first/last half second: the cut itself shows up as a big change
        mv = [m for t, m in entry.get("motion") or [] if s + 0.5 <= t < e - 0.5]
        focus, single = subject_of(entry, s, e)
        out.append(described(Shot(rel, "video", round(s, 3), round(e, 3), sorted(stags), entry.get("crop"),
                                  entry["w"], entry["h"], round(statistics.mean(mv), 2) if mv else None, focus,
                                  single, entry.get("fps")), entry, lab))
    return out


def aspect_of(entry: dict) -> float:
    c = entry.get("crop")
    return (c[0] / max(c[1], 1)) if c else entry["w"] / max(entry["h"], 1)


def described(shot: Shot, entry: dict, lab: dict | None) -> Shot:
    """The shot with its colour, subject position, hashes and vision labels."""
    aspect = aspect_of(entry)
    stats = shot_info.shot_stats(entry, shot.start, shot.end, aspect)
    shot.labels = lab
    if stats:
        shot.yuv, shot.hashes, shot.subject_x = stats["yuv"], stats["hashes"], stats["focus"]
    if lab:
        shot.subject_x = shot_info.subject_focus(float(lab.get("subject_x", 0.5)), float(lab.get("subject_w", 0.3)),
                                             aspect)
    shot.crop_ok = shot_info.crop_ok(stats, lab.get("subject_w") if lab else None, aspect)
    return shot


BIG_FACE = 0.004      # a face this share of the frame (or more) is a subject, not a crowd extra
SINGLE_SHARE = 0.6    # one clear subject: one big face (and no other) in this share of the samples


def subject_of(entry: dict, start: float, end: float, fps: float = 1.0) -> tuple[tuple[float, float] | None, bool]:
    """(focus, single) of a shot from the library's face samples (fractions of the frame
    after the letterbox crop). Pure."""
    rows = entry.get("faces")
    if not rows:
        return None, False
    k0, k1 = math.ceil(start * fps), math.floor(end * fps - 1e-9)
    n = max(0, k1 - k0 + 1)                       # sampled frames inside the shot
    if n == 0:
        return None, False
    w0, h0 = entry.get("w") or 1, entry.get("h") or 1
    cw, ch, cx, cy = entry.get("crop") or [w0, h0, 0, 0]

    def inside(fx, fy):   # full-frame fraction -> fraction of the cropped picture
        return ((fx * w0 - cx) / cw, (fy * h0 - cy) / ch)
    singles, centres = [], []
    for t, faces in rows:
        if not (start <= t <= end):
            continue
        big = [f for f in faces if f[2] * f[3] >= BIG_FACE]
        if len(big) == 1:
            singles.append(inside(big[0][0], big[0][1]))
        if big:
            centres.append(inside(sum(f[0] for f in big) / len(big), sum(f[1] for f in big) / len(big)))
    if not centres:
        return None, False

    def med(pts, i):
        return round(min(1.0, max(0.0, statistics.median(p[i] for p in pts))), 3)
    if len(singles) >= max(1, SINGLE_SHARE * n):
        return (med(singles, 0), med(singles, 1)), True
    return (med(centres, 0), med(centres, 1)), False


def nudity_scan(path: Path, entry: dict, rel: str) -> None:
    """Once per file: the times showing nudity / sexual framing (spikebot/safety.py), stored as
    entry["nsfw"]; build_shots never uses a shot that contains one. Without NudeNet nothing is
    stored, so it runs again next time."""
    from . import safety
    try:
        log.info("nudity and face check of %s (once)", rel)
        faces: list = []
        entry["nsfw"] = safety.flag_times(path, fps=1.0, faces=faces)
        entry["faces"] = faces
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
            if on_file and (not cached or "nsfw" not in cache[rel] or "faces" not in cache[rel]
                            or (cache[rel].get("kind") == "video" and "motion" not in cache[rel])
                            or self.needs_extra(cache[rel])):
                on_file(i, len(files), rel)
            if cached:
                out[rel] = cache[rel]
                if "nsfw" not in cache[rel] or "faces" not in cache[rel]:
                    nudity_scan(p, out[rel], rel)   # files from before the face pass: once more
                if cache[rel].get("kind") == "video" and "fps" not in cache[rel]:
                    try:
                        out[rel]["fps"] = editor.probe(p).get("fps")
                    except (editor.EditError, subprocess.TimeoutExpired, ValueError) as e:
                        log.warning("could not read the frame rate of %s: %s", rel, e)
                if cache[rel].get("kind") == "video" and "motion" not in cache[rel]:
                    try:   # files analysed before the motion measure: add it once
                        log.info("measuring motion in %s (once)", rel)
                        out[rel]["motion"] = measure_motion(p)
                    except (editor.EditError, subprocess.TimeoutExpired) as e:
                        log.warning("motion measure failed for %s: %s", rel, e)
                        out[rel]["motion"] = []
                if self.add_extra(p, out[rel], rel):
                    self.save(out, cache, files)
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
            self.add_extra(p, entry, rel)
            out[rel] = entry
            self.save(out, cache, files)
        self.save(out)
        self.entries = out
        return out

    def save(self, out: dict, cache: dict | None = None, files: list[Path] | None = None) -> None:
        """Write library.json (mid-scan: what is done so far plus the rest of the old cache, so
        a long first scan isn't lost if the process stops)."""
        data = dict(out)
        if cache is not None and files is not None:
            names = {p.relative_to(self.root).as_posix() for p in files}
            for rel, e in cache.items():
                if rel in names and rel not in data:
                    data[rel] = e
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.cache_file)

    def needs_extra(self, entry: dict) -> bool:
        if entry.get("kind") != "video":
            return False
        if (entry.get("looks") or {}).get("v") != shot_info.VERSION:
            return True
        from . import ai
        return shot_labels.needs_labels(entry) and ai.available(self.cfg.get("_base"))

    def add_extra(self, path: Path, entry: dict, rel: str) -> bool:
        """Colour / subject / hash measurements and vision labels, once per file. Never raises."""
        if entry.get("kind") != "video":
            return False
        changed = False
        if (entry.get("looks") or {}).get("v") != shot_info.VERSION:
            try:
                log.info("measuring colour and framing of %s (once)", rel)
                entry["looks"] = shot_info.measure(path)
                changed = True
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                log.warning("colour/framing measure failed for %s: %s", rel, e)
        if shot_labels.ensure(path, entry, rel, self.cfg.get("_base"), ask=self.label_ask):
            changed = True
        return changed

    label_ask = None     # tests: a fake vision model
    entries: dict | None = None

    def presents_shots(self, entries: dict | None = None) -> list[Shot]:
        """The trailers' "Rockstar Games Presents" shots over scenery (never the black title card),
        longest first. Empty until the vision labels have found one."""
        entries = self.entries if entries is None else entries
        if entries is None:
            entries = self.scan()
        out = []
        for rel, entry in entries.items():
            for a, b, kind in shot_labels.presents_segments(entry):
                if kind != "scenery" or b - a < 1.0:
                    continue
                if any(a - 1.0 <= t <= b + 1.0 for t, _ in entry.get("nsfw") or []):
                    continue
                sh = Shot(rel, "video", round(a + 0.05, 3), round(b - 0.1, 3), ["presents"], entry.get("crop"),
                          entry["w"], entry["h"])
                out.append(described(sh, entry, shot_labels.for_range(entry, a, b)))
        return sorted(out, key=lambda x: -x.length)

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
        self.entries = entries
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
    shot2: Shot | None = None   # framing "stack": the clip shown below this one
    offset2: float = 0.0
    hold: float | None = None   # seconds of clean source from `offset`; past it the last frame is held
    fade_in: float = 0.0        # crossfade from the previous clip (s); set just before rendering


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


def calm_shot(shots: list[Shot], dur: float, avoid: set[str], uses: dict[str, int],
              openers: set[str] | None = None, prefs: "Prefs | None" = None) -> Shot | None:
    """A long, calm trailer clip (low motion, scenery tags) for the logo intro. `openers`: clips
    that opened recent edits (opener_rotation), used only when nothing else is calm enough."""
    fits = [s for s in shots if s.kind == "video" and s.length >= dur and s.relevant()]
    if not fits:
        return None
    if prefs is not None:
        fits = narrow(fits, lambda s: s.crop_ok) if prefs.crop_safe else fits
        if prefs.unique and prefs.groups:
            bad = {prefs.groups.get(k, k) for k in avoid}
            fits = narrow(fits, lambda s: prefs.groups.get(s.key, s.key) not in bad)
    fits = narrow(fits, lambda s: s.key not in (openers or set()))
    def calm(s: Shot):
        if s.labels:
            return (1.0 - s.calm()) * 30.0
        return (s.motion if s.motion is not None else 50.0) - (8.0 if CALM_TAGS & set(s.tags) else 0.0)
    least = min(calm(s) for s in fits)
    calmest = [s for s in fits if calm(s) <= least + 4.0]      # calm first, then variety
    fresh = [s for s in calmest if s.key not in avoid] or calmest
    return min(fresh, key=lambda s: (uses.get(s.key, 0), calm(s)))


def with_intro_shot(plan: Plan, shots: list[Shot], intro_s: float, uses: dict[str, int],
                    shot: Shot | None = None, openers: set[str] | None = None,
                    prefs: "Prefs | None" = None) -> Plan:
    """The intro plays over one clip: the shots before the drop become one. `shot`: that clip
    (the "Rockstar Games Presents" shot, shown from its start so the text fades in; if it is
    shorter than the intro its last frame is held); else a calm clip for the logo."""
    if intro_s <= 0 or not plan.slots:
        return plan
    k = next((i for i, sl in enumerate(plan.slots) if sl.start + sl.dur >= intro_s - 1e-3), len(plan.slots) - 1)
    end = plan.slots[k].start + plan.slots[k].dur
    if shot is not None:
        first = Slot(shot, shot.start, sum(sl.beats for sl in plan.slots[:k + 1]), 0.0, end, 0, flash=False, pan=0,
                     hold=shot.length if shot.length < end else None)
        return Plan(plan.story, plan.cards, [first] + plan.slots[k + 1:], plan.duration)
    shot = calm_shot(shots, end, {sl.shot.key for sl in plan.slots[k + 1:]}, uses, openers, prefs)
    if shot is None:
        return plan
    first = Slot(shot, round(shot.start + (shot.length - end) / 2, 3), sum(sl.beats for sl in plan.slots[:k + 1]),
                 0.0, end, 0, flash=False, pan=0)
    return Plan(plan.story, plan.cards, [first] + plan.slots[k + 1:], plan.duration)


def footage_of(plan: Plan) -> list[dict]:
    """The clips of a rendered edit, in order (saved on the variant so a re-render can reuse them)."""
    return [{"key": sl.shot.key, "offset": sl.offset, "pan": sl.pan, "start": round(sl.start, 3),
             "dur": round(sl.dur, 3)} | ({"key2": sl.shot2.key, "offset2": sl.offset2} if sl.shot2 else {})
            for sl in plan.slots]


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
        new = Slot(shot, round(offset, 3), sl.beats, sl.start, sl.dur, sl.card, sl.flash,
                   int(f.get("pan") or sl.pan), sl.punch)
        s2 = by_key.get(f.get("key2") or "")
        if s2 is not None and s2.length >= sl.dur - 1e-3:   # a stacked version keeps its lower clip too
            off2 = min(max(float(f.get("offset2", s2.start)), s2.start), s2.end - sl.dur) if s2.kind == "video" else 0.0
            new = dataclasses.replace(new, shot2=s2, offset2=round(off2, 3))
        slots.append(new)
        kept += 1
    return Plan(plan.story, plan.cards, slots, plan.duration), kept


def plan_from_footage(plan: Plan, footage: list[dict], shots: list[Shot], beats: list[float] | None,
                      bpm: float, skip: int = 0) -> tuple[Plan, int]:
    """Every clip of an earlier version, in its order, each for about as long as it was there, with
    the cuts snapped to this edit's beats (the same sound gives the same timing). Used when the new
    plan has fewer slots than that version had clips, so none is dropped. Returns (plan, clips kept)."""
    by_key = {s.key: s for s in shots}
    beat = beat_s(bpm)
    grid = sorted(beats) if beats else None
    slots = list(plan.slots[:skip])
    t = slots[-1].start + slots[-1].dur if slots else 0.0
    kept = 0

    def snap(x: float) -> float:
        if grid:
            later = [b for b in grid if b > t + 0.2]
            if later:
                return min(later, key=lambda b: abs(b - x))
        k = max(1, round((x - t) / beat))
        return t + k * beat

    for f in footage:
        shot = by_key.get(f.get("key"))
        if shot is None:
            continue    # that clip is gone from the library
        end = snap(t + float(f.get("dur") or beat * 2))
        dur = round(end - t, 3)
        hold = shot.length if shot.kind == "video" and shot.length < dur else None
        if shot.kind == "video":
            offset = min(max(float(f.get("offset", shot.start)), shot.start), max(shot.start, shot.end - dur))
        else:
            offset = 0.0
        slots.append(Slot(shot, round(offset, 3), max(1, round(dur / beat)), round(t, 3), dur, 0, False,
                          int(f.get("pan") or 1), hold=hold))
        kept += 1
        t = end
    cards = [(a, min(b, t), x) for a, b, x in plan.cards if a < t - 0.25]
    return Plan(plan.story, cards, slots, round(t, 3)), kept


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


@dataclass
class Prefs:
    """How footage is picked (style settings, see style.resolve). None of it applies to `make`."""
    unique: bool = False         # unique_clips: never the same clip / same footage twice in one edit
    crop_safe: bool = False      # crop_safe (fill framing only): the subject fits the 9:16 crop
    relevant: bool = False       # footage_match: no filler / title cards
    ambient: float = 0.0         # ambient_bias 0-1: prefer calm, atmospheric shots
    palette: float = 0.0         # palette_match 0-1: clips close to the opening clip's colours
    action_first: bool = False   # most action-packed clips in the first third
    impact_ending: bool = False  # an impactful closing shot
    openers: set[str] = field(default_factory=set)    # clips that opened the last N edits
    groups: dict[str, str] = field(default_factory=dict)   # shot key -> same-footage group
    tail: float = 0.0            # crossfade length: prefer clips long enough to run on under the next
    variety: float = 0.0         # variety 0-1: avoid clips / source videos used in the recent edits
    recent: set[str] = field(default_factory=set)            # clips of the last few edits
    src_load: dict[str, float] = field(default_factory=dict)  # recent uses per source video / its shots
    theme: set[str] = field(default_factory=set)   # vision tags the caption line names ("night"): preferred


_groups_cache: dict[tuple, dict[str, str]] = {}


def footage_groups(shots: list[Shot]) -> dict[str, str]:
    """{shot key: group}: shots that show the same footage (two copies of a trailer, an extended
    cut) share a group, so unique_clips never shows it twice."""
    sig = tuple(s.key for s in shots)
    if sig not in _groups_cache:
        if len(_groups_cache) > 8:
            _groups_cache.clear()
        _groups_cache[sig] = shot_info.dup_groups([(s.key, s.hashes) for s in shots if s.kind == "video"])
    return _groups_cache[sig]


def recent_load(shots: list[Shot], edits: list[list[str]] | None, variety: float) -> tuple[set[str], dict[str, float]]:
    """(clips of the last N edits, recent uses per source video divided by its number of shots); N
    grows with `variety` (0.7 -> the last 8 edits)."""
    if variety <= 0 or not edits:
        return set(), {}
    n = max(1, round(3 + 7 * variety))
    keys = [k for e in edits[-n:] for k in e]
    per_src: dict[str, int] = {}
    for sh in shots:
        per_src[sh.src] = per_src.get(sh.src, 0) + 1
    used: dict[str, int] = {}
    for k in keys:
        src = k.rpartition("@")[0] or k
        used[src] = used.get(src, 0) + 1
    return set(keys), {src: used.get(src, 0) / cnt for src, cnt in per_src.items()}


def prefs_for(r: dict, shots: list[Shot], openers: list[str] | None = None, fade: float = 0.0,
              edits: list[list[str]] | None = None) -> Prefs:
    """Prefs from a resolved style (style.resolve). `edits`: the clip keys of recent edits (variety)."""
    unique = bool(r.get("unique_clips", True))
    n = int(r.get("opener_rotation") or 0)
    variety = float(r.get("variety") if r.get("variety") is not None else 0.7)
    recent, load = recent_load(shots, edits, variety)
    return Prefs(unique=unique, crop_safe=bool(r.get("crop_safe", True)) and r.get("framing", "fill") in CROPPED,
                 relevant=bool(r.get("footage_match", True)), ambient=float(r.get("ambient_bias") or 0),
                 palette=float(r.get("palette_match") or 0), action_first=bool(r.get("action_first")),
                 impact_ending=bool(r.get("impact_ending")),
                 openers=set((openers or [])[-n:]) if n > 0 else set(),
                 groups=footage_groups(shots) if unique else {}, tail=fade, variety=variety, recent=recent,
                 src_load=load)


CROPPED = ("fill", "box45", "box11")   # framings that crop the sides of a 16:9 clip (crop_safe applies)


def narrow(pool: list, keep) -> list:
    """The part of `pool` that passes `keep`, or the whole pool if none does (a preference)."""
    sub = [x for x in pool if keep(x)]
    return sub or pool


def group_of(prefs: Prefs | None, key: str) -> str:
    return prefs.groups.get(key, key) if prefs is not None and prefs.unique else key


def action_order(plan: Plan, skip: int = 0) -> Plan:
    """action_first: the most action-packed clips move into the first third (each clip still fills
    a slot it is long enough for; the beat timing doesn't change). `skip` leading slots stay."""
    body = plan.slots[skip:]
    n = len(body)
    if n < 3:
        return plan
    first = max(1, round(n / 3))
    left = list(range(n))
    out: list[Slot] = []
    for j, sl in enumerate(body):
        fit = [i for i in left if body[i].shot.length >= sl.dur - 1e-3]
        if not fit:
            return plan     # lengths don't allow a reorder: keep the plan as it was
        i = max(fit, key=lambda i: (body[i].shot.action(), -i)) if j < first else fit[0]
        left.remove(i)
        src = body[i]
        room = src.shot.length - src.dur
        frac = (src.offset - src.shot.start) / room if room > 1e-3 and src.shot.kind == "video" else 0.5
        off = src.shot.start + max(0.0, src.shot.length - sl.dur) * min(max(frac, 0.0), 1.0) \
            if src.shot.kind == "video" else 0.0
        out.append(replace(sl, shot=src.shot, offset=round(off, 3), pan=src.pan, hold=None))
    return Plan(plan.story, plan.cards, plan.slots[:skip] + out, plan.duration)


def impact_end(plan: Plan, shots: list[Shot], uses: dict[str, int], prefs: Prefs | None = None) -> Plan:
    """impact_ending: the last clip becomes the most impactful closing shot that fits (someone
    walking away, a car driving into the distance), shown to its end. Needs vision labels."""
    if not plan.slots:
        return plan
    last = plan.slots[-1]
    used = {group_of(prefs, sl.shot.key) for sl in plan.slots[:-1]}
    cands = [s for s in shots if s.kind == "video" and s.length >= last.dur and s.impact() >= 0.5
             and group_of(prefs, s.key) not in used and s.relevant()]
    if not cands:
        return plan
    best = max(cands, key=lambda s: (round(s.impact(), 1), -uses.get(s.key, 0)))
    if best.key == last.shot.key:
        return plan
    return Plan(plan.story, plan.cards, plan.slots[:-1]
                + [replace(last, shot=best, offset=round(best.end - last.dur, 3), hold=None)], plan.duration)


def plan_clip(story: Story, shots: list[Shot], lcfg: dict, today: date, rng: random.Random,
              uses: dict[str, int] | None = None, pace: str = "normal", transition: str = "flash",
              cards: list[str] | None = None, beats: list[float] | None = None, hold_beats: int = 1,
              intro_s: float = 0.0, intact: bool = False, prefs: Prefs | None = None,
              intro_shot: Shot | None = None) -> Plan:
    """`beats`: real beat times (seconds from the edit start, from music.grid) so cuts
    follow the song; None = an even grid at lcfg["bpm"]. `hold_beats`: no shot shorter than
    that; `intact`: show the middle of each clip (its main action) instead of a random part;
    `intro_s`: the first seconds are one clip (a calm one for the logo intro, or `intro_shot`);
    `prefs`: the footage settings (unique clips, crop-safe, palette, action first...)."""
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
    used_here: list[str] = []
    k = 0
    anchor: Shot | None = None
    for ci, (text, nb) in enumerate(zip(texts, bars)):
        cstart = at(t)
        lens = merge_holds([x for b in range(nb) for x in pattern[bar_i + b]], max(1, int(hold_beats)))
        bar_i += nb
        for n_beats in lens:
            dur = at(t + n_beats) - at(t)
            tag = want[k % len(want)]
            k += 1
            pool = shots
            if prefs is not None and not slots and prefs.openers and intro_s <= 0:
                pool = narrow(shots, lambda s: s.key not in prefs.openers)   # opener_rotation
            shot = choose_shot(pool, tag, dur, used_here, uses, rng, prefs, anchor)
            if anchor is None and (intro_s <= 0 or at(t) >= intro_s - 1e-3):
                anchor = shot     # palette_match: the opening clip sets the colours
            used_here.append(shot.key)
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
    if intro_s > 0:
        plan = with_intro_shot(plan, shots, intro_s, uses, intro_shot,
                               prefs.openers if prefs is not None else None, prefs)
    if prefs is not None:
        if prefs.action_first:
            plan = action_order(plan, skip=1 if intro_s > 0 else 0)
        if prefs.impact_ending:
            plan = impact_end(plan, shots, uses, prefs)
    return plan


NEAR_S = 30.0   # one edit never shows two clips from within this many seconds of the same video


def near_used(shot: Shot, used_here: set[str]) -> bool:
    """A clip from (about) the same moment of the same video is already in this edit."""
    if shot.kind != "video":
        return False
    for k in used_here:
        src, _, at = k.rpartition("@")
        if src == shot.src:
            try:
                if abs(float(at) - shot.start) < NEAR_S:
                    return True
            except ValueError:
                pass
    return False


def choose_shot(shots: list[Shot], tag: str, dur: float, used_here, uses: dict[str, int],
                rng: random.Random, prefs: "Prefs | None" = None, anchor: Shot | None = None) -> Shot:
    """The least-used fitting clip, preferring the story's tag, spread over the footage (never
    next to a clip already in this edit, so a long video like the Extended Look gives variety)."""
    fits = [s for s in shots if s.length >= dur]
    if not fits:
        raise editor.EditError(f"no shot is {dur:.1f}s long")
    if prefs is not None:
        return _choose_pref(fits, tag, dur, used_here, uses, rng, prefs, anchor)
    for pool in ([s for s in fits if tag in s.tags and s.key not in used_here and not near_used(s, used_here)],
                 [s for s in fits if s.key not in used_here and not near_used(s, used_here)],
                 [s for s in fits if s.key not in used_here],
                 fits):
        if pool:
            least = min(uses.get(s.key, 0) for s in pool)
            return rng.choice([s for s in pool if uses.get(s.key, 0) <= least + 1])
    raise AssertionError("unreachable")


def _choose_pref(fits: list[Shot], tag: str, dur: float, used_here, uses: dict[str, int], rng: random.Random,
                 prefs: "Prefs", anchor: Shot | None) -> Shot:
    """choose_shot with the footage settings (unique_clips, footage_match, crop_safe, palette_match,
    ambient_bias); each is a preference that never leaves the pool empty."""
    order = list(used_here)
    used = {group_of(prefs, k) for k in order}
    fresh = [s for s in fits if group_of(prefs, s.key) not in used]
    near = set(order)
    themed = [s for s in fresh if s.vtags & prefs.theme] if prefs.theme else []
    for pool in (themed, [s for s in fresh if tag in s.tags], fresh):
        if not pool:
            continue
        pool = narrow(pool, lambda s: not near_used(s, near))   # variety across one long video
        if prefs.relevant:
            pool = narrow(pool, lambda s: s.relevant())
        if prefs.tail > 0:
            pool = narrow(pool, lambda s: s.kind != "video" or s.length >= dur + prefs.tail)
        if prefs.crop_safe:
            pool = narrow(pool, lambda s: s.crop_ok)
        if prefs.palette > 0 and anchor is not None and anchor.yuv:
            lim = shot_info.palette_limit(prefs.palette)
            pool = narrow(pool, lambda s: shot_info.colour_distance(s.yuv, anchor.yuv) <= lim)
        if prefs.variety > 0:     # a broad range between edits: not the recent edits' clips or videos
            pool = narrow(pool, lambda s: s.key not in prefs.recent)
            if prefs.src_load:
                lo = min(prefs.src_load.get(s.src, 0.0) for s in pool)
                slack = (1.0 - prefs.variety) * 0.6 + 0.05
                pool = narrow(pool, lambda s: prefs.src_load.get(s.src, 0.0) <= lo + slack)
        least = min(uses.get(s.key, 0) for s in pool)
        cand = [s for s in pool if uses.get(s.key, 0) <= least + 1]
        if prefs.ambient > 0:
            weights = [0.15 + (s.calm() ** 2) * 4.0 * prefs.ambient + (1.0 - prefs.ambient) for s in cand]
            return rng.choices(cand, weights=weights, k=1)[0]
        return rng.choice(cand)
    # every clip is already in the edit: reuse as a last resort, the one used longest ago
    last = {group_of(prefs, k): i for i, k in enumerate(order)}
    return min(fits, key=lambda s: (last.get(group_of(prefs, s.key), -1), uses.get(s.key, 0)))


def with_seconds(plan: Plan, shots: list[Shot], rng: random.Random, uses: dict[str, int] | None = None,
                 tags: list[str] | None = None) -> Plan:
    """Framing "stack": every slot gets a second clip (shown below it), different from every
    other clip in the edit."""
    uses = dict(uses or {})
    used = {sl.shot.key for sl in plan.slots} | {sl.shot2.key for sl in plan.slots if sl.shot2}
    want = tags or plan.story.tags or ["city"]
    out = []
    for i, sl in enumerate(plan.slots):
        if sl.shot2 is None:
            try:
                s2 = choose_shot(shots, want[i % len(want)], sl.dur, used, uses, rng)
            except editor.EditError:
                out.append(sl)
                continue
            used.add(s2.key)
            uses[s2.key] = uses.get(s2.key, 0) + 1
            off = s2.start + (s2.length - sl.dur) * rng.uniform(0.2, 0.8) if s2.kind == "video" else 0.0
            sl = dataclasses.replace(sl, shot2=s2, offset2=round(off, 3))
        out.append(sl)
    return dataclasses.replace(plan, slots=out)


# --------------------------------------------------------------------------
# Text (ASS, libass)
# --------------------------------------------------------------------------
def card_markup(text: str, highlight: str = "&H00E5FF&") -> str:
    t = editor.ass_escape(text)
    return re.sub(r"\*(.+?)\*", rf"{{\\c{highlight}}}\1{{\\c&HFFFFFF&}}", t).replace("*", "")


TEXT_POS = {"high": (8, 150), "middle": (5, 0), "low": (2, 170)}  # ASS alignment, MarginV (4:5 frame)
BAND_FRAMINGS = ("box11", "stack", "fit")   # framings with a black band above the picture for the text
LABEL_PX = 58   # corner labels on comparison pictures


def build_text_ass(cards: list[tuple[float, float, str]], font: str, size: int,
                   style: dict | None = None, labels: list[dict] | None = None) -> str:
    """Big centred text, upper middle, in TikTok's own text style (Tim, 20 Nov): TikTok Sans, white
    with a thin black outline; the last card (the question) is black text on a white box. A card
    appears on its beat and just disappears: no pop, no movement, no fade. It stays
    editor.CAPTION_HOLD_S longer when the next card isn't due yet, so it can be read.
    `style` (style.resolve output) changes size, position and colours."""
    from .style import ass_colour
    r = style or {}
    font = editor.CAPTION_FONT
    size = int(r.get("text_px") or size * float(r.get("scale") or 1.0))
    q = int(size * 1.05)
    align, margin = TEXT_POS.get(r.get("position") or "high", TEXT_POS["high"])
    in_band = False
    if r.get("text_y") is not None:
        align, margin = 2, int(max(150, min(H - 150, H * (1 - float(r["text_y"])) - size)))
    elif (r.get("position") or "high") == "high" and r.get("framing") in BAND_FRAMINGS:
        margin = BAND_MARGIN   # in the band above the picture, not over it
        in_band = True
    main = ass_colour(r.get("text_colour") or "#FFFFFF")
    hi = "&H" + ass_colour(r.get("highlight") or "#FFE500")[4:] + "&"
    outline = int(r["outline"]) if r.get("outline") is not None else 5
    shadow = int(r["shadow"]) if r.get("shadow") is not None else 1
    box = editor.text_box(r)
    if box > 0 and not in_band:   # over the picture: on a see-through dark box, so it never blends in
        bc = editor.box_colour(box)
        card_style = (f"Style: Card,{font},{size},{main},{main},{bc},{bc},-1,0,0,0,100,100,0,0,3,{editor.BOX_PAD + 4},0,"
                      f"{align},90,90,{margin},1")
    else:
        card_style = (f"Style: Card,{font},{size},{main},{main},&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,{outline},"
                      f"{shadow},{align},90,90,{margin},1")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{card_style}
Style: Ask,{font},{q},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,22,0,{align},90,90,{margin},1
Style: Label,{font},{LABEL_PX},&H00FFFFFF,&H00FFFFFF,&H50000000,&H50000000,-1,0,0,0,100,100,0,0,3,14,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    end_all = max((e for _s, e, _t in cards), default=0.0)
    for i, (s, e, text) in enumerate(cards):
        ask = i == len(cards) - 1 and plain(text).rstrip().endswith("?")
        style_name = "Ask" if ask else "Card"
        nxt = cards[i + 1][0] if i + 1 < len(cards) else end_all
        e = max(e, min(e + editor.CAPTION_HOLD_S, nxt))   # longer to read, never over the next card
        markup = card_markup(text, "&H000000&" if ask else hi)
        if ask:
            markup = markup.replace("{\\c&HFFFFFF&}", "{\\c&H000000&}")
        lines.append(f"Dialogue: 0,{editor.ass_time(s)},{editor.ass_time(e)},{style_name},,0,0,0,,{markup}")
    for lb in labels or []:   # corner labels ("GTA 5 · 2013") on a comparison's pictures
        colour = "&H" + ass_colour(lb.get("colour") or "#FFFFFF")[4:] + "&"
        lines.append(f"Dialogue: 1,{editor.ass_time(lb['start'])},{editor.ass_time(lb['end'])},Label,,0,0,0,,"
                     f"{{\\pos({lb['x']},{lb['y']})\\c{colour}}}{editor.ass_escape(lb['text'])}")
    return head + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------
MOTION_AMOUNT = {"none": 0.0, "subtle": 0.5, "full": 1.0}


def fit_filter(pre: list[str], flash: bool) -> str:
    """The whole frame, nothing cropped (no text or subject cut off), centred on black."""
    head = ",".join(pre) + "," if pre else ""
    tail = ",fade=t=in:st=0:d=0.12:color=white" if flash else ""
    return (f"{head}scale={W}:{H}:force_original_aspect_ratio=decrease:flags=bicubic,setsar=1,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,fps={FPS},setsar=1,format=yuv420p{tail}")


# The picture's size in the 4:5 frame: box45 fills it, box11 is a square on black bars.
BOXES = {"box45": (W, H), "box11": (1080, 1080)}
BLACK_BARS = ("box45", "box11")    # boxes drawn on plain black (no blurred copy: Tim, 19 Nov)
BAND_MARGIN = 70          # "high" text with a boxed / stacked picture: in the band above it
STACK_H, STACK_GAP = 608, 16                               # two 16:9 clips, one above the other


def box_top(framing: str) -> int:
    """Where a boxed picture starts (it is centred; the hook sits in the band above it)."""
    return (H - BOXES[framing][1]) // 2


def stack_tops() -> tuple[int, int]:
    top = (H - 2 * STACK_H - STACK_GAP) // 2
    return top, top + STACK_H + STACK_GAP


TEXT_TAGS = {"title_card", "text_overlay", "rockstar_presents"}   # vision labels: words on screen


def cuts_off(shot: Shot) -> bool:
    """A 4:5 crop would cut something off: words on screen (title cards, captions in the clip) or a
    subject wider than the crop (shot_info / vision labels)."""
    return not shot.crop_ok or bool(shot.vtags & TEXT_TAGS)


def slot_layout(slot: Slot, framing: str, smart: bool = True) -> str:
    """The framing one clip is drawn with. A cropping framing (fill / box45 / box11) shows the whole
    frame instead ("fit") when the crop would cut off text or the subject (Tim, 20 Nov: never cut
    off text or things shown in a clip); smart fill puts a close-up of one person full frame; a
    stacked slot without its second clip falls back to 4:5."""
    chosen = framing
    if framing == "stack" and slot.shot2 is None:
        framing = "box45"
    if framing in CROPPED and cuts_off(slot.shot):
        return "fit"
    if smart and slot.shot.single and chosen in ("box45", "box11", "fit"):
        return "fill"
    return framing


def blur_bg(head: str = "") -> str:
    """The background under stacked pictures: plain black now (no blur, Tim 19 Nov)."""
    return f"{head}scale={W}:{H},drawbox=x=0:y=0:w={W}:h={H}:color=black:t=fill"


def cover_chain(shot: Shot, pan: int, dur: float, zoom: float, motion: str, tw: int, th: int,
                pre: list[str] | None = None) -> list[str]:
    """Scale to cover tw x th and crop, with the slow pan (none with motion "none"). The crop
    is centred on the shot's subject when the library knows where it is."""
    vf = list(pre or [])
    sw, shh = (shot.crop[0], shot.crop[1]) if shot.crop else (shot.w, shot.h)
    s = max(tw / sw, th / shh) * zoom
    ow, oh = max(editor.even(sw * s), tw), max(editor.even(shh * s), th)
    room, yroom = ow - tw, oh - th
    fx, fy = shot.focus if shot.focus else (0.5, 0.5)
    cx0 = min(max(fx * ow - tw / 2, 0), room) if shot.focus else room / 2
    cy0 = min(max(fy * oh - th / 2, 0), yroom) if shot.focus else yroom / 2
    amp = min(160 * tw / W, room) * MOTION_AMOUNT.get(motion, 1.0)
    if amp == 0:   # no camera movement: a fixed crop (nothing depends on time)
        x, y = f"{cx0:.1f}", f"{cy0:.1f}"
    else:
        x = f"clip({cx0:.1f}+{pan * amp:.1f}*(t/{dur:.3f}-0.5),0,{room})"
        y = (f"clip({cy0:.1f}+{-pan * min(80, yroom) * MOTION_AMOUNT.get(motion, 1.0):.1f}"
             f"*(t/{dur:.3f}-0.5),0,{yroom})")
    return vf + [f"scale={ow}:{oh}:flags=bicubic", f"crop={tw}:{th}:'{x}':'{y}'"]


def _pre(shot: Shot) -> list[str]:
    if shot.crop:
        cw, ch, cx, cy = shot.crop
        return [f"crop={cw}:{ch}:{cx}:{cy}"]
    return []


def slot_filter(slot: Slot, zoom: float, motion: str = "full", framing: str = "fill",
                extra: float = 0.0, smart: bool = True, fps: int | None = None) -> str:
    """The -filter_complex graph ([0:v], and [1:v] for a stacked slot -> [v]) for one clip:
    "fill" / "box45" cover-crop to the 1080x1350 frame with a slow pan; "box11" a 1:1 picture on
    black bars; "stack" two 16:9 clips one above the other on black; "fit" the whole frame on black.
    See slot_layout for when a crop becomes "fit" and for smart fill."""
    fps = fps or FPS
    sh = slot.shot
    tail = f"fps={fps},setsar=1,format=yuv420p" + (",fade=t=in:st=0:d=0.12:color=white" if slot.flash else "")
    kind = slot_layout(slot, framing, smart)
    if kind == "fit":
        return "[0:v]" + fit_filter(_pre(sh), slot.flash).replace(f"fps={FPS}", f"fps={fps}") + "[v]"
    if kind == "fill":
        return "[0:v]" + ",".join(cover_chain(sh, slot.pan, slot.dur, zoom, motion, W, H, _pre(sh)) + [tail]) + "[v]"
    if kind in BLACK_BARS:   # 4:5: plain black bars above and below the picture (Tim, 19 Nov)
        bw, bh = BOXES[kind]
        return ("[0:v]" + ",".join(cover_chain(sh, slot.pan, slot.dur, zoom, motion, bw, bh, _pre(sh)))
                + f",pad={W}:{H}:0:{box_top(kind)}:color=black,{tail}[v]")
    if kind in BOXES:
        bw, bh = BOXES[kind]
        pre = ",".join(_pre(sh))
        return (f"[0:v]{pre + ',' if pre else ''}split[a][b];[a]{blur_bg()}[bg];"
                f"[b]{','.join(cover_chain(sh, slot.pan, slot.dur, zoom, motion, bw, bh))}[fg];"
                f"[bg][fg]overlay=0:{box_top(kind)},{tail}[v]")
    # stack: the slot's clip on top, its second clip below, each whole (16:9)
    y1, y2 = stack_tops()
    s2 = slot.shot2
    pre1, pre2 = ",".join(_pre(sh)), ",".join(_pre(s2))
    return (f"[0:v]{pre1 + ',' if pre1 else ''}split[a][b];[a]{blur_bg()}[bg];"
            f"[b]{','.join(cover_chain(sh, slot.pan, slot.dur, zoom, motion, W, STACK_H))}[t];"
            f"[1:v]{','.join(cover_chain(s2, -slot.pan, slot.dur, zoom, motion, W, STACK_H, _pre(s2)))}[u];"
            f"[bg][t]overlay=0:{y1}[m];[m][u]overlay=0:{y2},{tail}[v]")


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


def cut_fades(plan: Plan, fade_s: float, fps: int | None = None) -> Plan:
    """Each slot's crossfade from the previous clip (Tim: a fade only ever blends the TWO clips
    next to each other). A fade is never longer than half of either clip, so it is over before
    the next one starts and no third image is ever in the mix. Whole frames, at least 2."""
    if fade_s <= 0 or len(plan.slots) < 2:
        return Plan(plan.story, plan.cards, [replace(sl, fade_in=0.0) for sl in plan.slots], plan.duration)
    out = [replace(plan.slots[0], fade_in=0.0)]
    for prev, sl in zip(plan.slots, plan.slots[1:]):
        rate = fps or FPS
        f = min(fade_s, prev.dur / 2, sl.dur / 2)
        frames = max(2, int(math.floor(f * rate + 1e-6)))
        out.append(replace(sl, fade_in=frames / rate))
    return Plan(plan.story, plan.cards, out, plan.duration)


def tails(plan: Plan, tail: float = 0.0) -> list[float]:
    """Seconds each slot runs on under the next one (the next slot's crossfade)."""
    n = len(plan.slots)
    if any(sl.fade_in for sl in plan.slots):
        return [plan.slots[i + 1].fade_in if i + 1 < n else 0.0 for i in range(n)]
    return [tail if i < n - 1 else 0.0 for i in range(n)]


def clean_plan(plan: Plan, media: Path, tail: float = 0.0, cuts_of=None) -> Plan:
    """Move each trailer clip's window off any hard cut inside it, so no other scene flashes up
    for a few frames. `tail` = extra seconds rendered past a slot (crossfade overlap; the slots'
    own `fade_in` wins when set). A clip stays as close to its planned spot as it can; if no
    clean stretch is long enough it takes the longest one and HOLDS its last clean frame for the
    rest (`hold`), so another scene never shows, even for a frame, and a fade is never cut off."""
    cuts_of = cuts_of or (lambda sh: window_cuts((media / sh.src).resolve(), sh.start, sh.end))
    margin = 0.05
    slots = []
    for sl, extra in zip(plan.slots, tails(plan, tail)):
        if sl.shot.kind != "video":
            slots.append(sl)
            continue
        need = sl.dur + extra
        inner = [c for c in cuts_of(sl.shot) if sl.shot.start + margin < c < sl.shot.end - margin]
        off = sl.offset
        if any(sl.offset < c < sl.offset + need for c in inner):
            edges = [sl.shot.start] + inner + [sl.shot.end]
            parts = [(a + (margin if a > sl.shot.start else 0), b - (margin if b < sl.shot.end else 0))
                     for a, b in zip(edges, edges[1:])]
            fits = [(a, b) for a, b in parts if b - a >= need]
            if fits:
                a, b = min(fits, key=lambda p: abs(min(max(sl.offset, p[0]), p[1] - need) - sl.offset))
                off = min(max(sl.offset, a), b - need)
            else:
                a, b = max(parts, key=lambda p: p[1] - p[0])
                off = a
            log.info("clip %s: moved %.2f -> %.2f s to avoid a scene cut", sl.shot.key, sl.offset, off)
        # clean source from `off`: up to the next cut or the end of the shot
        stop = min([c - margin for c in inner if c > off + 1e-3] + [sl.shot.end])
        clean = stop - off
        hold = sl.hold
        if clean < need - 0.5 / FPS:
            hold = max(1.0 / FPS, round(clean, 3)) if hold is None else min(hold, max(1.0 / FPS, round(clean, 3)))
        slots.append(replace(sl, offset=round(off, 3), hold=hold))
    return Plan(plan.story, plan.cards, slots, plan.duration)


def prepare(plan: Plan, r: dict, media: Path, cuts_of=None) -> Plan:
    """Crossfades per cut, then clips moved off scene cuts (with held frames where needed)."""
    plan = crop_safe_focus(plan, r)
    return clean_plan(cut_fades(plan, fade_of(r, plan), edit_fps(plan)), media, cuts_of=cuts_of)


def render_slot(slot: Slot, media: Path, out: Path, motion: str = "full", framing: str = "fill",
                extra: float = 0.0, smart: bool = True, fps: int | None = None) -> None:
    """`extra`: seconds rendered past the slot's end (the overlap of a crossfade). A slot with
    `hold` reads only that much clean source and holds its last frame for the rest."""
    fps = fps or FPS
    # frame count from the slot's edges, so uneven real beats never drift over a whole edit
    frames = round((slot.start + slot.dur) * fps) - round(slot.start * fps) + round(extra * fps)
    still = motion == "none"
    held = slot.shot.kind == "video" and slot.hold is not None and slot.hold < slot.dur + extra
    length = f"{slot.dur + extra + 0.2:.3f}"

    def source(shot: Shot, offset: float, t: str = length) -> list[str]:
        src = str((media / shot.src).resolve())
        if shot.kind == "video":
            return ["-ss", f"{offset:.3f}", "-t", t, "-i", src]
        return ["-loop", "1", "-framerate", str(fps), "-t", t, "-i", src]
    inp = source(slot.shot, slot.offset, f"{slot.hold:.3f}" if held else length)
    if slot.shot.kind == "video":
        zoom = 1.0 if (still or slot.shot.w / max(slot.shot.h, 1) > 1.2) else 1.08
    else:
        zoom = 1.0 if still else 1.15
    if slot_layout(slot, framing, smart) == "stack":
        inp += source(slot.shot2, slot.offset2)
    if slot.punch and not still:
        zoom *= 1.18
    graph = slot_filter(slot, zoom, motion, framing, extra, smart, fps)
    if held:   # never run into the next scene: freeze on the last clean frame
        graph = graph[:-len("[v]")] + f",tpad=stop_mode=clone:stop_duration={slot.dur + extra - slot.hold + 0.3:.3f}[v]"
    editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inp, "-an",
                "-filter_complex", graph, "-map", "[v]", "-frames:v", str(frames),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-r", str(fps),
                str(out)], timeout=300)


REAL_60 = 50.0   # a source at least this fast has real 60 fps frames (59.94 counts)


def edit_fps(plan: "Plan") -> int:
    """60 when every trailer clip in the edit really has 60 fps frames (stills move smoothly at
    any rate), else 30: doubling 30 fps trailer frames to 60 stuttered on TikTok (#39)."""
    shots = [sl.shot for sl in plan.slots] + [sl.shot2 for sl in plan.slots if sl.shot2]
    vids = [x for x in shots if x.kind == "video"]
    return 60 if vids and all((x.fps or 0) >= REAL_60 for x in vids) else FPS


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
            sl = dataclasses.replace(sl, dur=round(limit - sl.start, 3))
        slots.append(sl)
    cards = [(a, min(b, limit), t) for a, b, t in plan.cards if a < limit - 0.25]
    return Plan(plan.story, cards, slots, round(slots[-1].start + slots[-1].dur, 3) if slots else limit)


def trim_to(plan: Plan, n_slots: int, min_s: float = 6.0) -> Plan:
    """The plan cut after its first `n_slots` clips (they end on a beat), so a re-render that
    reuses another version's clips adds none of its own. Kept whole if that leaves < min_s."""
    if n_slots >= len(plan.slots) or n_slots <= 0:
        return plan
    end = plan.slots[n_slots - 1].start + plan.slots[n_slots - 1].dur
    if end < min_s:
        return plan
    return trim_plan(plan, end)


def sound_note(note: str | None, fade: float) -> str | None:
    """The card's sound line, plus the fade-out Tim does in the app (edits are silent)."""
    if not note or fade <= 0:
        return note
    return f"{note} · fade the sound out over the last {fade:g} s (the picture fades to black)"


GLITCH_THRESHOLD = 0.3


def edit_cuts(mp4: Path, threshold: float = GLITCH_THRESHOLD) -> list[float]:
    """Hard cuts in a finished edit (scene changes above `threshold`), in seconds."""
    res = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(mp4), "-an",
                          "-vf", f"scale=270:-2,select='gt(scene,{threshold})',metadata=print:file=-",
                          "-f", "null", "-"], capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise editor.EditError(f"cut scan failed: {res.stderr[-200:]}")
    return [t for t, _ in parse_meta(res.stdout)]


def glitches(cuts: list[float], plan: Plan, slack_frames: float = 1.6, also: list[float] | None = None) -> list[float]:
    """Cuts the plan doesn't have: every hard cut must sit on a planned hard cut (a beat where the
    transition isn't a fade, or a text card / intro boundary). Anything else is a stray frame, a
    fade that broke off or a cut that drifted off the beat."""
    ok = [0.0] + [sl.start for sl in plan.slots[1:] if not sl.fade_in] + [a for a, _, _ in plan.cards] + list(also or [])
    slack = slack_frames / FPS
    return [round(t, 3) for t in cuts if t < plan.duration - slack and not any(abs(t - b) <= slack for b in ok)]


def fix_glitches(plan: Plan, times: list[float], shots: list[Shot], uses: dict[str, int],
                 prefs: Prefs | None = None, keep: set[str] | None = None) -> Plan:
    """A clip with an unexpected cut inside it gets another clip of the same length (one that
    isn't in the edit yet); a clip that must stay (kept footage, the intro) holds the frame just
    before the cut instead, so the stray picture never shows."""
    slots = list(plan.slots)
    for t in times:
        i = next((k for k in range(len(slots) - 1, -1, -1) if slots[k].start <= t + 1e-6), 0)
        sl = slots[i]
        if sl.shot.kind != "video":
            continue
        into = t - sl.start
        if sl.shot.key in (keep or set()) or i == 0 or into > sl.dur - 1e-3:
            # the problem is in this clip's own picture (or its fade tail): hold it before the cut
            slots[i] = replace(sl, hold=max(1.0 / FPS, round(into - 1.0 / FPS, 3)))
            continue
        used = {group_of(prefs, x.shot.key) for x in slots}
        cands = [s for s in shots if s.kind == "video" and s.length >= sl.dur + sl.fade_in
                 and group_of(prefs, s.key) not in used and s.key != sl.shot.key]
        if not cands:
            slots[i] = replace(sl, hold=max(1.0 / FPS, round(into - 1.0 / FPS, 3)))
            continue
        new = min(cands, key=lambda s: (uses.get(s.key, 0), -s.relevant(), s.key))
        slots[i] = replace(sl, shot=new, offset=round(new.start + (new.length - sl.dur) / 2, 3), hold=None)
    return Plan(plan.story, plan.cards, slots, plan.duration)


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
    return (f"{days_text(d)} left until GTA 6 releases" if d > 0
            else ("GTA 6 is out today" if d == 0 else "GTA 6 is out now"))


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
PRESENTS_MAX_S = 12.0   # the "Rockstar Games Presents" opening may play this long (added on top of max_s)


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


def join_crossfade(names: list[str], plan: Plan, fade_s: float, cwd: Path, fps: int | None = None) -> str:
    """Join the shots with a soft crossfade starting on each cut's beat: each fade blends only the
    two clips at that cut (its own length from cut_fades; whole frames, so nothing drifts)."""
    inputs = []
    for n in names:
        inputs += ["-i", n]
    graph, last = [], "0:v"
    for k in range(1, len(names)):
        out = f"x{k}"
        d = plan.slots[k].fade_in or fade_s
        rate = fps or FPS
        off = round(plan.slots[k].start * rate) / rate
        graph.append(f"[{last}][{k}:v]xfade=transition=fade:duration={d:.4f}"
                     f":offset={off:.4f}[{out}]")
        last = out
    editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
                "-filter_complex", ";".join(graph), "-map", f"[{last}]", "-an", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "16", "-r", str(fps or FPS), "joined.mp4"], timeout=600, cwd=cwd)
    return "joined.mp4"


def end_fade(r: dict, duration: float) -> float:
    """Seconds of the final fade to black (end_fade_s; at most a third of the edit)."""
    v = r.get("end_fade_s")
    v = 0.0 if v is None else float(v)
    return round(min(v, duration / 3), 3) if v > 0 and duration > 0 else 0.0


def crop_safe_focus(plan: Plan, r: dict) -> Plan:
    """crop_safe: a clip with no known face whose subject would be cut off by a centred crop is
    framed on the subject (Shot.focus, which every renderer centres the crop on)."""
    if not r.get("crop_safe") or r.get("framing", "fill") not in CROPPED:
        return plan
    slots = []
    for sl in plan.slots:
        sh = sl.shot
        if sh.kind == "video" and sh.focus is None and abs(sh.subject_x - 0.5) > 1e-3:
            sl = replace(sl, shot=replace(sh, focus=(sh.subject_x, 0.5)))
        slots.append(sl)
    return Plan(plan.story, plan.cards, slots, plan.duration)


def fade_of(r: dict, plan: Plan) -> float:
    """Crossfade length (0 = hard cuts)."""
    return float(r.get("fade_s") or 0.45) if r.get("transition") == "fade" and len(plan.slots) > 1 else 0.0


def render_plan(plan: Plan, lcfg: dict, out: Path, grade: str = "", text_style: dict | None = None,
                cards: list[tuple[float, float, str]] | None = None, logo: Path | None = None,
                intro_s: float = 0.0, look: str = "", labels: list[dict] | None = None) -> Path:
    """`cards` overrides plan.cards (text level); `logo` + `intro_s` add the logo intro
    (it bursts away exactly at `intro_s`, the drop); `look` is style.look_filter() (grain /
    vignette / cinema bars). `text_style` (style.resolve) also carries motion, framing,
    the crossfade and the logo size."""
    media = Path(lcfg["media_dir"])
    cards = plan.cards if cards is None else cards
    r = text_style or {}
    motion, framing = r.get("motion", "full"), r.get("framing", "fill")
    smart = r.get("smart_fill", True)
    fade = fade_of(r, plan)
    plan = prepare(plan, r, media)
    fps = edit_fps(plan)
    logo_out = float(r.get("logo_fade_s") or LOGO_OUT_S)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        names = []
        from . import progress
        job = progress.current()
        for i, (slot, extra) in enumerate(zip(plan.slots, tails(plan))):
            seg = tmpd / f"s{i:03d}.mp4"
            render_slot(slot, media, seg, motion, framing, extra, smart, fps)
            names.append(seg.name)
            job.update(0.8 * (i + 1) / len(plan.slots), detail=f"shot {i + 1}/{len(plan.slots)}")
        job.update(0.85, detail="joining shots, text and colour")
        base_chain = ",".join(x for x in (grade, look) if x) or "null"
        if fade:
            inputs = ["-i", join_crossfade(names, plan, fade, tmpd, fps)]
        else:
            (tmpd / "list.txt").write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
            inputs = ["-f", "concat", "-safe", "0", "-i", "list.txt"]
        if logo is not None and intro_s > 0:
            inputs += ["-loop", "1", "-t", f"{intro_s + logo_out:.3f}", "-i", str(Path(logo).resolve())]
            graph = (f"[0:v]{base_chain},eq=brightness=-0.18:saturation=0.55:enable='lt(t,{intro_s:.3f})'[b];"
                     + intro_filter(logo_width(r.get("logo_size") or 0.78), intro_s, logo_out)
                     + ";[b][lg]overlay=x=(W-w)/2:y=(H-h)/2:eval=frame:eof_action=pass:format=auto[v0]")
        else:
            graph = f"[0:v]{base_chain}[v0]"
        end = end_fade(r, plan.duration)
        tail_fx = f",fade=t=out:st={plan.duration - end:.3f}:d={end:.3f}" if end else ""
        if cards or labels:
            (tmpd / "text.ass").write_text(build_text_ass(cards or [], lcfg["font"], lcfg["font_size"], text_style,
                                                          labels), encoding="utf-8")
            graph += f";[v0]{editor.ass_filter('text.ass')}{tail_fx},format=yuv420p[v]"
        else:
            graph += f";[v0]null{tail_fx},format=yuv420p[v]"
        part = tmpd / "edit.mp4"
        # -an: no audio stream at all. Tim adds the official sound in the app.
        editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
                    "-filter_complex", graph, "-map", "[v]", "-an", "-r", str(fps),
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
    shots = [s for s in (Library(lcfg).shots() if shots is None else shots) if s.kind == "video"]   # never stills
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
        if (item.get("source") or {}).get("kind") == "compare":   # GTA 5 vs GTA 6 (spikebot/compare.py)
            from . import compare
            return compare.render_variants(self, item, specs, n, folder, preview_bytes)
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
            # trailer video only, never screenshots (Tim, 24 Nov: "never use images"; no setting brings them back)
            shots = [x for x in self.shots() if x.kind == "video"]
            if not shots:
                raise editor.EditError("no trailer clips in the library (screenshots are never used):"
                                       " run `fetch` or add official trailers")
            logo = Path(self.lcfg.get("logo_file") or "")
            intro, pres, intro_note = self.pick_intro(r, item, n, spec, base_v, state)
            want_logo = intro == "logo" and logo.is_file()
            want_pres = intro == "rockstar_presents" and pres is not None
            if intro == "logo" and not want_logo:
                log.warning("logo intro asked for but %s is missing: rendering without it", logo)
            lead_s = r["logo_s"] or None    # how long the logo builds before the drop
            if want_pres:   # the "Rockstar Games Presents" shot plays up to the drop
                lead_s = min(pres.length, PRESENTS_MAX_S)
            fade = float(r.get("fade_s") or 0.45) if r["transition"] == "fade" else 0.0
            prefs = prefs_for(r, shots, state.get("openers"), fade, state.get("recent_edits"))
            cap = edit_cap(r, self.lcfg, trend)
            if want_pres:   # the long Presents opening comes on top: the edit after it keeps its length
                cap += lead_s
            plan_cfg, song_start = dict(self.lcfg, max_s=cap, min_s=min(float(self.lcfg["min_s"]), cap)), None
            build_beats = music.BUILD_BEATS
            if song:
                if lead_s and (want_pres or r["intro"] in ("logo", "countdown")):
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
            # the caption line, and footage that shows what it names ("vice city at night" = night clips)
            line = pick_line(story, today, "", shots)
            prefs.theme = line_tags(_raw_of(line, today) or "") if not base_v else set()
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
            if want_logo or want_pres or intro == "countdown":
                # the build-up ends exactly on the drop (the song's, or the BPM grid's)
                intro_s = beats[build_beats] if beats else (lead_s or bar_s(self.lcfg["bpm"]))
            one_shot = want_logo or want_pres     # the intro plays over a single clip
            plan_kw = dict(pace=pace, transition=r["transition"], cards=cards, beats=beats,
                           hold_beats=r["hold_beats"], intro_s=intro_s if one_shot else 0.0,
                           intact=r["hold_beats"] >= 2 or r["motion"] == "none", prefs=prefs,
                           intro_shot=pres if want_pres else None)
            plan = plan_clip(story, shots, plan_cfg, today, random.Random(seed), state.get("uses"), **plan_kw)
            if song:   # the edit starts at song_start: it must end before the sound does
                end = float(song["duration"]) - song_start
                for _ in range(4):
                    if plan.duration <= end + 1e-3 or float(plan_cfg["max_s"]) <= float(plan_cfg["min_s"]):
                        break
                    plan_cfg = dict(plan_cfg, max_s=float(plan_cfg["max_s"]) - bar_s(float(song["bpm"])))
                    plan = plan_clip(story, shots, plan_cfg, today, random.Random(seed), state.get("uses"),
                                     **plan_kw)
                plan = trim_plan(plan, end)
            kept, recovered = 0, False
            # whose clips to reuse: the version Tim named ("use the clips from B1"), else this one's
            src_v = spec.get("footage_from") or base_v
            src_dir = Path(spec.get("footage_dir") or folder)
            footage = (src_v or {}).get("footage")
            if src_v and spec.get("keep_footage") and not footage:
                footage = self.recover(src_dir, src_v)   # made before clip lists were saved
                recovered = bool(footage)
            if src_v and spec.get("keep_footage") and footage:
                src_intro = src_v.get("intro") or "none"
                src_one = src_intro in ("logo", "rockstar_presents")
                skip = 1 if (one_shot and intro_s) else 0     # this edit's own intro clip stays
                if src_one and (not skip or src_intro != intro or want_pres):
                    footage = footage[1:]    # the old intro's clip goes with the old intro
                elif src_one and skip:
                    skip = 0                 # same kind of intro: its clip is one of the kept ones
                if len(footage) > len(plan.slots) - skip:    # fewer slots than clips: never drop one
                    plan, kept = plan_from_footage(plan, footage, shots, beats, float(self.lcfg["bpm"]), skip)
                    limit = (float(song["duration"]) - song_start) if song else cap
                    plan = trim_plan(plan, limit)
                else:
                    plan, kept = refit(plan, footage, shots, skip=skip)
                    # exactly those clips, none added: the edit ends with the last one (on its beat)
                    plan = trim_to(plan, skip + len(footage), float(self.lcfg.get("min_reuse_s", 6.0)))
                if recovered:
                    src_v = dict(src_v, footage=footage, recovered=True)
                if spec.get("footage_from"):
                    base_v = dict(base_v or {}, label=src_v["label"], footage=src_v.get("footage") or footage,
                                  recovered=src_v.get("recovered", False))
                elif recovered:
                    base_v = src_v
            if r["framing"] == "stack":
                plan = with_seconds(plan, shots, random.Random(seed + 1), state.get("uses"))
            line = line_for_footage(line, plan, story, today, spec["label"])   # never "at night" over daytime
            stem = f"r{n}_{spec['label']}"
            mp4 = folder / f"{stem}.mp4"
            if r["text_level"] == "plain":
                shown = [(intro_s, plan.duration, plain_line(today))] if plan.duration - intro_s >= 1.5 else []
            else:
                shown = cards_for_level(plan.cards, r["text_level"], intro_s)
            if r["intro"] == "countdown" and intro_s > 0:
                shown = [(0.0, intro_s, countdown_line(today))] + shown
            job.update(0.05, f"Version {spec['label']} ({vi + 1}/{len(specs)}): rendering")
            the_logo = logo if want_logo and intro_s else None
            keep_keys = {f.get("key") for f in footage or []} if (src_v and spec.get("keep_footage")) else set()
            with job.span(0.05, 0.9):
                plan = prepare(plan, r, Path(self.lcfg["media_dir"]))
                engine = self.render_edit(plan, mp4, r, shown, the_logo, intro_s)
                glitch_note = None
                if r.get("glitch_check", True):
                    plan, engine, glitch_note = self.glitch_pass(plan, mp4, r, shown, the_logo, intro_s, engine,
                                                                 shots, state.get("uses") or {}, prefs, keep_keys)
            job.update(0.9, f"Version {spec['label']} ({vi + 1}/{len(specs)}): preview")
            prev = editor.preview(mp4, folder / f"{stem}_preview.mp4", preview_bytes, plan.duration)
            uses = state.setdefault("uses", {})
            for sl in plan.slots:
                for sh in (sl.shot, sl.shot2):
                    if sh is not None:
                        uses[sh.key] = uses.get(sh.key, 0) + 1
            if plan.slots and not want_pres:   # opener_rotation: what opened the recent edits
                state["openers"] = (state.get("openers", []) + [plan.slots[0].shot.key])[-40:]
            state["recent_edits"] = (state.get("recent_edits", []) + [[sl.shot.key for sl in plan.slots]])[-20:]
            state["recent"] = (state.get("recent", []) + [story.id])[-12:]
            hook = plain(fill(texts["hook"], today)) if texts.get("hook") else None
            same_song = ((base_v or {}).get("music") or {}).get("song") == (song or {}).get("name")
            caps = dict(base_v["captions"]) if base_v and not hook and same_song else \
                (tg_captions if (is_tg_song and not trend) else trend_captions)(story, today, self.lcfg, hook or line)
            title = (base_v["title"] if base_v and not hook
                     else (hook or line)[:95])
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
                        "intro": intro if intro_s and (want_logo or want_pres or intro == "countdown") else "none",
                        "intro_note": intro_note, "glitch_note": glitch_note,
                        "music": ({"song": song["name"], "start_s": song_start, "bpm": song["bpm"]}
                                  if song else None),
                        "music_note": sound_note(music.note(song, song_start), end_fade(r, plan.duration))
                                      if song else None})
        save_state(state_path, state)
        return out

    def recover(self, folder: Path, base_v: dict) -> list[dict]:
        """The clip list of an old version, read back from its video (footage_match)."""
        from . import footage_match
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
            framing = (base_v.get("style") or {}).get("framing") or "fill"   # versions before framing: fill
            if framing not in ("fill", "fit"):
                return []   # boxed / stacked versions always save their clip list
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
        plan = prepare(plan, r, Path(self.lcfg["media_dir"]))
        if engine == "remotion" or (engine == "auto" and remotion.available()):
            try:
                remotion.render(plan, self.lcfg, mp4, r, shown, logo, intro_s)
                return "remotion"
            except (remotion.RemotionError, subprocess.SubprocessError, OSError) as e:
                log.warning("Remotion render failed, using ffmpeg instead: %s", e)
        render_plan(plan, self.lcfg, mp4, st.grade_filter(r["grade"]), r, cards=shown, logo=logo,
                    intro_s=intro_s, look=st.look_filter(r["look"], "tokyo_girl", W, H))
        return "ffmpeg"

    def presents(self) -> list[Shot]:
        """The library's "Rockstar Games Presents" shots over scenery (needs the vision labels)."""
        if getattr(self, "_presents", None) is None:
            lib = Library(self.lcfg)
            try:
                self._presents = lib.presents_shots()
            except Exception as e:  # noqa: BLE001 - no such intro this time
                log.warning("Rockstar Games Presents shots unavailable: %s", e)
                self._presents = []
        return self._presents

    def pick_intro(self, r: dict, item: dict, n: int, spec: dict, base_v: dict | None,
                   state: dict) -> tuple[str, Shot | None, str | None]:
        """(intro, Rockstar Games Presents shot or None, note for the card). intro_mix sends a share
        of logo-intro edits to the Rockstar Games Presents opening instead (a re-render keeps the
        intro its version had)."""
        intro = r["intro"]
        if intro == "logo" and float(r.get("intro_mix") or 0) > 0:
            if base_v and base_v.get("intro") in ("logo", "rockstar_presents"):
                intro = base_v["intro"]
            elif random.Random(f"{item['id']}-{n}-{spec['label']}-intro").random() < float(r["intro_mix"]):
                intro = "rockstar_presents"
        if intro != "rockstar_presents":
            return intro, None, None
        shots = self.presents()
        if not shots:
            log.warning("no Rockstar Games Presents shot found in the library: using the logo intro")
            return "logo", None, ("No \"Rockstar Games Presents\" shot found in the trailers yet (it needs the"
                                  " shot labels): used the logo intro")
        uses = state.get("uses") or {}
        return intro, min(shots, key=lambda s: (uses.get(s.key, 0), -s.length)), None

    def glitch_pass(self, plan: Plan, mp4: Path, r: dict, shown, logo, intro_s: float, engine: str,
                    shots: list[Shot], uses: dict, prefs: Prefs, keep: set[str], tries: int = 1):
        """glitch_check: scan the finished edit for hard cuts the plan doesn't have (a stray frame
        flashing in, a fade cut off) and fix them (another clip, or the same clip held), re-rendering
        at most `tries` times. Returns (plan, engine, note for the card)."""
        fixed = 0
        for attempt in range(tries + 1):
            try:
                bad = glitches(edit_cuts(mp4), plan, also=[intro_s] if intro_s else None)   # the drop
            except Exception as e:  # noqa: BLE001 - the check must never lose the edit
                log.warning("glitch check of %s failed: %s", mp4.name, e)
                return plan, engine, None
            if not bad:
                return plan, engine, (f"Glitch check: fixed {fixed} stray cut(s)" if fixed else None)
            if attempt == tries:
                break
            log.warning("%s: %d unexpected cut(s) at %s s: fixing and re-rendering", mp4.name, len(bad),
                        ", ".join(f"{t:.2f}" for t in bad))
            plan = fix_glitches(plan, bad, shots, uses, prefs, keep)
            plan = prepare(plan, r, Path(self.lcfg["media_dir"]))
            engine = self.render_edit(plan, mp4, r, shown, logo, intro_s)
            fixed += len(bad)
        return plan, engine, f"Glitch check: {len(bad)} cut(s) still look off at " + ", ".join(
            f"{t:.1f}s" for t in bad[:4])

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
        if (item.get("source") or {}).get("kind") in ("trend", "compare"):
            export = export.parent / item["source"]["kind"]
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
    kw = {"on_file": on_file} if on_file else {}   # the /trend bar; daily runs call it plainly
    shots = Library(lcfg).shots(**kw)
    if not shots:
        log.info("no usable media in %s yet, fetching official Rockstar media", lcfg["media_dir"])
        try:
            fetch(lcfg)
        except Exception as e:
            log.warning("fetch failed: %s", e)
        shots = Library(lcfg).shots(**kw)
    return shots


def daily(lcfg: dict, now: float | None = None) -> list:
    """Top today's edits up to per_day (review items, or exports with review off).
    Safe to run any number of times a day."""
    now = now or time.time()
    if not lcfg["enabled"]:
        log.info("Tokyo Girl mode is off (TOKYO_GIRL_MODE=false)")
        return []
    state = load_state(Path(lcfg["state_file"]))
    wanted = list(lcfg["sources"]) + list(lcfg.get("searches") or []) + [GTA5_FETCH]
    new_source = any(x not in (state.get("fetched") or []) for x in wanted)
    if new_source or now - state.get("last_fetch", 0) > lcfg["refetch_days"] * 86400:
        from . import compare
        for c in (lcfg, compare.gta5_cfg(lcfg)):   # GTA 6 media, and GTA 5's for the comparisons
            try:
                fetch(c)
            except Exception as e:  # never let a fetch problem stop the edits
                log.warning("fetch failed, using media already on disk: %s", e)
        state = load_state(Path(lcfg["state_file"]))
        state["last_fetch"] = now
        state["fetched"] = wanted   # a source added later (e.g. the Extended Look) is fetched straight away
        save_state(Path(lcfg["state_file"]), state)
    day = datetime.fromtimestamp(now, UK).strftime("%Y-%m-%d")
    if lcfg.get("review"):
        daily_compare(lcfg, day, now)
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


GTA5_FETCH = "gta5-official-trailers-2"   # marks the GTA 5 library as fetched (comparisons; -2 = character trailers)


def daily_compare(lcfg: dict, day: str, now: float) -> list[str]:
    """Top up today's GTA 5 vs GTA 6 comparisons to `compare_per_day`. Never stops the rest."""
    per_day = int(lcfg.get("compare_per_day") or 0)
    state = load_state(Path(lcfg["state_file"]))
    done = (state.get("compare_days") or {}).get(day, 0)
    if per_day <= done:
        return []
    from . import compare
    try:
        ids = compare.make(lcfg, per_day - done, now=now)
    except Exception as e:  # noqa: BLE001 - no GTA 5 footage / no API key yet: the edits still get made
        log.warning("GTA 5 vs GTA 6 comparison skipped: %s", e)
        return []
    state = load_state(Path(lcfg["state_file"]))
    state.setdefault("compare_days", {})[day] = done + len(ids)
    save_state(Path(lcfg["state_file"]), state)
    return ids


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
    jobs = [(v, "uploader~='(?i)rockstar'") for v in dict.fromkeys(videos)]
    for q in lcfg.get("searches") or []:
        # the newest few results for the title; only Rockstar's own upload with that title is taken.
        # A dict search gives its own title regex and an `exclude` regex (no backslashes in either).
        q = q if isinstance(q, dict) else {"query": q}
        flt = f"uploader~='(?i)^rockstar games$' & title~='{q.get('title') or title_regex(q['query'])}'"
        if q.get("exclude"):
            flt += f" & title!~='{q['exclude']}'"
        jobs.append((f"ytsearch{int(q.get('max', 5))}:{q['query']} Rockstar Games", flt))
    for v, flt in jobs:
        # Video only (no audio needed), max 1080p (60 fps when offered), Rockstar's own channel only.
        p = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist",
                            "-f", "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b[height<=1080]",
                            "-S", "res:1080,fps", "--match-filter", flt,
                            "--download-archive", str(media / ".yt_archive"),
                            "--restrict-filenames", "-o", str(media / "trailers" / "%(title).80s_trailer_%(id)s.%(ext)s"),
                            v], capture_output=True, text=True, timeout=3600)
        if p.returncode != 0:
            log.warning("yt-dlp failed for %s: %s", v, p.stderr.strip()[-300:])


def title_regex(title: str) -> str:
    """yt-dlp match-filter regex: every word of the title, in order, any punctuation between."""
    words = re.findall(r"[A-Za-z0-9]+", title)
    return "(?i)" + "[^A-Za-z0-9]+".join(words)   # no backslashes: they would need escaping in the filter


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
    cp = sub.add_parser("compare", help="GTA 5 vs GTA 6 comparison edits (review items)")
    cp.add_argument("-n", type=int, default=1)
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
    elif args.cmd == "compare":
        job = progress.current()
        job.update(0.0, "Checking the trailer libraries")
        from . import compare
        try:
            with job.span(0.0, 0.1):
                ensure_media(lcfg, on_file=lambda i, n, rel: job.update(i / max(1, n), detail=f"{i + 1}/{n} · {Path(rel).name}"))
                compare.ensure_gta5(lcfg)
            with job.span(0.1, 1.0):
                ids = compare.make(lcfg, args.n)
        except progress.Cancelled:
            print("comparison edits cancelled")
            return
        except editor.EditError as e:
            sys.exit(f"FAILED: {e}")
        from . import review
        store = review.Store(Path(lcfg["data_dir"]))
        failed = 0
        for item_id in ids:
            item = store.load(item_id)
            ok = item.get("status") != "failed"
            failed += not ok
            print(f"review item {item_id}: " + ("cancelled" if item.get("status") == "cancelled"
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
