"""Lacy mode: turn Lacy's GTA chat spikes into ready-to-post Shorts for the
Lacy clipping campaign (Reach / Content Rewards).

Flow (all automatic up to review):
  1. Bot.handle() sees a spike on twitch/lacy, re-checks his category with Helix
     Get Streams right then, and only queues a job for Grand Theft Auto V / VI.
  2. LacyMode.worker() waits for the VOD to contain the moment, then runs
     `python -m spikebot.lacy render JOB` in a subprocess: cut the segment, skip
     BRB/away/camera-off, transcribe (English only), render 1080x1920 with his
     facecam on top for the whole clip, captions, no logo, unique thumbnail.
  3. A review card with a small preview video goes to #moments. Tim approves:
        python -m spikebot.lacy approve JOB
     which uploads to YouTube (max N a day), sets the thumbnail and appends the
     Shorts URL to logs/lacy_submissions.csv for Content Rewards.
     With lacy.auto_upload=true step 3 happens without approval (still capped).

CLI:  python -m spikebot.lacy list | render ID | approve ID | reject ID | layout VIDEO
"""
from __future__ import annotations

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
    "max_renders_per_day": 12,               # CPU guard: clips made, approved or not
    "auto_upload": False,                    # False = Tim approves every clip
    "min_score": None,                       # None = use min_score_to_alert
    "vod_delay_s": 300,                      # wait for the VOD to contain the moment
    "max_clip_s": 59,
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
    if cfg.get("lacy_auto_upload") not in (None, ""):
        out["auto_upload"] = str(cfg["lacy_auto_upload"]).strip().lower() in TRUE
    if out["min_score"] is None:
        out["min_score"] = float(cfg.get("min_score_to_alert", 0))
    out["channel"] = out["channel"].lower()
    return out


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
    "funny": ["Lacy can't stop laughing at this {game} moment",
              "Lacy's chat lost it over this",
              "Lacy did NOT expect this in {game}",
              "This {game} moment had Lacy in tears laughing",
              "Lacy and chat could not keep it together"],
    "shock": ["Lacy can't believe this happened in GTA",
              "Lacy's reaction to this {game} moment",
              "Wait for Lacy's reaction",
              "Lacy was not ready for this in {game}",
              "Nobody saw this coming, not even Lacy"],
    "hype": ["Lacy actually pulled this off in {game}",
             "Lacy's cleanest {game} moment yet",
             "Chat went wild for Lacy here",
             "Lacy made this look easy in {game}",
             "Lacy's {game} run just hit different"],
    "neutral": ["Lacy's best {game} moment today",
                "Lacy in {game} never disappoints",
                "Lacy and chat react to this {game} moment",
                "This is why we watch Lacy play {game}"],
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


def title_ok(title: str) -> bool:
    return ("lacy" in title.lower() and 10 <= len(title) <= 100
            and not BANNED.search(title) and bool(ENGLISH.match(title)))


def mood(vibe: list[str]) -> str:
    for word in vibe or []:
        for name, words in VIBES.items():
            if word.upper() in {w.upper() for w in words}:
                return name
    return "neutral"


def make_title(vibe: list[str], category: str, used: set[str], rng: random.Random | None = None) -> str:
    """A title for this clip's mood that hasn't been used on an earlier upload."""
    rng = rng or random.Random()
    game = game_tag(category)
    first = TITLES[mood(vibe)][:]
    rest = [t for k, ts in TITLES.items() if k != mood(vibe) for t in ts]
    rng.shuffle(first)
    rng.shuffle(rest)
    used_l = {u.lower() for u in used}
    for tpl in first + rest:
        t = tpl.format(game=game)
        if title_ok(t) and t.lower() not in used_l:
            return t
    base = first[0].format(game=game)
    for n in range(2, 1000):  # every template used: number them
        t = f"{base} (part {n})"
        if title_ok(t) and t.lower() not in used_l:
            return t
    raise ValueError("no title available")


def description(title: str, category: str, channel: str) -> str:
    game = game_tag(category)
    tag = "#GTA6" if game == "GTA 6" else "#GTA5"
    return (f"{title}\n\n"
            f"Lacy playing {category} live on Twitch: https://www.twitch.tv/{channel}\n"
            f"Clipped from Lacy's stream as part of the Lacy clipping programme.\n\n"
            f"#Lacy #Shorts {tag} #GTA")


def tags(category: str) -> list[str]:
    return ["Lacy", "Lacy GTA", "Shorts", game_tag(category), category, "Twitch clips"]


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

    def renders_on(self, day: str) -> int:
        return sum(1 for j in self.all() if j.get("rendered_day") == day)

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


def new_job(rec: dict, state: dict, lcfg: dict) -> dict:
    start, end = clip_window(rec, lcfg["max_clip_s"])
    job_id = datetime.fromtimestamp(rec["onset"], UK).strftime("%Y%m%d-%H%M%S")
    return {
        "id": job_id, "status": "queued", "reason": None, "created": time.time(),
        "channel": lcfg["channel"], "programme": rec.get("programme"),
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
               now: float | None = None) -> dict:
    """Make the Short. Sets job status to ready / skipped / failed with a reason."""
    now = now or time.time()
    d = jobs.dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)

    def finish(status: str, reason: str | None = None) -> dict:
        job["status"], job["reason"] = status, reason
        (d / "segment.mp4").unlink(missing_ok=True)  # big, never needed again
        jobs.save(job)
        log.info("lacy job %s: %s%s", job["id"], status, f" ({reason})" if reason else "")
        return job

    # Hard guards, whatever the job file says.
    if job.get("channel") != lcfg["channel"] or not job.get("programme"):
        return finish("skipped", "not the Lacy programme channel")
    if not is_gta(job.get("category"), lcfg["allowed_categories"]):
        return finish("skipped", f"category {job.get('category')!r} is not GTA V/VI")
    if not job.get("vod_url") or not job.get("vod_started_at"):
        return finish("failed", "no Twitch VOD for this stream (VODs off or not published)")
    day = uk_day(now)
    if jobs.renders_on(day) >= lcfg["max_renders_per_day"]:
        return finish("skipped", f"render limit {lcfg['max_renders_per_day']}/day reached")

    from .main import parse_time
    from .messages import vod_offset
    vod_start = parse_time(job["vod_started_at"])
    job["vod_link"] = job["vod_url"] + vod_offset(job["clip_start"] - vod_start, "twitch")
    duration = job["clip_end"] - job["clip_start"]
    seg = d / "segment.mp4"
    try:
        fetch(job["vod_url"], job["clip_start"] - vod_start, duration, seg)
        info = editor.probe(seg)
        duration = info["duration"] or duration
        lay = editor.layout(info["width"], info["height"], lcfg["facecam"])
        away = editor.away_reason(seg, lay, duration)
        if away:
            return finish("skipped", f"BRB/away: {away}")
        words, lang, prob = transcribe(seg, lcfg["whisper_model"])
        if lang != "en" or prob < lcfg["min_english_prob"]:
            return finish("skipped", f"not English (detected {lang} {prob:.0%})")
        if len(words) < lcfg["min_words"]:
            return finish("skipped", "almost no speech, nothing to caption")
        title = make_title(job["vibe"], job["category"], used_titles)
        job["title"] = title
        job["description"] = description(title, job["category"], job["channel"])
        job["tags"] = tags(job["category"])
        ass = d / "captions.ass"
        # Lacy mode: never a watermark, channel logo or "6" overlay.
        ass.write_text(editor.build_ass(words, title, duration, lay.cam_h, watermark=None),
                       encoding="utf-8")
        short = editor.render(seg, d / "short.mp4", lay, ass)
        editor.review_copy(short, d / "preview.mp4")
        peak = (job["peak_time"] - job["clip_start"]) if job.get("peak_time") else None
        h = editor.pick_thumbnail(short, d / "thumb.jpg", duration, peak, jobs.used_hashes(),
                                  lcfg["thumb_min_distance"])
        jobs.add_hash(h)
        job.update(thumb_hash=f"{h:016x}", duration=round(duration, 1), language=lang,
                   rendered_day=day, words=len(words))
        return finish("ready")
    except editor.EditError as e:
        return finish("failed", str(e))


def upload_job(job: dict, lcfg: dict, jobs: Jobs, subs: Submissions, yt,
               now: float | None = None, send=None) -> dict:
    """Upload a ready job (respecting the daily cap) and log it to the CSV.
    `send(path, job)` (if given) then posts the full-quality Short to #ready-to-post
    for TikTok/Reels; the file is only deleted once that worked."""
    now = now or time.time()
    if job.get("status") != "ready":
        raise ValueError(f"job {job['id']} is {job.get('status')}, not ready")
    if not title_ok(job.get("title") or ""):
        raise ValueError(f"title {job.get('title')!r} breaks the Lacy title rules")
    for t in ("#Lacy", "#Shorts"):
        if t not in job.get("description", ""):
            raise ValueError(f"description is missing {t}")
    day = uk_day(now)
    done = subs.uploads_on(day)
    if done >= lcfg["max_uploads_per_day"]:
        raise ValueError(f"already {done} Lacy uploads today (max {lcfg['max_uploads_per_day']});"
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
    return f"{job['title']} #Lacy {tag} #GTA"


def video_blocks(job: dict) -> list[tuple[str, str]]:
    """What to paste: a caption per platform, then the command that logs both links."""
    cap = crosspost_caption(job)
    return [("TikTok caption", cap), ("Instagram Reels caption", cap),
            ("Then log both links (for Content Rewards)",
             f"cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy posted {job['id']} "
             "--tiktok TIKTOK_URL --instagram INSTAGRAM_URL")]


def send_video(dcfg: dict, path: Path, job: dict, **kw) -> str:
    """#ready-to-post message for an uploaded Lacy Short (original file, no re-encode)."""
    from . import delivery
    bits = ([f"{job['duration']:g}s"] if job.get("duration") else []) + \
           ([f"YouTube \u2705 <{job['youtube_url']}>"] if job.get("youtube_url") else []) + \
           ["now post on **TikTok + Instagram**"]
    return delivery.send(dcfg, path, "lacy", bits, video_blocks(job), name=f"lacy_{job['id']}.mp4", **kw)


# --------------------------------------------------------------------------
# In-bot side: queue jobs, run renders, post review cards
# --------------------------------------------------------------------------
class LacyMode:
    def __init__(self, cfg: dict, data_dir: Path, config_path: str | None = None):
        self.cfg = settings(cfg)
        self.jobs = Jobs(data_dir)
        self.config_path = config_path
        self.queue: asyncio.Queue | None = None

    def applies(self, key: tuple[str, str]) -> bool:
        return key == ("twitch", self.cfg["channel"])

    def category_ok(self, category: str | None) -> bool:
        return is_gta(category, self.cfg["allowed_categories"])

    def enqueue(self, rec: dict, state: dict) -> dict | None:
        if rec.get("score", 0) < self.cfg["min_score"]:
            return None
        job = new_job(rec, state, self.cfg)
        self.jobs.save(job)
        if self.queue is not None:
            self.queue.put_nowait(job["id"])
        log.info("lacy job %s queued (render after %s)", job["id"], uk_stamp(job["ready_at"]))
        return job

    def _cli(self, *args: str) -> list[str]:
        cmd = [sys.executable, "-m", "spikebot.lacy"]
        if self.config_path:
            cmd += ["--config", self.config_path]
        return cmd + list(args)

    async def _run(self, *args: str, timeout: float = 3600) -> tuple[int, str]:
        p = await asyncio.create_subprocess_exec(*self._cli(*args), stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill()
            return -1, "timed out"
        return p.returncode or 0, out.decode(errors="replace")[-1500:]

    async def worker(self, helix, post_review, post_text, post_status) -> None:
        """Render queued jobs one at a time. Never raises: Lacy problems must not
        take the chat bot down."""
        self.queue = asyncio.Queue()
        for j in self.jobs.all():  # jobs left waiting by a restart
            if j.get("status") in ("queued", "rendering"):
                self.queue.put_nowait(j["id"])
        while True:
            job_id = await self.queue.get()
            try:
                await self._process(job_id, helix, post_review, post_text, post_status)
            except Exception as e:  # noqa: BLE001
                log.warning("lacy job %s error: %s", job_id, e)

    async def _process(self, job_id, helix, post_review, post_text, post_status) -> None:
        job = self.jobs.load(job_id)
        wait = job["ready_at"] - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        if helix and not job.get("vod_url"):
            vod = await helix.stream_vod(job.get("user_id"), job.get("stream_id"))
            if vod:
                job["vod_url"], job["vod_started_at"] = vod.get("url"), vod.get("started_at")
        job["status"] = "rendering"
        self.jobs.save(job)
        code, out = await self._run("render", job_id)
        job = self.jobs.load(job_id)
        if job["status"] == "rendering":  # the subprocess died before finishing
            job.update(status="failed", reason=f"render crashed: {out[-300:]}")
            self.jobs.save(job)
        if job["status"] == "failed":
            await post_status(job)
            return
        if job["status"] != "ready":
            return
        if self.cfg["auto_upload"]:
            code, out = await self._run("approve", job_id)
            job = self.jobs.load(job_id)
            if job["status"] == "uploaded":
                await post_text(job)
                return
            job["reason"] = out.strip().splitlines()[-1] if out.strip() else "upload failed"
        await post_review(job, self.jobs.dir(job_id) / "preview.mp4")


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
    lay.add_argument("video")
    lay.add_argument("--at", type=float, default=5.0)
    lay.add_argument("--out", default="layout_check.jpg")
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
        for j in jobs.all()[-30:]:
            todo = missing_platforms(j)
            print(f"  {j['id']}  {j['status']:<9} score {j.get('score', '-'):<5} "
                  f"{j.get('title') or ''}  {j.get('youtube_url') or j.get('reason') or ''}"
                  + (f"  NOT YET ON: {', '.join(todo)}" if todo else ""))
        return 0
    if args.cmd == "layout":
        src = Path(args.video)
        info = editor.probe(src)
        lay_ = editor.layout(info["width"], info["height"], lcfg["facecam"])
        tmp = Path(args.out).with_suffix(".tmp.mp4")
        editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(args.at),
                    "-i", str(src), "-t", "1", "-filter_complex", lay_.filter() + "[v]",
                    "-map", "[v]", str(tmp)])
        editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(tmp),
                    "-frames:v", "1", args.out])
        tmp.unlink(missing_ok=True)
        print(f"Wrote {args.out}. If his face isn't filling the top part, change lacy.facecam.")
        return 0

    job = jobs.load(args.job)
    if args.cmd == "render":
        used = subs.titles() | {j.get("title") for j in jobs.all()
                                if j.get("title") and j["id"] != job["id"]}
        job = render_job(job, lcfg, jobs, used)
        print(f"{job['id']}: {job['status']} {job.get('reason') or ''}")
        return 0 if job["status"] in ("ready", "skipped") else 1
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
            job = upload_job(job, lcfg, jobs, subs, yt, send=send)
        except (ValueError, YouTubeError) as e:
            print(f"NOT uploaded: {e}")
            return 1
        print(f"Uploaded: {job['youtube_url']}  ({job['title']})")
        if job.get("reason"):
            print(job["reason"])
        print(f"Logged to {lcfg['submissions_csv']}")
        if send is not None:
            print(f"#ready-to-post: {job.get('delivered') or 'FAILED (file kept, see reason)'}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
