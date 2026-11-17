"""Launch-day watchlist: auto-discovered GTA streams and cross-streamer alert ranking.

Pure logic, no networking, so it is testable with explicit timestamps.

- `settings(cfg)`: the `discovery` config block (+ DISCOVERY_MODE from .env).
- `plan()`: which top live GTA streams to start / stop watching.
- `AlertGate`: ranks every candidate moment against the last hour across all
  streamers and caps #moments alerts for streamers without a programme.

Discovered streamers are chat-only: the bot reads their public chat and posts a
VOD link, nothing is downloaded, and every card says "No programme: check
permission before posting" (they never have one).
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass

TRUE = {"1", "true", "yes", "on"}

DEFAULTS = {
    "enabled": False,
    "categories": ["Grand Theft Auto VI"],  # exact Twitch category names
    "languages": ["en"],                    # [] = any language
    "min_viewers": 3000,
    "max_channels": 40,                     # discovered channels on top of config.json ones
    "interval_s": 300,                      # how often to re-check the top streams
    "drop_after_s": 1800,                   # stop watching once out of the top for this long
    "exclude": [],                          # Twitch logins never to auto-watch
    "max_alerts_per_hour": 12,              # #moments cap for no-programme streamers
    "rank_window_s": 3600,
}


def settings(cfg: dict) -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    out.update(cfg.get("discovery") or {})
    if cfg.get("discovery_mode") not in (None, ""):
        out["enabled"] = str(cfg["discovery_mode"]).strip().lower() in TRUE
    if cfg.get("discovery_max_channels") not in (None, ""):
        out["max_channels"] = int(cfg["discovery_max_channels"])
    out["exclude"] = [x.lower() for x in out["exclude"]]
    out["languages"] = [x.lower() for x in out["languages"]]
    return out


def eligible(stream: dict, s: dict) -> bool:
    """A Helix stream qualifies for auto-watch."""
    cats = {c.casefold() for c in s["categories"]}
    return ((stream.get("game_name") or "").casefold() in cats
            and int(stream.get("viewer_count") or 0) >= s["min_viewers"]
            and (not s["languages"] or (stream.get("language") or "").lower() in s["languages"])
            and (stream.get("user_login") or "").lower() not in s["exclude"])


def plan(watched: dict[str, float], top: list[dict], fixed: set[str], s: dict,
         now: float) -> tuple[list[dict], list[str]]:
    """Decide discovery changes.

    watched: discovered login -> last time it was seen in the eligible top list
             (the caller updates these times from the return value via `seen`).
    top:     Helix streams (any order); fixed: logins already in config.json.
    Returns (streams to add, logins to drop). Adds are biggest audience first and
    never exceed max_channels, counting channels kept from before.
    """
    good = sorted((t for t in top if eligible(t, s)),
                  key=lambda t: -int(t.get("viewer_count") or 0))
    in_top = {t["user_login"].lower() for t in good}
    drop = [n for n, last in watched.items()
            if n not in in_top and now - last >= s["drop_after_s"]]
    kept = len(watched) - len(drop)
    add = []
    for t in good:
        login = t["user_login"].lower()
        if login in watched or login in fixed:
            continue
        if kept + len(add) >= s["max_channels"]:
            break
        add.append(t)
    return add, drop


def seen(watched: dict[str, float], top: list[dict], s: dict, now: float) -> None:
    """Refresh last-seen times for watched channels still in the eligible top list."""
    for t in top:
        login = (t.get("user_login") or "").lower()
        if login in watched and eligible(t, s):
            watched[login] = now


@dataclass
class Decision:
    send: bool
    rank: int    # 1 = best moment across all streamers in the window
    total: int   # moments in the window, this one included


class AlertGate:
    """Cross-streamer ranking for #moments.

    Every candidate is ranked against all candidates in the last `rank_window_s`.
    Streamers with a programme are always alerted. Others are alerted while fewer
    than `max_alerts_per_hour` of them went out in the window; after that only a
    moment that beats every one already sent in the window gets through."""

    def __init__(self, max_per_hour: int = 12, window_s: float = 3600):
        self.max_per_hour = max_per_hour
        self.window_s = window_s
        self.seen: deque[tuple[float, float]] = deque()   # (ts, score), all candidates
        self.sent: deque[tuple[float, float]] = deque()   # (ts, score), no-programme alerts sent

    def _trim(self, now: float) -> None:
        for q in (self.seen, self.sent):
            while q and q[0][0] < now - self.window_s:
                q.popleft()

    def decide(self, score: float, permitted: bool, now: float) -> Decision:
        self._trim(now)
        rank = 1 + sum(1 for _, s in self.seen if s > score)
        self.seen.append((now, score))
        total = len(self.seen)
        if permitted:
            return Decision(True, rank, total)
        if self.max_per_hour <= 0 or len(self.sent) < self.max_per_hour:
            send = True
        else:
            send = score > max(s for _, s in self.sent)
        if send:
            self.sent.append((now, score))
        return Decision(send, rank, total)
