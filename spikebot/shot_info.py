"""What each trailer shot looks like, measured once per library file (cached in library.json).

One cheap decode pass per file (2 frames a second, 72x40 pixels) gives, per frame:

- the mean colour (Y, U, V): clips of a similar colour palette (`palette_match`);
- how much detail each of 12 vertical strips holds (edges): where the subject sits, so a
  9:16 crop can be framed on it and shots whose subject would be cut off are avoided
  (`crop_safe`);
- a 64-bit picture hash: the same footage in two files (Trailer 2 and its "extended look",
  a re-upload) is recognised, so an edit never shows it twice (`unique_clips`).

Pure numpy apart from the ffmpeg decode; never downloads anything.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("shot_info")

VERSION = 1
FW, FH = 72, 40          # decode size: 12 strips of 6 px, a 9x8 hash grid of 8x5 blocks
STRIPS = 12
FPS = 2.0


def measure(path: Path, timeout: int = 1800) -> dict:
    """{"v", "rows": [[t, y, u, v, [12 strip energies], "hash hex"], ...]} for a video file.
    Raises OSError / subprocess errors (the caller skips the file this time)."""
    import numpy as np
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                        "-vf", f"fps={FPS},scale={FW}:{FH}:flags=area,format=rgb24", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise OSError(f"ffmpeg could not read {Path(path).name}: {p.stderr.decode(errors='replace')[-200:]}")
    size = FW * FH * 3
    buf = np.frombuffer(p.stdout[: len(p.stdout) // size * size], dtype=np.uint8)
    frames = buf.reshape(-1, FH, FW, 3).astype(np.float32)
    rows = []
    for i, f in enumerate(frames):
        rows.append(frame_row(i / FPS, f))
    return {"v": VERSION, "rows": rows}


def frame_row(t: float, f) -> list:
    """One frame (FH, FW, 3 float RGB) -> [t, y, u, v, strips, hash]."""
    import numpy as np
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    u = (b - y) * 0.565
    v = (r - y) * 0.713
    gx = np.abs(np.diff(y, axis=1))[:-1, :]
    gy = np.abs(np.diff(y, axis=0))[:, :-1]
    edge = gx + gy                                            # (FH-1, FW-1)
    edge = np.pad(edge, ((0, 1), (0, 1)))
    strips = edge.reshape(FH, STRIPS, FW // STRIPS).sum(axis=(0, 2))
    tot = float(strips.sum()) or 1.0
    grid = y.reshape(8, FH // 8, 9, FW // 9).mean(axis=(1, 3))    # 8 rows x 9 cols
    bits = (grid[:, 1:] > grid[:, :-1]).flatten()
    h = 0
    for bit in bits:
        h = (h << 1) | int(bit)
    return [round(t, 2), round(float(y.mean()), 1), round(float(u.mean()), 1), round(float(v.mean()), 1),
            [int(round(1000 * s / tot)) for s in strips], f"{h:016x}"]


def _rows(entry: dict, s: float, e: float) -> list:
    rows = (entry.get("looks") or {}).get("rows") or []
    inner = [r for r in rows if s + 0.2 <= r[0] < e - 0.2] or [r for r in rows if s <= r[0] < e]
    if not inner and rows:
        inner = [min(rows, key=lambda r: abs(r[0] - (s + e) / 2))]
    return inner


def crop_share(aspect: float) -> float:
    """Share of the (cropped) source width a 9:16 fill crop shows."""
    return min(1.0, (9 / 16) / max(aspect, 1e-3))


def frame_window(strips: list[float], share: float) -> tuple[float, float, float]:
    """(focus 0-1, energy inside the best crop window, strongest strip left outside it) for a
    strip-energy profile summing to ~1. `share` = crop width as a share of the frame."""
    n = len(strips)
    tot = sum(strips) or 1.0
    p = [x / tot for x in strips]
    w = share * n
    best, best_lo = -1.0, 0.0
    steps = 48
    for k in range(steps + 1):
        lo = (n - w) * k / steps
        hi = lo + w
        e = 0.0
        for i, x in enumerate(p):
            a, b = max(lo, i), min(hi, i + 1)
            if b > a:
                e += x * (b - a)
        if e > best + 1e-9 or (abs(e - best) <= 1e-9 and abs(lo + w / 2 - n / 2) < abs(best_lo + w / 2 - n / 2)):
            best, best_lo = e, lo
    outside = [x for i, x in enumerate(p) if i + 1 <= best_lo + 1e-9 or i >= best_lo + w - 1e-9]
    peak_out = max(outside) * n if outside else 0.0
    return (best_lo + w / 2) / n, best, peak_out


def centre_window(strips: list[float], share: float) -> tuple[float, float]:
    """(energy inside, strongest strip outside) for the centred crop window."""
    n = len(strips)
    tot = sum(strips) or 1.0
    p = [x / tot for x in strips]
    w = share * n
    lo, hi = (n - w) / 2, (n + w) / 2
    inside = sum(x * max(0.0, min(hi, i + 1) - max(lo, i)) for i, x in enumerate(p))
    outside = [x for i, x in enumerate(p) if i + 1 <= lo + 1e-9 or i >= hi - 1e-9]
    return inside, (max(outside) * n if outside else 0.0)


# Move the crop off the centre only when it matters: the best window holds this much more of the
# picture's detail than the centred one, or the centred crop would cut a strong subject off.
SHIFT_GAIN = 0.12


def shot_stats(entry: dict, s: float, e: float, aspect: float) -> dict | None:
    """Colour, subject position and hashes of one shot; None without measurements. `focus` is
    0.5 (centred crop) unless the centred crop would leave the subject out."""
    rows = _rows(entry, s, e)
    if not rows:
        return None
    n = len(rows)
    strips = [sum(r[4][i] for r in rows) / n for i in range(STRIPS)]
    share = crop_share(aspect)
    focus, inside, peak_out = frame_window(strips, share)
    c_in, c_peak = centre_window(strips, share)
    if not (inside - c_in >= SHIFT_GAIN or c_peak > CUT_OFF_PEAK):
        focus, peak_out = 0.5, c_peak
    picks = [rows[min(n - 1, int(n * q))] for q in (0.25, 0.5, 0.75)]
    return {"yuv": [round(sum(r[k] for r in rows) / n, 1) for k in (1, 2, 3)],
            "focus": round(focus, 3), "inside": round(inside, 3), "peak_out": round(peak_out, 2),
            "hashes": [int(r[5], 16) for r in picks]}


def subject_focus(subject_x: float, subject_w: float, aspect: float) -> float:
    """Crop centre from the vision label: the middle unless the subject would be cut off there."""
    half = crop_share(aspect) / 2
    lo, hi = subject_x - subject_w / 2, subject_x + subject_w / 2
    if lo >= 0.5 - half - 0.01 and hi <= 0.5 + half + 0.01:
        return 0.5
    return min(max(subject_x, half), 1 - half)


# A 9:16 crop of a wide shot "looks incomplete" when a strong subject sits outside it: a strip
# outside the window holding more than this many times the even share of detail.
CUT_OFF_PEAK = 2.2


def crop_ok(stats: dict | None, subject_w: float | None = None, aspect: float = 16 / 9) -> bool:
    """True when the shot crops cleanly to 9:16 (unknown = True). `subject_w`: the main subject's
    width as a share of the frame, from the vision labels (the crop is centred on the subject)."""
    if subject_w is not None:
        return subject_w <= crop_share(aspect) + 0.06
    if not stats:
        return True
    return stats.get("peak_out", 0.0) <= CUT_OFF_PEAK


def colour_distance(a: list[float] | None, b: list[float] | None) -> float:
    """How different two shots' palettes are (0 = same; ~10 = close; 40+ = very different)."""
    if not a or not b:
        return 0.0
    return ((a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5 * 1.5 + abs(a[0] - b[0]) * 0.4


def palette_limit(strength: float) -> float:
    """Largest colour distance allowed at palette_match `strength` (0 = any, 1 = strict)."""
    s = max(0.0, min(1.0, float(strength)))
    return 70.0 - 55.0 * s


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


DUP_BITS = 9     # of 64: closer than this = the same picture


def same_footage(a: list[int], b: list[int]) -> bool:
    """Two shots show the same footage when at least 2 of their 3 sample frames match one of the
    other's (a few frames' shift between two encodes still matches)."""
    if not a or not b:
        return False
    hits = sum(1 for x in a if any(hamming(x, y) <= DUP_BITS for y in b))
    return hits >= 2


def dup_groups(items: list[tuple[str, list[int]]]) -> dict[str, str]:
    """{key: group key}: shots showing the same footage share a group (the first key seen)."""
    out: dict[str, str] = {}
    reps: list[tuple[str, list[int]]] = []
    for key, hs in items:
        g = key
        if hs:
            for rk, rh in reps:
                if same_footage(hs, rh):
                    g = rk
                    break
            else:
                reps.append((key, hs))
        out[key] = g
    return out
