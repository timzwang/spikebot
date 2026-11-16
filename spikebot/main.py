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
import socket
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from curl_cffi.requests import AsyncSession

from . import messages as msg_
from . import status as st_
from .detector import DetectorConfig, Spike, SpikeDetector
from .sources import (TwitchHelix, kick_channel_info, kick_stream_state, kick_stream_vod,
                      run_kick_chat, run_twitch_chat)

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


class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.det_cfg = DetectorConfig(**cfg.get("detector", {}))
        self.store = Store(Path(cfg.get("data_dir", "data")))
        self.channels = cfg["channels"]
        self.detectors = {
            (c["platform"], c["name"].lower()): SpikeDetector(c["platform"], c["name"].lower(), self.det_cfg)
            for c in self.channels
        }
        self.meta = {(c["platform"], c["name"].lower()): c for c in self.channels}
        self.names = {k: c.get("display") or c["name"] for k, c in self.meta.items()}
        self.state: dict[tuple[str, str], dict] = {k: {} for k in self.detectors}
        self.counts = {k: 0 for k in self.detectors}
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
        self.conn = st_.ConnectionWatch(self.detectors, time.time())
        self.live = st_.LiveWatch()
        self.status_events: asyncio.Queue | None = None

    def on_msg(self, key):
        det = self.detectors[key]

        def cb(ts: float, user: str, text: str) -> None:
            det.add_message(ts, user, text)
            self.counts[key] += 1
        return cb

    async def poll_state(self) -> None:
        """Refresh live status, start time and category every 2 minutes."""
        while True:
            for key in self.detectors:
                platform, name = key
                try:
                    if platform == "kick":
                        self.state[key] = kick_stream_state(await kick_channel_info(name))
                    elif self.helix:
                        self.state[key] = await self.helix.stream_state(name)
                    else:
                        continue
                except Exception as e:  # noqa: BLE001
                    log.debug("state %s/%s failed: %s", platform, name, e)
                    continue
                change = self.live.update(key, self.state[key])
                if change:
                    await self.live_alert(key, *change)
            await asyncio.sleep(120)

    async def ticker(self) -> None:
        last_status = time.time()
        while True:
            now = time.time()
            for key, det in self.detectors.items():
                spike = det.tick(now)
                if spike:
                    await self.handle(key, spike)
            if now - last_status >= 300:
                last_status = now
                summary = ", ".join(f"{p}/{n}: {self.detectors[(p, n)].last_rate:.1f}/s"
                                    f" (normal {self.detectors[(p, n)].mean or 0:.1f})"
                                    for p, n in self.detectors)
                log.info("rates: %s", summary)
            await asyncio.sleep(max(0.0, 1 - (time.time() - now)))

    async def handle(self, key, spike: Spike) -> None:
        st = self.state.get(key) or {}
        meta = self.meta[key]
        start = parse_time(st.get("started_at"))
        category = st.get("category") or ""
        status = "candidate"
        if self.required and category and not any(r in category.lower() for r in self.required):
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
        if status == "candidate":
            rec["vod_url"] = await self.vod_link(key, st, spike.clip_start, start)
        self.store.save(rec)
        log.info("SPIKE %s/%s score=%s status=%s at %s offset=%s",
                 spike.platform, spike.channel, spike.score, status,
                 rec["onset_uk"], rec["stream_offset_start"])
        if status == "candidate":
            await self.alert(rec)

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
                tasks.append(run_twitch_chat(c["name"].lower(), self.on_msg(key),
                                             on_status=status))
            else:
                raise ValueError(f"unknown platform {c['platform']}")
        return tasks

    async def run(self) -> None:
        await self.startup_alert()
        await asyncio.gather(self.ticker(), self.poll_state(), self.status_loop(),
                             self.summary_loop(), *self.chat_tasks())

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
        free = st_.disk_free_share(self.data_dir)
        print(f"  disk free: {free:.0%} (status alert below {st_.DISK_MIN_FREE:.0%})")
        preview = msg_.plain(self.summary_message(time.time())).replace(" | ", "\n")
        print("\nDaily summary preview (last 24h):\n" + preview)
        hooks = [("discord_webhook_url", "#moments", "Spike bot connection test: alerts are working."),
                 ("status_webhook_url", "#status", "Spike bot connection test: status alerts are working."),
                 ("summary_webhook_url", "#daily-summary",
                  "Spike bot connection test: the daily summary will post here at 22:00 UK time.")]
        print()
        for field, chan, msg in hooks:
            if self.cfg.get(field):
                await post_discord(self.cfg[field], msg_.status("start", msg))
                print(f"  Sent a test message to {chan} ({field}).")
            else:
                print(f"  {field} not set: {chan} messages are disabled.")


ENV_KEYS = {"TWITCH_CLIENT_ID": "twitch_client_id",
            "TWITCH_CLIENT_SECRET": "twitch_client_secret"}


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
    """Overlay Twitch credentials onto the config. Precedence: real environment
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
    bot = Bot(cfg)
    asyncio.run(bot.check() if args.check else bot.run())


if __name__ == "__main__":
    main()
