"""Review queue: every finished clip waits for Tim's approval in Discord.

Producers (Lacy mode, Tokyo Girl mode, and the stream-clip editor later) create an
*item* with a first *round* of 3 variants (A/B/C) rendered with different styles.
The Discord bot (discord_bot.py) posts each ready round in #ready-to-post with
buttons; nothing is published until Tim presses Approve.

  data/review/items/<id>/item.json      the item (rounds, variants, decisions, publish state)
  data/review/items/<id>/r<N>_<L>.mp4   full-quality variant, <..>_preview.mp4 small copy

Item status:  rendering -> open -> approved -> done
                               \\-> rejected / expired (48 h after the last round was ready)

Redo / Feedback / Edit caption / /preset apply add a *request*; the bot runs
`python -m spikebot.review render ITEM` for it (CPU-heavy work stays out of the bot
process) and posts the new round in the item's thread.

On approval the chosen variant becomes final.mp4. Platforms listed in
review.modes.<mode>.auto (default Lacy: YouTube + Instagram) are queued and the
bot's publish loop uploads them inside the posting windows and under the daily caps;
the TikTok file (and any platform not automated) goes to #tiktok-queue.

CLI:  python -m spikebot.review list | render ITEM | approve ITEM LABEL | reject ITEM | publish
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import random
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import presets as presets_
from . import style as st

log = logging.getLogger("review")
UK = ZoneInfo("Europe/London")
TRUE = {"1", "true", "yes", "on"}

DEFAULTS = {
    "enabled": True,
    "channel_id": "",              # #ready-to-post
    "tiktok_channel_id": "",       # #tiktok-queue
    "inspiration_channel_id": "",  # #inspiration
    "guild_id": "",                # makes slash commands appear at once
    "approver_ids": [],            # Discord user ids allowed to press buttons; empty = server managers
    "expire_hours": 48,
    "inspiration_archive_hours": 12,   # #inspiration threads quiet this long are archived (0 = off)
    "preview_total_mb": 9.0,       # all 3 previews in one message (10 MB unboosted limit)
    "privacy": "public",           # YouTube privacy for non-Lacy modes (Lacy uses lacy.privacy)
    "youtube_max_per_day": 6,      # all modes together; Lacy also obeys lacy.max_uploads_per_day
    "max_attempts": 3,
    "windows": {"youtube": ["07:00-23:00"], "instagram": ["08:00-22:30"]},  # UK time
    "modes": {
        "lacy": {"auto": ["youtube", "instagram"]},
        # Tokyo Girl edits are silent: the official sound is added in each app by hand,
        # so by default every platform is manual (#tiktok-queue).
        "tokyo_girl": {"auto": []},
        "default": {"auto": ["youtube", "instagram"]},
    },
}
PLATFORMS = ("youtube", "instagram", "tiktok")
NAMES = {"youtube": "YouTube Shorts", "instagram": "Instagram Reels", "tiktok": "TikTok"}
OPEN = ("open",)
FINAL = ("done", "rejected", "expired")


def settings(cfg: dict) -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in (cfg.get("review") or {}).items():
        out[k] = (out[k] | v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    for env_key in ("channel_id", "tiktok_channel_id", "inspiration_channel_id", "guild_id"):
        if cfg.get(f"discord_{env_key}") not in (None, ""):
            out[env_key] = cfg[f"discord_{env_key}"]
    if cfg.get("discord_approver_ids") not in (None, ""):
        out["approver_ids"] = cfg["discord_approver_ids"]
    if isinstance(out["approver_ids"], str):
        out["approver_ids"] = [x for x in re.split(r"[,\s]+", out["approver_ids"]) if x]
    out["approver_ids"] = [int(x) for x in out["approver_ids"]]
    for k in ("channel_id", "tiktok_channel_id", "inspiration_channel_id", "guild_id"):
        out[k] = int(out[k]) if str(out[k]).strip().isdigit() else 0
    if cfg.get("review_enabled") not in (None, ""):
        out["enabled"] = str(cfg["review_enabled"]).strip().lower() in TRUE
    out["token"] = cfg.get("discord_bot_token") or ""
    return out


def auto_platforms(rcfg: dict, mode: str) -> list[str]:
    m = rcfg["modes"].get(mode) or rcfg["modes"].get("default") or {}
    return [p for p in m.get("auto", []) if p in ("youtube", "instagram")]


def uk_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UK).strftime("%Y-%m-%d")


def in_window(windows: list[str], now: float) -> bool:
    """Is the UK time inside one of the 'HH:MM-HH:MM' windows? (Empty = always.)
    A window may cross midnight ('22:00-01:00')."""
    if not windows:
        return True
    t = datetime.fromtimestamp(now, UK)
    m = t.hour * 60 + t.minute
    for w in windows:
        a, b = (int(x[:2]) * 60 + int(x[3:5]) for x in w.split("-"))
        if (a <= m < b) if a <= b else (m >= a or m < b):
            return True
    return False


def next_window(windows: list[str], now: float) -> float:
    """Next time (unix) a window opens; `now` if already inside."""
    t = now
    for _ in range(48 * 4):
        if in_window(windows, t):
            return t
        t += 900
    return now


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------
class Store:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "review" / "items"

    def dir(self, item_id: str) -> Path:
        if not re.fullmatch(r"[\w-]+", item_id or ""):
            raise ValueError(f"bad item id {item_id!r}")
        return self.root / item_id

    def load(self, item_id: str) -> dict:
        return json.loads((self.dir(item_id) / "item.json").read_text(encoding="utf-8"))

    def save(self, item: dict) -> None:
        d = self.dir(item["id"])
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "item.json.tmp"
        tmp.write_text(json.dumps(item, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(d / "item.json")

    @contextlib.contextmanager
    def edit(self, item_id: str):
        """Read-modify-write under a lock (the bot and render subprocesses both write)."""
        d = self.dir(item_id)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            item = self.load(item_id)
            yield item
            self.save(item)

    def all(self) -> list[dict]:
        out = []
        for f in sorted(self.root.glob("*/item.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return out

    def create(self, mode: str, source: dict, now: float | None = None, title: str = "") -> dict:
        now = now or time.time()
        base = f"{mode.replace('_', '')}-{datetime.fromtimestamp(now, UK):%Y%m%d-%H%M%S}"
        item_id, n = base, 2
        while self.dir(item_id).exists():
            item_id, n = f"{base}-{n}", n + 1
        item = {"id": item_id, "mode": mode, "title": title, "source": source, "status": "rendering",
                "created": now, "expires": None, "rounds": [], "request": None,
                "approved": None, "publish": {}, "thread_id": None, "log": []}
        self.save(item)
        return item

    def profile(self) -> st.Profile:
        return st.Profile(self.data_dir / "style_profile.json")

    def presets(self) -> presets_.Presets:
        return presets_.Presets(self.data_dir / "presets")


# --------------------------------------------------------------------------
# Item helpers (pure on the dict)
# --------------------------------------------------------------------------
def note(item: dict, text: str, now: float | None = None) -> None:
    item.setdefault("log", []).append({"ts": now or time.time(), "text": text})
    item["log"] = item["log"][-100:]


def round_of(item: dict, n: int) -> dict:
    for r in item["rounds"]:
        if r["n"] == n:
            return r
    raise ValueError(f"{item['id']} has no round {n}")


def variant_of(item: dict, n: int, label: str) -> dict:
    for v in round_of(item, n)["variants"]:
        if v["label"] == label:
            return v
    raise ValueError(f"{item['id']} round {n} has no variant {label}")


def latest_round(item: dict) -> dict | None:
    return item["rounds"][-1] if item["rounds"] else None


def add_round(item: dict, kind: str, variants: list[dict], rcfg: dict, now: float | None = None,
              comment: str = "") -> dict:
    now = now or time.time()
    r = {"n": len(item["rounds"]) + 1, "kind": kind, "status": "ready", "created": now,
         "message_id": None, "variants": variants, "comment": comment}
    item["rounds"].append(r)
    if item["status"] in ("rendering", "open"):
        item["status"] = "open"
        item["expires"] = now + float(rcfg["expire_hours"]) * 3600
    return r


def require_open(item: dict) -> None:
    if item["status"] not in OPEN:
        raise ValueError(f"this clip is already {item['status']}")


def request(item: dict, kind: str, now: float | None = None, **kw) -> dict:
    """Queue a re-render: redo | feedback (round, label, notes) | preset (round, label, name)."""
    require_open(item)
    if item.get("request") and item["request"].get("status") in ("queued", "rendering"):
        raise ValueError("a re-render for this clip is already running; wait for it to post")
    item["request"] = {"kind": kind, "status": "queued", "ts": now or time.time(), **kw}
    note(item, f"{kind} requested", now)
    return item["request"]


def edit_caption(item: dict, n: int, label: str, title: str, instagram: str, tiktok: str,
                 validate=None) -> dict:
    """Change a variant's title/captions before approval. `validate(item, v)` may raise."""
    require_open(item)
    v = variant_of(item, n, label)
    new = json.loads(json.dumps(v))
    new["title"] = title.strip()
    new["captions"] = dict(new.get("captions") or {}, instagram=instagram.strip(), tiktok=tiktok.strip())
    if validate:
        validate(item, new)
    v.update(title=new["title"], captions=new["captions"], description=new.get("description", v.get("description")),
             edited=True)
    return v


def approve(item: dict, n: int, label: str, rcfg: dict, available: set[str],
            now: float | None = None) -> dict:
    """Mark a variant approved and plan publishing. `available` = platforms that can
    be automated right now (credentials present). Returns the variant."""
    require_open(item)
    now = now or time.time()
    v = variant_of(item, n, label)
    item["status"] = "approved"
    item["approved"] = {"round": n, "label": label, "ts": now}
    auto = [p for p in auto_platforms(rcfg, item["mode"]) if p in available]
    manual = [p for p in PLATFORMS if p not in auto]
    item["publish"] = {p: {"status": "queued", "attempts": 0} for p in auto}
    item["publish"]["manual"] = {"status": "queued", "platforms": manual}
    for r in item["rounds"]:
        r["status"] = "closed"
    note(item, f"approved {label} (round {n}); auto: {', '.join(auto) or 'none'}; manual: {', '.join(manual)}", now)
    return v


def close(item: dict, status: str, now: float | None = None, why: str = "") -> None:
    item["status"] = status
    item["closed"] = now or time.time()
    for r in item["rounds"]:
        r["status"] = "closed"
    if item.get("request") and item["request"].get("status") == "queued":
        item["request"]["status"] = "cancelled"
    note(item, why or status, now)


def due_to_expire(item: dict, now: float) -> bool:
    return item["status"] in OPEN and bool(item.get("expires")) and now >= item["expires"]


def publish_finished(item: dict) -> bool:
    pub = item.get("publish") or {}
    return item["status"] == "approved" and all(
        p.get("status") in ("done", "failed", "off") for p in pub.values())


def shown_styles(item: dict) -> list[dict]:
    return [v["style"] for r in item["rounds"] for v in r["variants"]]


def final_path(store: Store, item: dict) -> Path:
    return store.dir(item["id"]) / "final.mp4"


def make_final(store: Store, item: dict) -> Path:
    a = item["approved"]
    v = variant_of(item, a["round"], a["label"])
    src = store.dir(item["id"]) / v["file"]
    dst = final_path(store, item)
    if not dst.exists():
        shutil.copyfile(src, dst)
    return dst


def free_files(store: Store, item: dict, keep_final: bool = False) -> None:
    d = store.dir(item["id"])
    for f in d.glob("*.mp4"):
        if keep_final and f.name == "final.mp4":
            continue
        f.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Rendering rounds (runs in a subprocess, or inside the producer's own process)
# --------------------------------------------------------------------------
def adapter_for(mode: str, cfg: dict, base: Path):
    """The mode's renderer: .render(item, specs, round_n, folder, preview_bytes) -> variants,
    .validate(item, variant) for caption edits, .finish(item, status) when it closes."""
    if mode == "lacy":
        from .lacy import ReviewAdapter
    elif mode == "tokyo_girl":
        from .tokyo_girl import ReviewAdapter
    else:
        raise ValueError(f"no renderer for mode {mode!r}")
    return ReviewAdapter(cfg, base)


def specs_for(item: dict, req: dict, store: Store, rng: random.Random,
              base_dir: Path | None = None, ai_client=None) -> tuple[list[dict], str]:
    """Styles (and text overrides) to render for a request, and a comment for Discord."""
    profile = store.profile()
    presets = store.presets().all()
    kind = req["kind"]
    if kind in ("initial", "redo"):
        specs = st.pick_variants(profile, item["mode"], rng, presets, avoid=shown_styles(item))
        return specs, ""
    base = variant_of(item, int(req["round"]), req["label"])
    n = len(item["rounds"]) + 1
    if kind == "feedback":
        from . import feedback as fb
        res = fb.for_variant(req.get("notes", ""), item["mode"], base["style"], profile, base_dir,
                             item_id=item["id"], client=ai_client)
        new = res["style"]
        spec = {"label": f"{base['label'][0]}{n}", "style": new, "preset": base.get("preset"),
                "experimental": False, "texts": res["texts"], "from": base, "intents": res["intents"]}
        profile.feedback(item["id"], base["label"], req.get("notes", ""), res["intents"], base["style"], new,
                         item["mode"])
        profile.save()
        return [spec], res["comment"]
    if kind == "preset":
        p = store.presets().load(req["name"])
        ps = presets_.to_style(p, item["mode"])
        new = json.loads(json.dumps(base["style"]))
        custom = dict(new.get("custom") or {}) | dict(ps.pop("custom", {}) or {})
        new.update(ps)
        if custom:
            new["custom"] = custom
        spec = {"label": f"P{n}", "style": new, "preset": p["name"], "experimental": False,
                "texts": {}, "from": base}
        return [spec], f"Re-rendered {base['label']} with preset **{p['name']}** ({', '.join(p.get('copy') or [])})"
    raise ValueError(f"unknown request {kind!r}")


def render_round(store: Store, item_id: str, cfg: dict, base: Path, req: dict | None = None,
                 now: float | None = None, adapter=None, seed=None) -> dict:
    """Render the item's pending request (or `req`) and add the round. Failures are
    recorded on the request, never raised, so the bot can tell Tim."""
    rcfg = settings(cfg)
    now = now or time.time()
    with store.edit(item_id) as item:
        if req is None:
            req = dict(item.get("request") or {"kind": "initial"})
            if item.get("request"):
                item["request"]["status"] = "rendering"
    item = store.load(item_id)
    try:
        rng = random.Random(seed if seed is not None else f"{item_id}-{len(item['rounds'])}-{now}")
        specs, comment = specs_for(item, req, store, rng, base)
        adapter = adapter or adapter_for(item["mode"], cfg, base)
        n = len(item["rounds"]) + 1
        budget = int(rcfg["preview_total_mb"] * 1_000_000 / max(len(specs), 1))
        variants = adapter.render(item, specs, n, store.dir(item_id), budget)
        if not variants:
            raise RuntimeError("nothing could be rendered")
    except Exception as e:  # noqa: BLE001 - reported to Tim in Discord
        log.warning("review %s: render failed: %s", item_id, e)
        with store.edit(item_id) as item:
            if item.get("request"):
                item["request"].update(status="failed", error=str(e)[:400])
            elif item["status"] == "rendering":
                item["status"] = "failed"
                item["error"] = str(e)[:400]
            note(item, f"render failed: {e}", now)
        return store.load(item_id)
    for v in variants:
        v.pop("from", None)
        v.pop("texts", None)
    profile = store.profile()
    profile.shown(item["mode"], variants, now)
    profile.save()
    with store.edit(item_id) as item:
        add_round(item, req["kind"], variants, rcfg, now, comment)
        if item.get("request"):
            item["request"]["status"] = "done"
        note(item, f"round {n} ready ({req['kind']}): {', '.join(v['label'] for v in variants)}", now)
    return store.load(item_id)


def run_pending(store: Store, item_id: str, cfg: dict, base: Path) -> dict:
    item = store.load(item_id)
    if not item.get("request") or item["request"].get("status") not in ("queued", "rendering"):
        return item
    return render_round(store, item_id, cfg, base)


# --------------------------------------------------------------------------
# Publishing (called from the bot's publish loop, in a worker thread)
# --------------------------------------------------------------------------
class Publisher:
    """Uploads approved items to YouTube / Instagram inside the posting windows and
    under the daily caps. Each call handles one platform of one item."""

    def __init__(self, cfg: dict, base: Path, store: Store, youtube_factory=None, ig=None):
        from . import instagram, lacy
        self.cfg, self.base, self.store = cfg, base, store
        self.rcfg = settings(cfg)
        self.lcfg = lacy.settings(cfg)
        self.youtube_factory = youtube_factory or self._youtube
        self.ig = ig if ig is not None else instagram.crossposter(cfg, base)

    def _youtube(self):
        from .youtube import YouTube
        return YouTube(self.cfg.get("youtube_client_id", ""), self.cfg.get("youtube_client_secret", ""),
                       self.cfg.get("youtube_refresh_token", ""))

    def available(self) -> set[str]:
        from . import instagram
        out = set()
        if all(self.cfg.get(k) for k in ("youtube_client_id", "youtube_client_secret", "youtube_refresh_token")):
            out.add("youtube")
        if instagram.active(self.ig.icfg, self.ig.token):
            out.add("instagram")
        return out

    # ---- caps -------------------------------------------------------------------
    def youtube_today(self, day: str) -> int:
        return sum(1 for it in self.store.all()
                   if (it.get("publish") or {}).get("youtube", {}).get("day") == day
                   and it["publish"]["youtube"].get("status") == "done")

    def blocked(self, item: dict, platform: str, now: float) -> str | None:
        """Why this platform can't post right now (None = go)."""
        windows = self.rcfg["windows"].get(platform) or []
        if not in_window(windows, now):
            return f"outside the posting window ({', '.join(windows)} UK)"
        day = uk_day(now)
        if platform == "youtube":
            if self.youtube_today(day) >= int(self.rcfg["youtube_max_per_day"]):
                return f"YouTube daily cap {self.rcfg['youtube_max_per_day']} reached"
            if item["mode"] == "lacy":
                from .lacy import Submissions
                done = Submissions(Path(self.lcfg["submissions_csv"])).uploads_on(day)
                if done >= self.lcfg["max_uploads_per_day"]:
                    return f"Lacy daily cap {self.lcfg['max_uploads_per_day']} reached"
        if platform == "instagram":
            if self.ig.posts.published_on(day) >= int(self.ig.icfg["max_per_day"]):
                return f"Instagram daily cap {self.ig.icfg['max_per_day']} reached"
        return None

    def due(self, now: float) -> list[tuple[str, str]]:
        out = []
        for it in self.store.all():
            if it["status"] != "approved":
                continue
            for p in ("youtube", "instagram"):
                row = (it.get("publish") or {}).get(p)
                if row and row.get("status") in ("queued", "waiting", "retry"):
                    out.append((it["id"], p))
        return out

    def publish(self, item_id: str, platform: str, now: float | None = None) -> dict:
        """Returns the platform row: status done (url) / waiting (reason) / retry / failed."""
        now = now or time.time()
        item = self.store.load(item_id)
        row = dict(item["publish"][platform])
        if row.get("status") in ("done", "failed", "off"):   # never upload twice
            return row
        why = self.blocked(item, platform, now)
        if why:
            row.update(status="waiting", reason=why)
            return self._store_row(item_id, platform, row)
        a = item["approved"]
        v = variant_of(item, a["round"], a["label"])
        path = make_final(self.store, item)
        row["attempts"] = int(row.get("attempts") or 0) + 1
        try:
            if platform == "youtube":
                row.update(self._youtube_upload(item, v, path, now))
            else:
                row.update(self._instagram_post(item, v, path, now))
            row.update(status="done", day=uk_day(now), ts=now, reason=None)
        except Exception as e:  # noqa: BLE001 - reported in the thread
            err = str(e)
            from .instagram import scrub
            err = scrub(err, self.ig.token.get())[:300]
            final = row["attempts"] >= int(self.rcfg["max_attempts"])
            row.update(status="failed" if final else "retry", error=err)
            log.warning("review %s: %s failed (try %s): %s", item_id, platform, row["attempts"], err)
        return self._store_row(item_id, platform, row)

    def _store_row(self, item_id: str, platform: str, row: dict) -> dict:
        with self.store.edit(item_id) as item:
            item["publish"][platform] = row
        return row

    def _youtube_upload(self, item: dict, v: dict, path: Path, now: float) -> dict:
        from .youtube import YouTubeError, shorts_url
        yt = self.youtube_factory()
        privacy = self.lcfg["privacy"] if item["mode"] == "lacy" else self.rcfg["privacy"]
        if item["mode"] == "lacy":
            from . import lacy
            if not lacy.title_ok(v["title"]):
                raise ValueError(f"title {v['title']!r} breaks the Lacy title rules")
        vid = yt.upload(path, v["title"], v["description"], v.get("tags") or [], privacy)
        url = shorts_url(vid)
        out = {"url": url, "video_id": vid}
        thumb = self.store.dir(item["id"]) / "thumb.jpg"
        try:
            if item["mode"] == "lacy":
                from . import editor, lacy
                jobs = lacy.Jobs(self.store.data_dir)
                h = editor.pick_thumbnail(path, thumb, v.get("duration") or 30, v.get("peak"),
                                          jobs.used_hashes(), self.lcfg["thumb_min_distance"])
                jobs.add_hash(h)
                yt.set_thumbnail(vid, thumb)
        except (YouTubeError, Exception) as e:  # noqa: BLE001 - the upload itself worked
            out["note"] = f"thumbnail not set: {e}"
        if item["mode"] == "lacy":
            self._lacy_log(item, v, url, vid, now)
        return out

    def _lacy_log(self, item: dict, v: dict, url: str, vid: str, now: float) -> None:
        from . import lacy
        jobs = lacy.Jobs(self.store.data_dir)
        job_id = item["source"].get("job_id")
        lacy.Submissions(Path(self.lcfg["submissions_csv"])).append(
            {"uploaded_uk": lacy.uk_stamp(now), "youtube_url": url, "title": v["title"], "video_id": vid,
             "job_id": job_id or item["id"], "source_vod": item["source"].get("vod_link") or ""})
        if job_id:
            try:
                job = jobs.load(job_id)
                job.update(status="uploaded", video_id=vid, youtube_url=url, uploaded=now, title=v["title"],
                           review_item=item["id"])
                jobs.save(job)
            except (FileNotFoundError, ValueError):
                pass

    def _instagram_post(self, item: dict, v: dict, path: Path, now: float) -> dict:
        from . import instagram
        res = instagram.post_reel(self.ig.ig_factory(), self.ig.host_factory(), path,
                                  v["captions"]["instagram"], self.ig.icfg)
        self.ig.posts.append({"posted_uk": instagram._uk(now), "status": "published", "job_id": item["id"],
                              "mode": item["mode"], "instagram_url": res["permalink"],
                              "media_id": res["media_id"], "hook": v.get("hook")})
        if item["mode"] == "lacy" and res["permalink"]:
            from . import lacy
            job_id = item["source"].get("job_id") or item["id"]
            lacy.Submissions(Path(self.lcfg["submissions_csv"])).set_links(job_id, {"instagram": res["permalink"]})
            try:
                jobs = lacy.Jobs(self.store.data_dir)
                job = jobs.load(job_id)
                job.update(instagram_url=res["permalink"], instagram_status="published")
                jobs.save(job)
            except (FileNotFoundError, ValueError):
                pass
        return {"url": res["permalink"] or f"instagram media {res['media_id']}", "media_id": res["media_id"]}


def tiktok_export(cfg: dict, base: Path, store: Store, item: dict) -> Path | None:
    """Copy the approved file to exports/tiktok/ with its caption (manual upload)."""
    from . import instagram
    icfg = instagram.settings(cfg, base)
    a = item["approved"]
    v = variant_of(item, a["round"], a["label"])
    try:
        return instagram.export_tiktok(make_final(store, item), f"{item['mode']}_{item['id']}",
                                       v["captions"]["tiktok"], icfg["tiktok_dir"], int(icfg["tiktok_keep_days"]))
    except OSError as e:
        log.warning("review %s: TikTok export failed: %s", item["id"], e)
        return None


def finish(store: Store, cfg: dict, base: Path, item_id: str, status: str, now: float | None = None,
           why: str = "") -> dict:
    """Close an item (rejected / expired / done) and free its disk space."""
    with store.edit(item_id) as item:
        if status != "done":
            close(item, status, now, why)
        else:
            item["status"] = "done"
            item["closed"] = now or time.time()
    item = store.load(item_id)
    try:
        adapter_for(item["mode"], cfg, base).finish(item, status)
    except Exception as e:  # noqa: BLE001
        log.warning("review %s: cleanup: %s", item_id, e)
    free_files(store, item)
    return item


# --------------------------------------------------------------------------
# CLI (fallback when Discord is down)
# --------------------------------------------------------------------------
def load_cfg(path: str) -> tuple[dict, Path]:
    from .main import apply_env
    import os
    p = Path(path)
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    env_files = list(dict.fromkeys([p.resolve().parent / ".env", Path.cwd() / ".env"]))
    apply_env(cfg, env_files, dict(os.environ))
    return cfg, p.resolve().parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m spikebot.review")
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("render").add_argument("item")
    a = sub.add_parser("approve")
    a.add_argument("item")
    a.add_argument("label", help="A, B, C (latest round) or e.g. B3")
    sub.add_parser("reject").add_argument("item")
    sub.add_parser("publish", help="post approved items that are due (normally the bot does this)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg, base = load_cfg(args.config)
    store = Store(Path(cfg.get("data_dir", "data")) if Path(cfg.get("data_dir", "data")).is_absolute()
                  else base / cfg.get("data_dir", "data"))
    if args.cmd == "list":
        for it in store.all()[-30:]:
            r = latest_round(it)
            labels = ", ".join(v["label"] for v in r["variants"]) if r else "-"
            pub = " ".join(f"{p}:{x.get('status')}" for p, x in (it.get("publish") or {}).items())
            print(f"{it['id']:<28} {it['status']:<9} round {r['n'] if r else 0} [{labels}] {pub}")
        return 0
    if args.cmd == "render":
        it = run_pending(store, args.item, cfg, base)
        req = it.get("request") or {}
        print(f"{it['id']}: {it['status']} request={req.get('status')} {req.get('error') or ''}")
        return 0 if req.get("status") != "failed" else 1
    if args.cmd == "publish":
        pub = Publisher(cfg, base, store)
        for item_id, p in pub.due(time.time()):
            row = pub.publish(item_id, p)
            print(item_id, p, row.get("status"), row.get("url") or row.get("reason") or row.get("error") or "")
        return 0
    item = store.load(args.item)
    if args.cmd == "reject":
        profile = store.profile()
        r = latest_round(item)
        profile.decide("reject", item["mode"], item["id"], r["variants"] if r else [])
        profile.save()
        finish(store, cfg, base, item["id"], "rejected", why="rejected (CLI)")
        print(f"{item['id']}: rejected")
        return 0
    if args.cmd == "approve":
        m = re.fullmatch(r"([A-Z])(\d*)", args.label.upper())
        r = latest_round(item)
        if not m or not r:
            print(f"no variant {args.label}")
            return 1
        n = int(m.group(2)) if m.group(2) else r["n"]
        try:
            labels = [v["label"] for v in round_of(item, n)["variants"]]
        except ValueError as e:
            print(e)
            return 1
        label = m.group(0) if m.group(0) in labels else m.group(1)
        if label not in labels:
            print(f"round {n} has {', '.join(labels)}, not {args.label}")
            return 1
        pub = Publisher(cfg, base, store)
        with store.edit(item["id"]) as it:
            approve(it, n, label, settings(cfg), pub.available())
        profile = store.profile()
        profile.decide("approve", item["mode"], item["id"], round_of(item, n)["variants"], label)
        profile.save()
        print(f"{item['id']}: approved {label}. The bot posts it inside the windows; or run `publish`.")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
