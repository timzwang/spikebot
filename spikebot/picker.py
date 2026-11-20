"""Which chat moments become clips: the BEST-rated ones of each streamer's day, not the first ones
(Tim, 20 Nov: "don't just generate them by order, do the best rated clips of that day").

Each streamer gets `cap` clips per UK day (lacy.max_renders_per_day, 10). A moment's rating is its
spike score (detector: how far chat surged above normal, hype emotes, "clip it" mentions, unique
chatters). Rather than rendering moments as they come, every job waits in `standby` and is
released by `decide`:

  * go now      its score clears the streamer's usual top-`cap` bar (from the last days' moments),
                and fewer than the remaining slots are better and still waiting;
  * go later    no bar yet (no history): after HOLD_S it goes if it ranks in the top third of the
                remaining slots among the moments waiting; once the stream is over (offline), or
                late in the UK evening, the best waiting ones fill whatever slots are left;
  * drop        the day's slots are full, or the stream is over and it isn't among the best left.

Pure: jobs are plain dicts (lacy job.json), times are explicit.
"""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")
HOLD_S = 20 * 60              # a moment waits this long for better ones before going without a bar
HISTORY_DAYS = 7
LATE_HOUR = 23                # from 23:00 UK the day's leftovers are filled best-first
RECHECK_S = 5 * 60            # how often a waiting job is looked at again
WAITING = ("queued", "standby", "waiting_vod")
NOT_COUNTED = ("skipped", "failed", "cancelled", "dropped")   # a slot used by these is free again


def uk_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UK).strftime("%Y-%m-%d")


def _key(j: dict) -> tuple[str, str]:
    return (j.get("platform") or "twitch", str(j.get("channel") or "lacy").lower())


def _rank_key(j: dict) -> tuple[float, float]:
    return (-float(j.get("score") or 0), float(j.get("onset") or 0))   # higher score first, then earlier


def bar(history: list[dict], cap: int, today: str) -> float | None:
    """Score a moment needs to be one of a typical day's top `cap`: the (1 - cap/per_day) quantile of
    the last HISTORY_DAYS days' moments. None without at least one earlier day."""
    days: dict[str, list[float]] = {}
    for j in history:
        d = uk_day(float(j.get("onset") or 0))
        if d < today:
            days.setdefault(d, []).append(float(j.get("score") or 0))
    recent = sorted(days)[-HISTORY_DAYS:]
    if not recent:
        return None
    scores = sorted(s for d in recent for s in days[d])
    per_day = len(scores) / len(recent)
    if per_day <= cap:
        return scores[0]   # a normal day has fewer moments than slots: every moment qualifies
    q = 1 - cap / per_day
    return scores[min(len(scores) - 1, int(math.floor(q * len(scores))))]


def decide(job: dict, jobs: list[dict], cap: int, now: float, live: bool | None = True,
           hold_s: float = HOLD_S) -> tuple[str, str]:
    """("go" | "wait" | "drop", why) for a job whose VOD delay has passed. `jobs` = every job
    (any streamer, any day); `live` = is the streamer still live (None = unknown: treated as live)."""
    key, day = _key(job), uk_day(float(job.get("onset") or now))
    mine = [j for j in jobs if _key(j) == key]
    today = [j for j in mine if uk_day(float(j.get("onset") or 0)) == day]
    taken = sum(1 for j in today if j.get("selected") and j.get("status") not in NOT_COUNTED
                and j.get("id") != job.get("id"))
    left = cap - taken
    if left <= 0:
        return "drop", f"today's {cap} clips are already made (best-rated first)"
    waiting = [j for j in today if not j.get("selected") and j.get("status") in WAITING
               and float(j.get("ready_at") or 0) <= now]
    if job not in waiting and all(j.get("id") != job.get("id") for j in waiting):
        waiting.append(job)
    waiting.sort(key=_rank_key)
    rank = next(i for i, j in enumerate(waiting) if j.get("id") == job.get("id"))
    score = float(job.get("score") or 0)
    over = live is False or datetime.fromtimestamp(now, UK).hour >= LATE_HOUR or uk_day(now) != day
    b = bar(mine, cap, day)
    if rank >= left:
        if over:
            return "drop", f"not among the {cap} best-rated moments of the day (rank {rank + 1})"
        return "wait", f"{rank} better moment(s) waiting for {left} slot(s)"
    if b is not None and score >= b:
        return "go", f"score {score:.1f} clears today's top-{cap} bar ({b:.1f})"
    if over:
        return "go", f"stream over: one of the best {left} left (rank {rank + 1})"
    waited = now - float(job.get("ready_at") or now)
    if b is None and waited >= hold_s and rank < max(1, math.ceil(left / 3)):
        return "go", f"waited {waited / 60:.0f} min, still rank {rank + 1} of the moments waiting"
    why = f"score {score:.1f} below the top-{cap} bar ({b:.1f})" if b is not None else "waiting for better moments"
    return "wait", why + ": made later if it's still one of the day's best"
