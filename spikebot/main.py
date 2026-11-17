"""GTA 6 Clipped spike bot: listens to chat, logs clip-worthy moments, alerts Discord.

Usage:
    python -m spikebot.main --config config.json            # run forever
    python -m spikebot.main --config config.json --check    # 60s connection test
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from curl_cffi.requests import AsyncSession

from . import instagram as ig_
from . import lacy as lacy_
from . import messages as msg_
from . import review as rv_
from . import status as st_
from . import watchlist as wl_
from .detector import DetectorConfig, Spike, SpikeDetector
from .sources import (TwitchChatPool, TwitchHelix, kick_channel_info, kick_stream_state,
                      kick_stream_vod, run_kick_chat)

log = logging.getLogger("spikebot")
UK = ZoneInfo("Europe/London")


def parse_time(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def hms(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def uk(ts: float) -> str:
    return datetime.fromtimestamp(ts, UK).strftime("%a %d %b %H:%M:%S %Z")


class Store:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = data_dir / "spikes.jsonl"
        self.db = sqlite3.connect(data_dir / "spikes.db")
        self.db.execute("""CREATE TABLE IF NOT EXISTS spikes (
            id INTEGER PRIMARY KEY, platform TEXT, channel TEXT, programme TEXT,
            status TEXT, score REAL, onset REAL, onset_uk TEXT,
            stream_offset_start TEXT, stream_offset_end TEXT,
            clip_start REAL, clip_end REAL, category TEXT, title TEXT, data TEXT)""")
        self.db.commit()

    def save(self, rec: dict) -> None:
        with self.jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.db.execute(
            "INSERT INTO spikes (platform, channel, programme, status, score, onset, onset_uk,"
            " stream_offset_start, stream_offset_end, clip_start, clip_end, category, title, data)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["platform"], rec["channel"], rec["programme"], rec["status"], rec["score"],
             rec["onset"], rec["onset_uk"], rec["stream_offset_start"], rec["stream_offset_end"],
             rec["clip_start"], rec["clip_end"], rec["category"], rec["title"], json.dumps(rec)))
        self.db.commit()

    def candidates_since(self, ts: float) -> list[dict]:
        cur = self.db.execute(
            "SELECT platform, channel, score, onset, stream_offset_start, data FROM spikes"
            " WHERE status = 'candidate' AND onset >= ? ORDER BY onset", (ts,))
        rows = []
        for platform, channel, score, onset, offset, data in cur.fetchall():
            try:
                extra = json.loads(data or "{}")
            except ValueError:
                extra = {}
            rows.append({"platform": platform, "channel": channel, "score": score,
                         "onset": onset, "stream_offset_start": offset,
                         "peak_rate": extra.get("peak_rate"), "baseline": extra.get("baseline"),
                         "vod_url": extra.get("vod_url")})
        return rows


async def post_discord(url: str, payload: str | dict) -> None:
    """Send a webhook message: a payload dict from messages.py, or plain text."""
    if isinstance(payload, str):
        payload = {"content": payload[:1900], "allowed_mentions": msg_.NO_MENTIONS}
    try:
        async with AsyncSession() as s:
            r = await s.post(url, json=payload, timeout=15)
            if r.status_code >= 300:
                log.warning("discord post failed: HTTP %s %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        log.warning("discord post failed: %s", e)


async def post_discord_file(url: str, payload: dict, path: Path) -> None:
    """Send a webhook message with one file attached (multipart/form-data)."""
    boundary = "spikebot" + os.urandom(8).hex()
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\";"
            f" filename=\"{path.name}\"\r\nContent-Type: video/mp4\r\n\r\n").encode()
    try:
        body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        async with AsyncSession() as s:
            r = await s.post(url, data=body, timeout=120,
                             headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            if r.status_code >= 300:
                log.warning("discord upload failed: HTTP %s %s", r.status_code, r.text[:200])
                await post_discord(url, payload)  # e.g. file too big: send the card anyway
    except Exception as e:  # noqa: BLE001
        log.warning("discord upload failed: %s", e)


class Bot:
    def __init__(self, cfg: dict, config_path: str | None = None):
        self.cfg = cfg
        self.det_cfg = DetectorConfig(**cfg.get("detector", {}))
        self.store = Store(Path(cfg.get("data_dir", "data")))
        self.channels = cfg["channels"]
        self.detectors: dict[tuple[str, str], SpikeDetector] = {}
        self.meta: dict[tuple[str, str], dict] = {}
        self.names: dict[tuple[str, str], str] = {}
        self.state: dict[tuple[str, str], dict] = {}
        self.counts: dict[tuple[str, str], int] = {}
        for c in self.channels:
            self._register(c)
        # Twitch chat for every channel shares a few sockets (sources.TwitchChatPool)
        self.pool = TwitchChatPool()
        # launch-day watchlist: auto-discovered GTA streams + cross-streamer ranking
        self.disc = wl_.settings(cfg)
        self.discovered: dict[str, float] = {}  # twitch login -> last seen in the top list
        self.gate = wl_.AlertGate(self.disc["max_alerts_per_hour"], self.disc["rank_window_s"])
        self.required = [s.lower() for s in cfg.get("require_category_contains", [])]
        self.min_score = float(cfg.get("min_score_to_alert", 0))
        self.helix = None
        if cfg.get("twitch_client_id") and cfg.get("twitch_client_secret"):
            self.helix = TwitchHelix(cfg["twitch_client_id"], cfg["twitch_client_secret"])

        # health alerts (#status) and the daily summary (#daily-summary)
        self.data_dir = Path(cfg.get("data_dir", "data"))
        self.status_state = st_.StatusState(self.data_dir / "status_state.json")
        self.notifier = st_.Notifier(cfg.get("status_webhook_url") or None,
                                     self.status_state, post_discord)
        self.summary_url = cfg.get("summary_webhook_url") or None
        self.started_at = time.time()
        self.conn = st_.ConnectionWatch(self.detectors, time.time())
        self.live = st_.LiveWatch()
        self.status_events: asyncio.Queue | None = None

        # Lacy mode: Shorts for the Lacy campaign (spikebot/lacy.py)
        self.lacy = None
        if lacy_.settings(cfg)["enabled"]:
            self.lacy = lacy_.LacyMode(cfg, self.data_dir, config_path)
            if not self.helix:
                log.warning("Lacy mode is on but Twitch keys are missing: no Lacy clips"
                            " (the GTA category can't be checked)")

        # Review queue + Discord approval bot (spikebot/review.py, spikebot/discord_bot.py)
        self.review_bot = None
        rcfg = rv_.settings(cfg)
        if rcfg["enabled"] and rcfg["token"] and config_path:
            from .discord_bot import ReviewBot
            self.review_bot = ReviewBot(cfg, config_path, self.notifier)
            self.review_bot.stats_source = self.stats_snapshot
        elif rcfg["enabled"]:
            log.warning("DISCORD_BOT_TOKEN not set: clips wait in data/review until the bot is set up"
                        " (python -m spikebot.review list)")

        # Instagram Reels: token refresh + Reels waiting for the daily cap (spikebot/instagram.py)
        self.ig = None
        if config_path:
            xp = ig_.crossposter(cfg, Path(config_path).resolve().parent, dict(os.environ))
            if xp.icfg["enabled"] and xp.icfg["user_id"]:
                self.ig = xp

    def _register(self, c: dict) -> tuple[str, str]:
        key = (c["platform"], c["name"].lower())
        self.detectors[key] = SpikeDetector(key[0], key[1], self.det_cfg)
        self.meta[key] = c
        self.names[key] = c.get("display") or c["name"]
        self.state[key] = {}
        self.counts[key] = 0
        return key

    def add_discovered(self, stream: dict, now: float) -> tuple[str, str] | None:
        """Start watching an auto-discovered Twitch stream (chat only, no programme)."""
        login = stream["user_login"].lower()
        key = ("twitch", login)
        if key in self.detectors:
            return None
        key = self._register({"platform": "twitch", "name": login, "programme": None,
                              "display": stream.get("user_name") or login, "discovered": True})
        self.state[key] = {"live": True, "started_at": stream.get("started_at"),
                           "category": stream.get("game_name", ""), "title": stream.get("title"),
                           "stream_id": stream.get("id"), "user_id": stream.get("user_id"),
                           "viewers": stream.get("viewer_count")}
        self.conn.down_since.setdefault(key, now)
        self.discovered[login] = now
        self.pool.add(login, self.on_msg(key), self.on_status(key))
        return key

    def drop_discovered(self, login: str) -> None:
        key = ("twitch", login)
        if not self.meta.get(key, {}).get("discovered"):
            return
        self.pool.remove(login)
        for d in (self.detectors, self.meta, self.names, self.state, self.counts):
            d.pop(key, None)
        self.conn.down_since.pop(key, None)
        self.conn.announced.discard(key)
        self.live.live.pop(key, None)
        self.discovered.pop(login, None)

    async def discovery_loop(self, now=time.time) -> None:
        """Every interval_s: watch the biggest live GTA VI streams on Twitch."""
        game_ids: list[str] = []
        while True:
            try:
                if not game_ids:
                    game_ids = await self.helix.game_ids(self.disc["categories"])
                    if not game_ids:
                        log.warning("discovery: no Twitch category found for %s", self.disc["categories"])
                top = await self.helix.top_streams(game_ids, self.disc["languages"])
                await self.discovery_tick(top, now())
            except Exception as e:  # noqa: BLE001 - never take the bot down
                log.warning("discovery failed: %s", e)
            await asyncio.sleep(self.disc["interval_s"])

    async def discovery_tick(self, top: list[dict], now: float) -> None:
        fixed = {n for p, n in self.meta if p == "twitch" and not self.meta[(p, n)].get("discovered")}
        wl_.seen(self.discovered, top, self.disc, now)
        add, drop = wl_.plan(self.discovered, top, fixed, self.disc, now)
        for login in drop:
            self.drop_discovered(login)
        added = [k for k in (self.add_discovered(t, now) for t in add) if k]
        if added or drop:
            names = ", ".join(f"{self.names[k]} ({self.state[k].get('viewers') or '?'})" for k in added)
            text = (f"👀 Auto-watch: now **{len(self.discovered)}** extra GTA streams."
                    + (f"\nAdded: {msg_.escape(names)}" if added else "")
                    + (f"\nDropped: {msg_.escape(', '.join(drop))}" if drop else ""))
            log.info("discovery: +%s -%s (watching %s extra)", len(added), len(drop), len(self.discovered))
            await self.notifier.send(f"discovery:{now:.0f}", text)

    def on_msg(self, key):
        det = self.detectors[key]

        def cb(ts: float, user: str, text: str) -> None:
            det.add_message(ts, user, text)
            self.counts[key] += 1
        return cb

    async def poll_state(self) -> None:
        """Refresh live status, start time and category every 2 minutes."""
        while True:
            await self.poll_once()
            await asyncio.sleep(120)

    async def poll_once(self) -> None:
        fresh: dict = {}
        twitch = [n for p, n in list(self.detectors) if p == "twitch"]
        if self.helix and twitch:
            try:  # one Helix request per 100 channels
                for login, st in (await self.helix.stream_states(twitch)).items():
                    fresh[("twitch", login)] = st
            except Exception as e:  # noqa: BLE001
                log.debug("twitch state batch failed: %s", e)
        for key in [k for k in list(self.detectors) if k[0] == "kick"]:
            try:
                fresh[key] = kick_stream_state(await kick_channel_info(key[1]))
            except Exception as e:  # noqa: BLE001
                log.debug("state %s/%s failed: %s", *key, e)
        for key, st in fresh.items():
            if key not in self.detectors:  # dropped meanwhile
                continue
            self.state[key] = st
            change = self.live.update(key, st)
            if change and not self.meta[key].get("discovered"):  # no #status spam for auto-watch
                await self.live_alert(key, *change)

    async def ticker(self) -> None:
        last_status = time.time()
        while True:
            now = time.time()
            for key, det in list(self.detectors.items()):
                spike = det.tick(now)
                if spike:
                    await self.handle(key, spike)
            if now - last_status >= 300:
                last_status = now
                busiest = sorted(self.detectors, key=lambda k: -self.detectors[k].last_rate)[:15]
                summary = ", ".join(f"{p}/{n}: {self.detectors[(p, n)].last_rate:.1f}/s"
                                    f" (normal {self.detectors[(p, n)].mean or 0:.1f})"
                                    for p, n in busiest)
                log.info("rates (%s channels, busiest 15): %s", len(self.detectors), summary)
            await asyncio.sleep(max(0.0, 1 - (time.time() - now)))

    async def handle(self, key, spike: Spike) -> None:
        st = self.state.get(key) or {}
        meta = self.meta.get(key)
        if meta is None:  # channel was dropped while the spike was open
            return
        lacy_key = bool(self.lacy and self.lacy.applies(key))
        lacy_ok = False
        if lacy_key:
            # Lacy mode: check his category with Helix Get Streams right now, not
            # the 2-minute poll. The polled value (closer to the chat onset) must
            # also be GTA if we have one.
            polled = st.get("category")
            fresh = await self.fresh_state(key)
            if fresh is not None:
                st = fresh
                self.state[key] = fresh
            lacy_ok = (fresh is not None and fresh.get("live")
                       and self.lacy.category_ok(fresh.get("category"))
                       and (not polled or self.lacy.category_ok(polled)))
        start = parse_time(st.get("started_at"))
        category = st.get("category") or ""
        status = "candidate"
        if self.required and category and not any(r in category.lower() for r in self.required):
            status = "filtered_category"
        elif lacy_key and category and not lacy_ok:
            status = "filtered_category"
        elif spike.score < self.min_score:
            status = "low_score"

        rec = asdict(spike) | {
            "programme": meta.get("programme"),
            "status": status,
            "onset_uk": uk(spike.onset),
            "category": category,
            "title": st.get("title"),
            "stream_offset_start": hms(spike.clip_start - start) if start else None,
            "stream_offset_end": hms(spike.clip_end - start) if start else None,
            "vod_url": None,
        }
        decision = None
        if status == "candidate":
            rec["vod_url"] = await self.vod_link(key, st, spike.clip_start, start)
            decision = self.gate.decide(spike.score, bool(meta.get("programme")), time.time())
            rec["rank"], rec["rank_of"], rec["alerted"] = decision.rank, decision.total, decision.send
        self.store.save(rec)
        log.info("SPIKE %s/%s score=%s status=%s at %s offset=%s",
                 spike.platform, spike.channel, spike.score, status,
                 rec["onset_uk"], rec["stream_offset_start"])
        if decision and decision.send:
            await self.alert(rec)
        elif decision:
            log.info("not alerted (hourly cap for no-programme streamers): %s/%s score=%s rank %s/%s",
                     spike.platform, spike.channel, spike.score, decision.rank, decision.total)
        if lacy_key and lacy_ok and status == "candidate":
            try:
                self.lacy.enqueue(rec, st)
            except Exception as e:  # noqa: BLE001 - never take the bot down
                log.warning("lacy enqueue failed: %s", e)
        elif lacy_key:
            log.info("lacy: no clip for this spike (status=%s, category=%r)", status, category)

    async def fresh_state(self, key) -> dict | None:
        """Live/category from Helix Get Streams at this moment, or None if unknown."""
        if not self.helix:
            return None
        try:
            return await self.helix.stream_state(key[1])
        except Exception as e:  # noqa: BLE001
            log.info("fresh Helix lookup %s failed: %s", key[1], e)
            return None

    async def vod_link(self, key, st: dict, clip_start: float, stream_start: float | None) -> str | None:
        """Link to the VOD at the clip start, or None if it isn't published yet.
        Only a link: nothing is downloaded."""
        platform, name = key
        try:
            if platform == "kick":
                vod = await kick_stream_vod(name, st.get("stream_id"))
            elif self.helix:
                vod = await self.helix.stream_vod(st.get("user_id"), st.get("stream_id"))
            else:
                return None
        except Exception as e:  # noqa: BLE001
            log.info("VOD lookup %s/%s failed: %s", platform, name, e)
            return None
        if not vod or not vod.get("url"):
            return None
        base = parse_time(vod.get("started_at")) or stream_start
        if base is None:
            return vod["url"]
        return vod["url"] + msg_.vod_offset(clip_start - base, platform)

    async def alert(self, rec: dict) -> None:
        url = self.cfg.get("discord_webhook_url")
        if not url:
            return
        key = (rec["platform"], rec["channel"])
        await post_discord(url, msg_.moment(rec, self.names.get(key, rec["channel"])))

    # ---- Lacy mode ---------------------------------------------------------
    async def lacy_failed(self, job: dict) -> None:
        await self.notifier.send("lacy_failed", f"🎬 Lacy clip `{job['id']}` failed:"
                                 f" {msg_.escape(job.get('reason') or 'unknown error')}",
                                 kind="lacy")

    async def instagram_loop(self, interval_s: float = 3600, now=time.time) -> None:
        """Hourly: refresh the IG token when due (every 50 days) and post Lacy Reels that
        waited for the daily cap or failed. Never raises."""
        state = ig_.State(self.data_dir)
        while True:
            try:
                await self.instagram_tick(state, now())
            except Exception as e:  # noqa: BLE001
                log.warning("instagram loop: %s", e)
            await asyncio.sleep(interval_s)

    async def instagram_tick(self, state, now: float) -> None:
        if not ig_.active(self.ig.icfg, self.ig.token):
            return
        res = await asyncio.to_thread(ig_.maybe_refresh, self.ig.icfg, self.ig.token, state, now)
        if res == "unified":  # no refresh endpoint: check it still works once a day
            res = await asyncio.to_thread(ig_.check_token, self.ig.icfg, self.ig.token, state,
                                          self.ig.ig_factory(), now)
            if res.startswith("failed"):
                await self.notifier.send(
                    "instagram_token", f"📸 Instagram token stopped working ({res[8:]}). Generate a new"
                    " one (README: Instagram Reels), put it in .env as IG_ACCESS_TOKEN, then run"
                    " `python -m spikebot.instagram check`.", kind="lacy")
        elif res.startswith("failed"):
            exp = state.load().get("expires_at") or now
            await self.notifier.send(
                "instagram_token", f"📸 Instagram token refresh {res}. It expires about "
                f"{ig_._uk(exp)}. Retrying every 6h; or run `python -m spikebot.instagram refresh`,"
                " or paste a new token into .env as IG_ACCESS_TOKEN.", kind="lacy")
        if not self.lacy:
            return
        subs = lacy_.Submissions(Path(self.lacy.cfg["submissions_csv"]))
        done = await asyncio.to_thread(ig_.retry_pending, self.ig, self.lacy.jobs, subs, None, now)
        for job_id, how in done:
            if how == "failed":
                job = self.lacy.jobs.load(job_id)
                await self.notifier.send(f"instagram_failed:{job_id}",
                                         f"📸 Instagram Reel for Lacy clip `{job_id}` failed"
                                         f" (try {job.get('instagram_attempts')}): {job.get('instagram_error')}",
                                         kind="lacy")

    def lacy_uploads_today(self) -> int:
        subs = lacy_.Submissions(Path(self.lacy.cfg["submissions_csv"]))
        return subs.uploads_on(lacy_.uk_day(time.time()))

    # ---- status alerts -----------------------------------------------------
    def on_status(self, key):
        def cb(event: str, detail: str) -> None:
            now = time.time()
            if event == "connected":
                outage = self.conn.connected(key, now)
                if outage is not None:
                    self._queue("reconnected", key, outage)
            elif event == "disconnected":
                self.conn.disconnected(key, now)
            elif event == "key_error":
                self._queue("key_error", key, detail)
        return cb

    def _queue(self, *event) -> None:
        if self.status_events is not None:
            self.status_events.put_nowait(event)

    def _name(self, key) -> str:
        prog = self.meta[key].get("programme")
        return (f"**{msg_.escape(self.names[key])}** ({key[0].capitalize()})"
                + (f" · {msg_.escape(prog)}" if prog else ""))

    async def live_alert(self, key, change: str, category: str) -> None:
        cat = msg_.escape(category)
        if change == "live":
            started = parse_time(self.state[key].get("started_at")) or time.time()
            text = (f"🟢 {self._name(key)} is **LIVE**: {cat} (since {msg_.ts(started)})\n"
                    f"{msg_.live_url(*key)}")
        else:
            text = f"⚫ {self._name(key)} went offline (was: {cat})"
        await self.notifier.send(f"{change}:{key[0]}/{key[1]}", text)

    async def startup_alert(self) -> None:
        last = self.status_state.data.get("last_seen")
        chans = ", ".join(f"{p}/{n}" for p, n in self.detectors)
        now = time.time()
        if last:
            head = (f"🔄 Spike bot **restarted** on {socket.gethostname()} at {msg_.ts(now)}."
                    f" Previous run last seen {msg_.ts(last)}"
                    f" ({st_.duration(now - last)} ago).")
        else:
            head = f"🚀 Spike bot **started** on {socket.gethostname()} at {msg_.ts(now)}."
        await self.notifier.send("start", f"{head}\nTracking: {chans}")

    async def status_loop(self) -> None:
        self.status_events = asyncio.Queue()
        last_disk = 0.0
        while True:
            try:
                event = await asyncio.wait_for(self.status_events.get(), timeout=30)
            except asyncio.TimeoutError:
                event = None
            now = time.time()
            try:
                if event and event[0] == "reconnected":
                    _, key, outage = event
                    await self.notifier.send(f"reconnected:{key[0]}/{key[1]}",
                                             f"✅ {self._name(key)} chat reconnected"
                                             f" after {st_.duration(outage)}.")
                elif event and event[0] == "key_error":
                    _, key, used = event
                    await self.notifier.send(
                        "kick_key_4001",
                        f"🔑 Kick returned **error 4001** for {self._name(key)}: the chat key"
                        f" `{used}` has rotated. Kick chat is down until kick_pusher_key /"
                        f" kick_pusher_cluster are updated (README: 'If Kick chat shows 0 messages').",
                        kind="key_error")
                for key, since in self.conn.due(now):
                    await self.notifier.send(
                        f"disconnected:{key[0]}/{key[1]}",
                        f"🔴 {self._name(key)} chat has been disconnected for"
                        f" {st_.duration(now - since)} (since {msg_.ts(since)}).")
                if now - last_disk >= 300:
                    last_disk = now
                    free = st_.disk_free_share(self.data_dir)
                    if free < st_.DISK_MIN_FREE:
                        await self.notifier.send(
                            "disk", f"💾 Disk space low: only **{free:.0%} free** on the bot server.")
                self.status_state.data["last_seen"] = now
                self.status_state.save()
            except Exception as e:  # noqa: BLE001 - alerts must never take the bot down
                log.warning("status loop error: %s", e)

    def stats_snapshot(self, now: float) -> dict:
        """For the Discord stats channels: live streamers, moments since UK midnight."""
        from datetime import datetime as _dt
        midnight = _dt.fromtimestamp(now, UK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return {"live": sum(1 for v in self.live.live.values() if v), "watching": len(self.detectors),
                "moments_today": len(self.store.candidates_since(midnight)), "started": self.started_at}

    def summary_message(self, now: float) -> dict:
        return msg_.summary(self.store.candidates_since(now - 86400),
                            list(self.detectors), now, self.names)

    async def summary_loop(self) -> None:
        if st_.summary_due_on_start(time.time(), self.status_state.data.get("last_summary_date")):
            await self.send_summary()
        while True:
            await asyncio.sleep(max(1.0, st_.next_summary_time(time.time()) - time.time()))
            await self.send_summary()
            await asyncio.sleep(60)  # never fire twice for the same 22:00

    async def send_summary(self) -> None:
        now = time.time()
        self.status_state.data["last_summary_date"] = st_.uk_time(now, "%Y-%m-%d")
        self.status_state.save()
        payload = self.summary_message(now)
        log.info("daily summary: %s", msg_.plain(payload))
        if self.summary_url:
            await post_discord(self.summary_url, payload)

    # ---- running -----------------------------------------------------------
    def chat_tasks(self, with_status: bool = True) -> list:
        tasks = []
        for c in self.channels:
            key = (c["platform"], c["name"].lower())
            status = self.on_status(key) if with_status else (lambda e, d: None)
            if c["platform"] == "kick":
                tasks.append(run_kick_chat(c["name"].lower(), self.on_msg(key),
                                           self.cfg.get("kick_pusher_key") or None,
                                           self.cfg.get("kick_pusher_cluster") or None,
                                           c.get("chatroom_id"), on_status=status))
            elif c["platform"] == "twitch":
                self.pool.add(c["name"].lower(), self.on_msg(key), status)
            else:
                raise ValueError(f"unknown platform {c['platform']}")
        tasks.append(self.pool.run())  # also serves channels added later by discovery
        return tasks

    async def run(self) -> None:
        await self.startup_alert()
        extra = []
        if self.lacy:
            extra.append(self.lacy.worker(self.helix, self.lacy_failed))
        if self.review_bot:
            extra.append(self.review_bot.run_forever())
        if self.ig:
            extra.append(self.instagram_loop())
        if self.disc["enabled"]:
            if self.helix:
                extra.append(self.discovery_loop())
            else:
                log.warning("discovery is on but Twitch keys are missing: no auto-watch")
        await asyncio.gather(self.ticker(), self.poll_state(), self.status_loop(),
                             self.summary_loop(), *self.chat_tasks(), *extra)

    async def check(self, seconds: int = 60) -> None:
        tasks = [asyncio.create_task(t) for t in self.chat_tasks(with_status=False)]
        await asyncio.sleep(seconds)
        for t in tasks:
            t.cancel()
        print("\nMessages received in", seconds, "seconds:")
        for (p, n), count in self.counts.items():
            ok = "OK" if count else "NONE (offline, or connection problem: see log above)"
            print(f"  {p}/{n}: {count}  {ok}")
        for key in self.detectors:
            try:
                if key[0] == "kick":
                    st = kick_stream_state(await kick_channel_info(key[1]))
                elif self.helix:
                    st = await self.helix.stream_state(key[1])
                else:
                    continue
                print(f"  {key[0]}/{key[1]} live={st['live']} category={st['category']!r}")
            except Exception as e:  # noqa: BLE001
                print(f"  {key[0]}/{key[1]} info lookup failed: {e}")
        if self.lacy:
            lc = self.lacy.cfg
            print(f"  Lacy mode ON: twitch/{lc['channel']}, categories {lc['allowed_categories']},"
                  f" uploads today {self.lacy_uploads_today()}/{lc['max_uploads_per_day']}"
                  " (every clip needs your approval in Discord)")
            print(f"    ffmpeg: {'OK' if shutil.which('ffmpeg') else 'MISSING (apt install ffmpeg)'}")
            print(f"    yt-dlp: {'OK' if _has('yt_dlp') else 'MISSING (pip install -r requirements.txt)'}")
            print(f"    faster-whisper: {'OK' if _has('faster_whisper') else 'MISSING (pip install -r requirements.txt)'}")
            yt = all(self.cfg.get(k) for k in ("youtube_client_id", "youtube_client_secret",
                                                "youtube_refresh_token"))
            print(f"    YouTube keys: {'set' if yt else 'MISSING (see README: Lacy mode)'}")
            if not self.helix:
                print("    WARNING: Twitch keys missing, so no Lacy clips will be made")
        else:
            print("  Lacy mode off")
        if self.ig:
            ic, tok = self.ig.icfg, self.ig.token
            today = self.ig.posts.published_on(lacy_.uk_day(time.time()))
            print(f"  Instagram auto-post {'ON' if ig_.active(ic, tok) else 'OFF (IG_ACCESS_TOKEN missing)'}:"
                  f" Reels today {today}/{ic['max_per_day']}"
                  "  (full check: python -m spikebot.instagram check)")
        else:
            print("  Instagram auto-post off (IG_USER_ID not set)")
        d = self.disc
        if d["enabled"] and self.helix:
            try:
                ids = await self.helix.game_ids(d["categories"])
                top = await self.helix.top_streams(ids, d["languages"])
                ok = [t for t in top if wl_.eligible(t, d)][:d["max_channels"]]
                print(f"  Auto-watch ON: {len(ok)} live {d['categories']} streams with"
                      f" {d['min_viewers']}+ viewers right now (max {d['max_channels']}):"
                      f" {', '.join(t['user_login'] for t in ok[:10]) or 'none'}")
            except Exception as e:  # noqa: BLE001
                print(f"  Auto-watch ON but the Twitch lookup failed: {e}")
        else:
            print(f"  Auto-watch {'ON but Twitch keys missing' if d['enabled'] else 'off (DISCOVERY_MODE)'}")
        rc = rv_.settings(self.cfg)
        print(f"  Discord approval bot: {'token set' if rc['token'] else 'OFF (DISCORD_BOT_TOKEN missing)'};"
              f" #ready-to-post {rc['channel_id'] or 'NOT SET'}, #tiktok-queue {rc['tiktok_channel_id'] or 'NOT SET'},"
              f" #inspiration {rc['inspiration_channel_id'] or 'NOT SET'}")
        print(f"    discord.py: {'OK' if _has('discord') else 'MISSING (pip install -r requirements.txt)'};"
              f" PySceneDetect: {'OK' if _has('scenedetect') else 'MISSING'};"
              f" easyocr: {'OK' if _has('easyocr') else 'not installed (optional, see README)'}")
        free = st_.disk_free_share(self.data_dir)
        print(f"  disk free: {free:.0%} (status alert below {st_.DISK_MIN_FREE:.0%})")
        preview = msg_.plain(self.summary_message(time.time())).replace(" | ", "\n")
        print("\nDaily summary preview (last 24h):\n" + preview)
        hooks = [("discord_webhook_url", "#moments", "Spike bot connection test: alerts are working."),
                 ("status_webhook_url", "#status", "Spike bot connection test: status alerts are working."),
                 ("summary_webhook_url", "#daily-summary",
                  "Spike bot connection test: the daily summary will post here at 22:00 UK time."),
                 ("videos_webhook_url", "#ready-to-post (old webhook)",
                  "Spike bot connection test: the approval bot now posts clips here.")]
        if not self.cfg.get("videos_webhook_url") and self.cfg.get("tokyo_girl_webhook_url"):
            self.cfg["videos_webhook_url"] = self.cfg["tokyo_girl_webhook_url"]  # old name
        print()
        for field, chan, msg in hooks:
            if self.cfg.get(field):
                await post_discord(self.cfg[field], msg_.status("start", msg))
                print(f"  Sent a test message to {chan} ({field}).")
            else:
                print(f"  {field} not set: {chan} messages are disabled.")


def _has(module: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module) is not None


ENV_KEYS = {"TWITCH_CLIENT_ID": "twitch_client_id",
            "TWITCH_CLIENT_SECRET": "twitch_client_secret",
            "LACY_MODE": "lacy_mode",
            "LACY_MAX_UPLOADS_PER_DAY": "lacy_max_uploads_per_day",
            "LACY_AUTO_UPLOAD": "lacy_auto_upload",  # ignored now: every clip needs approval
            "DISCORD_BOT_TOKEN": "discord_bot_token",
            "DISCORD_REVIEW_CHANNEL_ID": "discord_channel_id",
            "DISCORD_TIKTOK_CHANNEL_ID": "discord_tiktok_channel_id",
            "DISCORD_INSPIRATION_CHANNEL_ID": "discord_inspiration_channel_id",
            "DISCORD_GUILD_ID": "discord_guild_id",
            "DISCORD_APPROVER_IDS": "discord_approver_ids",
            "REVIEW_ENABLED": "review_enabled",
            "TOKYO_GIRL_MODE": "tokyo_girl_mode",
            "TOKYO_GIRL_PER_DAY": "tokyo_girl_per_day",
            "VIDEOS_GCS_BUCKET": "videos_gcs_bucket",
            "TOKYO_GIRL_GCS_BUCKET": "tokyo_girl_gcs_bucket",  # old name
            "YOUTUBE_CLIENT_ID": "youtube_client_id",
            "YOUTUBE_CLIENT_SECRET": "youtube_client_secret",
            "YOUTUBE_REFRESH_TOKEN": "youtube_refresh_token",
            # IG_ACCESS_TOKEN is read straight from .env by instagram.Token (it gets refreshed)
            "IG_USER_ID": "ig_user_id",
            "IG_MAX_REELS_PER_DAY": "ig_max_reels_per_day",
            "INSTAGRAM_AUTO_POST": "instagram_auto_post",
            "DISCOVERY_MODE": "discovery_mode",
            "DISCOVERY_MAX_CHANNELS": "discovery_max_channels"}


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file (comments, blank lines, optional quotes
    and a leading `export ` are allowed). A missing file gives an empty dict."""
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.removeprefix("export ").partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip()] = val
    return out


def apply_env(cfg: dict, env_files: list[Path], environ: dict[str, str]) -> dict:
    """Overlay Twitch/YouTube credentials and LACY_* switches onto the config. Precedence: real environment
    (e.g. systemd EnvironmentFile) > .env files (first found wins) > config.json."""
    file_vals: dict[str, str] = {}
    for f in reversed(env_files):
        file_vals.update(load_dotenv(f))
    for env_key, cfg_key in ENV_KEYS.items():
        val = environ.get(env_key) or file_vals.get(env_key)
        if val:
            cfg[cfg_key] = val
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--check", action="store_true", help="60 second connection test")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg_path = Path(args.config)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    # .env next to config.json, then in the working directory
    env_files = list(dict.fromkeys([cfg_path.resolve().parent / ".env", Path.cwd() / ".env"]))
    apply_env(cfg, env_files, dict(os.environ))
    bot = Bot(cfg, str(cfg_path.resolve()))
    asyncio.run(bot.check() if args.check else bot.run())


if __name__ == "__main__":
    main()
