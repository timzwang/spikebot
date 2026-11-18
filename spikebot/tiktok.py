"""TikTok Content Posting API: approved videos go to TikTok without opening the app.

Two ways in (TikTok's own rules decide which one works):
  * direct  - posted straight to the profile (scope video.publish). Until TikTok has audited
              the app, the API only allows private posts; then this falls back to the inbox.
  * inbox   - sent to the TikTok app as a draft notification (scope video.upload): open
              TikTok, tap the notification, add the sound, post. Silent edits (Tokyo Girl,
              trending sounds) always go this way: the sound must be added in the app for the
              edit to count on the sound's page.

Setup (once): create an app at developers.tiktok.com with Login Kit + Content Posting API,
scopes user.info.basic, video.upload, video.publish, and a redirect URI you control
(TIKTOK_REDIRECT_URI). Put TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET in .env, then
    python -m spikebot.tiktok auth --env .env
opens the consent link and saves TIKTOK_REFRESH_TOKEN (never printed). Refresh tokens last a
year; each refresh can return a new one, which is saved straight back to .env.

CLI:  python -m spikebot.tiktok auth | check | post VIDEO --caption TEXT [--inbox]
"""
from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

log = logging.getLogger("tiktok")

API = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = API + "/v2/oauth/token/"
SCOPES = "user.info.basic,video.upload,video.publish"
INBOX_SCOPES = "user.info.basic,video.upload"   # TIKTOK_MODE=inbox: no Direct Post needed
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024
DONE = {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}


class TikTokError(RuntimeError):
    pass


def _http():
    from curl_cffi import requests
    return requests


def chunks(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) by TikTok's rules: one chunk up to 64 MB, otherwise
    10 MB chunks with the remainder folded into the last one."""
    if size <= MAX_CHUNK:
        return size, 1
    n = max(1, size // CHUNK)
    return CHUNK, n


def settings(cfg: dict, env: dict | None = None) -> dict:
    env = env or {}
    t = cfg.get("tiktok") or {}
    get = lambda k, d="": env.get(k) or cfg.get(k.lower()) or t.get(k.lower().removeprefix("tiktok_")) or d  # noqa: E731
    return {"client_key": get("TIKTOK_CLIENT_KEY"), "client_secret": get("TIKTOK_CLIENT_SECRET"),
            "refresh_token": get("TIKTOK_REFRESH_TOKEN"), "redirect_uri": get("TIKTOK_REDIRECT_URI"),
            "mode": (get("TIKTOK_MODE", "direct") or "direct").lower(),       # direct | inbox
            "privacy": get("TIKTOK_PRIVACY", "PUBLIC_TO_EVERYONE"),
            "max_per_day": int(get("TIKTOK_MAX_PER_DAY", "15") or 15)}


def configured(tcfg: dict) -> bool:
    return bool(tcfg.get("client_key") and tcfg.get("client_secret") and tcfg.get("refresh_token"))


class TikTok:
    def __init__(self, tcfg: dict, env_path: Path | None = None, http=None, sleep=time.sleep):
        if not configured(tcfg):
            raise TikTokError("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET / TIKTOK_REFRESH_TOKEN not all set in .env"
                              " (python -m spikebot.tiktok auth)")
        self.cfg, self.env_path = tcfg, env_path
        self.http = http or _http()
        self.sleep = sleep
        self._token, self._expires = None, 0.0

    # ---- auth ------------------------------------------------------------------
    def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = self.http.post(TOKEN_URL, data={
            "client_key": self.cfg["client_key"], "client_secret": self.cfg["client_secret"],
            "grant_type": "refresh_token", "refresh_token": self.cfg["refresh_token"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        j = _json(r)
        if r.status_code != 200 or not j.get("access_token"):
            raise TikTokError(f"token refresh failed: HTTP {r.status_code} {j.get('error_description') or j.get('error') or ''}")
        self._token, self._expires = j["access_token"], time.time() + int(j.get("expires_in", 86400))
        new = j.get("refresh_token")
        if new and new != self.cfg["refresh_token"]:
            self.cfg["refresh_token"] = new
            if self.env_path:   # rotated: keep the new one (never printed)
                from .youtube import set_env_value
                set_env_value(self.env_path, "TIKTOK_REFRESH_TOKEN", new)
        return self._token

    def _post(self, path: str, body: dict) -> dict:
        r = self.http.post(API + path, data=json.dumps(body), timeout=60, headers={
            "Authorization": f"Bearer {self.token()}", "Content-Type": "application/json; charset=UTF-8"})
        j = _json(r)
        err = (j.get("error") or {})
        if r.status_code != 200 or (err.get("code") not in (None, "ok")):
            raise TikTokError(f"{path}: HTTP {r.status_code} {err.get('code')} {err.get('message', '')[:200]}")
        return j.get("data") or {}

    # ---- posting -----------------------------------------------------------------
    def creator(self) -> dict:
        return self._post("/v2/post/publish/creator_info/query/", {})

    def _upload(self, url: str, video: Path) -> None:
        data = video.read_bytes()
        size = len(data)
        csize, n = chunks(size)
        for i in range(n):
            a = i * csize
            b = size if i == n - 1 else a + csize
            r = self.http.put(url, data=data[a:b], timeout=900, headers={
                "Content-Type": "video/mp4", "Content-Length": str(b - a),
                "Content-Range": f"bytes {a}-{b - 1}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise TikTokError(f"upload chunk {i + 1}/{n} failed: HTTP {r.status_code}")

    def status(self, publish_id: str, wait_s: float = 300) -> dict:
        end = time.monotonic() + wait_s
        while True:
            d = self._post("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
            st = d.get("status")
            if st in DONE:
                return d
            if st == "FAILED":
                raise TikTokError(f"TikTok rejected the video: {d.get('fail_reason') or 'unknown reason'}")
            if time.monotonic() > end:
                return d   # still processing: TikTok finishes it on its own
            self.sleep(5)

    def post(self, video: Path, caption: str, inbox: bool = False) -> dict:
        """Post (or send to the inbox). Returns {"status", "publish_id", "url", "how"}."""
        size = video.stat().st_size
        csize, n = chunks(size)
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": csize, "total_chunk_count": n}
        how = "inbox" if inbox or self.cfg.get("mode") == "inbox" else "direct"
        if how == "direct":
            options = self.creator().get("privacy_level_options") or []
            privacy = self.cfg.get("privacy") or "PUBLIC_TO_EVERYONE"
            if privacy not in options:
                # not audited yet (only SELF_ONLY allowed): a private post helps nobody, use the inbox
                log.info("TikTok direct post can't be %s (allowed: %s): sending to the inbox", privacy, options)
                how = "inbox"
        if how == "direct":
            d = self._post("/v2/post/publish/video/init/", {
                "post_info": {"title": caption[:2200], "privacy_level": privacy, "disable_duet": False,
                              "disable_comment": False, "disable_stitch": False,
                              "video_cover_timestamp_ms": 1000},
                "source_info": source})
        else:
            d = self._post("/v2/post/publish/inbox/video/init/", {"source_info": source})
        pid, url = d.get("publish_id"), d.get("upload_url")
        if not pid or not url:
            raise TikTokError("TikTok gave no upload address")
        self._upload(url, video)
        st = self.status(pid)
        post_ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
        link = (f"https://www.tiktok.com/video/{post_ids[0]}" if post_ids else
                "sent to your TikTok inbox: open TikTok, tap the notification, add the sound and post"
                if how == "inbox" else "posted (TikTok is still processing it)")
        return {"status": st.get("status"), "publish_id": pid, "url": link, "how": how}


def _json(r) -> dict:
    try:
        return r.json() or {}
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------
# One-time consent: saves TIKTOK_REFRESH_TOKEN to .env
# --------------------------------------------------------------------------
def auth(env_path: Path, tcfg: dict, ask=input, http=None) -> None:
    if not (tcfg.get("client_key") and tcfg.get("client_secret") and tcfg.get("redirect_uri")):
        raise SystemExit("Put TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI in .env first"
                         " (the redirect URI must match the one in your TikTok app).")
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": tcfg["client_key"], "scope": INBOX_SCOPES if tcfg.get("mode") == "inbox" else SCOPES,
        "response_type": "code",
        "redirect_uri": tcfg["redirect_uri"], "state": state})
    print("1. Open this link, log in as the TikTok account and allow access:\n")
    print(url + "\n")
    print("2. TikTok then opens your redirect page. Copy that page's FULL address from the browser bar")
    back = ask("   and paste it here: ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(back).query)
    if (q.get("state") or [""])[0] != state or not q.get("code"):
        raise SystemExit(f"Authorisation failed: {(q.get('error_description') or q.get('error') or ['state mismatch'])[0]}")
    http = http or _http()
    r = http.post(TOKEN_URL, data={"client_key": tcfg["client_key"], "client_secret": tcfg["client_secret"],
                                   "code": q["code"][0], "grant_type": "authorization_code",
                                   "redirect_uri": tcfg["redirect_uri"]},
                  headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    tok = _json(r).get("refresh_token")
    if not tok:
        raise SystemExit(f"No refresh token returned (HTTP {r.status_code}).")
    from .youtube import set_env_value
    set_env_value(env_path, "TIKTOK_REFRESH_TOKEN", tok)
    print(f"Saved TIKTOK_REFRESH_TOKEN to {env_path} (not shown here).")


def main(argv: list[str] | None = None) -> int:
    from .main import load_dotenv
    import os
    ap = argparse.ArgumentParser(description="TikTok posting")
    ap.add_argument("--env", default=".env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="one-time consent; saves TIKTOK_REFRESH_TOKEN")
    sub.add_parser("check", help="test the token and show what this app may post")
    p = sub.add_parser("post")
    p.add_argument("video")
    p.add_argument("--caption", default="")
    p.add_argument("--inbox", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env_path = Path(args.env)
    tcfg = settings({}, {**load_dotenv(env_path), **{k: v for k, v in os.environ.items() if k.startswith("TIKTOK_")}})
    if args.cmd == "auth":
        auth(env_path, tcfg)
        return 0
    tt = TikTok(tcfg, env_path)
    if args.cmd == "check":
        info = tt.creator()
        print(f"OK: @{info.get('creator_username', '?')} · privacy options {info.get('privacy_level_options')}"
              f" · max video {info.get('max_video_post_duration_sec', '?')} s")
        if "PUBLIC_TO_EVERYONE" not in (info.get("privacy_level_options") or []):
            print("Public direct posts need TikTok's app audit; until then videos go to your inbox as drafts.")
        return 0
    res = tt.post(Path(args.video), args.caption, inbox=args.inbox)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
