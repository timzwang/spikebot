"""Minimal YouTube Data API v3 uploader: OAuth refresh token, resumable upload,
custom thumbnail.

One-time setup (writes YOUTUBE_REFRESH_TOKEN into the .env file, never prints it):
    python -m spikebot.youtube auth --env .env
"""
from __future__ import annotations

import argparse
import http.server
import logging
import secrets
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests

log = logging.getLogger("youtube")

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
# upload + read-only (the 📊 Stats panel reads channel totals; tokens made before only upload)
SCOPE = ("https://www.googleapis.com/auth/youtube.upload"
         " https://www.googleapis.com/auth/youtube.readonly")
GAMING = "20"


class YouTubeError(RuntimeError):
    pass


def shorts_url(video_id: str) -> str:
    return f"https://youtube.com/shorts/{video_id}"


class YouTube:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        if not (client_id and client_secret and refresh_token):
            raise YouTubeError("YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN"
                               " are not all set in .env")
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self._token: str | None = None
        self._expires = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = requests.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"}, timeout=30)
        if r.status_code != 200:
            raise YouTubeError(f"token refresh failed: HTTP {r.status_code} {r.text[:200]}")
        j = r.json()
        self._token, self._expires = j["access_token"], time.time() + int(j.get("expires_in", 3600))
        return self._token

    def upload(self, video: Path, title: str, description: str, tags: list[str],
               privacy: str = "public") -> str:
        """Upload a video, return its id."""
        meta = {
            "snippet": {"title": title, "description": description, "tags": tags,
                        "categoryId": GAMING, "defaultLanguage": "en",
                        "defaultAudioLanguage": "en"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False,
                       "embeddable": True},
        }
        data = video.read_bytes()
        r = requests.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                          json=meta, timeout=60, headers={
                              "Authorization": f"Bearer {self.token()}",
                              "X-Upload-Content-Type": "video/mp4",
                              "X-Upload-Content-Length": str(len(data))})
        session = r.headers.get("Location") or r.headers.get("location")
        if r.status_code != 200 or not session:
            raise YouTubeError(f"upload start failed: HTTP {r.status_code} {r.text[:300]}")
        for attempt in range(3):
            r = requests.put(session, data=data, timeout=900, headers={
                "Authorization": f"Bearer {self.token()}", "Content-Type": "video/mp4"})
            if r.status_code in (200, 201):
                return r.json()["id"]
            if r.status_code < 500:
                break
            time.sleep(5 * (attempt + 1))
        raise YouTubeError(f"upload failed: HTTP {r.status_code} {r.text[:300]}")

    def set_thumbnail(self, video_id: str, jpg: Path) -> None:
        r = requests.post(THUMB_URL, params={"videoId": video_id}, data=jpg.read_bytes(),
                          timeout=120, headers={"Authorization": f"Bearer {self.token()}",
                                                "Content-Type": "image/jpeg"})
        if r.status_code != 200:
            raise YouTubeError(f"thumbnail failed: HTTP {r.status_code} {r.text[:300]}")


# --------------------------------------------------------------------------
# One-time OAuth: get a refresh token and store it in .env
# --------------------------------------------------------------------------
def set_env_value(path: Path, key: str, value: str) -> None:
    """Replace or append KEY=value in a .env file, keeping everything else."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.strip().removeprefix("export ").split("=", 1)[0].strip() == key:
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value}")
    tmp = path.with_name(path.name + ".tmp")  # atomic: a crash can't lose the other secrets
    tmp.touch(mode=0o600)
    tmp.chmod(0o600)
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)


def auth(env_path: Path, client_id: str, client_secret: str, port: int = 8765) -> None:
    redirect = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state})
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

        def log_message(self, *a):
            pass

    print("1. Open this link, sign in as the channel's Google account and allow access:\n")
    print(url + "\n")
    print(f"2. Waiting for Google to redirect to {redirect} ...")
    print(f"   (On a server, first run on your laptop: ssh -L {port}:127.0.0.1:{port} YOUR_SERVER)")
    with http.server.HTTPServer(("127.0.0.1", port), Handler) as srv:
        while "code" not in got and "error" not in got:
            srv.handle_request()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"Authorisation failed: {got.get('error', 'state mismatch')}")
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret, "code": got["code"],
        "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
    tok = r.json().get("refresh_token") if r.status_code == 200 else None
    if not tok:
        raise SystemExit(f"No refresh token returned (HTTP {r.status_code}).")
    set_env_value(env_path, "YOUTUBE_REFRESH_TOKEN", tok)
    print(f"Saved YOUTUBE_REFRESH_TOKEN to {env_path} (not shown here).")


def main() -> None:
    from .main import load_dotenv  # local import: keep this module light
    import os

    ap = argparse.ArgumentParser(description="YouTube uploader setup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth", help="get a refresh token and save it to .env")
    a.add_argument("--env", default=".env", help="the .env file to update")
    a.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    env_path = Path(args.env)
    vals = load_dotenv(env_path)
    cid = os.environ.get("YOUTUBE_CLIENT_ID") or vals.get("YOUTUBE_CLIENT_ID")
    sec = os.environ.get("YOUTUBE_CLIENT_SECRET") or vals.get("YOUTUBE_CLIENT_SECRET")
    if not (cid and sec):
        raise SystemExit(f"Put YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in {env_path} first.")
    auth(env_path, cid, sec, args.port)


if __name__ == "__main__":
    main()
