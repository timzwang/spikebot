"""Lacy mode: turn Lacy's GTA chat spikes into ready-to-post Shorts for the
Lacy clipping campaign (Reach / Content Rewards).

Flow (all automatic up to review):
  1. Bot.handle() sees a spike on twitch/lacy, re-checks his category with Helix
     Get Streams right then, and only queues a job for Grand Theft Auto V / VI.
  2. LacyMode.worker() waits for the VOD to contain the moment, then runs
     `python -m spikebot.lacy render JOB` in a subprocess: cut the segment, skip
     BRB/away/camera-off, transcribe (English only), render 1080x1920 with his
     facecam on top for the whole clip, captions, no logo, unique thumbnail.
  3. Three variants (A/B/C: different grade, pacing, text style and hook) go to the
     review queue (spikebot/review.py); the Discord bot posts them in #ready-to-post.
     Nothing is uploaded until Tim presses Approve. Then the bot uploads to YouTube
     (max N a day, posting window), sets the thumbnail, posts the Instagram Reel,
     appends to logs/lacy_submissions.csv and puts the TikTok copy in #tiktok-queue.

CLI:  python -m spikebot.lacy list | render ID | approve ID | reject ID | layout VIDEO
"""
from __future__ import annotations

import contextlib

import argparse
import asyncio
import csv
import json
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import editor

log = logging.getLogger("lacy")
UK = ZoneInfo("Europe/London")

DEFAULTS = {
    "enabled": False,
    "channel": "lacy",                       # twitch.tv/lacy
    "allowed_categories": ["Grand Theft Auto V", "Grand Theft Auto VI"],
    "max_uploads_per_day": 3,                # UK calendar day
    "max_renders_per_day": 10,               # clips made per streamer a day (the best-scored ones, Tim 20 Nov)
    "auto_upload": False,                    # ignored: Tim approves every clip in Discord
    "min_score": None,                       # None = use min_score_to_alert
    "vod_delay_s": 300,                      # wait for the VOD to contain the moment
    "max_clip_s": 59,
    # How much of the VOD is cut around each spike for the editor to choose from: the chat
    # window plus this much before/after, at most `sample_s` in all (centred on the peak).
    "sample_before_s": 30,
    "sample_after_s": 30,
    "sample_s": 150,
    "privacy": "public",
    "facecam": {"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.30},  # fractions of the frame
    "whisper_model": "base",
    "min_english_prob": 0.6,
    "min_words": 4,
    "thumb_min_distance": 10,
    "submissions_csv": "logs/lacy_submissions.csv",
}

TRUE = {"1", "true", "yes", "on"}


def settings(cfg: dict) -> dict:
    """Lacy settings: defaults < config.json "lacy" block < LACY_* from .env/env
    (main.apply_env puts those in cfg["lacy_mode"] / cfg["lacy_max_uploads_per_day"])."""
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in (cfg.get("lacy") or {}).items():
        out[k] = (out[k] | v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    if cfg.get("lacy_mode") not in (None, ""):
        out["enabled"] = str(cfg["lacy_mode"]).strip().lower() in TRUE
    if cfg.get("lacy_max_uploads_per_day") not in (None, ""):
        out["max_uploads_per_day"] = int(cfg["lacy_max_uploads_per_day"])
    # Auto-upload was removed: every clip waits for approval in Discord.
    out["auto_upload"] = False
    if out["min_score"] is None:
        out["min_score"] = float(cfg.get("min_score_to_alert", 0))
    out["channel"] = out["channel"].lower()
    return out


# --------------------------------------------------------------------------
# Stream-clip editor for the other programme channels (Adin Ross, Seal...).
# Same pipeline as Lacy (chat spike -> VOD segment -> 3 captioned variants -> review),
# one *profile* per streamer. Lacy's profile comes from the "lacy" block; every other
# channel in config "channels" with a programme gets one from the "clips" block
# (CLIPS_MODE=false switches them off). A channel without a programme never does:
# Tim has to check clipping permission first.
# --------------------------------------------------------------------------
CLIP_DEFAULTS = {
    "enabled": True,
    # Channels without a programme that Tim has cleared for clipping himself (his call,
    # 18 Nov 2026: "Use Kai stream clips anyway"). They need to be in "channels" too.
    "allow": ["kaicenat"],
    # per-streamer overrides keyed by channel name, e.g.
    #   "adinross": {"name": "Adin Ross", "facecam": {"x": 0.75, "y": 0.0, "w": 0.25, "h": 0.3}}
    "streamers": {},
}
# Display names used in titles and captions when config gives no `display`.
KNOWN_NAMES = {"adinross": "Adin Ross", "sealstats": "Seal", "lacy": "Lacy", "kaicenat": "Kai Cenat"}
# (every campaign's name is in spikebot/campaigns.py)
# Kick names its categories a little differently from Twitch.
KICK_GTA = ["GTA V", "GTA VI", "GTA 5", "GTA 6", "Grand Theft Auto 5", "Grand Theft Auto 6"]
PLATFORM_URL = {"twitch": "https://www.twitch.tv/{}", "kick": "https://kick.com/{}"}
PLATFORM_NAME = {"twitch": "Twitch", "kick": "Kick"}


def clip_settings(cfg: dict) -> dict:
    out = json.loads(json.dumps(CLIP_DEFAULTS))
    for k, v in (cfg.get("clips") or {}).items():
        out[k] = v
    if cfg.get("clips_mode") not in (None, ""):
        out["enabled"] = str(cfg["clips_mode"]).strip().lower() in TRUE
    return out


def hashtag(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", name) or "GTA"


def profiles(cfg: dict, enabled_only: bool = True) -> dict[tuple[str, str], dict]:
    """(platform, channel) -> settings for every streamer whose clips get made.
    Each profile is a full Lacy-style settings dict plus name / platform / hashtag / programme."""
    from . import campaigns
    out: dict[tuple[str, str], dict] = {}
    # every campaign Tim joined is watched and clipped (spikebot/campaigns.py), on top of config
    merged = campaigns.merge_channels({"channels": [dict(c) for c in cfg.get("channels") or []],
                                       "campaigns": cfg.get("campaigns") or {}})
    chans = {(c.get("platform"), str(c.get("name", "")).lower()): c for c in merged["channels"]}
    reg = campaigns.registry(cfg)
    lacy = settings(cfg)
    lkey = ("twitch", lacy["channel"])
    if (lacy["enabled"] or not enabled_only) and not (enabled_only and campaigns.ended(cfg, lkey[1])):
        prog = (chans.get(lkey) or {}).get("programme") or "Lacy (Reach pool)"
        out[lkey] = with_campaign(lacy | {"name": "Lacy", "platform": "twitch", "hashtag": "Lacy",
                                          "programme": prog}, reg.get(lkey[1]))
    cs = clip_settings(cfg)
    if not cs["enabled"] and enabled_only:
        return out
    common = {k: v for k, v in cs.items() if k not in ("enabled", "streamers", "allow")}
    allow = {str(a).lower() for a in cs.get("allow") or []}
    for key, c in chans.items():
        cleared = key[1] in allow
        if key in out or key == lkey or not (c.get("programme") or cleared) or key[0] not in PLATFORM_URL:
            continue
        if enabled_only and campaigns.ended(cfg, key[1]):   # budget gone / campaign over: no more clips
            continue
        p = json.loads(json.dumps(DEFAULTS))
        if key[0] == "kick":
            p["allowed_categories"] = p["allowed_categories"] + KICK_GTA
        for src in (common, (cs.get("streamers") or {}).get(key[1]) or {}):
            for k, v in src.items():
                p[k] = (p[k] | v) if isinstance(p.get(k), dict) and isinstance(v, dict) else v
        p["enabled"] = True
        p["auto_upload"] = False
        # no box in config for this streamer: find the webcam in each clip (safety.find_facecam)
        p["facecam_auto"] = "facecam" not in ((cs.get("streamers") or {}).get(key[1]) or {}) \
            and "facecam" not in common
        if p["min_score"] is None:
            p["min_score"] = float(cfg.get("min_score_to_alert", 0))
        camp = reg.get(key[1])
        name = (p.get("name") or c.get("display") or (camp or {}).get("name") or KNOWN_NAMES.get(key[1])
                or key[1])
        p.update(channel=key[1], platform=key[0], name=name, hashtag=hashtag(name),
                 programme=c.get("programme") or "cleared by Tim (no programme)")
        p = with_campaign(p, camp)
        p.setdefault("submissions_csv", f"logs/{key[1]}_submissions.csv")
        if p["submissions_csv"] == DEFAULTS["submissions_csv"]:   # never mix with Lacy's CSV
            p["submissions_csv"] = f"logs/{key[1]}_submissions.csv"
        out[key] = p
    return out


def with_campaign(p: dict, camp: dict | None) -> dict:
    """A streamer profile plus its campaign's rules: where it pays (`platforms`), whether AI content
    is allowed (the voiceover teaser), the layout ("full" keeps the whole stream frame) and the
    caption rules (`campaign`, read by campaigns.finish_copy)."""
    p = dict(p)
    p["campaign"] = camp
    if camp:
        p.setdefault("platforms", camp.get("platforms"))
        p.setdefault("ai_allowed", bool(camp.get("ai_allowed")))
        p.setdefault("layout", camp.get("layout") or "auto")
    else:
        p.setdefault("ai_allowed", False)
        p.setdefault("layout", "auto")
    return p


def job_key(job: dict) -> tuple[str, str]:
    """(platform, channel) of a job or a review item's source (old Lacy jobs have no platform)."""
    return (job.get("platform") or "twitch", str(job.get("channel") or "lacy").lower())


def profile_for(cfg: dict, job: dict) -> dict:
    """The streamer profile a job/item belongs to, even if that mode was switched off since."""
    key = job_key(job)
    p = profiles(cfg, enabled_only=False).get(key)
    if p is None:
        raise ValueError(f"{key[0]}/{key[1]} has no clipping programme in config")
    return p


def is_gta(category: str | None, allowed: list[str]) -> bool:
    """Exact (case-insensitive) match, so GTA IV, San Andreas or 'GTA RP' style
    categories don't sneak in."""
    c = (category or "").strip().casefold()
    return bool(c) and c in {a.strip().casefold() for a in allowed}


def game_tag(category: str) -> str:
    return "GTA 6" if category.strip().upper().endswith(" VI") else "GTA 5"


# --------------------------------------------------------------------------
# Titles: always "Lacy", neutral or funny, never mocking or drama
# --------------------------------------------------------------------------
TITLES = {
    "funny": ["{name} can't stop laughing at this {game} moment",
              "{name}'s chat lost it over this",
              "{name} did NOT expect this in {game}",
              "This {game} moment had {name} in tears laughing",
              "{name} and chat could not keep it together"],
    "shock": ["{name} can't believe this happened in GTA",
              "{name}'s reaction to this {game} moment",
              "Wait for {name}'s reaction",
              "{name} was not ready for this in {game}",
              "Nobody saw this coming, not even {name}"],
    "hype": ["{name} actually pulled this off in {game}",
             "{name}'s cleanest {game} moment yet",
             "Chat went wild for {name} here",
             "{name} made this look easy in {game}",
             "{name}'s {game} run just hit different"],
    "neutral": ["{name}'s best {game} moment today",
                "{name} in {game} never disappoints",
                "{name} and chat react to this {game} moment",
                "This is why we watch {name} play {game}"],
}
VIBES = {"funny": {"LOL", "LMAO", "LUL", "KEKW", "OMEGALUL", "ICANT", "💀", "😂", "🤣", "😭"},
         "shock": {"NO WAY", "WTF", "HOLY", "OOO", "NAH", "BRO", "MONKAS"},
         "hype": {"W", "POG", "INSANE", "🔥"}}

BANNED = re.compile(
    r"\b(hate\w*|haters?|farm\w*|exposed?|cringe\w*|drama|beef|fraud|clown\w*|trash|loser\w*|"
    r"fail\w*|embarrass\w*|humiliat\w*|roast\w*|destroy\w*|cancel\w*|toxic|rag(e|ing|ed)|"
    r"meltdown|worst|terrible|pathetic|idiot\w*|stupid|dumb\w*|mock\w*|scam\w*|banned|"
    r"owned|ratio\w*|cope|seethe|crash(es|ed)? out|takes? an l|caught|disgust\w*|freak\w*)\b",
    re.IGNORECASE)
ENGLISH = re.compile(r"^[A-Za-z0-9 '’!?.,:&()\-]+$")


def voice_line_ok(line: str, name: str = "Lacy") -> bool:
    """A spoken teaser line: the streamer's name, 5-14 words, plain English, no banned words."""
    n = len((line or "").split())
    return (name.lower() in (line or "").lower() and 5 <= n <= 14 and not BANNED.search(line)
            and bool(ENGLISH.match(line)))


WRONG_PRONOUNS = {
    "he/him": re.compile(r"\b(she|her|hers|herself|she's|she'll|she'd)\b", re.I),
    "she/her": re.compile(r"\b(he|him|his|himself|he's|he'll|he'd)\b", re.I),
}


def pronouns_ok(text: str, pronouns: str = "he/him") -> bool:
    """No pronoun for the streamer other than theirs (Lacy, Adin Ross, Seal... are he/him). Used on
    every AI-written title, caption, hook and voice line; a miss falls back to the templates."""
    bad = WRONG_PRONOUNS.get(pronouns)
    return not (bad and bad.search(text or ""))


def screen_hook_ok(hook: str, name: str = "Lacy") -> bool:
    """The on-screen hook: the streamer's name, 3-8 words, <= 42 characters, plain English, no banned
    words, no calls to follow (those go in the caption)."""
    h = (hook or "").strip()
    n = len(h.split())
    return (name.lower() in h.lower() and 3 <= n <= 8 and len(h) <= 42 and not BANNED.search(h)
            and bool(ENGLISH.match(h)) and not re.search(r"\b(follow|subscribe|twitch\.tv|kick\.com)\b", h, re.I))


def title_ok(title: str, name: str = "Lacy") -> bool:
    return (name.lower() in title.lower() and 10 <= len(title) <= 100
            and not BANNED.search(title) and bool(ENGLISH.match(title)))


def mood(vibe: list[str]) -> str:
    for word in vibe or []:
        for name, words in VIBES.items():
            if word.upper() in {w.upper() for w in words}:
                return name
    return "neutral"


def make_title(vibe: list[str], category: str, used: set[str], rng: random.Random | None = None,
               tone: str | None = None, hook_style: str | None = None, name: str = "Lacy") -> str:
    """A title for this clip's mood (or the given tone) that hasn't been used on an
    earlier upload. `hook_style` (from a preset: pov / wait / question...) moves
    templates of that kind to the front."""
    from .style import hook_style as classify
    rng = rng or random.Random()
    game = game_tag(category)
    m = tone if tone in TITLES else mood(vibe)
    first = TITLES[m][:]
    rest = [t for k, ts in TITLES.items() if k != m for t in ts]
    rng.shuffle(first)
    rng.shuffle(rest)
    order = first + rest
    if hook_style:
        order.sort(key=lambda t: classify(t.format(game=game, name=name)) != hook_style)
    used_l = {u.lower() for u in used}
    for tpl in order:
        t = tpl.format(game=game, name=name)
        if title_ok(t, name) and t.lower() not in used_l:
            return t
    base = first[0].format(game=game, name=name)
    for n in range(2, 1000):  # every template used: number them
        t = f"{base} (part {n})"
        if title_ok(t, name) and t.lower() not in used_l:
            return t
    raise ValueError("no title available")


def description(title: str, category: str, channel: str, name: str = "Lacy", platform: str = "twitch") -> str:
    game = game_tag(category)
    tag = "#GTA6" if game == "GTA 6" else "#GTA5"
    url = PLATFORM_URL.get(platform, PLATFORM_URL["twitch"]).format(channel)
    return (f"{title}\n\n"
            f"{name} playing {category} live on {PLATFORM_NAME.get(platform, 'Twitch')}: {url}\n"
            f"Clipped from {name}'s stream as part of the {name} clipping programme.\n\n"
            f"{name} {game} stream clips: the best {game} gameplay moments, every day.\n\n"
            f"#{hashtag(name)} #Shorts {tag} {tag}Gameplay {tag}Clips #GTA")


def tags(category: str, name: str = "Lacy", platform: str = "twitch") -> list[str]:
    # What people search in launch week (vidIQ, 18 Nov 2026): "gta 6 gameplay", "gta 6 clips",
    # "<streamer> gta 6", "gta 6 stream".
    game = game_tag(category)
    return [name, f"{name} {game}", f"{name} GTA", "Shorts", game, f"{game} gameplay", f"{game} clips",
            f"{game} stream", category, f"{PLATFORM_NAME.get(platform, 'Twitch')} clips"]


def who(job: dict) -> tuple[str, str]:
    """(display name, platform) of a job; old Lacy jobs have neither."""
    return job.get("streamer") or "Lacy", job.get("platform") or "twitch"


# --------------------------------------------------------------------------
# Submission log (the CSV Tim pastes into Content Rewards) and the daily cap
# --------------------------------------------------------------------------
CSV_FIELDS = ["uploaded_uk", "youtube_url", "title", "video_id", "job_id", "source_vod",
              "tiktok_url", "instagram_url"]
# The same Short goes on all three platforms. YouTube is uploaded by the API; TikTok and
# Instagram are posted by hand from #ready-to-post and logged with `lacy posted`.
CROSSPOST = {"tiktok": ("TikTok", "tiktok.com"), "instagram": ("Instagram", "instagram.com")}


def crosspost_url_ok(platform: str, url: str) -> bool:
    from urllib.parse import urlparse
    u = urlparse((url or "").strip())
    host = (u.hostname or "").lower()
    dom = CROSSPOST[platform][1]
    return u.scheme == "https" and (host == dom or host.endswith("." + dom)) and len(u.path) > 1


def missing_platforms(job: dict) -> list[str]:
    """Platforms this uploaded Short still needs (names for humans)."""
    if job.get("status") != "uploaded":
        return []
    return [name for p, (name, _) in CROSSPOST.items() if not job.get(f"{p}_url")]


class Submissions:
    def __init__(self, path: Path):
        self.path = path

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def uploads_on(self, day: str) -> int:
        """Uploads on a UK date (YYYY-MM-DD)."""
        return sum(1 for r in self.rows() if (r.get("uploaded_uk") or "").startswith(day))

    def titles(self) -> set[str]:
        return {r["title"] for r in self.rows() if r.get("title")}

    def _write_all(self, rows: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        tmp.replace(self.path)

    def _upgrade(self) -> None:
        """A CSV written before the TikTok/Instagram columns gets them (blank)."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != CSV_FIELDS:
            self._write_all(self.rows())

    def set_links(self, job_id: str, links: dict[str, str]) -> bool:
        """Fill tiktok_url / instagram_url on that job's row. False if no row."""
        self._upgrade()
        rows = self.rows()
        hit = False
        for r in rows:
            if r.get("job_id") == job_id:
                r.update({f"{p}_url": u for p, u in links.items()})
                hit = True
        if hit:
            self._write_all(rows)
        return hit

    def append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._upgrade()
        new = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)


def uk_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UK).strftime("%Y-%m-%d")


def uk_stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, UK).strftime("%Y-%m-%d %H:%M:%S %Z")


# --------------------------------------------------------------------------
# Jobs: one folder per clip under data/lacy/jobs/<id>/
# --------------------------------------------------------------------------
class Jobs:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "lacy" / "jobs"
        self.thumbs = data_dir / "lacy" / "thumbnails.json"

    def dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[\w-]+", job_id):
            raise ValueError(f"bad job id {job_id!r}")
        return self.root / job_id

    def load(self, job_id: str) -> dict:
        return json.loads((self.dir(job_id) / "job.json").read_text(encoding="utf-8"))

    def save(self, job: dict) -> None:
        d = self.dir(job["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(d / "job.json")

    def all(self) -> list[dict]:
        out = []
        for f in sorted(self.root.glob("*/job.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return out

    def renders_on(self, day: str, key: tuple[str, str] | None = None) -> int:
        """Clips rendered on a UK day (for one streamer if `key` is given)."""
        return sum(1 for j in self.all() if j.get("rendered_day") == day and (key is None or job_key(j) == key))

    def used_hashes(self) -> list[int]:
        try:
            return [int(h, 16) for h in json.loads(self.thumbs.read_text(encoding="utf-8"))]
        except (FileNotFoundError, ValueError):
            return []

    def add_hash(self, h: int) -> None:
        hashes = [f"{x:016x}" for x in self.used_hashes()] + [f"{h:016x}"]
        self.thumbs.parent.mkdir(parents=True, exist_ok=True)
        self.thumbs.write_text(json.dumps(hashes), encoding="utf-8")


def clip_window(rec: dict, max_s: float) -> tuple[float, float]:
    """The spike's suggested window, trimmed to max_s around the chat peak."""
    start, end = rec["clip_start"], rec["clip_end"]
    if end - start <= max_s:
        return start, end
    peak = rec.get("peak_time") or rec.get("onset") or start
    start = max(start, min(peak - max_s * 0.55, end - max_s))
    return start, start + max_s


def sample_window(rec: dict, lcfg: dict) -> tuple[float, float]:
    """The part of the VOD to cut: the spike's window widened by sample_before_s/after_s,
    at most sample_s long around the chat peak. The editor picks the Short from inside it."""
    widened = dict(rec, clip_start=rec["clip_start"] - float(lcfg.get("sample_before_s", 0)),
                   clip_end=rec["clip_end"] + float(lcfg.get("sample_after_s", 0)))
    return clip_window(widened, max(float(lcfg.get("sample_s") or 0), float(lcfg["max_clip_s"])))


def new_job(rec: dict, state: dict, lcfg: dict) -> dict:
    start, end = sample_window(rec, lcfg)
    job_id = datetime.fromtimestamp(rec["onset"], UK).strftime("%Y%m%d-%H%M%S")
    platform = lcfg.get("platform", "twitch")
    if lcfg.get("name", "Lacy") != "Lacy":   # Lacy keeps the old ids
        job_id = f"{lcfg['channel']}-{job_id}"
    return {
        "id": job_id, "status": "queued", "reason": None, "created": time.time(),
        "channel": lcfg["channel"], "platform": platform, "streamer": lcfg.get("name", "Lacy"),
        "programme": rec.get("programme") or lcfg.get("programme"),
        "score": rec["score"], "onset": rec["onset"], "onset_uk": rec.get("onset_uk"),
        "peak_time": rec.get("peak_time"), "vibe": rec.get("vibe") or [],
        "sample": rec.get("sample") or [], "category": state.get("category") or "",
        "stream_title": state.get("title"), "user_id": state.get("user_id"),
        "stream_id": state.get("stream_id"), "stream_started_at": state.get("started_at"),
        "clip_start": start, "clip_end": end,
        "vod_url": None, "vod_started_at": None,
        "ready_at": end + lcfg["vod_delay_s"],
    }


# --------------------------------------------------------------------------
# Render (runs in a subprocess: python -m spikebot.lacy render JOB)
# --------------------------------------------------------------------------
def render_job(job: dict, lcfg: dict, jobs: Jobs, used_titles: set[str],
               fetch=editor.fetch_segment, transcribe=editor.transcribe,
               now: float | None = None, review_store=None, cfg: dict | None = None,
               base: Path | None = None) -> dict:
    """Make the clip. With `review_store` (normal): 3 variants go to the review queue
    and the job becomes `review`. Without: one Short (`ready`, old flow).
    Sets skipped / failed with a reason when no clip can be made."""
    now = now or time.time()
    d = jobs.dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)

    from . import progress
    pj = None
    if review_store is not None:
        pj = progress.Job.create(review_store.data_dir, f"{who(job)[0]} clip {job['id']} ({job.get('category') or '?'})",
                                 channel="review", icon="🎮", meta={"lacy_job": job["id"]})

    def finish(status: str, reason: str | None = None) -> dict:
        if pj:
            if status == "waiting_vod":   # not a failure: the VOD just doesn't reach the moment yet
                pj.finish(False, f"⏳ {reason}: trying again in {VOD_RETRY_S // 60} min", state="skipped")
            else:
                pj.finish(False, reason or status, state="skipped" if status == "skipped" else None)
        job["status"], job["reason"] = status, reason
        (d / "segment.mp4").unlink(missing_ok=True)  # big; kept only while a clip is in review
        jobs.save(job)
        log.info("lacy job %s: %s%s", job["id"], status, f" ({reason})" if reason else "")
        return job

    # Hard guards, whatever the job file says.
    platform = lcfg.get("platform", "twitch")
    if (job_key(job) != (platform, lcfg["channel"]) or not job.get("programme")
            or not lcfg.get("programme", True)):
        return finish("skipped", f"not a clipping-programme channel ({job_key(job)[0]}/{job_key(job)[1]})")
    if not is_gta(job.get("category"), lcfg["allowed_categories"]):
        return finish("skipped", f"category {job.get('category')!r} is not GTA V/VI")
    if not job.get("vod_url") or not job.get("vod_started_at"):
        return finish("failed", f"no {PLATFORM_NAME.get(platform, platform)} VOD for this stream"
                                " (VODs off or not published)")
    from . import campaigns
    over = campaigns.ended(cfg or {}, job_key(job)[1], base) if cfg is not None else None
    if over:
        return finish("cancelled", f"campaign over: {over}")
    day = uk_day(now)
    if jobs.renders_on(day, job_key(job)) >= lcfg["max_renders_per_day"]:
        return finish("skipped", f"render limit {lcfg['max_renders_per_day']}/day reached")

    from .main import parse_time
    from .messages import vod_offset
    vod_start = parse_time(job["vod_started_at"])
    if vod_start and job["clip_start"] < vod_start:   # the sample can't start before the VOD does
        job["clip_start"] = vod_start
    job["vod_link"] = job["vod_url"] + vod_offset(job["clip_start"] - vod_start, platform)
    duration = job["clip_end"] - job["clip_start"]
    seg = d / "segment.mp4"
    with progress.tracking(pj) if pj else contextlib.nullcontext():
        try:
            return _render_job_body(job, lcfg, jobs, used_titles, fetch, transcribe, now, review_store, cfg, base,
                                    d, day, duration, vod_start, seg, finish, pj)
        except progress.Cancelled:
            return finish("cancelled", "cancelled in Discord")
        except Exception as e:
            if pj and pj.data.get("state") == "running":
                pj.finish(False, f"{type(e).__name__}: {e}")
            raise


def _render_job_body(job, lcfg, jobs, used_titles, fetch, transcribe, now, review_store, cfg, base,
                     d, day, duration, vod_start, seg, finish, pj):
    from . import progress
    track = progress.current()
    try:
        track.update(0.02, "Downloading the VOD segment")
        fetch(job["vod_url"], job["clip_start"] - vod_start, duration, seg)
        track.update(0.2, "Checking the facecam (BRB / away)")
        info = editor.probe(seg)
        duration = info["duration"] or duration
        facecam = lcfg["facecam"]
        if lcfg.get("layout") == "full":   # the campaign needs the whole frame (e.g. a stream overlay)
            facecam = None
        elif lcfg.get("facecam_auto"):
            track.update(0.15, "Finding the facecam")
            try:
                from . import safety
                found = safety.find_facecam(seg)
            except Exception as e:  # noqa: BLE001 - fall back to the default box
                log.info("clip job %s: facecam search failed: %s", job["id"], e)
                found = None
            if found:
                facecam = found
                job["facecam"] = found
                log.info("clip job %s: facecam found at %s", job["id"], found)
            else:   # no webcam on screen: no facecam panel at all, the clip is 4:5 (Tim, 20 Nov)
                facecam = None
        if facecam is not None and facecam_required(review_store):   # the top panel must be a real face
            track.update(0.18, "Checking the facecam shows a face")
            from . import safety
            if safety.real_facecam(seg, facecam) is False:
                log.info("clip job %s: the facecam box shows no face: whole frame in 4:5 instead", job["id"])
                facecam = None
        job["no_facecam"] = facecam is None
        lay = editor.layout(info["width"], info["height"], facecam, src_fps=info.get("fps"))
        away = editor.away_reason(seg, lay, duration)
        if away:
            return finish("skipped", f"BRB/away: {away}")
        track.update(0.25, "Transcribing speech (Whisper)")
        words, lang, prob = transcribe(seg, lcfg["whisper_model"])
        if lang != "en" or prob < lcfg["min_english_prob"]:
            return finish("skipped", f"not English (detected {lang} {prob:.0%})")
        if len(words) < lcfg["min_words"]:
            return finish("skipped", "almost no speech, nothing to caption")
        # Is it a real moment, or did chat only spike because the streamer asked it something?
        # Checked before any rendering so no time goes into editing a non-moment.
        track.update(0.32, "Checking it's a real moment (not just chat answering a question)")
        from . import impact
        try:
            levels = editor.loudness(editor.decode_audio(seg))
        except editor.EditError:
            levels = []
        verdict = impact.judge(job, words, levels, cfg, base)
        job["impact"] = {k: verdict.get(k) for k in ("score", "kind", "why", "asked", "reaction", "loud", "by")
                         if verdict.get(k) not in (None, "")}
        if verdict["skip"]:
            return finish("skipped", f"low impact: {verdict['why']}")
        if review_store is None:  # old single-render path (CLI `lacy approve` / tests)
            return _render_single(job, lcfg, jobs, used_titles, seg, lay, words, lang, duration, day, finish)
        (d / "words.json").write_text(json.dumps([[w.start, w.end, w.text] for w in words]), encoding="utf-8")
        job.update(duration=round(duration, 1), language=lang, rendered_day=day, words=len(words),
                   src_w=info["width"], src_h=info["height"], src_fps=info.get("fps"), audio=info.get("audio", True))
        jobs.save(job)
        from . import review
        name, platform = who(job)
        item = review_store.create("lacy", {"job_id": job["id"], "vod_link": job.get("vod_link"),
                                            "channel": job["channel"], "platform": platform, "streamer": name,
                                            "category": job["category"], "score": job.get("score"),
                                            # where this campaign pays: the others are never planned
                                            "platforms": lcfg.get("platforms")},
                                   now, title=f"{name} {job['id']} ({job['category']})")
        job["review_item"] = item["id"]
        jobs.save(job)
        with track.span(0.4, 1.0):
            item = review.render_round(review_store, item["id"], cfg or {}, base or Path("."), {"kind": "initial"},
                                       now=now, adapter=ReviewAdapter(cfg or {}, base or Path("."), lcfg, jobs,
                                                                      used_titles))
        if item["status"] != "open":
            return finish("failed", item.get("error") or "variants could not be rendered")
        job["status"], job["reason"] = "review", None
        jobs.save(job)
        if pj:
            pj.finish(True)
        log.info("lacy job %s: 3 variants waiting for approval (%s)", job["id"], item["id"])
        return job
    except editor.VodNotReady as e:
        return finish("waiting_vod", str(e))
    except editor.EditError as e:
        return finish("failed", str(e))


def phrase_lines(words: list, gap: float = 0.6, max_words: int = 14) -> list[list[int]]:
    """Word indices grouped into spoken lines (a pause of `gap` s or `max_words` starts a new one)."""
    out: list[list[int]] = []
    for i, w in enumerate(words):
        if out and w.start - words[out[-1][-1]].end < gap and len(out[-1]) < max_words:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def clip_facecam(job: dict, lcfg: dict) -> dict | None:
    """The facecam box for a job's layout, or None = no facecam (4:5, the whole frame)."""
    if job.get("no_facecam") or lcfg.get("layout") == "full":
        return None
    return job.get("facecam") or lcfg["facecam"]


def facecam_required(review_store) -> bool:
    """The require_facecam rule for stream clips (default on)."""
    if review_store is None:
        return True
    try:
        v = review_store.profile().custom_pins("lacy").get("require_facecam")
    except Exception as e:  # noqa: BLE001 - an unreadable profile keeps the default
        log.warning("style profile unreadable (%s): require_facecam stays on", e)
        return True
    return True if v is None else bool(float(v))


def _render_single(job, lcfg, jobs, used_titles, seg, lay, words, lang, duration, day, finish):
    d = jobs.dir(job["id"])
    name, platform = who(job)
    title = make_title(job["vibe"], job["category"], used_titles, name=name)
    job["title"] = title
    job["description"] = description(title, job["category"], job["channel"], name, platform)
    job["tags"] = tags(job["category"], name, platform)
    ass = d / "captions.ass"
    # Lacy mode: never a watermark, channel logo or "6" overlay.
    ass.write_text(editor.build_ass(words, title, duration, lay.cam_h, watermark=None, frame_h=lay.out_h,
                                    picture=lay.picture), encoding="utf-8")
    short = editor.render(seg, d / "short.mp4", lay, ass)
    editor.review_copy(short, d / "preview.mp4")
    peak = (job["peak_time"] - job["clip_start"]) if job.get("peak_time") else None
    h = editor.pick_thumbnail(short, d / "thumb.jpg", duration, peak, jobs.used_hashes(),
                              lcfg["thumb_min_distance"])
    jobs.add_hash(h)
    job.update(thumb_hash=f"{h:016x}", duration=round(duration, 1), language=lang,
               rendered_day=day, words=len(words))
    return finish("ready")


class ReviewAdapter:
    """Renders review variants of a stream clip (Lacy, Adin Ross, Seal...) from its kept
    segment + transcript. Every variant obeys the clip rules: facecam on top, word captions,
    no watermark/logo, English title containing the streamer's name with no banned words."""

    def __init__(self, cfg: dict, base: Path, lcfg: dict | None = None, jobs: Jobs | None = None,
                 used_titles: set[str] | None = None):
        self.cfg, self.base = cfg, base
        self._given = lcfg
        self.lcfg = lcfg or settings(cfg)
        data_dir = Path(cfg.get("data_dir", "data"))
        self.jobs = jobs or Jobs(data_dir if data_dir.is_absolute() else base / data_dir)
        self.used_titles = used_titles

    def _profile(self, job: dict) -> dict:
        if self._given is not None:
            return self._given
        try:
            return profile_for(self.cfg, job)
        except ValueError:
            return self.lcfg

    def pronouns(self, job: dict) -> str:
        from . import campaigns
        return self.lcfg.get("pronouns") or campaigns.pronouns_for(job.get("channel"), self.cfg)

    def clip_copy(self, job: dict, d: Path, words: list) -> list[dict]:
        """Titles + captions written by Claude from the transcript (spikebot/ai.py), made once
        per clip and kept in copy.json. [] without a key or on any failure: templates then."""
        f = d / "copy.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        from . import ai
        if not ai.available(self.base):
            return []
        name, _ = who(job)
        text = " ".join(w.text.strip() for w in words)
        try:
            out = ai.write_clip_copy(name, game_tag(job.get("category") or ""), text, job.get("vibe"),
                                     n=3, base=self.base, pronouns=self.pronouns(job))
        except ai.AIError as e:
            log.info("clip %s: AI captions unavailable (%s), using templates", job["id"], e)
            return []
        f.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out

    def streamer_words(self, job: dict, d: Path, words: list) -> list:
        """streamer_only: the transcript without the lines that are game audio (cutscenes, NPCs, radio),
        judged once per clip by Claude and kept in speakers.json. Without a key every word stays."""
        lines = phrase_lines(words)
        f = d / "speakers.json"
        game: list[int] | None = None
        try:
            saved = json.loads(f.read_text(encoding="utf-8"))
            if saved.get("n") == len(words):
                game = saved["game"]
        except (OSError, ValueError, KeyError):
            pass
        if game is None:
            from . import ai
            if not ai.available(self.base) or not lines:
                return words
            name, _ = who(job)
            rows = [(words[ix[0]].start, words[ix[-1]].end, " ".join(words[i].text.strip() for i in ix))
                    for ix in lines]
            try:
                game = ai.game_lines(name, game_tag(job.get("category") or ""), rows, base=self.base)
            except ai.AIError as e:
                log.info("clip %s: speaker check unavailable (%s): captions keep every line", job["id"], e)
                return words
            f.write_text(json.dumps({"n": len(words), "game": game}), encoding="utf-8")
            if game:
                log.info("clip %s: %d line(s) are game audio, left out of the captions", job["id"], len(game))
        drop = {i for g in game if 0 <= g < len(lines) for i in lines[g]}
        return [w for i, w in enumerate(words) if i not in drop]

    def voice_line(self, job: dict, d: Path, words: list, title: str) -> str:
        """The spoken teaser hook (Claude, from what was said), made once per clip and kept in
        voice.json. Falls back to the clip's title, which always passes the title rules."""
        name, _ = who(job)
        f = d / "voice.json"
        try:
            line = json.loads(f.read_text(encoding="utf-8")).get("line") or ""
            if voice_line_ok(line, name) and pronouns_ok(line, self.pronouns(job)):
                return line
        except (OSError, ValueError):
            pass
        from . import ai
        if ai.available(self.base):
            try:
                line = ai.write_voice_hook(name, game_tag(job.get("category") or ""),
                                           " ".join(w.text.strip() for w in words), job.get("vibe"), base=self.base,
                                           pronouns=self.pronouns(job))
                if voice_line_ok(line, name) and pronouns_ok(line, self.pronouns(job)):
                    f.write_text(json.dumps({"line": line}, ensure_ascii=False), encoding="utf-8")
                    return line
                log.info("clip %s: AI voiceover line %r breaks the rules, using the title", job["id"], line)
            except ai.AIError as e:
                log.info("clip %s: AI voiceover line unavailable (%s), using the title", job["id"], e)
        return title

    def teaser(self, seg: Path, peak: float | None, duration: float, line: str, folder: Path,
               stem: str) -> dict:
        """The teaser opening: {line, voice (mp3 path or None), s (length), window (source start,
        end)}. Without a voice the line still shows on screen."""
        from . import voice as voice_
        vo = voice_.speak(line, folder / f"{stem}_vo.mp3", self.cfg)
        vo_s = editor.probe(vo).get("duration") if vo else None
        length = editor.teaser_length(vo_s)
        try:
            levels = editor.loudness(editor.decode_audio(seg))
        except editor.EditError:
            levels = []
        return {"line": line, "voice": vo, "s": length,
                "window": editor.teaser_window(levels, peak, duration, length)}

    def _used(self) -> set[str]:
        if self.used_titles is not None:
            return set(self.used_titles)
        return Submissions(Path(self.lcfg["submissions_csv"])).titles()

    def render(self, item: dict, specs: list[dict], n: int, folder: Path, preview_bytes: int) -> list[dict]:
        from . import instagram, style as st
        job = self.jobs.load(item["source"]["job_id"])
        self.lcfg = self._profile(job)
        name, platform = who(job)
        d = self.jobs.dir(job["id"])
        seg = d / "segment.mp4"
        if not seg.exists():
            raise editor.EditError("the source segment is gone (clip closed or disk cleaned)")
        words = [editor.Word(a, b, t) for a, b, t in json.loads((d / "words.json").read_text(encoding="utf-8"))]
        w, h = int(job["src_w"]), int(job["src_h"])
        fps = job.get("src_fps") or editor.probe(seg).get("fps")   # jobs from before #39 lack it
        lay = editor.layout(w, h, clip_facecam(job, self.lcfg), src_fps=fps)
        duration = float(job["duration"])
        peak = (job["peak_time"] - job["clip_start"]) if job.get("peak_time") else None
        used = self._used() | {v["title"] for r in item["rounds"] for v in r["variants"]}
        # streamer_only (default on): captions, titles and hooks come from the streamer's voice only
        voice = self.streamer_words(job, d, words) if any(
            st.resolve(sp["style"]).get("streamer_only", True) for sp in specs) else words
        written = self.clip_copy(job, d, voice) if not any(sp.get("from") for sp in specs) else []
        pro = self.pronouns(job)
        # wrong pronouns anywhere in an option (Lacy is he/him): that option is dropped, templates instead
        written = [o for o in written if all(pronouns_ok(str(o.get(k) or ""), pro) for k in ("title", "caption", "hook"))]
        hooks_used = instagram.PostLog(instagram.settings(self.cfg, self.base)["posts_csv"]).recent_hooks()
        from . import campaigns
        camp = self.lcfg.get("campaign") or campaigns.for_channel(self.cfg, job.get("channel") or "lacy")
        out = []
        from . import progress
        track = progress.current()
        for vi, spec in enumerate(specs):
          with track.span(vi / len(specs), (vi + 1) / len(specs)):
            track.update(0.0, f"Version {spec['label']} ({vi + 1}/{len(specs)}): rendering")
            if spec["style"].get("opening") == "teaser" and not self.lcfg.get("ai_allowed"):
                # the AI voiceover is AI content: this campaign bans it, so a normal opening
                spec = dict(spec, style=dict(spec["style"], opening="normal"))
            r = st.resolve(spec["style"])
            base_v = spec.get("from")
            texts = spec.get("texts") or {}
            if texts.get("hook"):
                title = texts["hook"].strip()
                if not pronouns_ok(title, pro):
                    raise ValueError(f"new hook {title!r} uses the wrong pronouns: {name} is {pro}")
                if not title_ok(title, name):
                    raise ValueError(f"new hook {title!r} breaks the clip title rules (must contain"
                                     f" '{name}', 10-100 English characters, no negative words)")
            elif base_v:
                title = base_v["title"]
            elif (vi < len(written) and title_ok(written[vi]["title"].strip(), name)
                  and written[vi]["title"].strip().lower() not in {u.lower() for u in used}):
                title = written[vi]["title"].strip()   # Claude, from what was said
            else:
                tone = spec["style"].get("hook")
                title = make_title(job["vibe"], job["category"], used, random.Random(f"{item['id']}{n}{spec['label']}"),
                                   tone=None if tone == "matched" else tone, hook_style=r.get("hook_style"),
                                   name=name)
            used.add(title)
            # the on-screen hook: Claude's short setup line when it passes, else the title
            screen = title
            if texts.get("hook"):
                screen = title
            elif base_v and base_v.get("screen_hook"):
                screen = base_v["screen_hook"]
            elif vi < len(written) and screen_hook_ok(written[vi].get("hook", ""), name):
                screen = written[vi]["hook"].strip()
            ai_cap = written[vi]["caption"].strip() if vi < len(written) else ""
            if base_v and not texts.get("hook"):
                caps = dict(base_v["captions"])
                social = base_v.get("hook")
            elif ai_cap and title == written[vi]["title"].strip() and instagram.hook_ok(ai_cap, title, name):
                social = ai_cap
                caps = instagram.captions(job | {"title": title}, social)
            else:
                tone = spec["style"].get("hook")
                social = instagram.pick_hook(job | {"title": title}, hooks_used | used,
                                             random.Random(f"{item['id']}{n}{spec['label']}"),
                                             tone=None if tone == "matched" else tone)
                hooks_used.add(social)
                caps = instagram.captions(job | {"title": title}, social)
            stem = f"r{n}_{spec['label']}"
            # experimental opening: the big moment first under an AI voiceover hook, then the clip
            tz = None
            # AI content: only where the campaign allows it (Reach and Clipping Nation ban it)
            if r.get("opening") == "teaser" and job.get("audio", True) and self.lcfg.get("ai_allowed"):
                track.update(0.05, f"Version {spec['label']} ({vi + 1}/{len(specs)}): voiceover")
                line = (base_v.get("teaser") or {}).get("line") if base_v and not texts.get("hook") else None
                tz = self.teaser(seg, peak, duration, line or self.voice_line(job, d, voice, title),
                                 folder, stem)
            lead = float(r.get("lead_in_s") or 0)    # lead_in_s: context before the clip's start
            window = min(float(r["pace"]["window_s"]),
                         float(self.lcfg["max_clip_s"]) - (tz["s"] if tz else 0) - lead)
            segs = editor.keep_segments(words, duration, window, peak, r["pace"]["max_gap_s"], r["payoff"],
                                        lead_in=lead)
            cut_words = editor.remap_words(voice if r.get("streamer_only", True) else words, segs)
            length = editor.cut_length(segs)
            lead = tz["s"] if tz else 0.0
            peak_cut = None
            if peak is not None:
                off = 0.0
                for a, b in segs:
                    if a <= peak < b:
                        peak_cut = off + peak - a
                    off += b - a
            if r["text_level"] == "none":   # stream clips always carry word captions (Tim's rule)
                r["text_level"] = "full"
            ass = None
            if r["text_level"] != "none" or r["ending"] != "none":
                ass = folder / f"{stem}.ass"
                # Lacy mode: never a watermark, channel logo or "6" overlay.
                ass.write_text(editor.build_ass(cut_words, screen, length, lay.cam_h, watermark=None, style=r,
                                                focus=editor.caption_focus(peak_cut), lead=lead,
                                                lead_text=tz["line"] if tz else "", frame_h=lay.out_h,
                                                picture=lay.picture),
                               encoding="utf-8")
            mp4 = folder / f"{stem}.mp4"
            look = st.grade_filter(r["grade"])
            fx = st.look_filter(r["look"], "lacy")
            # no sound effects on any edit or clip (Tim, 20 Nov): the clip's own sound only
            try:
                editor.render_variant(seg, mp4, lay, ass, w, h, segs, ",".join(x for x in (look, fx) if x),
                                      r["transition"], audio=bool(job.get("audio", True)), sfx_cues=None,
                                      teaser=tz["window"] if tz else None, voice=tz["voice"] if tz else None)
            finally:
                if tz and tz["voice"]:
                    Path(tz["voice"]).unlink(missing_ok=True)
            if ass is not None:
                ass.unlink(missing_ok=True)
            if tz:
                length += lead
                peak_cut = None if peak_cut is None else peak_cut + lead
            track.update(0.85, f"Version {spec['label']} ({vi + 1}/{len(specs)}): preview")
            prev = editor.preview(mp4, folder / f"{stem}_preview.mp4", preview_bytes, length)
            desc = description(title, job["category"], job["channel"], name, platform)
            caps, desc = campaigns.finish_copy(caps, desc, camp)   # required hashtags + "watch live" line
            out.append({"label": spec["label"], "style": spec["style"], "preset": spec.get("preset"),
                        "experimental": spec.get("experimental", False), "title": title,
                        "description": desc, "frame": "4:5" if lay.cam is None else "9:16", "screen_hook": screen,
                        "tags": tags(job["category"], name, platform), "hook": social, "captions": caps,
                        "file": mp4.name, "preview": prev.name, "duration": round(length, 1),
                        "cuts": len(segs) - 1, "peak": peak_cut,
                        "teaser": {"line": tz["line"], "voice": bool(tz["voice"]), "s": tz["s"]} if tz else None})
        return out

    def validate(self, item: dict, v: dict) -> None:
        job = self.jobs.load(item["source"]["job_id"])
        name, platform = who(job)
        if not title_ok(v["title"], name):
            raise ValueError(f"clip titles must contain '{name}', be 10-100 English characters"
                             " and avoid negative words")
        from . import campaigns as _c
        pro = self.lcfg.get("pronouns") or _c.pronouns_for(job.get("channel"), self.cfg)
        for k, text in [("title", v["title"]), *v["captions"].items()]:
            if not pronouns_ok(text, pro):
                raise ValueError(f"the {k} uses the wrong pronouns: {name} is {pro}")
        for k, text in v["captions"].items():
            if BANNED.search(text):
                raise ValueError(f"the {k} caption has a word the clip rules ban: {BANNED.search(text).group(0)!r}")
            if name.lower() not in text.lower():
                raise ValueError(f"the {k} caption must mention {name}")
        from . import campaigns
        camp = campaigns.for_channel(self.cfg, job.get("channel") or "lacy")
        bad = campaigns.copy_problems(v["captions"], camp)
        if bad:
            raise ValueError("; ".join(bad))
        v["description"] = campaigns.finish_caption(description(v["title"], job["category"], job["channel"],
                                                                name, platform), camp, limit=4900)

    def finish(self, item: dict, status: str) -> None:
        job_id = item["source"].get("job_id")
        if not job_id:
            return
        d = self.jobs.dir(job_id)
        for f in ("segment.mp4", "short.mp4", "preview.mp4"):
            (d / f).unlink(missing_ok=True)
        try:
            job = self.jobs.load(job_id)
        except FileNotFoundError:
            return
        if job.get("status") == "review":
            job["status"] = {"done": "uploaded", "rejected": "rejected", "expired": "expired"}.get(status, status)
            job["reason"] = None if status == "done" else f"{status} in review"
            self.jobs.save(job)


def upload_job(job: dict, lcfg: dict, jobs: Jobs, subs: Submissions, yt,
               now: float | None = None, send=None, social=None) -> dict:
    """Upload a ready job (respecting the daily cap) and log it to the CSV.
    `social(path, job)` (if given, instagram.Crossposter) copies it to exports/tiktok/
    and auto-posts the Instagram Reel. `send(path, job)` (if given) then posts the
    full-quality Short to #ready-to-post; the file is only deleted once that worked."""
    now = now or time.time()
    if job.get("status") != "ready":
        raise ValueError(f"job {job['id']} is {job.get('status')}, not ready")
    name, _ = who(job)
    if not title_ok(job.get("title") or "", name):
        raise ValueError(f"title {job.get('title')!r} breaks the clip title rules")
    for t in (f"#{hashtag(name)}", "#Shorts"):
        if t not in job.get("description", ""):
            raise ValueError(f"description is missing {t}")
    day = uk_day(now)
    done = subs.uploads_on(day)
    if done >= lcfg["max_uploads_per_day"]:
        raise ValueError(f"already {done} {name} uploads today (max {lcfg['max_uploads_per_day']});"
                         " approve it again tomorrow")
    d = jobs.dir(job["id"])
    from .youtube import YouTubeError, shorts_url
    vid = yt.upload(d / "short.mp4", job["title"], job["description"], job["tags"], lcfg["privacy"])
    url = shorts_url(vid)
    job.update(status="uploaded", video_id=vid, youtube_url=url, uploaded=now)
    try:
        yt.set_thumbnail(vid, d / "thumb.jpg")
        job["thumbnail_set"] = True
    except YouTubeError as e:  # e.g. channel not phone-verified yet
        job["thumbnail_set"] = False
        job["reason"] = f"uploaded, but thumbnail not set: {e}"
    subs.append({"uploaded_uk": uk_stamp(now), "youtube_url": url, "title": job["title"],
                 "video_id": vid, "job_id": job["id"], "source_vod": job.get("vod_link") or ""})
    jobs.save(job)  # the upload is recorded even if the Discord step fails
    if social is not None:
        try:
            social(d / "short.mp4", job, now=now)
        except Exception as e:  # noqa: BLE001 - YouTube is done; Instagram retries later
            log.warning("lacy job %s: TikTok/Instagram step failed: %s", job["id"], e)
        if job.get("instagram_url"):
            subs.set_links(job["id"], {"instagram": job["instagram_url"]})
        jobs.save(job)
    if send is not None:
        deliver_job(job, jobs, send)
    else:
        free_disk(jobs, job)
    return job


def free_disk(jobs: Jobs, job: dict) -> None:
    for f in ("short.mp4", "preview.mp4"):  # YouTube (and #ready-to-post) have it now
        (jobs.dir(job["id"]) / f).unlink(missing_ok=True)


def deliver_job(job: dict, jobs: Jobs, send) -> str | None:
    """Post the finished Short to #ready-to-post. On failure the file is kept and
    `python -m spikebot.lacy deliver ID` retries."""
    path = jobs.dir(job["id"]) / "short.mp4"
    if not path.exists():
        raise ValueError(f"{path} is gone (already delivered or deleted)")
    try:
        how = send(path, job)
    except Exception as e:  # noqa: BLE001 - YouTube upload already done; never undo that
        job["delivered"] = None
        job["reason"] = (f"{job['reason']}; " if job.get("reason") else "") + \
            f"not sent to #ready-to-post ({e}); retry: python -m spikebot.lacy deliver {job['id']}"
        jobs.save(job)
        log.warning("lacy job %s: delivery failed: %s", job["id"], e)
        return None
    job["delivered"] = how
    free_disk(jobs, job)
    jobs.save(job)
    return how


def crosspost_caption(job: dict) -> str:
    tag = "#GTA6" if game_tag(job.get("category") or "") == "GTA 6" else "#GTA5"
    return f"{job['title']} #{hashtag(who(job)[0])} {tag} #GTA"


def video_blocks(job: dict) -> list[tuple[str, str]]:
    """What to paste: a caption per platform still to post by hand, then the command
    that logs the links. Instagram is skipped once it was auto-posted."""
    cmd = f"cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy posted {job['id']} "
    tiktok = job.get("tiktok_caption") or crosspost_caption(job)
    if job.get("instagram_url") or job.get("instagram_status") == "waiting":
        return [("TikTok caption", tiktok), ("Then log the TikTok link (for Content Rewards)",
                                             cmd + "--tiktok TIKTOK_URL")]
    insta = job.get("instagram_caption") or crosspost_caption(job)
    return [("TikTok caption", tiktok), ("Instagram Reels caption", insta),
            ("Then log both links (for Content Rewards)", cmd + "--tiktok TIKTOK_URL --instagram INSTAGRAM_URL")]


def send_video(dcfg: dict, path: Path, job: dict, **kw) -> str:
    """#ready-to-post message for an uploaded Lacy Short (original file, no re-encode)."""
    from . import delivery
    ig = job.get("instagram_status")
    if job.get("instagram_url"):
        todo = "now post on **TikTok**"
    elif ig == "waiting":
        todo = "now post on **TikTok** (Instagram auto-posts once the daily Reels cap allows)"
    elif ig == "failed":
        todo = ("now post on **TikTok** · Instagram auto-post failed, retrying hourly"
                " (post it by hand and log it to stop that)")
    else:
        todo = "now post on **TikTok + Instagram**"
    bits = ([f"{job['duration']:g}s"] if job.get("duration") else []) + \
           ([f"YouTube \u2705 <{job['youtube_url']}>"] if job.get("youtube_url") else []) + \
           ([f"Instagram \u2705 <{job['instagram_url']}>"] if job.get("instagram_url") else []) + \
           [todo]
    return delivery.send(dcfg, path, "lacy", bits, video_blocks(job), name=f"lacy_{job['id']}.mp4", **kw)


VOD_RETRY_S = 600          # look for a missing VOD again every 10 min...
VOD_WAIT_S = 14 * 3600     # ...for up to 14 h (Kick publishes VODs when the stream ends)
# Renders that crashed on the PyAV/faster-whisper break (fixed): run them again once.
# failures fixed since: those jobs are rendered once more after the deploy. "segment download came
# back empty" / TimeoutExpired = live VODs ffmpeg couldn't seek in (fixed 21 Nov: editor.cut_playlist)
FIXED_CRASHES = ("metadata_errors", "segment download came back empty", "TimeoutExpired")


def retry_after_fix(job: dict, now: float | None = None) -> bool:
    return (job.get("status") == "failed" and not job.get("retried")
            and any(m in (job.get("reason") or "") for m in FIXED_CRASHES)
            and (now or time.time()) - float(job.get("created") or 0) < 2 * 86400)


# --------------------------------------------------------------------------
# In-bot side: queue jobs, run renders, post review cards
# --------------------------------------------------------------------------
class LacyMode:
    """Stream-clip jobs for every programme streamer (Lacy + the `clips` channels)."""

    def __init__(self, cfg: dict, data_dir: Path, config_path: str | None = None):
        self.cfg = settings(cfg)
        self.profiles = profiles(cfg)
        self.jobs = Jobs(data_dir)
        self.data_dir = Path(data_dir)
        self.config_path = config_path
        self.queue: asyncio.Queue | None = None
        self.full_cfg = cfg
        # (platform, channel) -> live? Set by Bot (its last stream-state poll); None = unknown
        self.is_live = lambda key: None

    def profile(self, key: tuple[str, str]) -> dict | None:
        return self.profiles.get((key[0], key[1].lower()))

    def applies(self, key: tuple[str, str]) -> bool:
        return self.profile(key) is not None

    def category_ok(self, category: str | None, key: tuple[str, str] | None = None) -> bool:
        p = self.profile(key) if key else None
        return is_gta(category, (p or self.cfg)["allowed_categories"])

    def enqueue(self, rec: dict, state: dict, key: tuple[str, str] | None = None) -> dict | None:
        p = self.profile(key or (rec.get("platform") or "twitch", rec.get("channel") or self.cfg["channel"]))
        if p is None or not (rec.get("programme") or p.get("programme")):
            return None
        from . import campaigns
        over = campaigns.ended(self.full_cfg, p["channel"])
        if over:   # budget gone / campaign over (checked live, not just at start-up)
            log.info("no clip for %s: %s", p["name"], over)
            return None
        if rec.get("score", 0) < p["min_score"]:
            return None
        job = new_job(rec, state, p)
        self.jobs.save(job)
        if self.queue is not None:
            self.queue.put_nowait(job["id"])
        log.info("clip job %s (%s) queued (render after %s)", job["id"], p["name"], uk_stamp(job["ready_at"]))
        return job

    def _cli(self, *args: str) -> list[str]:
        cmd = [sys.executable, "-m", "spikebot.lacy"]
        if self.config_path:
            cmd += ["--config", self.config_path]
        return cmd + list(args)

    async def _run(self, *args: str, timeout: float = 3600) -> tuple[int, str]:
        p = await asyncio.create_subprocess_exec(*self._cli(*args), stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT,
                                                 start_new_session=True)   # /cancel can stop its ffmpeg too
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            return -1, "timed out"
        return p.returncode or 0, out.decode(errors="replace")[-1500:]

    async def worker(self, helix, post_status) -> None:
        """Render queued jobs one at a time. Never raises: Lacy problems must not
        take the chat bot down. Finished variants wait in the review queue."""
        self.queue = asyncio.Queue()
        for j in self.jobs.all():  # jobs left waiting by a restart
            if j.get("status") in ("queued", "standby", "rendering", "waiting_vod") or retry_after_fix(j):
                self.queue.put_nowait(j["id"])
        while True:
            job_id = await self.queue.get()
            try:
                await self._process(job_id, helix, post_status)
            except Exception as e:  # noqa: BLE001
                log.warning("lacy job %s error: %s", job_id, e)

    async def _later(self, job_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.queue is not None:
            self.queue.put_nowait(job_id)

    async def find_vod(self, job: dict, helix) -> dict | None:
        platform = job_key(job)[0]
        if platform == "kick":
            from .sources import kick_stream_vod
            return await kick_stream_vod(job["channel"], job.get("stream_id"))
        if helix:
            return await helix.stream_vod(job.get("user_id"), job.get("stream_id"))
        return None

    async def _process(self, job_id, helix, post_status) -> None:
        job = self.jobs.load(job_id)
        if retry_after_fix(job):
            job.update(status="queued", reason=None, retried=time.time())
            self.jobs.save(job)
        wait = job["ready_at"] - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
            job = self.jobs.load(job_id)
        if job.get("status") in ("cancelled", "skipped", "review", "uploaded"):
            return
        if not job.get("selected"):   # only the day's best-rated moments become clips (spikebot/picker.py)
            from . import campaigns, picker
            over = campaigns.ended(self.full_cfg, job_key(job)[1])
            if over:
                job.update(status="cancelled", reason=f"campaign over: {over}")
                self.jobs.save(job)
                return
            prof = self.profile(job_key(job)) or self.cfg
            verdict, why = picker.decide(job, self.jobs.all(), int(prof.get("max_renders_per_day", 10)),
                                         time.time(), self.is_live(job_key(job)))
            if verdict == "wait":
                if job.get("status") != "standby" or job.get("reason") != why:
                    job.update(status="standby", reason=why)
                    self.jobs.save(job)
                asyncio.create_task(self._later(job_id, picker.RECHECK_S))
                return
            if verdict == "drop":
                job.update(status="skipped", reason=why)
                self.jobs.save(job)
                log.info("clip job %s: %s", job_id, why)
                return
            job.update(selected=time.time(), selected_why=why, status="queued", reason=None)
            self.jobs.save(job)
            log.info("clip job %s selected: %s", job_id, why)
        if not job.get("vod_url"):
            try:
                vod = await self.find_vod(job, helix)
            except Exception as e:  # noqa: BLE001 - Kick/Twitch hiccup: try again later
                log.info("clip job %s: VOD lookup failed: %s", job_id, e)
                vod = None
            if vod:
                job["vod_url"], job["vod_started_at"] = vod.get("url"), vod.get("started_at")
            elif time.time() - job["ready_at"] < VOD_WAIT_S:
                # Kick often publishes the VOD only when the stream ends: check again later.
                job["status"] = "waiting_vod"
                self.jobs.save(job)
                asyncio.create_task(self._later(job_id, VOD_RETRY_S))
                return
        job["status"] = "rendering"
        self.jobs.save(job)
        code, out = await self._run("render", job_id)
        job = self.jobs.load(job_id)
        if job["status"] == "rendering":  # the subprocess died before finishing
            from . import progress
            if progress.cancelled_for(self.data_dir, "lacy_job", job_id):   # force-stopped by /cancel
                job.update(status="cancelled", reason="cancelled in Discord")
                (self.jobs.dir(job_id) / "segment.mp4").unlink(missing_ok=True)
            else:
                job.update(status="failed", reason=f"render crashed: {out[-300:]}")
            self.jobs.save(job)
        if job["status"] == "waiting_vod":   # the live VOD doesn't reach the moment yet: later
            if time.time() - float(job.get("ready_at") or 0) < VOD_WAIT_S:
                asyncio.create_task(self._later(job_id, VOD_RETRY_S))
                return
            job.update(status="failed", reason=f"the VOD never reached this moment ({job.get('reason')})")
            self.jobs.save(job)
        if job["status"] == "failed":
            await post_status(job)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _load(config: str) -> dict:
    from .main import apply_env
    cfg_path = Path(config)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    env_files = list(dict.fromkeys([cfg_path.resolve().parent / ".env", Path.cwd() / ".env"]))
    import os
    return apply_env(cfg, env_files, dict(os.environ))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Lacy mode clip queue")
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show clips and their status")
    for name, hlp in (("render", "make the Short for a queued job"),
                      ("approve", "upload a ready clip to YouTube"),
                      ("deliver", "send an uploaded clip to #ready-to-post again"),
                      ("posted", "log the TikTok/Instagram links of an uploaded clip"),
                      ("reject", "drop a clip")):
        sub.add_parser(name, help=hlp).add_argument("job")
    for name, (label, _) in CROSSPOST.items():
        sub.choices["posted"].add_argument(f"--{name}", metavar="URL", help=f"{label} post link")
    lay = sub.add_parser("layout", help="still image of the vertical layout, to check the facecam box")
    lay.add_argument("video", nargs="?", help="a clip file or a VOD link; leave out to grab the live stream")
    lay.add_argument("--at", type=float, default=5.0)
    lay.add_argument("--out", default="layout_check.jpg")
    lay.add_argument("--streamer", default=None, help="channel whose facecam box to use (default lacy)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = _load(args.config)
    lcfg = settings(cfg)
    data_dir = Path(cfg.get("data_dir", "data"))
    jobs = Jobs(data_dir)
    subs = Submissions(Path(lcfg["submissions_csv"]))

    if args.cmd == "list":
        today = uk_day(time.time())
        print(f"Lacy mode {'ON' if lcfg['enabled'] else 'OFF'} · uploads today "
              f"{subs.uploads_on(today)}/{lcfg['max_uploads_per_day']}")
        for (plat, chan), p in profiles(cfg).items():
            if p["name"] != "Lacy":
                print(f"{p['name']} ({plat}/{chan}) clips ON · uploads today "
                      f"{Submissions(Path(p['submissions_csv'])).uploads_on(today)}/{p['max_uploads_per_day']}")
        for j in jobs.all()[-30:]:
            todo = missing_platforms(j)
            print(f"  {j['id']}  {j['status']:<9} score {j.get('score', '-'):<5} "
                  f"{j.get('title') or ''}  {j.get('youtube_url') or j.get('reason') or ''}"
                  + (f"  NOT YET ON: {', '.join(todo)}" if todo else ""))
        return 0
    if args.cmd == "layout":
        if args.streamer:
            match = [p for (_, c), p in profiles(cfg, enabled_only=False).items() if c == args.streamer.lower()]
            if not match:
                print(f"{args.streamer} has no clipping programme in config")
                return 1
            lcfg = match[0]
        src_arg = args.video
        tmp_src = None
        if not src_arg or "://" in src_arg:
            # no file: a few seconds of the live stream (or of the VOD link given)
            url = src_arg or PLATFORM_URL.get(lcfg.get("platform", "twitch"), PLATFORM_URL["twitch"]).format(lcfg["channel"])
            tmp_src = Path(args.out).with_suffix(".src.mp4")
            print(f"Grabbing a few seconds of {url} ...")
            try:
                editor.fetch_segment(url, args.at if src_arg else 0.0, 4.0, tmp_src)
            except editor.EditError as e:
                print(f"Couldn't grab it ({e}). If they're offline, pass a VOD link instead.")
                return 1
            args.at = 1.0
        src = tmp_src or Path(src_arg)
        info = editor.probe(src)
        if lcfg.get("facecam_auto"):
            from . import safety
            found = safety.find_facecam(src, samples=4)
            print(f"Auto-found facecam: {found}" if found else "No steady face found: using the default box.")
            if found:
                lcfg = dict(lcfg, facecam=found)
        lay_ = editor.layout(info["width"], info["height"], lcfg["facecam"], src_fps=info.get("fps"))
        tmp = Path(args.out).with_suffix(".tmp.mp4")
        editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(args.at),
                    "-i", str(src), "-t", "1", "-filter_complex", lay_.filter() + "[v]",
                    "-map", "[v]", str(tmp)])
        editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(tmp),
                    "-frames:v", "1", args.out])
        tmp.unlink(missing_ok=True)
        # the whole frame with the facecam box drawn on, to read the right x/y/w/h off
        fc = lcfg["facecam"]
        frame_out = str(Path(args.out).with_name(Path(args.out).stem + "_frame.jpg"))
        editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(args.at), "-i", str(src),
                    "-frames:v", "1", "-vf",
                    f"drawbox=x=iw*{fc['x']}:y=ih*{fc['y']}:w=iw*{fc['w']}:h=ih*{fc['h']}:color=invert:t=8,"
                    "drawgrid=w=iw/10:h=ih/10:t=1:c=white@0.35", frame_out])
        if tmp_src:
            tmp_src.unlink(missing_ok=True)
        where = (f"clips.streamers.{lcfg['channel']}.facecam" if lcfg.get("name", "Lacy") != "Lacy"
                 else "lacy.facecam")
        print(f"Wrote {args.out} (the Short's layout) and {frame_out} (whole frame: outlined box = facecam,"
              f" grid = tenths). If the face isn't in the box, change {where} (x, y, w, h as fractions).")
        return 0

    job = jobs.load(args.job)
    try:
        lcfg = profile_for(cfg, job)
    except ValueError:
        pass   # old job of a channel no longer in config: Lacy's settings
    subs = Submissions(Path(lcfg["submissions_csv"]))
    if args.cmd == "render":
        from . import progress
        progress.arm_cancel()
        used = subs.titles() | {j.get("title") for j in jobs.all()
                                if j.get("title") and j["id"] != job["id"]}
        from . import review
        base = Path(args.config).resolve().parent
        job = render_job(job, lcfg, jobs, used, review_store=review.Store(data_dir), cfg=cfg, base=base)
        print(f"{job['id']}: {job['status']} {job.get('reason') or ''}")
        return 0 if job["status"] in ("ready", "review", "skipped", "cancelled") else 1
    if args.cmd == "reject":
        job.update(status="rejected", reason="rejected by Tim")
        jobs.save(job)
        for f in ("short.mp4", "preview.mp4", "segment.mp4"):
            (jobs.dir(job["id"]) / f).unlink(missing_ok=True)
        print(f"{job['id']}: rejected")
        return 0
    from . import delivery
    dcfg = delivery.settings(cfg, Path(args.config).resolve().parent)
    send = (lambda path, j: send_video(dcfg, path, j)) if dcfg["webhook_url"] else None
    if args.cmd == "deliver":
        if send is None:
            print("videos_webhook_url is not set in config.json")
            return 1
        if job.get("status") != "uploaded":
            print(f"{job['id']} is {job.get('status')}; only uploaded clips are sent")
            return 1
        how = deliver_job(job, jobs, send)
        print(f"{job['id']}: {how or 'FAILED: ' + job['reason']}")
        return 0 if how else 1
    if args.cmd == "posted":
        if job.get("status") != "uploaded":
            print(f"{job['id']} is {job.get('status')}; upload it to YouTube first (approve)")
            return 1
        links = {p: getattr(args, p) for p in CROSSPOST if getattr(args, p)}
        if not links:
            print("give --tiktok URL and/or --instagram URL")
            return 1
        bad = [p for p, u in links.items() if not crosspost_url_ok(p, u)]
        if bad:
            print("not a " + " / ".join(CROSSPOST[p][0] for p in bad) + " post link: "
                  + " ".join(links[p] for p in bad))
            return 1
        job.update({f"{p}_url": u.strip() for p, u in links.items()})
        jobs.save(job)
        if not subs.set_links(job["id"], {p: u.strip() for p, u in links.items()}):
            print(f"warning: {job['id']} has no row in {lcfg['submissions_csv']}")
        todo = missing_platforms(job)
        print(f"{job['id']}: logged " + ", ".join(CROSSPOST[p][0] for p in links)
              + (f". Still to post: {', '.join(todo)}" if todo else ". On all 3 platforms."))
        return 0
    if args.cmd == "approve":
        from .youtube import YouTube, YouTubeError
        try:
            yt = YouTube(cfg.get("youtube_client_id", ""), cfg.get("youtube_client_secret", ""),
                         cfg.get("youtube_refresh_token", ""))
            from . import instagram
            import os
            social = instagram.crossposter(cfg, Path(args.config).resolve().parent, dict(os.environ))
            job = upload_job(job, lcfg, jobs, subs, yt, send=send, social=social)
        except (ValueError, YouTubeError) as e:
            print(f"NOT uploaded: {e}")
            return 1
        print(f"Uploaded: {job['youtube_url']}  ({job['title']})")
        if job.get("reason"):
            print(job["reason"])
        print(f"Logged to {lcfg['submissions_csv']}")
        if job.get("tiktok_export"):
            print(f"TikTok: {job['tiktok_export']} (+ caption .txt)")
        print(f"Instagram: {job.get('instagram_url') or job.get('instagram_status') or 'off'}"
              + (f" ({job['instagram_error']})" if job.get("instagram_error") else ""))
        if send is not None:
            print(f"#ready-to-post: {job.get('delivered') or 'FAILED (file kept, see reason)'}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
