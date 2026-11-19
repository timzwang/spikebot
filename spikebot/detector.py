"""Chat-spike detection.

Feeds on individual chat messages and decides when chat volume has jumped far
enough above that channel's own normal level to mark a clip-worthy moment.

Pure logic, no networking, so it can be tested with simulated chat.
"""
from __future__ import annotations

import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field

# Words and emotes that signal hype, laughter or shock in chat.
HYPE_RE = re.compile(
    r"(\bKEKW\b|\bOMEGALUL\b|\bLULW?\b|\bLMF?AO+\b|\bLO+L\b|\bICANT\b|\bHOLY\b|\bWTF\b"
    r"|\bNO+ WAY\b|\bNA+H+\b|\bW+\b|\bINSANE\b|\bPOG\w*\b|\bmonkaS\b|\bO{3,}\b|\bBRO+\b"
    r"|💀|😂|🤣|😭|🔥)",
    re.IGNORECASE,
)
CLIP_RE = re.compile(r"\bclip(ped|\s+it|\s+that)?\b", re.IGNORECASE)
# Chat that is busy but not reacting to a moment (spikebot/impact.py decides what to do with it):
# answers to a question / poll the streamer asked ("1", "2", "yes", "b"), bot commands and
# giveaway entries ("!enter", "!bet"), and subs / gifts / raids / greetings.
ANSWER_RE = re.compile(
    r"^\s*(?:(\d)\1{0,2}|\d{1,2}|[a-dA-D]|ye[sp]+|yea+h?|y|n|no|nope|left|right|first|second|third|"
    r"option\s*\w|team\s*\w+|[+-]1)\s*[!.?]*\s*$", re.IGNORECASE)
COMMAND_RE = re.compile(r"^\s*!\w+")
EVENT_RE = re.compile(
    r"\b(gift(ed|ing|s)?|subs?|subbed|subscri\w*|resub\w*|prime|raid(ed|ers?|ing)?|giveaway|donat\w*|"
    r"bits|cheer\d*|hype ?train|welcome( in| back)?|hi+|hello+|hey+|o7|gn|good ?night)\b", re.IGNORECASE)


def chat_kind(text: str, hype: bool = False) -> str | None:
    """"answer" / "command" / "event" for chat that isn't a reaction, else None. A message
    with a hype word or emote is always a reaction."""
    if hype:
        return None
    if COMMAND_RE.match(text):
        return "command"
    if ANSWER_RE.match(text):
        return "answer"
    if EVENT_RE.search(text) and len(text) <= 60:
        return "event"
    return None
KICK_EMOTE_RE = re.compile(r"\[emote:\d+:([^\]]+)\]")


# Spelling variants that mean the same thing collapse to one word for the alert.
_VIBE_FAMILIES = [
    (re.compile(r"^LO+L$"), "LOL"), (re.compile(r"^LMF?AO+$"), "LMAO"),
    (re.compile(r"^LULW?$"), "LUL"), (re.compile(r"^NO+ WAY$"), "NO WAY"),
    (re.compile(r"^NA+H+$"), "NAH"), (re.compile(r"^W+$"), "W"), (re.compile(r"^POG\w*$"), "POG"),
    (re.compile(r"^O{3,}$"), "OOO"), (re.compile(r"^BRO+$"), "BRO"), (re.compile(r"^MONKAS$"), "monkaS"),
]


def vibe_word(token: str) -> str:
    """Canonical form of a hype match: LOOOL -> LOL, lmfaooo -> LMAO, 😭 stays 😭."""
    t = re.sub(r"\s+", " ", token).upper()
    for rx, word in _VIBE_FAMILIES:
        if rx.match(t):
            return word
    return t


def normalise(text: str) -> str:
    """Turn Kick emote markup like [emote:123:KEKW] into plain KEKW."""
    return KICK_EMOTE_RE.sub(lambda m: f" {m.group(1)} ", text).strip()


@dataclass
class DetectorConfig:
    window_s: int = 10              # trailing window used to measure chat rate
    baseline_halflife_s: float = 600.0  # how fast "normal" adapts (10 min)
    warmup_s: int = 180             # learn the baseline before alerting
    min_rate: float = 1.0           # msgs/sec floor so dead chats never trigger
    ratio: float = 2.5              # rate must be this many times normal...
    z: float = 3.0                  # ...and this many std devs above normal
    end_ratio: float = 1.4          # spike ends when rate falls under normal x this
    cooldown_s: int = 45            # quiet period after a spike ends
    max_spike_s: int = 120          # force-close very long spikes
    pre_roll_s: int = 30            # clip starts this long before chat reacted
    post_roll_s: int = 15           # clip runs at least this long after reaction
    max_clip_s: int = 75


@dataclass
class Spike:
    platform: str
    channel: str
    onset: float          # unix time chat started surging
    peak_time: float
    end: float
    peak_rate: float      # msgs/sec
    baseline: float       # normal msgs/sec before the spike
    zscore: float
    messages: int
    unique_users: int
    hype_share: float
    clip_mentions: int
    score: float
    clip_start: float     # suggested clip window, unix time
    clip_end: float
    sample: list[str] = field(default_factory=list)
    vibe: list[str] = field(default_factory=list)  # top hype words/emotes, variants merged
    # share of messages that answered the streamer / were commands / were subs, raids, greetings
    answer_share: float = 0.0
    command_share: float = 0.0
    event_share: float = 0.0


@dataclass
class _Msg:
    ts: float
    user: str
    text: str
    hype: bool
    clip: bool
    kind: str | None = None


class SpikeDetector:
    def __init__(self, platform: str, channel: str, cfg: DetectorConfig | None = None):
        self.platform = platform
        self.channel = channel
        self.cfg = cfg or DetectorConfig()
        self._window: deque[_Msg] = deque()
        self._alpha = 1 - 0.5 ** (1 / self.cfg.baseline_halflife_s)
        self._alpha_fast = 1 - 0.5 ** (1 / 30)  # learn quickly during warm-up
        self.mean: float | None = None
        self.var: float = 0.0
        self._start: float | None = None
        self._cooldown_until = 0.0
        self._active: dict | None = None
        self.last_rate = 0.0

    # ---- input -------------------------------------------------------------
    def add_message(self, ts: float, user: str, text: str) -> None:
        clean = normalise(text)
        hype = bool(HYPE_RE.search(clean))
        msg = _Msg(ts, user.lower(), clean, hype, bool(CLIP_RE.search(clean)), chat_kind(clean, hype))
        self._window.append(msg)
        if self._active is not None:
            a = self._active
            a["messages"] += 1
            a["users"].add(msg.user)
            a["hype"] += msg.hype
            a["clip"] += msg.clip
            if msg.kind:
                a["kinds"][msg.kind] += 1
            if msg.hype:
                a["vibe"].update({vibe_word(w) for w in HYPE_RE.findall(clean)})
            if len(clean) <= 80:
                a["texts"][clean] += 1

    # ---- clock -------------------------------------------------------------
    def tick(self, now: float) -> Spike | None:
        """Call once a second. Returns a Spike when one has just finished."""
        cfg = self.cfg
        if self._start is None:
            self._start = now
        while self._window and self._window[0].ts < now - cfg.window_s:
            self._window.popleft()
        rate = len(self._window) / cfg.window_s
        self.last_rate = rate

        if now - self._start < cfg.window_s:
            return None  # window not full yet, rate would read too low
        if self.mean is None:
            self.mean = rate
            return None

        std = max(math.sqrt(self.var), 0.15 * self.mean, 0.3)
        z = (rate - self.mean) / std

        if self._active is not None:
            a = self._active
            if rate > a["peak_rate"]:
                a["peak_rate"], a["peak_time"], a["z"] = rate, now, z
            ended = rate < max(cfg.min_rate * 0.5, a["baseline"] * cfg.end_ratio)
            if ended or now - a["onset"] >= cfg.max_spike_s:
                return self._close(now)
            return None

        # Not in a spike: maybe start one, otherwise keep learning "normal".
        warmed = now - self._start >= cfg.warmup_s
        threshold = max(cfg.min_rate, self.mean * cfg.ratio)
        if warmed and now >= self._cooldown_until and rate >= threshold and z >= cfg.z:
            self._open(now, rate, z)
            return None

        alpha = self._alpha if warmed else self._alpha_fast
        d = rate - self.mean
        self.mean += alpha * d
        self.var = (1 - alpha) * (self.var + alpha * d * d)
        return None

    # ---- internals ---------------------------------------------------------
    def _open(self, now: float, rate: float, z: float) -> None:
        msgs = list(self._window)
        texts: Counter[str] = Counter(m.text for m in msgs if len(m.text) <= 80)
        self._active = {
            "onset": now - self.cfg.window_s / 2,
            "baseline": self.mean,
            "peak_rate": rate,
            "peak_time": now,
            "z": z,
            "messages": len(msgs),
            "users": {m.user for m in msgs},
            "hype": sum(m.hype for m in msgs),
            "clip": sum(m.clip for m in msgs),
            "kinds": Counter(m.kind for m in msgs if m.kind),
            "texts": texts,
            "vibe": Counter(w for m in msgs if m.hype
                            for w in {vibe_word(t) for t in HYPE_RE.findall(m.text)}),
        }

    def _close(self, now: float) -> Spike:
        cfg, a = self.cfg, self._active
        self._active = None
        self._cooldown_until = now + cfg.cooldown_s

        n = max(a["messages"], 1)
        hype_share = a["hype"] / n
        uniqueness = min(1.0, max(0.2, len(a["users"]) / n))
        base = max(a["baseline"], 0.2)
        ratio = a["peak_rate"] / base
        score = (
            math.log2(max(ratio, 1.0))
            * math.sqrt(a["peak_rate"])
            * (1 + hype_share)
            * (1 + min(a["clip"], 20) / 20)
            * uniqueness
        )

        clip_start = a["onset"] - cfg.pre_roll_s
        clip_end = max(a["onset"] + cfg.post_roll_s, a["peak_time"] + 5)
        clip_end = min(clip_end, clip_start + cfg.max_clip_s)

        return Spike(
            platform=self.platform,
            channel=self.channel,
            onset=a["onset"],
            peak_time=a["peak_time"],
            end=now,
            peak_rate=round(a["peak_rate"], 2),
            baseline=round(a["baseline"], 2),
            zscore=round(a["z"], 1),
            messages=a["messages"],
            unique_users=len(a["users"]),
            hype_share=round(hype_share, 2),
            clip_mentions=a["clip"],
            score=round(score, 1),
            clip_start=clip_start,
            clip_end=clip_end,
            sample=[t for t, _ in a["texts"].most_common(8)],
            vibe=[w for w, _ in a["vibe"].most_common(4)],
            answer_share=round(a["kinds"]["answer"] / n, 2),
            command_share=round(a["kinds"]["command"] / n, 2),
            event_share=round(a["kinds"]["event"] / n, 2),
        )
