"""Bot health alerts and the daily summary.

The trackers here are pure logic driven by explicit timestamps so they can be
tested without a network or a real clock. `Bot` in main.py wires them to the
chat readers and posts the results to Discord.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("status")
UK = ZoneInfo("Europe/London")

ALERT_COOLDOWN_S = 30 * 60   # never repeat the same alert more often than this
DISCONNECT_ALERT_S = 5 * 60  # a channel must be down this long before we alert
DISK_MIN_FREE = 0.20         # alert when free space drops below this share
SUMMARY_HOUR_UK = 22         # daily summary at 22:00 UK time

Poster = Callable[[str, str], Awaitable[None]]  # (webhook_url, content)


def uk_time(ts: float, fmt: str = "%a %d %b %H:%M %Z") -> str:
    return datetime.fromtimestamp(ts, UK).strftime(fmt)


def duration(seconds: float) -> str:
    s = int(max(seconds, 0))
    h, m = s // 3600, s % 3600 // 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m" if m else f"{s}s"


# --------------------------------------------------------------------------
# Persistent state (survives restarts, so a crash loop can't spam alerts)
# --------------------------------------------------------------------------
class StatusState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"sent": {}, "last_seen": None, "last_summary_date": None}
        try:
            self.data.update(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("status state unreadable (%s); starting fresh", e)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save status state: %s", e)


class Notifier:
    """Posts alerts to one webhook, dropping repeats of the same alert key
    within `cooldown_s`."""

    def __init__(self, url: str | None, state: StatusState, poster: Poster,
                 cooldown_s: float = ALERT_COOLDOWN_S, clock: Callable[[], float] = time.time):
        self.url, self.state, self.poster = url, state, poster
        self.cooldown_s, self.clock = cooldown_s, clock

    def allow(self, key: str) -> bool:
        """True (and records the send) if `key` hasn't fired within the cooldown."""
        now = self.clock()
        last = self.state.data["sent"].get(key)
        if last is not None and now - last < self.cooldown_s:
            return False
        self.state.data["sent"][key] = now
        # forget stale keys so the state file doesn't grow forever
        self.state.data["sent"] = {k: t for k, t in self.state.data["sent"].items()
                                   if now - t < 7 * 86400}
        self.state.save()
        return True

    async def send(self, key: str, text: str) -> bool:
        if not self.allow(key):
            log.info("status alert %r suppressed (sent within %ds)", key, self.cooldown_s)
            return False
        log.info("STATUS %s", text.replace("\n", " | "))
        if self.url:
            await self.poster(self.url, text)
        return True


# --------------------------------------------------------------------------
# Trackers
# --------------------------------------------------------------------------
class ConnectionWatch:
    """Tracks chat connections. A channel counts as down from its first
    disconnect (or from startup, if it never connected) until it reconnects."""

    def __init__(self, keys, now: float, threshold_s: float = DISCONNECT_ALERT_S):
        self.threshold_s = threshold_s
        self.down_since: dict = {k: now for k in keys}
        self.announced: set = set()

    def connected(self, key, now: float) -> float | None:
        """Returns the outage length if a disconnect alert went out for it."""
        since = self.down_since.pop(key, None)
        if key in self.announced:
            self.announced.discard(key)
            return now - since if since is not None else 0.0
        return None

    def disconnected(self, key, now: float) -> None:
        self.down_since.setdefault(key, now)

    def due(self, now: float) -> list[tuple]:
        """Channels newly down for longer than the threshold: [(key, down_since)]."""
        out = []
        for key, since in self.down_since.items():
            if key not in self.announced and now - since >= self.threshold_s:
                self.announced.add(key)
                out.append((key, since))
        return out


class LiveWatch:
    """Turns periodic stream-state polls into went-live / went-offline events."""

    def __init__(self):
        self.live: dict = {}
        self.category: dict = {}

    def update(self, key, state: dict) -> tuple[str, str] | None:
        live = bool(state.get("live"))
        prev = self.live.get(key)
        self.live[key] = live
        cat = state.get("category") or ""
        if live and cat:
            self.category[key] = cat
        if live and prev is not True:
            return "live", cat or "unknown"
        if not live and prev is True:
            return "offline", self.category.get(key) or "unknown"
        return None


def disk_free_share(path: Path) -> float:
    u = shutil.disk_usage(path)
    return u.free / u.total


# --------------------------------------------------------------------------
# Daily summary
# --------------------------------------------------------------------------
def next_summary_time(now: float, hour: int = SUMMARY_HOUR_UK) -> float:
    """Next HH:00 UK time strictly after `now`, correct across clock changes."""
    local = datetime.fromtimestamp(now, UK)
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target = (target.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=UK)
    return target.timestamp()


def summary_due_on_start(now: float, last_date: str | None, hour: int = SUMMARY_HOUR_UK) -> bool:
    """If the bot was down at 22:00, catch up when it comes back before midnight."""
    local = datetime.fromtimestamp(now, UK)
    return local.hour >= hour and last_date != local.date().isoformat()


def build_summary(rows: list[dict], channels: list[tuple[str, str]], now: float,
                  window_s: float = 86400) -> str:
    """rows: candidate moments in the window, each with platform, channel, score,
    onset_uk and stream_offset_start. channels: every tracked (platform, name)."""
    counts = {c: 0 for c in channels}
    for r in rows:
        k = (r["platform"], r["channel"])
        counts[k] = counts.get(k, 0) + 1
    start, end = uk_time(now - window_s, "%a %H:%M"), uk_time(now, "%a %H:%M %Z")
    lines = [f"**Daily summary** ({start} to {end})",
             f"Moments sent to #moments: **{len(rows)}**", ""]
    for (p, n), c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"• {p}/{n}: {c}")
    top = sorted(rows, key=lambda r: r["score"], reverse=True)[:3]
    lines += ["", "**Top 3 by score**"]
    if not top:
        lines.append("No moments today.")
    for i, r in enumerate(top, 1):
        lines.append(f"{i}. {r['platform']}/{r['channel']}  score **{r['score']}**  "
                     f"{r['onset_uk']}, stream time {r.get('stream_offset_start') or '?'}")
    return "\n".join(lines)[:1900]
