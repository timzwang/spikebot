"""#inspiration: measure the *style* of a reference Short and save it as a preset.

Input is a TikTok / Instagram / YouTube link (fetched with yt-dlp) or a file Tim
uploads. The video is only measured: it is deleted as soon as the analysis is
done and none of its footage, audio or wording is ever used in our edits. The
preset keeps numbers (shot length, grade, text size/position, hook *type*...).

Measured:
  pacing      shot detection (PySceneDetect; ffmpeg scene filter as fallback):
              average shot length, cuts per second
  music-sync  tempo from the audio onset envelope; share of cuts on the beat
  colour      ffmpeg signalstats: saturation, contrast, brightness, tint -> grade
  text        OCR (easyocr, optional) on a few frames: position, size, colour,
              outline, words per screen, the hook line
  structure   hook length, where the payoff lands (loudest moment), total length,
              ending (question / CTA / loop)
  motion      zoom and shake (OpenCV optical flow), flash / fade transitions
  look        with ANTHROPIC_API_KEY: Claude studies ~8 small frames plus these numbers
              and writes down the craft (opening, rhythm, how text is or isn't used,
              mood, techniques), and picks the closest text level / logo intro / film
              look for our editor (spikebot/ai.py). Frames are never stored.

CLI (run by the Discord bot as a subprocess):
  python -m spikebot.inspiration analyse (--url URL | --file PATH) --name NAME
        [--copy "pacing, colour"] [--json result.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from . import editor
from . import presets as presets_
from . import style as st

log = logging.getLogger("inspiration")

ALLOWED_HOSTS = ("tiktok.com", "instagram.com", "youtube.com", "youtu.be")
MAX_SECONDS = 240
URL_RE = re.compile(r"https?://[^\s<>]+")
REF_SAT = 32.0      # signalstats SATAVG of ordinary, ungraded footage
REF_RANGE = 200.0   # YHIGH - YLOW of ordinary footage
REF_Y = 112.0


class InspirationError(RuntimeError):
    pass


# What kind of video the reference is decides what it teaches (Tim, 19 Nov):
#   edit         a montage cut from footage, usually to music -> Tokyo Girl + trend edits (all parts)
#   stream_clip  a moment from a livestream (streamer's voice, webcam, speech captions) -> stream
#                clips (Lacy, Adin Ross, Seal); pacing = clip length, no framing / beat sync
#   other        anything else (vlog, podcast, news, meme...) -> kept for reference only: never
#                rotated into B automatically, only used with /preset apply
KINDS = ("edit", "stream_clip", "other")
KIND_MODES = {"edit": ["tokyo_girl", "trend"], "stream_clip": ["lacy"], "other": []}
KIND_PARTS = {"edit": presets_.PARTS,
              "stream_clip": ("pacing", "colour", "text", "transitions", "hook", "structure", "look"),
              "other": ("colour", "text", "look")}
KIND_LABEL = {"edit": "an **edit** → used for Tokyo Girl and trend edits",
              "stream_clip": "a **stream clip** → used for stream clips (Lacy, Adin Ross, Seal)",
              "other": "**another kind of video** → saved for reference only (use it with `/preset apply`)"}
KIND_WORDS = {"edit": "edit", "edits": "edit", "montage": "edit", "trailer": "edit", "trend": "edit",
              "clip": "stream_clip", "clips": "stream_clip", "stream": "stream_clip", "stream_clip": "stream_clip",
              "stream-clip": "stream_clip", "streamclip": "stream_clip", "other": "other", "reference": "other"}


def kind_from_note(note: str) -> str | None:
    """Tim's own word wins: `type: edit` / `type: clip` / `type: other` (also "kind:")."""
    m = re.search(r"(?i)\b(?:type|kind)\s*:\s*([a-z_\- ]+)", note or "")
    if not m:
        return None
    for w in re.split(r"[\s,]+", m.group(1).strip().lower()):
        if w in KIND_WORDS:
            return KIND_WORDS[w]
    return None


def guess_kind(a: dict, facecam: bool | None = None) -> tuple[str, str]:
    """(kind, why) from the measurements alone (no AI): a webcam that stays put = a stream
    clip; fast or beat-synced cutting = an edit; anything else = other. Pure."""
    p, m = a.get("pacing") or {}, a.get("music") or {}
    if facecam:
        return "stream_clip", "a webcam stays in the same spot all the way through"
    if p.get("cuts_per_s", 0) >= 0.5 or (m.get("on_beat", 0) >= 0.6 and p.get("cuts", 0) >= 6):
        return "edit", f"{p.get('cuts_per_s', 0)} cuts a second" + (", cut on the beat" if m.get("on_beat", 0) >= 0.6 else "")
    if p.get("avg_shot_s", 0) >= 4 and a.get("audio") and (a.get("text") or {}).get("words_per_screen", 9) <= 4:
        return "stream_clip", "long takes with talking and short word captions"
    return "other", "slow cutting and no webcam"


def has_facecam(path: Path) -> bool | None:
    """None when the face model can't run here."""
    try:
        from . import safety
        return safety.find_facecam(path) is not None
    except Exception as e:  # noqa: BLE001 - a bonus check, never fails an analysis
        log.info("facecam check skipped: %s", e)
        return None


def classify(a: dict, art: dict | None, forced: str | None = None, facecam=has_facecam,
             path: Path | None = None) -> tuple[str, str]:
    """(kind, why): Tim's `type:` > Claude's read of the frames > the measurements."""
    if forced in KINDS:
        return forced, "you said so"
    if art and art.get("kind") in KINDS:
        return art["kind"], art.get("kind_why") or "Claude's read of the frames"
    return guess_kind(a, facecam(path) if path is not None and facecam else None)


def clip_pace(length_s: float) -> str:
    """A stream clip's pace is how long it runs (our clip window), not its shot length."""
    return "fast" if length_s <= 32 else "normal" if length_s <= 48 else "slow"


def links(text: str) -> list[str]:
    return [u.rstrip(").,>") for u in URL_RE.findall(text or "") if host_ok(u.rstrip(").,>"))]


def host_ok(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for name, dom in (("tiktok", "tiktok.com"), ("instagram", "instagram.com"),
                      ("youtube", "youtube.com"), ("youtube", "youtu.be")):
        if host == dom or host.endswith("." + dom):
            return name
    return "upload"


def download(url: str, folder: Path) -> Path:
    """Fetch a reference video (<= 720p, <= 4 min) for measuring only."""
    if not host_ok(url):
        raise InspirationError("only TikTok, Instagram and YouTube links are accepted")
    out = folder / "ref.%(ext)s"
    p = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "-q",
                        "-f", "best[height<=720][ext=mp4]/best[height<=720]/best",
                        "--max-filesize", "150M", "--match-filter", f"duration <= {MAX_SECONDS}",
                        "-o", str(out), url], capture_output=True, text=True, timeout=300)
    files = [f for f in folder.glob("ref.*") if f.suffix not in (".part", ".ytdl")]
    if p.returncode != 0 or not files:
        err = (p.stderr or p.stdout).strip().splitlines()
        raise InspirationError(err[-1][:300] if err else "yt-dlp could not fetch it")
    return files[0]


# --------------------------------------------------------------------------
# Pacing
# --------------------------------------------------------------------------
def detect_cuts(path: Path, duration: float) -> tuple[list[float], str]:
    """Cut times in seconds, and which detector found them."""
    try:
        from scenedetect import ContentDetector, detect
        scenes = detect(str(path), ContentDetector(threshold=27.0, min_scene_len=6))
        cuts = [round(s[0].get_seconds(), 3) for s in scenes[1:]]
        return cuts, "PySceneDetect"
    except ImportError:
        pass
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf",
                        "select='gt(scene,0.3)',metadata=print:file=-", "-an", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=600)
    cuts = [float(t) for t in re.findall(r"pts_time:([\d.]+)", p.stdout + p.stderr)]
    return [c for c in cuts if 0.2 < c < duration - 0.2], "ffmpeg scene filter"


def pacing(cuts: list[float], duration: float) -> dict:
    shots = [b - a for a, b in zip([0.0] + cuts, cuts + [duration]) if b - a > 0]
    avg = duration / max(len(shots), 1)
    return {"cuts": len(cuts), "avg_shot_s": round(avg, 2),
            "cuts_per_s": round(len(cuts) / duration, 2) if duration else 0.0,
            "shortest_s": round(min(shots), 2) if shots else 0.0,
            "longest_s": round(max(shots), 2) if shots else 0.0}


def pace_name(avg_shot_s: float) -> str:
    return "fast" if avg_shot_s <= 0.9 else ("normal" if avg_shot_s <= 2.0 else "slow")


# --------------------------------------------------------------------------
# Music sync: tempo + share of cuts on the beat
# --------------------------------------------------------------------------
SR, HOP = 11025, 256


def onset_envelope(path: Path):
    import numpy as np
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                        "-ar", str(SR), "-f", "s16le", "-"], capture_output=True, timeout=300)
    x = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if len(x) < SR:
        return None
    n = 1024
    frames = len(x) // HOP - n // HOP
    win = np.hanning(n).astype(np.float32)
    mags = np.abs(np.fft.rfft(np.stack([x[i * HOP:i * HOP + n] * win for i in range(frames)]), axis=1))
    flux = np.maximum(0.0, np.diff(np.log1p(mags), axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    flux -= flux.mean()
    return np.maximum(flux, 0.0)


def tempo(env, hop_s: float = HOP / SR, lo: float = 70, hi: float = 180) -> tuple[float, float]:
    """(bpm, beat phase in seconds) from an onset envelope via autocorrelation."""
    import numpy as np
    env = np.asarray(env, dtype=float)
    if env.size < 8 or not env.any():
        return 0.0, 0.0
    ac = np.correlate(env, env, mode="full")[env.size - 1:]
    lags = np.arange(len(ac))
    bpms = 60.0 / np.maximum(lags * hop_s, 1e-9)
    ok = (bpms >= lo) & (bpms <= hi)
    if not ok.any():
        return 0.0, 0.0
    lag = int(lags[ok][np.argmax(ac[ok])])
    # parabolic interpolation: the true period is rarely a whole number of hops
    frac = 0.0
    if 0 < lag < len(ac) - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        den = a - 2 * b + c
        frac = 0.5 * (a - c) / den if den else 0.0
    period = (lag + max(-0.5, min(0.5, frac))) * hop_s
    beats = np.arange(0.0, env.size * hop_s, period)
    best, best_phase = -1.0, 0.0
    for k in range(int(period / hop_s) + 1):
        idx = np.round((beats + k * hop_s) / hop_s).astype(int)
        score = env[idx[idx < env.size]].sum()
        if score > best:
            best, best_phase = score, k * hop_s
    return round(60.0 / period, 1), round(best_phase, 3)


def on_beat_share(cuts: list[float], bpm: float, phase: float, tol: float = 0.08) -> float:
    if not cuts or bpm <= 0:
        return 0.0
    period = 60.0 / bpm
    hit = 0
    for c in cuts:
        d = (c - phase) % period
        if min(d, period - d) <= tol:
            hit += 1
    return round(hit / len(cuts), 2)


def loudest_point(env, hop_s: float = HOP / SR, window_s: float = 0.6) -> float | None:
    import numpy as np
    if env is None or len(env) < 4:
        return None
    k = max(1, int(window_s / hop_s))
    s = np.convolve(env, np.ones(k), mode="same")
    return round(float(np.argmax(s)) * hop_s, 2)


# --------------------------------------------------------------------------
# Colour: ffmpeg signalstats
# --------------------------------------------------------------------------
def signal_stats(path: Path, fps: int = 10) -> list[dict]:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf",
                        f"fps={fps},scale=320:-2,signalstats,metadata=print:file=-", "-an", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=600)
    frames: list[dict] = []
    cur: dict = {}
    for line in p.stdout.splitlines():
        m = re.match(r"frame:\d+\s+pts:\d+\s+pts_time:([\d.]+)", line)
        if m:
            if cur:
                frames.append(cur)
            cur = {"t": float(m.group(1))}
            continue
        m = re.match(r"lavfi\.signalstats\.(\w+)=([\d.\-]+)", line)
        if m and cur is not None:
            cur[m.group(1)] = float(m.group(2))
    if cur:
        frames.append(cur)
    return [f for f in frames if "YAVG" in f]


def colour(frames: list[dict]) -> dict:
    if not frames:
        return {}

    def mean(k):
        return sum(f.get(k, 0.0) for f in frames) / len(frames)
    y, sat = mean("YAVG"), mean("SATAVG")
    rng_ = mean("YHIGH") - mean("YLOW")
    u, v = mean("UAVG") - 128, mean("VAVG") - 128
    warm = v - u
    if abs(u) < 3 and abs(v) < 3:
        tone = "neutral"
    elif warm > 6:
        tone = "warm (orange/red)"
    elif warm < -6:
        tone = "cool (blue/teal)"
    elif u < -3 and v < -3:
        tone = "green"
    else:
        tone = "magenta/purple"
    return {"brightness": round(y / 255, 3), "contrast": round(rng_ / 255, 3),
            "saturation": round(sat, 1), "tint_u": round(u, 1), "tint_v": round(v, 1), "tone": tone}


def grade_from(c: dict) -> dict:
    """Measured colour -> grade settings for our renders."""
    lo, hi = st.CUSTOM_KEYS["saturation"]
    g = {"saturation": round(min(1.8, max(0.6, c["saturation"] / REF_SAT)), 2),
         "contrast": round(min(1.4, max(0.8, (c["contrast"] * 255) / REF_RANGE)), 2),
         "brightness": round(min(0.12, max(-0.12, (c["brightness"] * 255 - REF_Y) / 255 * 0.6)), 3),
         "warmth": round(min(0.15, max(-0.15, (c["tint_v"] - c["tint_u"]) / 120)), 3)}
    g["saturation"] = min(hi, max(lo, g["saturation"]))
    g["grade"] = min(st.GRADES, key=lambda k: (abs(st.GRADES[k]["saturation"] - g["saturation"])
                                               + abs(st.GRADES[k]["contrast"] - g["contrast"])
                                               + 2 * abs(st.GRADES[k]["warmth"] - g["warmth"])))
    return g


def transitions(frames: list[dict], cuts: list[float]) -> dict:
    """White flashes / dips to black right at the cuts."""
    flashes = fades = 0
    ys = [(f["t"], f["YAVG"]) for f in frames]
    if not ys:
        return {"flash": 0, "fade": 0}
    base = sorted(y for _, y in ys)[len(ys) // 2]
    for c in cuts:
        near = [y for t, y in ys if abs(t - c) <= 0.15]
        if near and max(near) > min(235, base + 70):
            flashes += 1
        elif near and min(near) < 18 and base > 40:
            fades += 1
    return {"flash": flashes, "fade": fades}


# --------------------------------------------------------------------------
# Motion: zoom and shake (OpenCV optical flow between sampled frames)
# --------------------------------------------------------------------------
def motion(path: Path, cuts: list[float], duration: float, fps: float = 8.0) -> dict:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"available": False}
    cap = cv2.VideoCapture(str(path))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(1, int(round(src_fps / fps)))
    prev, idx = None, 0
    scales, shifts, times = [], [], []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            g = cv2.cvtColor(cv2.resize(frame, (240, int(240 * frame.shape[0] / frame.shape[1]))),
                             cv2.COLOR_BGR2GRAY)
            t = idx / src_fps
            if prev is not None and not any(times and times[-1] < c <= t for c in cuts):
                pts = cv2.goodFeaturesToTrack(prev, 120, 0.01, 6)
                if pts is not None and len(pts) >= 8:
                    nxt, stt, _ = cv2.calcOpticalFlowPyrLK(prev, g, pts, None)
                    good = stt.reshape(-1) == 1
                    if good.sum() >= 8:
                        m, _ = cv2.estimateAffinePartial2D(pts[good], nxt[good])
                        if m is not None:
                            scales.append(math.hypot(m[0, 0], m[1, 0]))
                            shifts.append((m[0, 2], m[1, 2]))
            prev = g
            times.append(t)
        idx += 1
    cap.release()
    if not scales:
        return {"available": True, "zoom": "none", "shake": "none"}
    zooms = [math.log(s) * fps for s in scales]  # per second
    zoom_share = sum(1 for z in zooms if abs(z) > 0.08) / len(zooms)
    zoom_dir = sum(zooms) / len(zooms)
    jit = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(shifts, shifts[1:])]
    shake = sorted(jit)[len(jit) // 2] if jit else 0.0
    return {"available": True,
            "zoom": ("in" if zoom_dir > 0 else "out") if zoom_share > 0.3 else
                    ("some" if zoom_share > 0.12 else "none"),
            "zoom_share": round(zoom_share, 2),
            "shake": "strong" if shake > 3 else ("light" if shake > 1.2 else "none"),
            "shake_px": round(shake, 2)}


# --------------------------------------------------------------------------
# Text: OCR on a few frames (easyocr is optional and heavy, so imported lazily)
# --------------------------------------------------------------------------
def frame_rgb(path: Path, t: float, width: int = 540):
    import numpy as np
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(path),
                        "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, timeout=60)
    info = editor.probe(path)
    h = int(round(info["height"] * width / max(info["width"], 1) / 2) * 2)
    if len(p.stdout) < width * h * 3:
        return None
    return np.frombuffer(p.stdout[:width * h * 3], dtype=np.uint8).reshape(h, width, 3)


# --------------------------------------------------------------------------
# Framing: where the sharp picture sits in the 9:16 frame (Tim, 18 Nov)
# --------------------------------------------------------------------------
def sharp_rows(img) -> list[float]:
    """Per row, the share of pixels on a strong edge. Blurred backgrounds and black bars score
    near 0, real footage high."""
    import numpy as np
    g = img.astype(np.float32).mean(axis=2)
    dx = np.abs(np.diff(g, axis=1))[:-1, :]
    dy = np.abs(np.diff(g, axis=0))[:, :-1]
    edges = (dx + dy) > 18.0
    rows = edges.mean(axis=1)
    return [float(x) for x in np.append(rows, rows[-1:])]


EDGE_ROW = 0.012   # a row of real footage has at least this share of edge pixels (blur / bars: ~0)


def bands(rows: list[float], peaks: list[float] | None = None, min_share: float = 0.08) -> list[tuple[int, int]]:
    """Runs of picture rows (start, end), at least `min_share` of the height. `rows`: the median
    edge share per row over the sampled frames; `peaks`: the most any frame had. A short gap is
    closed (a smooth sky in the shot) unless no frame ever had an edge there: that's a divider
    between two stacked clips. Pure."""
    n = len(rows)
    if not n:
        return []
    peaks = peaks or rows
    top = sorted(rows)[int(n * 0.9)] if n > 9 else max(rows)
    thr = max(EDGE_ROW, 0.1 * top)
    on = [v >= thr for v in rows]
    runs, i = [], 0
    while i < n:
        if on[i]:
            j = i
            while j < n and on[j]:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    merged: list[list[int]] = []
    for a, b in runs:
        if merged and a - merged[-1][1] <= max(2, n // 25) \
                and max(peaks[merged[-1][1]:a] or [1.0]) > EDGE_ROW / 2:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_share * n]


def framing_from(width: int, height: int, found: list[tuple[int, int]]) -> dict:
    """Which of our framings a reference uses, from its sharp bands. Pure."""
    if not found:
        return {"framing": None, "why": "no clear picture area"}
    if len(found) >= 2:
        a, b = sorted(found, key=lambda r: r[1] - r[0], reverse=True)[:2]
        ha, hb = a[1] - a[0], b[1] - b[0]
        if min(ha, hb) >= 0.6 * max(ha, hb) and (ha + hb) >= 0.35 * height:
            return {"framing": "stack", "aspect": round(width / max(ha, hb), 2), "why": "two clips stacked"}
    top, bottom = min(r[0] for r in found), max(r[1] for r in found)
    big = max(found, key=lambda r: r[1] - r[0])
    share = (big[1] - big[0]) / height
    if share >= 0.88 or (bottom - top) / height >= 0.95 and share >= 0.6:
        return {"framing": "fill", "aspect": round(width / height, 2), "why": "full screen"}
    aspect = width / (big[1] - big[0])
    if aspect >= 1.5:
        name = "fit"           # a wide 16:9 (or wider) picture in the middle
    elif aspect >= 0.92:
        name = "box11"
    elif aspect >= 0.7:
        name = "box45"
    else:
        name = "fill"
    return {"framing": name, "aspect": round(aspect, 2), "share": round(share, 2),
            "why": f"a {aspect_name(aspect)} picture covering {share:.0%} of the height"}


def aspect_name(a: float) -> str:
    named = [(16 / 9, "16:9"), (2.39, "2.39:1"), (4 / 3, "4:3"), (1.0, "1:1"), (4 / 5, "4:5"), (3 / 4, "3:4"),
             (9 / 16, "9:16")]
    return min(named, key=lambda p: abs(p[0] - a))[1]


def framing(path: Path, duration: float, samples: int = 8) -> dict:
    """Sample frames across the video and measure the sharp picture area (median per row,
    so a caption in one frame doesn't count as picture)."""
    import statistics as stats_
    rows_all = []
    w = h = 0
    for i in range(samples):
        img = frame_rgb(path, duration * (i + 0.5) / samples, width=270)
        if img is None:
            continue
        h, w = img.shape[:2]
        rows_all.append(sharp_rows(img))
    if not rows_all:
        return {"framing": None, "why": "no frames"}
    rows = [stats_.median(col) for col in zip(*rows_all)]
    peaks = [max(col) for col in zip(*rows_all)]
    return framing_from(w, h, bands(rows, peaks))


def text_colour(img, box) -> tuple[str, bool]:
    """(text colour hex, has outline) from the pixels inside an OCR box."""
    import numpy as np
    xs = [int(p[0]) for p in box]
    ys = [int(p[1]) for p in box]
    x0, x1 = max(0, min(xs)), min(img.shape[1], max(xs))
    y0, y1 = max(0, min(ys)), min(img.shape[0], max(ys))
    crop = img[y0:y1, x0:x1].reshape(-1, 3).astype(float)
    if crop.size == 0:
        return "#FFFFFF", False
    lum = crop @ np.array([0.299, 0.587, 0.114])
    hi = crop[lum >= np.percentile(lum, 85)]
    lo = lum[lum <= np.percentile(lum, 15)]
    c = np.median(hi, axis=0) if len(hi) else np.array([255, 255, 255])
    outline = bool(len(lo) and lo.mean() < 60 and np.median(lum[lum >= np.percentile(lum, 85)]) > 170)
    return "#{:02X}{:02X}{:02X}".format(*[int(v) for v in c]), outline


def read_text(path: Path, duration: float, reader=None, samples: int = 8) -> dict:
    """OCR a few frames. `reader(image) -> [(box, text, conf)]` (easyocr.Reader.readtext)."""
    if reader is None:
        try:
            import easyocr  # heavy (PyTorch): optional, see README
        except ImportError:
            return {"available": False}
        r = easyocr.Reader(["en"], gpu=False, verbose=False)
        reader = r.readtext
    times = sorted({round(min(duration - 0.1, max(0.3, duration * k / (samples - 1))), 2)
                    for k in range(samples)} | {0.5})
    shots = []
    for t in times:
        img = frame_rgb(path, t)
        if img is None:
            continue
        found = [(b, txt.strip(), c) for b, txt, c in reader(img) if c >= 0.4 and len(txt.strip()) >= 2]
        h, w = img.shape[:2]
        boxes = []
        for b, txt, c in found:
            ys = [p[1] for p in b]
            colour_hex, outline = text_colour(img, b)
            boxes.append({"text": txt, "y": (min(ys) + max(ys)) / 2 / h, "h": (max(ys) - min(ys)) / h,
                          "colour": colour_hex, "outline": outline})
        shots.append({"t": t, "boxes": boxes, "text": " ".join(x["text"] for x in boxes)})
    with_text = [s for s in shots if s["boxes"]]
    if not with_text:
        return {"available": True, "found": False, "frames": len(shots)}
    allb = [b for s in with_text for b in s["boxes"]]
    big = max(allb, key=lambda b: b["h"])
    y = sorted(b["y"] for b in allb)[len(allb) // 2]
    hpx = sorted(b["h"] for b in allb)[len(allb) // 2] * 1920
    colours: dict[str, int] = {}
    for b in allb:
        colours[b["colour"]] = colours.get(b["colour"], 0) + 1
    ranked = sorted(colours, key=colours.get, reverse=True)
    first = next((s["text"] for s in shots if s["t"] <= 3.0 and s["text"]), with_text[0]["text"])
    last = next((s["text"] for s in reversed(shots) if s["text"]), "")
    changes = [s["t"] for a, s in zip(shots, shots[1:]) if s["text"] != a["text"]]
    return {"available": True, "found": True, "frames": len(shots), "frames_with_text": len(with_text),
            "y": round(y, 2), "line_px": int(hpx), "font_px": int(hpx * 1.3),
            "biggest": big["text"][:60], "colours": ranked[:3],
            "outline": sum(b["outline"] for b in allb) >= len(allb) / 2,
            "words_per_screen": round(sum(len(s["text"].split()) for s in with_text) / len(with_text), 1),
            "hook": first[:150], "last": last[:150], "first_change": changes[0] if changes else None}


def text_settings(t: dict) -> dict:
    if not t.get("found"):
        return {}
    y = t["y"]
    out = {"position": "high" if y < 0.38 else ("middle" if y < 0.62 else "low"),
           "text_y": round(min(0.85, max(0.15, y)), 2),
           "text_px": int(min(160, max(36, t["font_px"]))),
           "size": min(st.SIZES, key=lambda k: abs(84 * st.SIZES[k] - t["font_px"])),
           "outline": 7 if t.get("outline") else 0, "shadow": 2 if t.get("outline") else 4,
           "words": int(min(6, max(1, round(t["words_per_screen"]))))}
    if t.get("colours"):
        out["text_colour"] = t["colours"][0]
        if len(t["colours"]) > 1:
            out["highlight"] = t["colours"][1]
    return out


# --------------------------------------------------------------------------
# Whole analysis -> preset
# --------------------------------------------------------------------------
def thumbnail_strip(path: Path, out: Path, duration: float, n: int = 6) -> Path | None:
    times = [duration * (k + 0.5) / n for k in range(n)]
    tmp = out.parent / (out.stem + "_frames")
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for i, t in enumerate(times):
            editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(path),
                        "-frames:v", "1", "-vf", "scale=180:320:force_original_aspect_ratio=increase,crop=180:320",
                        str(tmp / f"f{i}.jpg")], timeout=60)
        editor.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1",
                    "-i", str(tmp / "f%d.jpg"), "-vf", f"tile={n}x1:padding=4:color=black",
                    "-frames:v", "1", str(out)], timeout=60)
        return out
    except editor.EditError as e:
        log.warning("thumbnail strip: %s", e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def first_last_similar(path: Path, duration: float) -> bool:
    try:
        a = editor.frame_hash(path, 0.2)
        b = editor.frame_hash(path, max(0.2, duration - 0.2))
    except editor.EditError:
        return False
    return editor.hamming(a, b) <= 10


def analyse(path: Path, reader=None) -> dict:
    info = editor.probe(path)
    duration = info["duration"]
    if duration <= 0:
        raise InspirationError("that file has no video in it")
    if duration > MAX_SECONDS:
        raise InspirationError(f"that video is {duration:.0f}s long; send one under {MAX_SECONDS}s")
    from . import progress
    job = progress.current()
    job.update(0.1, "Finding the cuts")
    cuts, detector = detect_cuts(path, duration)
    pace = pacing(cuts, duration)
    job.update(0.3, "Measuring tempo and colour")
    env = onset_envelope(path) if info.get("audio") else None
    bpm, phase = tempo(env) if env is not None else (0.0, 0.0)
    frames = signal_stats(path)
    col = colour(frames)
    job.update(0.45, "Reading on-screen text")
    text = read_text(path, duration, reader)
    job.update(0.6, "Measuring zooms and shake")
    mo = motion(path, cuts, duration)
    try:
        fr = framing(path, duration)
    except Exception as e:  # noqa: BLE001 - framing is a bonus, never fails an analysis
        fr = {"framing": None, "why": f"not measured ({e})"}
    tr = transitions(frames, cuts)
    loud = loudest_point(env)
    hook_s = text.get("first_change") or (cuts[0] if cuts else None) or min(3.0, duration)
    ending = st.ending_style(text.get("last", ""), first_last_similar(path, duration))
    return {"duration": round(duration, 2), "size": [info["width"], info["height"]], "audio": info.get("audio"),
            "detector": detector, "cut_times": cuts[:200], "pacing": pace,
            "music": {"bpm": bpm, "phase": phase, "on_beat": on_beat_share(cuts, bpm, phase)},
            "colour": col, "text": text, "motion": mo, "transitions": tr, "framing": fr,
            "structure": {"hook_s": round(min(hook_s, duration), 2),
                          "payoff_s": loud, "payoff": round(loud / duration, 2) if loud else None,
                          "ending": ending, "length_s": round(duration, 1)}}


def settings_from(a: dict) -> dict:
    """Analysis -> preset settings per part (see presets.PART_KEYS)."""
    out: dict = {}
    p = a["pacing"]
    out["pacing"] = {"pace": pace_name(p["avg_shot_s"]), "shot_s": round(min(6.0, max(0.2, p["avg_shot_s"])), 2)}
    if a.get("colour"):
        out["colour"] = grade_from(a["colour"])
    out["text"] = text_settings(a.get("text") or {})
    tr, mo = a.get("transitions") or {}, a.get("motion") or {}
    n = max(1, a["pacing"]["cuts"])
    trans = "flash" if tr.get("flash", 0) / n >= 0.2 else (
        "zoom" if mo.get("zoom") in ("in", "out", "some") else "cut")
    out["transitions"] = {"transition": trans}
    hook = (a.get("text") or {}).get("hook") or ""
    out["hook"] = {"hook_style": st.hook_style(hook)} if hook else {}
    s = a["structure"]
    out["structure"] = {"hook_s": round(min(60.0, max(1.0, s["hook_s"])), 2), "ending": s["ending"]}
    if s.get("payoff"):
        out["structure"]["payoff"] = round(min(0.9, max(0.2, s["payoff"])), 2)
    m = a["music"]
    out["music-sync"] = {"on_beat": m["on_beat"] >= 0.6, "bpm": m["bpm"], "beat_share": m["on_beat"]}
    if (a.get("framing") or {}).get("framing"):
        out["framing"] = {"framing": a["framing"]["framing"]}
    return out


FRAMING_LABEL = {"box45": "4:5 box", "box11": "1:1 over blur", "stack": "two clips stacked",
                 "fill": "full screen", "fit": "whole 16:9 frame over blur"}


def breakdown(preset: dict) -> str:
    """The readable reply in the #inspiration thread (fits one Discord message)."""
    a = preset["analysis"]
    s = preset["settings"]
    p, m, c, t, mo = a["pacing"], a["music"], a.get("colour") or {}, a.get("text") or {}, a.get("motion") or {}
    kind = preset.get("kind") or "edit"
    L = [f"**Preset `{preset['name']}` saved** · copies: {', '.join(preset['copy'])}",
         f"**Type** {KIND_LABEL.get(kind, kind)}" + (f" ({preset['kind_why']})" if preset.get("kind_why") else ""),
         f"**Length** {a['duration']:.1f}s · {a['size'][0]}x{a['size'][1]}",
         f"**Pacing** {p['cuts']} cuts, avg shot {p['avg_shot_s']}s ({p['cuts_per_s']}/s,"
         f" {p['shortest_s']}-{p['longest_s']}s) → pace **{s['pacing']['pace']}** ({a['detector']})"]
    if m["bpm"]:
        L.append(f"**Music sync** ~{m['bpm']:.0f} BPM, {m['on_beat']:.0%} of cuts on the beat"
                 + (" → cut on the beat" if m["on_beat"] >= 0.6 else " → not beat-cut"))
    else:
        L.append("**Music sync** no usable audio")
    if c:
        g = s.get("colour", {})
        L.append(f"**Colour** {c['tone']}, saturation {c['saturation']:.0f} (ours ≈{REF_SAT:.0f}),"
                 f" contrast {c['contrast']:.2f}, brightness {c['brightness']:.2f} → grade **{g.get('grade')}**"
                 f" (sat ×{g.get('saturation')}, contrast ×{g.get('contrast')}, warmth {g.get('warmth')})")
    if not t.get("available"):
        L.append("**Text** OCR not installed on the server (README: easyocr) – text style not measured")
    elif not t.get("found"):
        L.append("**Text** no on-screen text found")
    else:
        ts = s.get("text", {})
        L.append(f"**Text** {ts.get('position')} of screen (y {t['y']:.0%}), ~{t['font_px']}px,"
                 f" colour {', '.join(t['colours'])}{', outlined' if t.get('outline') else ''},"
                 f" {t['words_per_screen']} words/screen")
        L.append(f"**Hook** “{t['hook'][:90]}” → type **{s.get('hook', {}).get('hook_style')}**"
                 " (we copy the type, never the words)")
    st_ = a["structure"]
    L.append(f"**Structure** hook {st_['hook_s']}s, payoff at "
             + (f"{st_['payoff_s']}s ({st_['payoff']:.0%})" if st_.get("payoff") else "?")
             + f", ending **{st_['ending']}**")
    fr = a.get("framing") or {}
    if fr.get("framing") and kind == "edit":
        L.append(f"**Framing** {fr['why']} → **{FRAMING_LABEL.get(fr['framing'], fr['framing'])}**"
                 " (trailer and trend edits)")
    if mo.get("available"):
        L.append(f"**Motion** zoom {mo.get('zoom')}, shake {mo.get('shake')} · transitions:"
                 f" {a['transitions']['flash']} flash, {a['transitions']['fade']} fade"
                 f" → **{s['transitions']['transition']}**")
    L.append("Edit with `/preset edit`, combine with `/preset combine`, use with `/preset apply`."
             " Wrong type? Send it again with `type: edit`, `type: clip` or `type: other`."
             " The reference video has been deleted.")
    return "\n".join(L)[:1990]


def make_preset(name: str, copy: list[str], analysis: dict, source: dict, art: dict | None = None,
                kind: str = "edit", kind_why: str = "") -> dict:
    sets = settings_from(analysis)
    if kind == "stream_clip":
        sets["pacing"] = {"pace": clip_pace(analysis["duration"])}
        sets.pop("framing", None)
    allowed = KIND_PARTS.get(kind, presets_.PARTS)
    copy = [c for c in copy if c in allowed] or list(allowed)
    if art:
        look = {}
        ok, _bad = st.apply_settings(art.get("settings") or [], "all")
        for row in ok:
            if row["setting"] in presets_.PART_KEYS["look"]:
                look[row["setting"]] = row["value"]
        if look:
            sets["look"] = look
    out = {"name": name, "created": time.time(), "source": source, "copy": copy,
           "settings": sets, "analysis": analysis, "kind": kind, "kind_why": kind_why,
           "modes": KIND_MODES.get(kind, [])}
    if art:
        out["art"] = art
    return out


def sample_frames(path: Path, duration: float, cuts: list[float], n: int = 8,
                  width: int = 384) -> list[tuple[float, bytes]]:
    """Small JPEG frames for the AI: the opening, then spread through the video (a
    little after cuts where possible, so each frame is a different shot). Kept in
    memory only and dropped with the reference video."""
    times = [0.3, min(1.5, duration * 0.1)]
    after_cuts = [c + 0.25 for c in cuts if 1.5 < c < duration - 0.5]
    step = max(1, len(after_cuts) // max(1, n - 3))
    times += after_cuts[::step][: n - 3]
    while len(times) < n - 1:
        times.append(duration * len(times) / n)
    times.append(max(0.1, duration - 0.4))
    out = []
    for t in sorted({round(min(max(0.0, t), max(0.0, duration - 0.1)), 2) for t in times})[:n]:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(path),
                            "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2",
                            "-c:v", "mjpeg", "-"], capture_output=True, timeout=60)
        if p.returncode == 0 and p.stdout:
            out.append((t, p.stdout))
    return out


def study(src: Path, a: dict, note: str, base: Path | None, client=None) -> dict | None:
    """The artistic read (spikebot/ai.py), or None without a key / on failure."""
    from . import ai
    if client is None and not ai.available(base):
        return None
    measured = {k: a.get(k) for k in ("duration", "pacing", "music", "colour", "motion", "transitions", "structure")}
    t = a.get("text") or {}
    measured["text"] = {k: t.get(k) for k in ("available", "found", "y", "font_px", "words_per_screen",
                                               "frames_with_text", "frames") if k in t}
    try:
        frames = sample_frames(src, a["duration"], a.get("cut_times") or [])
        if not frames:
            return None
        return ai.study_reference(frames, measured, note, base=base, client=client)
    except (ai.AIError, subprocess.TimeoutExpired, OSError) as e:
        log.warning("artistic analysis skipped: %s", e)
        return None


def art_text(preset: dict) -> str | None:
    """Second #inspiration message: what makes the reference good (the craft, not the content)."""
    art = preset.get("art")
    if not art:
        return None
    L = [f"🎬 **Why it works** · {art.get('summary', '')}"]
    for g in (art.get("what_makes_it_good") or [])[:6]:
        L.append(f"• {g}")
    if art.get("opening"):
        L.append(f"**Opening:** {art['opening']}")
    if art.get("text_use"):
        L.append(f"**Text:** {art['text_use']}")
    tech = art.get("techniques") or []
    if tech:
        L.append("**Techniques:** " + " · ".join(
            f"{x['name']}{' ✅' if x.get('can_copy') else ' (not in our editor yet)'}" for x in tech[:8]))
    if art.get("avoid"):
        L.append("**Avoid:** " + "; ".join(art["avoid"][:3]))
    look = (preset.get("settings") or {}).get("look")
    if look:
        L.append("**Look copied:** " + ", ".join(f"{k}={v}" for k, v in look.items()))
    return "\n".join(L)[:1990]


def run(name: str, copy: list[str], data_dir: Path, out_dir: Path, url: str | None = None,
        file: Path | None = None, reader=None, note: str = "", base: Path | None = None,
        ai_client=None, kind: str | None = None, facecam=has_facecam) -> dict:
    """Download (if a link), analyse, save the preset, make the strip. The reference
    video is always deleted. Returns {ok, name, text, strip} or {ok: False, error, ask_upload}."""
    store = presets_.Presets(data_dir / "presets")
    tmp = Path(tempfile.mkdtemp(prefix="insp-", dir=out_dir))
    try:
        from . import progress
        job = progress.current()
        if url:
            job.update(0.0, "Downloading the video")
            try:
                src = download(url, tmp)
            except (InspirationError, subprocess.TimeoutExpired) as e:
                return {"ok": False, "error": str(e), "ask_upload": True}
        else:
            src = file
        try:
            a = analyse(src, reader)
        except (InspirationError, editor.EditError) as e:
            return {"ok": False, "error": str(e), "ask_upload": False}
        name = store.unique_name(name)
        job.update(0.75, "Claude is studying what makes it good")
        art = study(src, a, note, base, ai_client)
        job.update(0.9, "Working out what kind of video it is")
        kind, why = classify(a, art, kind, facecam, src)
        job.update(0.95, "Saving the preset")
        preset = make_preset(name, copy, a, {"url": url, "platform": platform_of(url) if url else "upload"}, art,
                             kind, why)
        store.save(preset)
        strip = thumbnail_strip(src, out_dir / f"{name}_strip.jpg", a["duration"])
        return {"ok": True, "name": name, "text": breakdown(preset), "art": art_text(preset),
                "strip": str(strip) if strip else None, "kind": kind}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if file is not None:
            Path(file).unlink(missing_ok=True)  # never keep their footage


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m spikebot.inspiration")
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    an = sub.add_parser("analyse")
    g = an.add_mutually_exclusive_group(required=True)
    g.add_argument("--url")
    g.add_argument("--file")
    an.add_argument("--name", required=True)
    an.add_argument("--copy", default="")
    an.add_argument("--json", help="write the result here")
    an.add_argument("--note", default="", help="Tim's words about why he sent it")
    an.add_argument("--kind", choices=KINDS, help="Tim's type: edit | stream_clip | other (else detected)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .review import load_cfg
    cfg, base = load_cfg(args.config)
    dd = Path(cfg.get("data_dir", "data"))
    data_dir = dd if dd.is_absolute() else base / dd
    out_dir = data_dir / "inspiration"
    out_dir.mkdir(parents=True, exist_ok=True)
    from . import progress
    progress.arm_cancel()
    try:
        res = run(presets_.clean_name(args.name), presets_.parse_copy("copy: " + args.copy) if args.copy
                  else list(presets_.PARTS), data_dir, out_dir, args.url, Path(args.file) if args.file else None,
                  note=args.note, base=base, kind=args.kind)
    except progress.Cancelled:   # the reference file is still deleted (run's finally)
        res = {"ok": False, "cancelled": True, "error": "cancelled"}
    if args.json:
        Path(args.json).write_text(json.dumps(res), encoding="utf-8")
    print(res.get("text") or f"FAILED: {res.get('error')}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
