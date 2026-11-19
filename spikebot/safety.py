"""Nudity check: nothing with nudity ever reaches #ready-to-post or a platform.

Every rendered version (stream clips, Tokyo Girl and trend edits) is scanned before it is
added to the review queue: frames are sampled with ffmpeg and run through NudeNet (a small
ONNX detector, CPU). A version showing exposed breasts, genitals, buttocks or anus is
**blocked**: its files are deleted and it is never shown or posted. If every version of an
item is blocked, the item fails with a "blocked: nudity" reason that goes to #status.

Tokyo Girl / trend footage is also screened once per library shot (`shot_flags`): shots
with nudity or clearly sexual framing (strip club, twerking: covered buttocks/breasts
filling the frame) are never picked.

CLI:  python -m spikebot.safety scan VIDEO [--fps 2]
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("safety")

# Never allowed anywhere.
EXPOSED = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
           "BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}
# Sexualised framing: a problem only when big and sustained (strip club, twerking), so a
# bikini in the background of a beach shot doesn't block a whole edit.
SUGGESTIVE = {"BUTTOCKS_COVERED", "FEMALE_BREAST_COVERED", "FEMALE_GENITALIA_COVERED", "ANUS_COVERED"}

BLOCK_SCORE = 0.45          # one frame this sure of exposed nudity blocks the video
WEAK_SCORE = 0.30           # ...or two frames at least this sure
SUGGESTIVE_SCORE = 0.55
SUGGESTIVE_AREA = 0.06      # box at least 6% of the frame
SUGGESTIVE_FRAMES = 3       # in at least this many sampled frames (≈1.5 s at 2 fps)
SAMPLE_FPS = 2.0
SAMPLE_W = 640


class Unavailable(RuntimeError):
    """NudeNet isn't installed: the check can't run."""


@lru_cache(maxsize=1)
def _detector():
    try:
        from nudenet import NudeDetector
    except Exception as e:  # noqa: BLE001 - missing package / onnxruntime problem
        raise Unavailable(f"nudity check unavailable: {e}") from e
    return NudeDetector()


def frames(path: Path, fps: float = SAMPLE_FPS, width: int = SAMPLE_W):
    """(time, BGR frame) pairs sampled with ffmpeg, never the whole video in memory."""
    import numpy as np
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=60)
    s = (json.loads(probe.stdout or "{}").get("streams") or [{}])[0]
    w0, h0 = int(s.get("width") or 0), int(s.get("height") or 0)
    if not w0 or not h0:
        return
    h = int(round(h0 * width / w0 / 2) * 2)
    p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                          "-vf", f"fps={fps},scale={width}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         stdout=subprocess.PIPE)
    size = width * h * 3
    n = 0
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield n / fps, np.frombuffer(buf, dtype=np.uint8).reshape(h, width, 3)
            n += 1
    finally:
        p.stdout.close()
        p.wait(timeout=60)


def detect(frame) -> list[dict]:
    return _detector().detect(frame)


def judge(per_frame: list[tuple[float, list[dict], tuple[int, int]]]) -> dict:
    """Verdict from detections: {"ok", "reason", "hits"}. Pure, so it is testable."""
    strong, weak, sugg = [], [], []
    for t, dets, (w, h) in per_frame:
        for d in dets:
            cls, score = d.get("class"), float(d.get("score") or 0)
            if cls in EXPOSED:
                if score >= BLOCK_SCORE:
                    strong.append((t, cls, score))
                elif score >= WEAK_SCORE:
                    weak.append((t, cls, score))
            elif cls in SUGGESTIVE and score >= SUGGESTIVE_SCORE:
                bw, bh = (d.get("box") or [0, 0, 0, 0])[2:4]
                if w and h and bw * bh / (w * h) >= SUGGESTIVE_AREA:
                    sugg.append((t, cls, score))
    hits = [{"t": round(t, 1), "class": c, "score": round(s, 2)} for t, c, s in strong + weak + sugg]
    if strong:
        t, c, s = strong[0]
        return {"ok": False, "reason": f"nudity ({c.lower()} {s:.0%} at {t:.1f}s)", "hits": hits}
    if len({round(t, 1) for t, _, _ in weak}) >= 2:
        t, c, s = weak[0]
        return {"ok": False, "reason": f"possible nudity ({c.lower()} at {t:.1f}s, several frames)", "hits": hits}
    if len({round(t, 1) for t, _, _ in sugg}) >= SUGGESTIVE_FRAMES:
        t, c, _ = sugg[0]
        return {"ok": False, "reason": f"sexual content ({c.lower()} close-up from {t:.1f}s)", "hits": hits}
    return {"ok": True, "reason": None, "hits": hits}


def scan(path: Path, fps: float = SAMPLE_FPS, detector=None) -> dict:
    """{"ok", "reason", "hits", "checked", "frames"} for a video. Raises Unavailable without
    NudeNet (callers decide what that means)."""
    det = detector or detect
    if detector is None:
        _detector()
    rows = []
    for t, f in frames(Path(path), fps):
        rows.append((t, det(f), (f.shape[1], f.shape[0])))
    out = judge(rows)
    out.update(checked=True, frames=len(rows))
    return out


def screen_variants(variants: list[dict], folder: Path, detector=None) -> tuple[list[dict], list[dict]]:
    """(clean, blocked). Blocked versions' files are deleted at once. Without NudeNet every
    version passes but is marked `safety: unchecked` (the card says so)."""
    clean, blocked = [], []
    for v in variants:
        f = folder / v["file"]
        try:
            res = scan(f, detector=detector)
        except Unavailable as e:
            log.error("%s", e)
            v["safety"] = "unchecked"
            clean.append(v)
            continue
        except Exception as e:  # noqa: BLE001 - a broken check must not let a video through silently
            log.warning("nudity scan of %s failed: %s", f.name, e)
            v["safety"] = "unchecked"
            clean.append(v)
            continue
        if res["ok"]:
            v["safety"] = "ok"
            clean.append(v)
        else:
            log.warning("BLOCKED %s: %s", f.name, res["reason"])
            for name in (v.get("file"), v.get("preview")):
                if name:
                    (folder / name).unlink(missing_ok=True)
            blocked.append({"label": v["label"], "reason": res["reason"]})
    return clean, blocked


def frame_flag(dets: list[dict], size: tuple[int, int]) -> str | None:
    """Stricter per-frame test for library footage (there is plenty, so any doubt = skip)."""
    w, h = size
    for d in dets:
        cls, score = d.get("class"), float(d.get("score") or 0)
        if cls in EXPOSED and score >= WEAK_SCORE:
            return cls.lower()
        if cls in SUGGESTIVE and score >= SUGGESTIVE_SCORE:
            bw, bh = (d.get("box") or [0, 0, 0, 0])[2:4]
            if w and h and bw * bh / (w * h) >= SUGGESTIVE_AREA:
                return cls.lower()
    return None


def flag_times(path: Path, fps: float = 1.0, detector=None, faces: list | None = None) -> list[list]:
    """[[time, what], ...] of frames in a library file that must never be used. Raises
    Unavailable without NudeNet. With `faces` (a list), the same pass also collects the faces
    in each frame (`frame_faces`): smart framing keeps the subject in the picture."""
    det = detector or detect
    if detector is None:
        _detector()
    out = []
    for t, f in frames(Path(path), fps):
        dets = det(f)
        why = frame_flag(dets, (f.shape[1], f.shape[0]))
        if why:
            out.append([round(t, 2), why])
        if faces is not None:
            found = frame_faces(dets, (f.shape[1], f.shape[0]))
            if found:
                faces.append([round(t, 2), found])
    return out


FACE_SCORE = 0.45


def frame_faces(dets: list[dict], size: tuple[int, int]) -> list[list[float]]:
    """Faces in one frame as [centre x, centre y, width, height], fractions of the frame. Pure."""
    w, h = size
    out = []
    for d in dets:
        if d.get("class") in FACES and float(d.get("score") or 0) >= FACE_SCORE and w and h:
            x, y, bw, bh = d["box"]
            out.append([round((x + bw / 2) / w, 3), round((y + bh / 2) / h, 3), round(bw / w, 3), round(bh / h, 3)])
    return out


def shot_flags(path: Path, start: float, end: float, fps: float = 1.0, detector=None) -> str | None:
    """Why a library shot must never be used (nudity / sexual framing), or None."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cut = Path(tmp) / "shot.mp4"
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.2f}",
                            "-i", str(path), "-t", f"{max(0.5, end - start):.2f}", "-an", "-c:v", "libx264",
                            "-preset", "ultrafast", "-crf", "30", str(cut)], capture_output=True, timeout=120)
        if p.returncode != 0 or not cut.exists():
            return None
        res = scan(cut, fps=fps, detector=detector)
    return None if res["ok"] else res["reason"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="nudity check")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("video")
    sc.add_argument("--fps", type=float, default=SAMPLE_FPS)
    args = ap.parse_args(argv)
    res = scan(Path(args.video), args.fps)
    print(json.dumps(res, indent=1))
    return 0 if res["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())


# --------------------------------------------------------------------------
# Facecam finder (same detector: it also finds faces)
# --------------------------------------------------------------------------
FACES = {"FACE_FEMALE", "FACE_MALE"}


def facecam_from_faces(per_frame: list[tuple[list[dict], tuple[int, int]]], min_share: float = 0.5) -> dict | None:
    """The streamer's camera box (fractions x, y, w, h) from face detections in sampled frames:
    the face that sits in the same place in most frames (game characters move, the webcam
    doesn't), widened to a head-and-shoulders box. None if no steady face. Pure."""
    found = []
    for dets, (w, h) in per_frame:
        for d in dets:
            if d.get("class") in FACES and float(d.get("score") or 0) >= 0.4:
                x, y, bw, bh = d["box"]
                found.append(((x + bw / 2) / w, (y + bh / 2) / h, bw / w, bh / h))
    if not found or not per_frame:
        return None
    best = []
    for cx, cy, _, _ in found:   # the biggest group of faces within 4% of each other
        near = [f for f in found if abs(f[0] - cx) < 0.04 and abs(f[1] - cy) < 0.06]
        if len(near) > len(best):
            best = near
    if len(best) < max(2, min_share * len(per_frame)):
        return None
    cx = sorted(f[0] for f in best)[len(best) // 2]
    cy = sorted(f[1] for f in best)[len(best) // 2]
    fw = sorted(f[2] for f in best)[len(best) // 2]
    fh = sorted(f[3] for f in best)[len(best) // 2]
    bw, bh = min(1.0, fw * 3.4), min(1.0, fh * 3.0)          # head and shoulders
    x0 = min(max(0.0, cx - bw / 2), 1.0 - bw)
    y0 = min(max(0.0, cy - bh * 0.42), 1.0 - bh)
    return {"x": round(x0, 3), "y": round(y0, 3), "w": round(bw, 3), "h": round(bh, 3)}


def find_facecam(path: Path, samples: int = 10) -> dict | None:
    """Look at `samples` frames spread over the video and find the webcam. Raises Unavailable."""
    _detector()
    dur = 0.0
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json",
                            str(path)], capture_output=True, text=True, timeout=60)
    try:
        dur = float(json.loads(probe.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        return None
    fps = max(samples / max(dur, 1.0), 0.05)
    rows = []
    for _, f in frames(Path(path), fps=fps, width=960):
        rows.append((detect(f), (f.shape[1], f.shape[0])))
        if len(rows) >= samples:
            break
    return facecam_from_faces(rows)
