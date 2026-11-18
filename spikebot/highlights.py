"""Gameplay highlights without a chat spike: the loudest moments of the stream audio.

Chat misses things (slow chat, sub-only mode, the streamer reacting before chat does). For every
clip streamer (spikebot/lacy.py profiles) this reads the VOD's *audio only*, a chunk at a time,
and finds moments where the sound jumps well above the last few minutes (shouting, laughing,
crashes, explosions). The best of them become normal clip jobs: same GTA-only check, same
nudity check and review, nothing posted without Approve.

Pure parts (`envelope`, `peaks`) are tested; the loop runs inside the bot (Bot.highlights_loop).
"""
from __future__ import annotations

import json
import logging
import math
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("highlights")

DEFAULTS = {
    "enabled": True,
    "every_s": 600,          # look at new audio every 10 min
    "chunk_s": 1800,         # at most 30 min of audio per look
    "lag_s": 180,            # leave the newest 3 min (the VOD lags behind live)
    "max_per_hour": 2,       # highlights per streamer per hour of stream
    "min_gap_s": 120,        # between highlights, and away from chat spikes
    "z": 3.2,                # how far above the recent level a moment must be
    "min_len_s": 1.5,        # ...for at least this long
}
RATE = 8000                  # Hz: enough to measure loudness
HOP = 0.5                    # one loudness value per half second


def settings(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out.update(cfg.get("highlights") or {})
    if cfg.get("highlights_mode") not in (None, ""):
        out["enabled"] = str(cfg["highlights_mode"]).strip().lower() in {"1", "true", "yes", "on"}
    return out


def envelope(samples, rate: int = RATE, hop: float = HOP) -> list[float]:
    """Loudness (dB of the RMS) per `hop` seconds."""
    import numpy as np
    n = int(rate * hop)
    if len(samples) < n:
        return []
    k = len(samples) // n
    x = np.asarray(samples[: k * n], dtype=np.float32).reshape(k, n)
    rms = np.sqrt((x * x).mean(axis=1)) + 1e-6
    return [float(v) for v in 20 * np.log10(rms)]


def peaks(env: list[float], hop: float = HOP, z: float = 3.2, min_len_s: float = 1.5,
          window_s: float = 180, min_gap_s: float = 120, top: int | None = None) -> list[tuple[float, float]]:
    """[(time s, score)] of moments louder than the surrounding `window_s` by `z` robust
    standard deviations for at least `min_len_s`, best first, `min_gap_s` apart."""
    import numpy as np
    if not env:
        return []
    a = np.asarray(env)
    w = max(3, int(window_s / hop))
    cands = []
    i = 0
    need = max(1, int(min_len_s / hop))
    while i < len(a):
        lo, hi = max(0, i - w), min(len(a), i + w)
        ref = a[lo:hi]
        med = float(np.median(ref))
        mad = float(np.median(np.abs(ref - med))) * 1.4826 or 1.0
        score = (a[i] - med) / mad
        if score >= z:
            j = i
            while j < len(a) and (a[j] - med) / mad >= z * 0.7:
                j += 1
            if j - i >= need:
                seg = a[i:j]
                cands.append(((i + int(np.argmax(seg))) * hop, float((seg.max() - med) / mad)))
            i = max(j, i + 1)
        else:
            i += 1
    cands.sort(key=lambda c: -c[1])
    out: list[tuple[float, float]] = []
    for t, s in cands:
        if all(abs(t - u) >= min_gap_s for u, _ in out):
            out.append((round(t, 1), round(s, 2)))
        if top and len(out) >= top:
            break
    return out


def read_audio(url: str, start_s: float, dur_s: float):
    """Mono float32 samples of [start, start+dur] of a VOD: audio only, never the video."""
    import numpy as np
    p = subprocess.run([sys.executable, "-m", "yt_dlp", "-g", "-f", "bestaudio/best", "--no-warnings", url],
                       capture_output=True, text=True, timeout=120)
    stream = (p.stdout.strip().splitlines() or [""])[0]
    if not stream:
        raise RuntimeError("yt-dlp returned no stream URL")
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{max(0.0, start_s):.1f}",
                        "-i", stream, "-t", f"{dur_s:.1f}", "-vn", "-ac", "1", "-ar", str(RATE),
                        "-f", "f32le", "-"], capture_output=True, timeout=1200)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode(errors="replace")[-300:])
    return np.frombuffer(p.stdout, dtype=np.float32)


class State:
    """data/highlights/state.json: how far each stream's VOD has been scanned."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "highlights" / "state.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        tmp.replace(self.path)


def plan_scan(scanned: float, available: float, chunk_s: float, lag_s: float) -> tuple[float, float] | None:
    """(start, duration) of the next chunk of VOD audio to read, or None if nothing new."""
    end = available - lag_s
    if end - scanned < 60:
        return None
    return scanned, min(chunk_s, end - scanned)


def pick(found: list[tuple[float, float]], offset: float, chat_times: list[float], hours: float,
         max_per_hour: int, min_gap_s: float) -> list[tuple[float, float]]:
    """Highlights to clip from one chunk: away from chat spikes (those are clipped already),
    at most max_per_hour per hour of audio. Times become VOD offsets."""
    out = []
    budget = max(1, math.ceil(hours * max_per_hour))
    for t, s in found:
        at = offset + t
        if any(abs(at - c) < min_gap_s for c in chat_times):
            continue
        out.append((at, s))
        if len(out) >= budget:
            break
    return out
