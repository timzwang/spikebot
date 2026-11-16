"""Live chat readers for Kick and Twitch, plus stream info lookups.

Both readers are read-only and anonymous: they only listen to public chat,
the same way a logged-out viewer's browser does. Neither downloads video.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import AsyncIterator, Callable

import websockets
from curl_cffi.requests import AsyncSession

log = logging.getLogger("sources")

ChatCallback = Callable[[float, str, str], None]  # (unix_ts, user, text)
# (event, detail): event is "connected", "disconnected" or "key_error"
StatusCallback = Callable[[str, str], None]


def _no_status(event: str, detail: str) -> None:
    pass


# --------------------------------------------------------------------------
# Kick
# --------------------------------------------------------------------------
async def kick_channel_info(slug: str) -> dict:
    """Kick's internal v2 channel endpoint (unofficial, Cloudflare-protected,
    so we impersonate a browser TLS fingerprint)."""
    async with AsyncSession(impersonate="chrome") as s:
        r = await s.get(f"https://kick.com/api/v2/channels/{slug}", timeout=20)
        r.raise_for_status()
        return r.json()


def kick_stream_state(info: dict) -> dict:
    ls = info.get("livestream") or {}
    cats = ls.get("categories") or []
    if not cats and ls.get("category"):
        cats = [ls["category"]]
    return {
        "live": bool(ls) and ls.get("is_live", True),
        "started_at": ls.get("start_time") or ls.get("created_at"),
        "category": ", ".join(c.get("name", "") for c in cats if isinstance(c, dict)),
        "title": ls.get("session_title"),
    }


# Kick rotates its chat key. 32cbd69e... was retired in October 2026.
RETIRED_KICK_KEYS = {"32cbd69e4b950bf97679"}
FALLBACK_KICK_PUSHER = ("34bf7a0ff419a2a775b9", "us2")
_KEY_RE = re.compile(r"^[a-z0-9]{10,40}$")
_CLUSTER_RE = re.compile(r"^[a-z0-9-]{2,20}$")


def _find_pusher_creds(obj) -> tuple[str, str] | None:
    """Walk Kick's realtime descriptor and pull out a Pusher key + cluster,
    whatever exact shape the JSON is in."""
    if isinstance(obj, dict):
        key = obj.get("app_key") or obj.get("key")
        cluster = obj.get("cluster")
        if (isinstance(key, str) and isinstance(cluster, str)
                and _KEY_RE.match(key) and _CLUSTER_RE.match(cluster)):
            return key, cluster
        for v in obj.values():
            found = _find_pusher_creds(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_pusher_creds(v)
            if found:
                return found
    return None


async def kick_pusher_credentials() -> tuple[str, str]:
    """Ask Kick for its current chat key, the same way kick.com's own page does.
    Offer only the Pusher provider, otherwise Kick answers with Centrifugo."""
    try:
        async with AsyncSession(impersonate="chrome") as s:
            r = await s.post("https://web.kick.com/api/v1/realtime/connection",
                             json={"accepted_providers": ["pusher"]}, timeout=15)
            r.raise_for_status()
            found = _find_pusher_creds(r.json())
            if found:
                return found
            log.warning("kick realtime descriptor had no pusher key; using fallback")
    except Exception as e:  # noqa: BLE001
        log.warning("kick key lookup failed (%s); using fallback", e)
    return FALLBACK_KICK_PUSHER


def _pusher_error_code(data) -> int | None:
    """Pusher error payloads arrive as a dict or a JSON string: {"code": 4001, ...}."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    code = data.get("code") if isinstance(data, dict) else None
    return code if isinstance(code, int) else None


def _close_code(exc: BaseException) -> int | None:
    """Websocket close code from a websockets ConnectionClosed error, if any."""
    for frame in (getattr(exc, "rcvd", None), getattr(exc, "sent", None)):
        if frame is not None and getattr(frame, "code", None):
            return frame.code
    return None


async def run_kick_chat(slug: str, on_msg: ChatCallback, pusher_key: str | None = None,
                        cluster: str | None = None, chatroom_id: int | None = None,
                        on_status: StatusCallback = _no_status) -> None:
    """Listen to a Kick chatroom forever, reconnecting on errors.
    With no key (or a retired one) set, the current key is fetched on every connect."""
    fixed = pusher_key if pusher_key and pusher_key not in RETIRED_KICK_KEYS else None
    key = fixed or "?"
    backoff = 2
    while True:
        try:
            key, clus = (fixed, cluster or "us2") if fixed else await kick_pusher_credentials()
            url = (f"wss://ws-{clus}.pusher.com/app/{key}"
                   "?protocol=7&client=js&version=8.4.0&flash=false")
            if chatroom_id is None:
                info = await kick_channel_info(slug)
                chatroom_id = int(info["chatroom"]["id"])
                log.info("kick/%s chatroom id %s", slug, chatroom_id)
            async with websockets.connect(url, ping_interval=None, open_timeout=20) as ws:
                async for raw in ws:
                    msg = json.loads(raw)
                    ev = msg.get("event", "")
                    if ev == "pusher:connection_established":
                        await ws.send(json.dumps({
                            "event": "pusher:subscribe",
                            "data": {"auth": "", "channel": f"chatrooms.{chatroom_id}.v2"},
                        }))
                    elif ev == "pusher_internal:subscription_succeeded":
                        log.info("kick/%s subscribed (key %s, cluster %s)", slug, key, clus)
                        on_status("connected", "")
                        backoff = 2
                    elif ev == "pusher:ping":
                        await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                    elif ev.endswith("ChatMessageEvent"):
                        data = msg.get("data")
                        if isinstance(data, str):
                            data = json.loads(data)
                        user = (data.get("sender") or {}).get("username", "?")
                        on_msg(time.time(), user, data.get("content", ""))
                    elif ev == "pusher:error":
                        log.warning("kick/%s pusher error: %s", slug, msg.get("data"))
                        if _pusher_error_code(msg.get("data")) == 4001:
                            on_status("key_error", key)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - keep listening whatever happens
            log.warning("kick/%s disconnected (%s); retry in %ss", slug, e, backoff)
            if _close_code(e) == 4001:
                on_status("key_error", key)
            on_status("disconnected", str(e)[:200])
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


# --------------------------------------------------------------------------
# Twitch
# --------------------------------------------------------------------------
def _parse_privmsg(line: str) -> tuple[str, str] | None:
    if " PRIVMSG " not in line:
        return None
    if line.startswith("@"):
        _, line = line.split(" ", 1)
    try:
        prefix, rest = line[1:].split(" PRIVMSG ", 1)
        user = prefix.split("!", 1)[0]
        text = rest.split(" :", 1)[1]
        return user, text
    except (IndexError, ValueError):
        return None


async def run_twitch_chat(channel: str, on_msg: ChatCallback,
                          on_status: StatusCallback = _no_status) -> None:
    """Anonymous read-only Twitch IRC over websocket, reconnecting forever."""
    url = "wss://irc-ws.chat.twitch.tv:443"
    backoff = 2
    while True:
        try:
            async with websockets.connect(url, ping_interval=None, open_timeout=20) as ws:
                await ws.send("PASS SCHMOOPIIE")
                await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
                await ws.send(f"JOIN #{channel.lower()}")
                log.info("twitch/%s joined", channel)
                backoff = 2
                async for raw in ws:
                    for line in raw.split("\r\n"):
                        if not line:
                            continue
                        if line.startswith("PING"):
                            await ws.send("PONG :tmi.twitch.tv")
                            continue
                        if f" JOIN #{channel.lower()}" in line:
                            on_status("connected", "")
                        parsed = _parse_privmsg(line)
                        if parsed:
                            on_msg(time.time(), *parsed)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("twitch/%s disconnected (%s); retry in %ss", channel, e, backoff)
            on_status("disconnected", str(e)[:200])
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


class TwitchHelix:
    """Optional: stream start time and category via the official Helix API.
    Needs a free Twitch developer app (client id + secret)."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id, self.client_secret = client_id, client_secret
        self._token: str | None = None
        self._expires = 0.0

    async def _auth(self, s: AsyncSession) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = await s.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "client_credentials"}, timeout=20)
        r.raise_for_status()
        j = r.json()
        self._token, self._expires = j["access_token"], time.time() + j["expires_in"]
        return self._token

    async def stream_state(self, login: str) -> dict:
        async with AsyncSession() as s:
            tok = await self._auth(s)
            r = await s.get("https://api.twitch.tv/helix/streams", params={"user_login": login},
                            headers={"Client-Id": self.client_id, "Authorization": f"Bearer {tok}"},
                            timeout=20)
            r.raise_for_status()
            data = r.json().get("data") or []
        if not data:
            return {"live": False, "started_at": None, "category": "", "title": None}
        d = data[0]
        return {"live": True, "started_at": d.get("started_at"),
                "category": d.get("game_name", ""), "title": d.get("title")}
