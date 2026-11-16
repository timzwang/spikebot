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
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from curl_cffi.requests import AsyncSession

from .detector import DetectorConfig, Spike, SpikeDetector
from .sources import (TwitchHelix, kick_channel_info, kick_stream_state,
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
        self.state: dict[tuple[str, str], dict] = {k: {} for k in self.detectors}
        self.counts = {k: 0 for k in self.detectors}
        self.required = [s.lower() for s in cfg.get("require_category_contains", [])]
        self.min_score = float(cfg.get("min_score_to_alert", 0))
        self.helix = None
        if cfg.get("twitch_client_id") and cfg.get("twitch_client_secret"):
            self.helix = TwitchHelix(cfg["twitch_client_id"], cfg["twitch_client_secret"])

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
                except Exception as e:  # noqa: BLE001
                    log.debug("state %s/%s failed: %s", platform, name, e)
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
        }
        self.store.save(rec)
        log.info("SPIKE %s/%s score=%s status=%s at %s offset=%s",
                 spike.platform, spike.channel, spike.score, status,
                 rec["onset_uk"], rec["stream_offset_start"])
        if status == "candidate":
            await self.alert(rec)

    async def alert(self, rec: dict) -> None:
        url = self.cfg.get("discord_webhook_url")
        if not url:
            return
        prog = rec["programme"] or "NO PROGRAMME: check permission before posting"
        lines = [
            f"**{rec['platform']}/{rec['channel']}**  score **{rec['score']}**",
            f"When: {rec['onset_uk']}",
            f"Stream time: {rec['stream_offset_start'] or '?'} to {rec['stream_offset_end'] or '?'}",
            f"Chat: {rec['peak_rate']}/s vs normal {rec['baseline']}/s,"
            f" {rec['unique_users']} chatters, {rec['clip_mentions']} said 'clip'",
            f"Category: {rec['category'] or 'unknown'}",
            f"Programme: {prog}",
            "Chat said: " + " | ".join(rec["sample"][:5])[:600],
        ]
        try:
            async with AsyncSession() as s:
                await s.post(url, json={"content": "\n".join(lines)[:1900]}, timeout=15)
        except Exception as e:  # noqa: BLE001
            log.warning("discord alert failed: %s", e)

    def chat_tasks(self) -> list:
        tasks = []
        for c in self.channels:
            key = (c["platform"], c["name"].lower())
            if c["platform"] == "kick":
                tasks.append(run_kick_chat(c["name"].lower(), self.on_msg(key),
                                           self.cfg.get("kick_pusher_key") or None,
                                           self.cfg.get("kick_pusher_cluster") or None,
                                           c.get("chatroom_id")))
            elif c["platform"] == "twitch":
                tasks.append(run_twitch_chat(c["name"].lower(), self.on_msg(key)))
            else:
                raise ValueError(f"unknown platform {c['platform']}")
        return tasks

    async def run(self) -> None:
        await asyncio.gather(self.ticker(), self.poll_state(), *self.chat_tasks())

    async def check(self, seconds: int = 60) -> None:
        tasks = [asyncio.create_task(t) for t in self.chat_tasks()]
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
        if self.cfg.get("discord_webhook_url"):
            async with AsyncSession() as s:
                await s.post(self.cfg["discord_webhook_url"],
                             json={"content": "Spike bot connection test: alerts are working."})
            print("  Sent a test message to Discord.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--check", action="store_true", help="60 second connection test")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    bot = Bot(cfg)
    asyncio.run(bot.check() if args.check else bot.run())


if __name__ == "__main__":
    main()
