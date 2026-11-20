"""Channel totals for the Discord 📊 Stats panel: views, likes and followers summed across
the platforms the bot can read with official APIs.

- YouTube (Data API v3): channel viewCount + subscriberCount, likes summed over every upload.
  Read with YOUTUBE_API_KEY (public data, no OAuth) or, if that isn't set, the upload OAuth token
  when it also has the youtube.readonly scope (re-run `python -m spikebot.youtube auth`).
  The channel is review.stats.youtube_channel (UC... id or @handle), else found from any video
  the bot uploaded.
- Instagram (Graph API, the IG_ACCESS_TOKEN the Reels poster uses): followers_count, like_count
  per post, views per post from media insights (needs the insights permission; skipped if not).
- TikTok isn't counted yet: its API gives totals only with the user.info.stats / video.list
  scopes, which the TikTok app (spikebot/tiktok.py) doesn't ask for.

`refresh()` runs in a thread from the Discord bot every `social_every_s`. A platform that
fails keeps its last good numbers for `STALE_S`, so one bad request never makes the totals jump.
Nothing secret is stored: data/social_stats.json holds only counts.
"""
from __future__ import annotations

import csv
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("socialstats")

YT_API = "https://www.googleapis.com/youtube/v3"
STALE_S = 6 * 3600            # last good numbers are shown this long after a failure
IG_INSIGHTS_PER_RUN = 25      # media insights calls per refresh (Instagram allows ~200/hour)
MAX_VIDEOS = 2000             # safety cap on pages read
SHORT = {"youtube": "YT", "instagram": "IG", "tiktok": "TT"}
KINDS = ("views", "likes", "followers")


class StatsError(RuntimeError):
    pass


def _http():
    from curl_cffi import requests
    return requests


def _json(r) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------
class YouTubeStats:
    def __init__(self, api_key: str = "", oauth=None, http=None):
        self.api_key, self.oauth, self.http = api_key, oauth, http or _http()

    def _get(self, path: str, params: dict, use_oauth: bool = False) -> dict:
        params = dict(params)
        headers = {}
        if use_oauth and self.oauth is not None:
            headers["Authorization"] = f"Bearer {self.oauth.token()}"
        elif self.api_key:
            params["key"] = self.api_key
        elif self.oauth is not None:
            headers["Authorization"] = f"Bearer {self.oauth.token()}"
        else:
            raise StatsError("no YOUTUBE_API_KEY and no YouTube login")
        r = self.http.get(f"{YT_API}/{path}", params=params, headers=headers, timeout=30)
        j = _json(r)
        if r.status_code != 200:
            err = (j.get("error") or {})
            reason = ((err.get("errors") or [{}])[0]).get("reason") or ""
            raise StatsError(f"YouTube {path}: HTTP {r.status_code} {reason}".strip())
        return j

    def channel_of_video(self, video_id: str) -> str:
        items = self._get("videos", {"part": "snippet", "id": video_id}).get("items") or []
        return (items[0].get("snippet") or {}).get("channelId", "") if items else ""

    def my_channel_id(self) -> str:
        """The channel of the upload login (needs the youtube.readonly scope); "" if it can't tell."""
        if self.oauth is None:
            return ""
        try:
            items = self._get("channels", {"part": "id", "mine": "true"}, use_oauth=True).get("items") or []
        except Exception as e:  # noqa: BLE001 - an upload-only token can't read the channel
            log.info("YouTube channel lookup with the upload login failed: %s", e)
            return ""
        return items[0].get("id", "") if items else ""

    def channel(self, ref: str) -> dict:
        p = {"part": "statistics,contentDetails,snippet"}
        if ref.startswith("@"):
            p["forHandle"] = ref
        elif ref:
            p["id"] = ref
        else:
            p["mine"] = "true"
        items = self._get("channels", p, use_oauth=not ref).get("items") or []
        if not items:
            raise StatsError(f"YouTube channel {ref or '(mine)'} not found")
        return items[0]

    def video_ids(self, uploads: str) -> list[str]:
        out, page = [], None
        while len(out) < MAX_VIDEOS:
            p = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
            if page:
                p["pageToken"] = page
            j = self._get("playlistItems", p)
            out += [(it.get("contentDetails") or {}).get("videoId") for it in j.get("items") or []]
            page = j.get("nextPageToken")
            if not page:
                break
        return [v for v in out if v]

    def likes(self, ids: list[str]) -> int:
        total = 0
        for i in range(0, len(ids), 50):
            j = self._get("videos", {"part": "statistics", "id": ",".join(ids[i:i + 50])})
            total += sum(int((it.get("statistics") or {}).get("likeCount") or 0) for it in j.get("items") or [])
        return total

    def totals(self, ref: str) -> dict:
        ch = self.channel(ref)
        st = ch.get("statistics") or {}
        uploads = ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        out = {"views": int(st.get("viewCount") or 0), "channel": ch.get("id", "")}
        if st.get("videoCount") is not None:
            out["videos"] = int(st["videoCount"])
        if (ch.get("snippet") or {}).get("title"):
            out["title"] = ch["snippet"]["title"]
        if not st.get("hiddenSubscriberCount"):
            out["followers"] = int(st.get("subscriberCount") or 0)
        if uploads:
            out["likes"] = self.likes(self.video_ids(uploads))
        return out


def uploaded_video_ids(base: Path, data_dir: Path) -> list[str]:
    """YouTube ids the bot uploaded (review items, then the submissions CSVs), newest first."""
    ids: list[tuple[float, str]] = []
    items = data_dir / "review" / "items"
    for f in items.glob("*/item.json") if items.is_dir() else []:
        try:
            it = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        yt = (it.get("publish") or {}).get("youtube") or {}
        if yt.get("video_id"):
            ids.append((f.stat().st_mtime, yt["video_id"]))
    for f in list((base / "logs").glob("*_submissions.csv")) if (base / "logs").is_dir() else []:
        try:
            with f.open(encoding="utf-8", newline="") as fh:
                ids += [(f.stat().st_mtime, r["video_id"]) for r in csv.DictReader(fh) if r.get("video_id")]
        except (OSError, KeyError, csv.Error):
            continue
    return [v for _, v in sorted(ids, reverse=True)]


def env_value(base: Path | None, name: str) -> str:
    """A value from the .env next to config.json, read fresh (a key added there works without a
    restart: the bot's own copy of the environment is from when it started)."""
    if base is None:
        return ""
    from .main import load_dotenv
    try:
        return (load_dotenv(Path(base) / ".env").get(name) or "").strip()
    except OSError:
        return ""


def youtube_client(cfg: dict, http=None, base: Path | None = None) -> YouTubeStats | None:
    """YOUTUBE_API_KEY reads the public numbers; the upload login (if set) is kept too, to find the
    channel when nothing has been uploaded yet."""
    key = env_value(base, "YOUTUBE_API_KEY") or str(cfg.get("youtube_api_key") or "").strip()
    oauth = None
    if all(cfg.get(k) for k in ("youtube_client_id", "youtube_client_secret", "youtube_refresh_token")):
        from .youtube import YouTube
        oauth = YouTube(cfg["youtube_client_id"], cfg["youtube_client_secret"], cfg["youtube_refresh_token"])
    if not key and oauth is None:
        return None
    return YouTubeStats(key, oauth, http)


def set_youtube_channel(data_dir: Path, ref: str) -> None:
    """/stats youtube_channel:@handle: remembered in data/social_stats.json."""
    st = load(Path(data_dir))
    st["youtube_channel"] = ref.strip()
    st.get("platforms", {}).pop("youtube", None)   # numbers of another channel must not linger
    p = path_for(Path(data_dir))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------
# Instagram
# --------------------------------------------------------------------------
def instagram_totals(ig, cache: dict, now: float, budget: int = IG_INSIGHTS_PER_RUN) -> dict:
    """followers, likes over every post, and views summed from per-post insights. Insights are
    re-read `budget` posts per run (oldest check first, new posts first of all), so views
    count only once every post has a number. `cache` = {"views", "checked", "denied_at"}."""
    acc = ig._call("get", ig.user_id, params={"fields": "followers_count,media_count"})
    out = {"followers": int(acc.get("followers_count") or 0)}
    media, after = [], None
    while len(media) < MAX_VIDEOS:
        p = {"fields": "id,like_count", "limit": 100}
        if after:
            p["after"] = after
        j = ig._call("get", f"{ig.user_id}/media", params=p)
        media += j.get("data") or []
        after = ((j.get("paging") or {}).get("cursors") or {}).get("after")
        if not after or not (j.get("paging") or {}).get("next"):
            break
    out["likes"] = sum(int(m.get("like_count") or 0) for m in media)
    ids = [m["id"] for m in media if m.get("id")]
    views = {k: v for k, v in (cache.get("views") or {}).items() if k in ids}
    checked = {k: v for k, v in (cache.get("checked") or {}).items() if k in ids}
    denied_at = cache.get("denied_at")
    if denied_at is None or now - float(denied_at) > 86400:   # no insights permission: retry daily
        fails = 0
        for mid in sorted(ids, key=lambda m: checked.get(m, -1.0))[:budget]:
            try:
                j = ig._call("get", f"{mid}/insights", params={"metric": "views"})
            except Exception as e:  # noqa: BLE001 - no permission, or a post type without views
                fails += 1
                if fails >= 3 and not views:
                    cache["denied_at"] = now
                    log.info("instagram insights unavailable (%s): views not counted", str(e)[:120])
                    break
                checked[mid] = now
                continue
            fails = 0
            for row in j.get("data") or []:
                if row.get("name") == "views":
                    vals = row.get("values") or [{}]
                    views[mid] = int((row.get("total_value") or {}).get("value") or vals[0].get("value") or 0)
            checked[mid] = now
    cache["views"], cache["checked"] = views, checked
    if ids and all(m in views for m in ids):
        out["views"] = sum(views.values())
    elif not ids:
        out["views"] = 0
    return out


def instagram_client(cfg: dict, base: Path):
    from . import instagram as ig_
    icfg = ig_.settings(cfg, base)
    token = ig_.Token(icfg["env_path"])
    if not (icfg["user_id"] and token.get()):
        return None
    return ig_.Instagram(icfg["user_id"], token, icfg["api_version"])


# --------------------------------------------------------------------------
# Totals + state
# --------------------------------------------------------------------------
def path_for(data_dir: Path) -> Path:
    return Path(data_dir) / "social_stats.json"


def load(data_dir: Path) -> dict:
    try:
        return json.loads(path_for(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def combine(platforms: dict, now: float) -> dict:
    """{"views": {"total": n, "platforms": ["YT", "IG"]}, ...} from the fresh-enough platforms."""
    out = {}
    for kind in KINDS:
        total, names = 0, []
        for name in ("youtube", "instagram", "tiktok"):
            row = platforms.get(name) or {}
            if kind in row and now - float(row.get("at") or 0) <= STALE_S:
                total += int(row[kind])
                names.append(SHORT[name])
        if names:
            out[kind] = {"total": total, "platforms": names}
    return out


def refresh(cfg: dict, base: Path, data_dir: Path, now: float | None = None,
            yt=None, ig=None) -> dict:
    """Re-read every platform it can, save and return {"platforms", "totals", "updated", ...}."""
    now = time.time() if now is None else now
    base, data_dir = Path(base), Path(data_dir)
    st = load(data_dir)
    plats = st.setdefault("platforms", {})
    errors = {}
    scfg = ((cfg.get("review") or {}).get("stats") or {})

    yt = yt if yt is not None else youtube_client(cfg, base=base)
    if yt is not None:
        try:
            ref = str(scfg.get("youtube_channel") or st.get("youtube_channel") or "").strip()
            if not ref and getattr(yt, "api_key", ""):
                for vid in uploaded_video_ids(base, data_dir)[:3]:
                    ref = yt.channel_of_video(vid)
                    if ref:
                        break
                if not ref and hasattr(yt, "my_channel_id"):
                    ref = yt.my_channel_id()
                if not ref:
                    raise StatsError("which channel? set it with /stats youtube_channel:@yourhandle"
                                     " (or review.stats.youtube_channel)")
            row = yt.totals(ref)
            if row.get("channel"):
                st["youtube_channel"] = row.pop("channel")
            plats["youtube"] = dict(row, at=now)
        except Exception as e:  # noqa: BLE001 - never take the bot down
            errors["youtube"] = str(e)[:200]

    ig = ig if ig is not None else instagram_client(cfg, base)
    if ig is not None:
        try:
            plats["instagram"] = dict(instagram_totals(ig, st.setdefault("ig_cache", {}), now), at=now)
        except Exception as e:  # noqa: BLE001
            errors["instagram"] = str(e)[:200]

    for name, err in errors.items():
        if err != (st.get("errors") or {}).get(name):
            log.warning("social stats %s: %s", name, err)
    st["errors"] = errors
    st["totals"] = combine(plats, now)
    st["updated"] = now
    p = path_for(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    tmp.replace(p)
    return st


def status_text(st: dict) -> str:
    """/stats reply: what each platform reads right now, or why it can't."""
    lines = []
    for name in ("youtube", "instagram"):
        row = (st.get("platforms") or {}).get(name) or {}
        err = (st.get("errors") or {}).get(name)
        label = {"youtube": "▶️ YouTube", "instagram": "📸 Instagram"}[name]
        if err:
            lines.append(f"{label}: ⚠️ {err}")
        elif row:
            nums = ", ".join(f"{k} {int(row[k]):,}" for k in (*KINDS, "videos") if k in row)
            lines.append(f"{label}{' (' + row['title'] + ')' if row.get('title') else ''}: {nums}")
        else:
            lines.append(f"{label}: not set up")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """python -m spikebot.socialstats --config config.json : print the totals once."""
    import argparse
    from .main import apply_env
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args(argv)
    cfg_path = Path(a.config).resolve()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    import os
    apply_env(cfg, list(dict.fromkeys([cfg_path.parent / ".env", Path.cwd() / ".env"])), dict(os.environ))
    dd = Path(cfg.get("data_dir", "data"))
    st = refresh(cfg, cfg_path.parent, dd if dd.is_absolute() else cfg_path.parent / dd)
    for name, row in st["platforms"].items():
        print(f"{name}: " + ", ".join(f"{k} {row[k]:,}" for k in KINDS if k in row))
    for name, err in st["errors"].items():
        print(f"{name}: FAILED {err}")
    for k, row in st["totals"].items():
        print(f"total {k}: {row['total']:,} ({'+'.join(row['platforms'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
