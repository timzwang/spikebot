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
from urllib.parse import urlencode

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
        "stream_id": ls.get("id"),
    }


async def kick_stream_vod(slug: str, stream_id) -> dict | None:
    """The VOD for live stream `stream_id`: {"url", "started_at"}, or None if Kick
    hasn't published it yet. A link only; nothing is downloaded."""
    if not stream_id:
        return None
    async with AsyncSession(impersonate="chrome") as s:
        r = await s.get(f"https://kick.com/api/v2/channels/{slug}/videos", timeout=20)
        r.raise_for_status()
        items = r.json()
    for it in items if isinstance(items, list) else []:
        uuid = (it.get("video") or {}).get("uuid")
        if uuid and str(it.get("id")) == str(stream_id):
            return {"url": f"https://kick.com/{slug}/videos/{uuid}",
                    "started_at": it.get("start_time") or it.get("created_at")}
    return None


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


def _parse_privmsg_chan(line: str) -> tuple[str, str, str] | None:
    """(channel, user, text) from an IRC PRIVMSG line, channel without '#'."""
    if " PRIVMSG #" not in line:
        return None
    if line.startswith("@"):
        _, line = line.split(" ", 1)
    try:
        prefix, rest = line[1:].split(" PRIVMSG #", 1)
        chan, text = rest.split(" :", 1)
        return chan.strip().lower(), prefix.split("!", 1)[0], text
    except (IndexError, ValueError):
        return None


def _parse_join(line: str) -> str | None:
    """Channel from our own ':justinfanN!... JOIN #chan' confirmation."""
    if " JOIN #" not in line or not line.startswith(":justinfan"):
        return None
    return line.rsplit(" JOIN #", 1)[1].strip().lower()


class JoinLimiter:
    """Shared JOIN budget across all pool connections. Twitch allows anonymous
    users about 20 JOINs per 10 s; we stay under it."""

    def __init__(self, per_window: int = 15, window_s: float = 11.0, clock=time.monotonic):
        self.per_window, self.window_s, self.clock = per_window, window_s, clock
        self.stamps: list[float] = []
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = self.clock()
                self.stamps = [t for t in self.stamps if now - t < self.window_s]
                if len(self.stamps) < self.per_window:
                    self.stamps.append(now)
                    return
                await asyncio.sleep(self.window_s - (now - self.stamps[0]) + 0.05)


class _PoolConn:
    def __init__(self, pool: "TwitchChatPool", idx: int):
        self.pool, self.idx = pool, idx
        self.channels: set[str] = set()
        self.joined: set[str] = set()
        self.ws = None
        self.wake = asyncio.Event()

    async def run(self) -> None:
        url = "wss://irc-ws.chat.twitch.tv:443"
        backoff = 2
        while True:
            try:
                async with websockets.connect(url, ping_interval=None, open_timeout=20,
                                              max_queue=4096) as ws:
                    self.ws, self.joined = ws, set()
                    await ws.send("PASS SCHMOOPIIE")
                    await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
                    log.info("twitch pool #%s connected (%s channels)", self.idx, len(self.channels))
                    backoff = 2
                    sync = asyncio.create_task(self._sync(ws))
                    try:
                        async for raw in ws:
                            for line in raw.split("\r\n"):
                                if line:
                                    await self._line(ws, line)
                    finally:
                        sync.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("twitch pool #%s disconnected (%s); retry in %ss", self.idx, e, backoff)
            for chan in list(self.channels):
                self.pool._status(chan, "disconnected", "pool connection lost")
            self.ws, self.joined = None, set()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    async def _sync(self, ws) -> None:
        """Keep JOINs/PARTs in line with self.channels, within the join budget."""
        while True:
            self.wake.clear()  # before diffing, so an add() during the sends isn't lost
            for chan in sorted(self.channels - self.joined):
                await self.pool.limiter.acquire()
                if chan in self.channels:
                    await ws.send(f"JOIN #{chan}")
                    self.joined.add(chan)
            for chan in sorted(self.joined - self.channels):
                await ws.send(f"PART #{chan}")
                self.joined.discard(chan)
            if self.channels == self.joined:
                await self.wake.wait()

    async def _line(self, ws, line: str) -> None:
        if line.startswith("PING"):
            await ws.send("PONG :tmi.twitch.tv")
            return
        parsed = _parse_privmsg_chan(line)
        if parsed:
            chan, user, text = parsed
            cb = self.pool.subs.get(chan)
            if cb:
                cb[0](time.time(), user, text)
            return
        chan = _parse_join(line)
        if chan and chan in self.channels:
            self.pool._status(chan, "connected", "")


class TwitchChatPool:
    """Anonymous read-only Twitch chat for many channels over a few shared
    websockets (`per_conn` channels each), instead of one socket per channel.
    Channels can be added and removed while running."""

    def __init__(self, per_conn: int = 50, limiter: JoinLimiter | None = None):
        self.per_conn = per_conn
        self.limiter = limiter or JoinLimiter()
        self.subs: dict[str, tuple[ChatCallback, StatusCallback]] = {}
        self.conns: list[_PoolConn] = []
        self.tasks: list[asyncio.Task] = []
        self.running = False

    def _status(self, chan: str, event: str, detail: str) -> None:
        cb = self.subs.get(chan)
        if cb:
            cb[1](event, detail)

    def add(self, channel: str, on_msg: ChatCallback,
            on_status: StatusCallback = _no_status) -> None:
        chan = channel.lower()
        self.subs[chan] = (on_msg, on_status)
        if any(chan in c.channels for c in self.conns):
            return
        conn = next((c for c in self.conns if len(c.channels) < self.per_conn), None)
        if conn is None:
            conn = _PoolConn(self, len(self.conns))
            self.conns.append(conn)
            if self.running:
                self.tasks.append(asyncio.create_task(conn.run()))
        conn.channels.add(chan)
        conn.wake.set()

    def remove(self, channel: str) -> None:
        chan = channel.lower()
        self.subs.pop(chan, None)
        for c in self.conns:
            if chan in c.channels:
                c.channels.discard(chan)
                c.wake.set()

    def channels(self) -> set[str]:
        return set().union(*(c.channels for c in self.conns)) if self.conns else set()

    async def run(self) -> None:
        """Run every connection forever (new ones start as channels are added)."""
        self.running = True
        self.tasks = [asyncio.create_task(c.run()) for c in self.conns]
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            self.running = False
            for t in self.tasks:
                t.cancel()


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

    async def _get(self, path: str, params: dict | list) -> list:
        query = urlencode(params)  # a list of pairs allows repeated keys (user_login=a&user_login=b)
        async with AsyncSession() as s:
            tok = await self._auth(s)
            r = await s.get(f"https://api.twitch.tv/helix/{path}?{query}",
                            headers={"Client-Id": self.client_id, "Authorization": f"Bearer {tok}"},
                            timeout=20)
            r.raise_for_status()
            return r.json().get("data") or []

    async def stream_state(self, login: str) -> dict:
        data = await self._get("streams", {"user_login": login})
        if not data:
            return {"live": False, "started_at": None, "category": "", "title": None}
        d = data[0]
        return {"live": True, "started_at": d.get("started_at"),
                "category": d.get("game_name", ""), "title": d.get("title"),
                "stream_id": d.get("id"), "user_id": d.get("user_id")}

    async def stream_states(self, logins: list[str]) -> dict[str, dict]:
        """stream_state for many channels in one request per 100 logins.
        Every login gets an entry; offline channels get live=False."""
        out = {}
        logins = [l.lower() for l in logins]
        for i in range(0, len(logins), 100):
            chunk = logins[i:i + 100]
            data = await self._get("streams", [("user_login", l) for l in chunk] + [("first", 100)])
            for d in data:
                out[(d.get("user_login") or "").lower()] = {
                    "live": True, "started_at": d.get("started_at"),
                    "category": d.get("game_name", ""), "title": d.get("title"),
                    "stream_id": d.get("id"), "user_id": d.get("user_id"),
                    "viewers": d.get("viewer_count")}
        offline = {"live": False, "started_at": None, "category": "", "title": None}
        return {l: out.get(l, dict(offline)) for l in logins}

    async def game_ids(self, names: list[str]) -> list[str]:
        if not names:
            return []
        return [g["id"] for g in await self._get("games", [("name", n) for n in names])]

    async def top_streams(self, game_ids: list[str], languages: list[str],
                          pages: int = 2) -> list[dict]:
        """Live streams in these categories, most viewers first (Helix order)."""
        if not game_ids:
            return []
        base = [("game_id", g) for g in game_ids] + [("type", "live"), ("first", 100)]
        base += [("language", l) for l in languages]
        out, cursor = [], None
        for _ in range(pages):
            async with AsyncSession() as s:
                tok = await self._auth(s)
                q = base + ([("after", cursor)] if cursor else [])
                r = await s.get(f"https://api.twitch.tv/helix/streams?{urlencode(q)}",
                                headers={"Client-Id": self.client_id,
                                         "Authorization": f"Bearer {tok}"}, timeout=20)
                r.raise_for_status()
                j = r.json()
            out += j.get("data") or []
            cursor = (j.get("pagination") or {}).get("cursor")
            if not cursor:
                break
        return out

    async def stream_vod(self, user_id: str, stream_id: str) -> dict | None:
        """The archive VOD of a live stream: {"url", "started_at"}, or None."""
        if not (user_id and stream_id):
            return None
        for v in await self._get("videos", {"user_id": user_id, "type": "archive", "first": 5}):
            if str(v.get("stream_id")) == str(stream_id):
                return {"url": v.get("url") or f"https://www.twitch.tv/videos/{v['id']}",
                        "started_at": v.get("created_at")}
        return None
