"""Instagram Reels auto-posting (Instagram API with Instagram Login, graph.instagram.com)
plus a TikTok export folder for manual uploads.

For every Lacy Short, right after the YouTube upload:
  1. TikTok: the file is copied to exports/tiktok/lacy_<id>.mp4 with a caption .txt
     beside it (TikTok is posted by hand).
  2. Instagram (if IG_USER_ID + IG_ACCESS_TOKEN are set, max `max_per_day` a UK day):
       - the file goes to the private GCS bucket (VIDEOS_GCS_BUCKET) under instagram/,
         and Instagram gets a signed HTTPS link that expires after `url_minutes`;
       - POST /{ig-user-id}/media  media_type=REELS, video_url, caption
       - poll GET /{container}?fields=status_code until FINISHED
       - POST /{ig-user-id}/media_publish creation_id=...
       - the bucket object is deleted (also when publishing fails);
       - the post is logged to logs/instagram_posts.csv and its link is added to the
         Lacy submissions CSV (instagram_url) for Content Rewards.
     Over the daily cap, or on failure, the job waits and the bot retries it hourly.
  3. The long-lived token (60 days) is refreshed every `refresh_every_days` (50) with
     refresh_access_token and written back to .env. The token is always read from the
     .env file first, so the running bot picks up the new one without a restart.

CLI:  python -m spikebot.instagram check | refresh | retry [JOB]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import random
import shutil
import time
from datetime import timedelta
from pathlib import Path

log = logging.getLogger("instagram")

GRAPH = "https://graph.instagram.com"
DEFAULTS = {
    "enabled": True,             # still needs IG_USER_ID + IG_ACCESS_TOKEN to do anything
    "max_per_day": 3,            # UK calendar day; .env IG_MAX_REELS_PER_DAY overrides
    "api_version": "v23.0",
    "refresh_every_days": 50,    # tokens last 60 days
    "share_to_feed": True,       # also show the Reel on the profile grid
    "url_minutes": 60,           # life of the signed link Instagram downloads from
    "poll_s": 10,
    "poll_timeout_s": 900,
    "max_attempts": 3,           # failed publishes retried by the bot, then left alone
    "posts_csv": "logs/instagram_posts.csv",
    "tiktok_dir": "exports/tiktok",
    "tiktok_keep_days": 14,      # exports older than this are deleted
}
TRUE = {"1", "true", "yes", "on"}


class InstagramError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Settings and the token (always re-read from .env, which the refresher rewrites)
# --------------------------------------------------------------------------
def settings(cfg: dict, base: Path | None = None) -> dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg.get("instagram") or {}).items() if k in DEFAULTS})
    if cfg.get("instagram_auto_post") not in (None, ""):
        out["enabled"] = str(cfg["instagram_auto_post"]).strip().lower() in TRUE
    if cfg.get("ig_max_reels_per_day") not in (None, ""):
        out["max_per_day"] = int(cfg["ig_max_reels_per_day"])
    out["user_id"] = str(cfg.get("ig_user_id") or "").strip()
    out["env_path"] = str((base or Path.cwd()) / ".env")
    for k in ("posts_csv", "tiktok_dir"):
        if base is not None and not Path(out[k]).is_absolute():
            out[k] = str(base / out[k])
    return out


class Token:
    """IG_ACCESS_TOKEN: the .env file wins over the process environment, because
    systemd loads .env once at start and the refresher rewrites the file later."""

    def __init__(self, env_path: Path | str, environ: dict | None = None):
        self.env_path = Path(env_path)
        self.environ = environ if environ is not None else {}

    def get(self) -> str:
        from .main import load_dotenv
        return (load_dotenv(self.env_path).get("IG_ACCESS_TOKEN")
                or self.environ.get("IG_ACCESS_TOKEN") or "").strip()

    def save(self, value: str) -> None:
        from .youtube import set_env_value
        set_env_value(self.env_path, "IG_ACCESS_TOKEN", value)


def fingerprint(token: str) -> str:
    """Short one-way id so the state file can notice a hand-pasted new token."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def active(icfg: dict, token: Token) -> bool:
    return bool(icfg["enabled"] and icfg["user_id"] and token.get())


def scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------
class Instagram:
    def __init__(self, user_id: str, token: Token, version: str = DEFAULTS["api_version"], http=None):
        if http is None:
            from curl_cffi import requests as http  # noqa: N813
        self.user_id, self.token, self.http = user_id, token, http
        self.base = f"{GRAPH}/{version}"

    def _call(self, method: str, path: str, **kw) -> dict:
        tok = self.token.get()
        if not tok:
            raise InstagramError("IG_ACCESS_TOKEN is not set in .env")
        try:
            r = getattr(self.http, method)(f"{self.base}/{path}", timeout=60,
                                           headers={"Authorization": f"Bearer {tok}"}, **kw)
        except Exception as e:  # noqa: BLE001 - never let a URL/token leak via the message
            raise InstagramError(f"{method.upper()} {path.split('?')[0]}: {type(e).__name__}") from None
        try:
            j = r.json()
        except ValueError:
            j = {}
        if r.status_code >= 300 or "error" in j:
            err = j.get("error") or {}
            msg = err.get("error_user_msg") or err.get("message") or r.text[:200]
            raise InstagramError(scrub(f"HTTP {r.status_code} {err.get('code', '')} {msg}".strip(), tok))
        return j

    def me(self) -> dict:
        return self._call("get", "me", params={"fields": "user_id,username"})

    def create_reel(self, video_url: str, caption: str, share_to_feed: bool = True) -> str:
        j = self._call("post", f"{self.user_id}/media", json={
            "media_type": "REELS", "video_url": video_url, "caption": caption,
            "share_to_feed": bool(share_to_feed)})
        return str(j["id"])

    def status(self, container_id: str) -> tuple[str, str]:
        j = self._call("get", container_id, params={"fields": "status_code,status"})
        return j.get("status_code") or "", j.get("status") or ""

    def publish(self, container_id: str) -> str:
        return str(self._call("post", f"{self.user_id}/media_publish",
                              json={"creation_id": container_id})["id"])

    def permalink(self, media_id: str) -> str:
        return self._call("get", media_id, params={"fields": "permalink"}).get("permalink") or ""


def refresh_token(token: Token, http=None) -> tuple[str, int]:
    """Exchange the long-lived token for a new one (only works once it's 24h+ old).
    Returns (new token, expires_in seconds)."""
    if http is None:
        from curl_cffi import requests as http  # noqa: N813
    tok = token.get()
    if not tok:
        raise InstagramError("IG_ACCESS_TOKEN is not set in .env")
    try:
        r = http.get(f"{GRAPH}/refresh_access_token", timeout=30,
                     params={"grant_type": "ig_refresh_token", "access_token": tok})
    except Exception as e:  # noqa: BLE001
        raise InstagramError(f"token refresh: {type(e).__name__}") from None
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code != 200 or not j.get("access_token"):
        msg = (j.get("error") or {}).get("message") or r.text[:200]
        raise InstagramError(scrub(f"token refresh failed: HTTP {r.status_code} {msg}", tok))
    return j["access_token"], int(j.get("expires_in") or 60 * 86400)


# --------------------------------------------------------------------------
# Token refresh schedule (state in data/instagram/state.json, no secrets in it)
# --------------------------------------------------------------------------
class State:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "instagram" / "state.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def save(self, st: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        tmp.replace(self.path)


RETRY_REFRESH_S = 6 * 3600


def maybe_refresh(icfg: dict, token: Token, state: State, now: float | None = None,
                  refresh=None, force: bool = False) -> str:
    """Refresh when due. Returns none / new_token / not_due / refreshed / failed: <why>.
    A token we haven't seen before (pasted by hand) counts as just issued."""
    now = now or time.time()
    tok = token.get()
    if not tok:
        return "none"
    st = state.load()
    if st.get("token_fp") != fingerprint(tok):
        st = {"token_fp": fingerprint(tok), "refreshed_at": now, "expires_at": now + 60 * 86400}
        state.save(st)
        if not force:
            return "new_token"
    due = now - st["refreshed_at"] >= icfg["refresh_every_days"] * 86400
    if not (force or due) or (not force and now - st.get("last_attempt", 0) < RETRY_REFRESH_S):
        return "not_due"
    st["last_attempt"] = now
    try:
        new, expires_in = (refresh or refresh_token)(token)
    except InstagramError as e:
        st["last_error"] = str(e)
        state.save(st)
        return f"failed: {e}"
    token.save(new)
    state.save({"token_fp": fingerprint(new), "refreshed_at": now, "expires_at": now + expires_in})
    log.info("instagram token refreshed; valid until %s", _uk(now + expires_in))
    return "refreshed"


# --------------------------------------------------------------------------
# Temporary public URL: private GCS bucket + short signed link, deleted after
# --------------------------------------------------------------------------
class GCSHost:
    def __init__(self, bucket: str, key_file: str, minutes: int):
        from google.cloud import storage  # lazy, only where a Reel is posted
        self.bucket = storage.Client.from_service_account_json(key_file).bucket(bucket)
        self.minutes = minutes

    def put(self, path: Path, name: str):
        """Upload; return (https url, delete function)."""
        blob = self.bucket.blob(f"instagram/{name}")
        blob.upload_from_filename(str(path), content_type="video/mp4", timeout=900)
        url = blob.generate_signed_url(version="v4", method="GET",
                                       expiration=timedelta(minutes=self.minutes))
        return url, blob.delete


def host_from_config(cfg: dict, base: Path | None, icfg: dict):
    from . import delivery
    dcfg = delivery.settings(cfg, base)
    if not dcfg["gcs_bucket"]:
        raise InstagramError("VIDEOS_GCS_BUCKET is not set: Instagram needs the video at a public"
                             " HTTPS link (see README: Instagram Reels)")
    return GCSHost(dcfg["gcs_bucket"], dcfg["gcs_key_file"], int(icfg["url_minutes"]))


def post_reel(ig: Instagram, host, path: Path, caption: str, icfg: dict,
              sleep=time.sleep, clock=time.monotonic) -> dict:
    """Upload -> container -> wait for FINISHED -> publish. The public copy is
    always deleted afterwards."""
    url, delete = host.put(path, path.name)
    try:
        cid = ig.create_reel(url, caption, icfg["share_to_feed"])
        deadline = clock() + icfg["poll_timeout_s"]
        while True:
            code, detail = ig.status(cid)
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise InstagramError(f"Instagram couldn't process the video ({code}: {detail})")
            if clock() > deadline:
                raise InstagramError(f"still {code or 'processing'} after {icfg['poll_timeout_s']}s")
            sleep(icfg["poll_s"])
        media_id = ig.publish(cid)
    finally:
        try:
            delete()
        except Exception as e:  # noqa: BLE001 - the bucket's own cleanup gets it later
            log.warning("could not delete the temporary Instagram upload: %s", e)
    try:
        link = ig.permalink(media_id)
    except InstagramError as e:
        log.warning("published %s but no permalink: %s", media_id, e)
        link = ""
    return {"container_id": cid, "media_id": media_id, "permalink": link}


# --------------------------------------------------------------------------
# Captions: a different hook from the YouTube title, plus GTA hashtags
# --------------------------------------------------------------------------
HOOKS = {
    "funny": ["POV: you're in Lacy's chat for this one 💀",
              "I can't stop replaying this Lacy moment 😭",
              "Lacy was NOT ready for this 😂",
              "Lacy's laugh at the end 😭",
              "Lacy and chat lost it here 💀"],
    "shock": ["Wait for Lacy's face at the end 😳",
              "Lacy did not see this coming 😳",
              "Nobody in Lacy's chat expected this",
              "Watch Lacy's reaction 👀",
              "Lacy's chat went silent after this 👀"],
    "hype": ["Lacy actually pulled this off 🔥",
             "Lacy made this look way too easy 🔥",
             "Chat went crazy for Lacy here 🔥",
             "This is why Lacy's chat goes wild 🔥"],
    "neutral": ["Lacy's best GTA moment today 🎮",
                "Lacy in GTA never misses 🎮",
                "Lacy's stream had moments like this all day",
                "Save this Lacy clip for later 🎮"],
}
HASHTAGS = {
    "GTA 6": {"instagram": "#GTA6 #GTAVI #GrandTheftAuto6 #GTA6Clips #Lacy #GamingReels #Reels",
              "tiktok": "#GTA6 #GTAVI #GTA6Clips #Lacy #gaming #fyp"},
    "GTA 5": {"instagram": "#GTA5 #GTAV #GrandTheftAuto #GTAClips #Lacy #GamingReels #Reels",
              "tiktok": "#GTA5 #GTAV #GTAClips #Lacy #gaming #fyp"},
}


def hook_ok(hook: str, youtube_title: str) -> bool:
    from .lacy import BANNED
    return ("lacy" in hook.lower() and not BANNED.search(hook)
            and hook.strip().lower() != (youtube_title or "").strip().lower())


def pick_hook(job: dict, used: set[str], rng: random.Random | None = None) -> str:
    """A hook for the clip's mood, not the YouTube title, not used recently."""
    from .lacy import mood
    rng = rng or random.Random(job.get("id"))
    m = mood(job.get("vibe") or [])
    first, rest = HOOKS[m][:], [h for k, hs in HOOKS.items() if k != m for h in hs]
    rng.shuffle(first)
    rng.shuffle(rest)
    ok = [h for h in first + rest if hook_ok(h, job.get("title") or "")]
    for h in ok:
        if h not in used:
            return h
    return ok[0]


def captions(job: dict, hook: str) -> dict[str, str]:
    from .lacy import game_tag
    game = game_tag(job.get("category") or "")
    tags = HASHTAGS[game]
    ctx = f"Lacy playing {game} live on Twitch (twitch.tv/{job.get('channel') or 'lacy'})"
    return {"instagram": f"{hook}\n\n{ctx}. Follow for daily {game} clips.\n\n{tags['instagram']}"[:2200],
            "tiktok": f"{hook} {tags['tiktok']}"[:2200]}


# --------------------------------------------------------------------------
# Logs: logs/instagram_posts.csv (one row per attempt) and the daily cap
# --------------------------------------------------------------------------
CSV_FIELDS = ["posted_uk", "status", "job_id", "mode", "instagram_url", "media_id", "hook", "error"]


class PostLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def published_on(self, day: str) -> int:
        return sum(1 for r in self.rows()
                   if r.get("status") == "published" and (r.get("posted_uk") or "").startswith(day))

    def recent_hooks(self, n: int = 20) -> set[str]:
        return {r["hook"] for r in self.rows()[-n:] if r.get("hook")}

    def append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)


def _uk(ts: float) -> str:
    from .lacy import uk_stamp
    return uk_stamp(ts)


# --------------------------------------------------------------------------
# TikTok export folder
# --------------------------------------------------------------------------
def export_tiktok(path: Path, name: str, caption: str, folder: Path | str, keep_days: int,
                  now: float | None = None) -> Path:
    """Copy the finished file (unchanged) to exports/tiktok/<name>.mp4 + <name>.txt."""
    now = now or time.time()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{name}.mp4"
    shutil.copyfile(path, out)
    out.with_suffix(".txt").write_text(caption + "\n", encoding="utf-8")
    for f in list(folder.glob("*.mp4")) + list(folder.glob("*.txt")):
        if f.stat().st_mtime < now - keep_days * 86400 and f.stem != name:
            f.unlink(missing_ok=True)
    return out


# --------------------------------------------------------------------------
# Lacy: after the YouTube upload
# --------------------------------------------------------------------------
class Crossposter:
    """Called by lacy.upload_job(social=...) and by the bot's hourly retry.
    Never raises: the YouTube upload is already done."""

    def __init__(self, icfg: dict, token: Token, posts: PostLog, ig_factory, host_factory,
                 sleep=time.sleep, clock=time.monotonic):
        self.icfg, self.token, self.posts = icfg, token, posts
        self.ig_factory, self.host_factory = ig_factory, host_factory
        self.sleep, self.clock = sleep, clock

    def __call__(self, path: Path, job: dict, now: float | None = None) -> None:
        now = now or time.time()
        if not job.get("tiktok_export"):
            hook = pick_hook(job, self.posts.recent_hooks())
            caps = captions(job, hook)
            job.update(social_hook=hook, instagram_caption=caps["instagram"], tiktok_caption=caps["tiktok"])
            try:
                out = export_tiktok(path, f"lacy_{job['id']}", caps["tiktok"], self.icfg["tiktok_dir"],
                                    int(self.icfg["tiktok_keep_days"]), now)
                job["tiktok_export"] = str(out)
            except OSError as e:
                log.warning("lacy %s: TikTok export failed: %s", job["id"], e)
        self.instagram(job, now)

    def instagram(self, job: dict, now: float | None = None) -> str:
        now = now or time.time()
        if job.get("instagram_url"):
            return "done"
        if not active(self.icfg, self.token):
            job["instagram_status"] = "off"
            return "off"
        src = Path(job.get("tiktok_export") or "")
        if not src.is_file():
            job["instagram_status"] = "failed"
            job["instagram_error"] = "no exported file to post"
            return "failed"
        from .lacy import uk_day
        done = self.posts.published_on(uk_day(now))
        if done >= int(self.icfg["max_per_day"]):
            job["instagram_status"] = "waiting"
            job["instagram_error"] = f"daily cap {self.icfg['max_per_day']} reached; posts tomorrow"
            return "waiting"
        job["instagram_attempts"] = int(job.get("instagram_attempts") or 0) + 1
        row = {"posted_uk": _uk(now), "job_id": job["id"], "mode": "lacy", "hook": job.get("social_hook")}
        try:
            res = post_reel(self.ig_factory(), self.host_factory(), src,
                            job.get("instagram_caption") or captions(job, job.get("social_hook") or "")["instagram"],
                            self.icfg, self.sleep, self.clock)
        except Exception as e:  # noqa: BLE001
            err = scrub(str(e), self.token.get())
            job.update(instagram_status="failed", instagram_error=err)
            self.posts.append(row | {"status": "failed", "error": err})
            log.warning("lacy %s: Instagram failed: %s", job["id"], err)
            return "failed"
        job.update(instagram_status="published", instagram_url=res["permalink"],
                   instagram_media_id=res["media_id"], instagram_error=None)
        self.posts.append(row | {"status": "published", "instagram_url": res["permalink"],
                                 "media_id": res["media_id"]})
        log.info("lacy %s: on Instagram %s", job["id"], res["permalink"] or res["media_id"])
        return "published"


def crossposter(cfg: dict, base: Path, environ: dict | None = None) -> Crossposter:
    icfg = settings(cfg, base)
    token = Token(icfg["env_path"], environ)
    return Crossposter(icfg, token, PostLog(icfg["posts_csv"]),
                       lambda: Instagram(icfg["user_id"], token, icfg["api_version"]),
                       lambda: host_from_config(cfg, base, icfg))


def retry_pending(xp: Crossposter, jobs, subs, only: str | None = None, now: float | None = None) -> list[tuple[str, str]]:
    """Post uploaded Lacy Shorts still waiting for Instagram (cap or earlier failure)."""
    now = now or time.time()
    out = []
    for job in jobs.all():
        if only and job["id"] != only:
            continue
        st = job.get("instagram_status")
        if job.get("status") != "uploaded" or job.get("instagram_url") or not job.get("tiktok_export"):
            continue
        if st not in ("waiting", "failed") and not only:
            continue
        if st == "failed" and int(job.get("instagram_attempts") or 0) >= int(xp.icfg["max_attempts"]) and not only:
            continue
        res = xp.instagram(job, now)
        if res == "published" and job.get("instagram_url"):
            subs.set_links(job["id"], {"instagram": job["instagram_url"]})
        jobs.save(job)
        out.append((job["id"], res))
        if res == "waiting":
            break  # cap reached: the rest wait too
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import os
    from . import lacy
    ap = argparse.ArgumentParser(description="Instagram Reels auto-posting")
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="check the token and settings (no posting)")
    sub.add_parser("refresh", help="refresh the long-lived token now and save it to .env")
    r = sub.add_parser("retry", help="post Lacy Shorts still waiting for Instagram")
    r.add_argument("job", nargs="?")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = lacy._load(args.config)
    base = Path(args.config).resolve().parent
    xp = crossposter(cfg, base, dict(os.environ))
    icfg, token = xp.icfg, xp.token
    state = State(Path(cfg.get("data_dir", "data")))
    if args.cmd == "check":
        print(f"IG_USER_ID: {'set' if icfg['user_id'] else 'MISSING'} · IG_ACCESS_TOKEN: "
              f"{'set' if token.get() else 'MISSING'} · auto-post {'ON' if active(icfg, token) else 'OFF'}")
        print(f"Reels today: {xp.posts.published_on(lacy.uk_day(time.time()))}/{icfg['max_per_day']}")
        st = state.load()
        if st.get("refreshed_at"):
            print(f"Token last refreshed {_uk(st['refreshed_at'])}, expires about {_uk(st['expires_at'])}")
        if token.get():
            try:
                me = Instagram(icfg["user_id"], token, icfg["api_version"]).me()
                print(f"Token OK: @{me.get('username')} (user_id {me.get('user_id')})")
                if icfg["user_id"] and str(me.get("user_id")) != icfg["user_id"]:
                    print("WARNING: IG_USER_ID doesn't match the token's account")
            except InstagramError as e:
                print(f"Token check FAILED: {e}")
                return 1
        try:
            host_from_config(cfg, base, icfg)
            print("Bucket for the temporary public link: OK")
        except Exception as e:  # noqa: BLE001
            print(f"Bucket: {e}")
        return 0
    if args.cmd == "refresh":
        res = maybe_refresh(icfg, token, state, force=True)
        print(f"Token: {res}" + (" (saved to .env)" if res == "refreshed" else ""))
        return 0 if res == "refreshed" else 1
    data_dir = Path(cfg.get("data_dir", "data"))
    lcfg = lacy.settings(cfg)
    res = retry_pending(xp, lacy.Jobs(data_dir), lacy.Submissions(Path(lcfg["submissions_csv"])), args.job)
    for job_id, how in res:
        print(f"{job_id}: {how}")
    if not res:
        print("Nothing waiting for Instagram.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
