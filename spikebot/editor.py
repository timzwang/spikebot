"""Video editing for Shorts: fetch a VOD segment, check the streamer is on screen,
build a vertical 1080x1920 layout with word-level captions, pick a thumbnail.

Only used by Lacy mode (spikebot/lacy.py), and only for a channel whose programme
allows clipping. The chat bot itself never calls this.

Everything is plain ffmpeg / yt-dlp subprocesses plus small pure functions
(layout maths, log parsing, ASS captions, image hashes) that are unit-tested.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("editor")

OUT_W, OUT_H = 1080, 1920
FONT = "DejaVu Sans"  # fonts-dejavu-core on Ubuntu (install.sh)


class EditError(RuntimeError):
    """A step failed in a way that means this clip can't be made."""


def run(cmd: list[str], timeout: float = 600, cwd: Path | None = None) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if p.returncode != 0:
        raise EditError(f"{Path(cmd[0]).name} failed ({p.returncode}): {p.stderr.strip()[-400:]}")
    return p


def even(x: float) -> int:
    """Nearest even size (x264 needs even dimensions), at least 2."""
    return max(2, int(round(x / 2)) * 2)


def even_pos(x: float) -> int:
    """Nearest even offset, at least 0."""
    return max(0, int(round(x / 2)) * 2)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def fetch_segment(vod_url: str, start_s: float, duration_s: float, out: Path) -> Path:
    """Cut [start, start+duration] out of a VOD without downloading the whole thing:
    yt-dlp resolves the HLS playlist, ffmpeg copies just that range."""
    # yt-dlp from this venv (systemd's PATH doesn't include venv/bin)
    p = run([sys.executable, "-m", "yt_dlp", "-g", "-f", "best[height<=1080]/best", "--no-warnings", vod_url], timeout=120)
    stream = p.stdout.strip().splitlines()[0] if p.stdout.strip() else ""
    if not stream:
        raise EditError("yt-dlp returned no stream URL (VOD private or sub-only?)")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(start_s, 0):.2f}",
         "-i", stream, "-t", f"{duration_s:.2f}", "-c", "copy", "-movflags", "+faststart",
         str(out)], timeout=600)
    if not out.exists() or out.stat().st_size < 10_000:
        raise EditError("segment download came back empty (VOD not ready yet?)")
    return out


def probe(path: Path) -> dict:
    """Width, height and duration of a video."""
    p = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", str(path)], timeout=60)
    j = json.loads(p.stdout)
    s = (j.get("streams") or [{}])[0]
    return {"width": int(s.get("width", 0)), "height": int(s.get("height", 0)),
            "duration": float((j.get("format") or {}).get("duration") or 0)}


# --------------------------------------------------------------------------
# Layout: facecam stacked above gameplay
# --------------------------------------------------------------------------
@dataclass
class Layout:
    cam: tuple[int, int, int, int]    # crop w, h, x, y in the source
    cam_h: int                        # facecam height in the output
    game: tuple[int, int, int, int]   # gameplay crop w, h, x, y in the source
    game_h: int                       # gameplay height in the output

    def filter(self) -> str:
        cw, ch, cx, cy = self.cam
        gw, gh, gx, gy = self.game
        return (f"[0:v]split=2[a][b];"
                f"[a]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{self.cam_h},setsar=1[cam];"
                f"[b]crop={gw}:{gh}:{gx}:{gy},scale={OUT_W}:{self.game_h},setsar=1[game];"
                f"[cam][game]vstack=inputs=2,fps=30")


def layout(src_w: int, src_h: int, facecam: dict, cam_max_share: float = 0.42) -> Layout:
    """Facecam box (fractions of the source frame: x, y, w, h) scaled to full width
    on top, gameplay centre-cropped to fill the rest. The facecam is on screen for
    the whole clip, never just an intro."""
    cx, cy = facecam["x"] * src_w, facecam["y"] * src_h
    cw, ch = facecam["w"] * src_w, facecam["h"] * src_h
    if cw < 16 or ch < 16:
        raise EditError("facecam box is too small; check lacy.facecam in config")
    cam_h = OUT_W * ch / cw
    cam_cap = OUT_H * cam_max_share
    if cam_h > cam_cap:  # very tall box: trim top and bottom equally, keep the face
        new_ch = cw * cam_cap / OUT_W
        cy += (ch - new_ch) / 2
        ch, cam_h = new_ch, cam_cap
    cam_h = even(cam_h)
    game_h = OUT_H - cam_h
    gh = src_h
    gw = gh * OUT_W / game_h
    if gw > src_w:
        gw = src_w
        gh = gw * game_h / OUT_W
    cam = (even(cw), even(ch), even_pos(min(cx, src_w - even(cw))), even_pos(min(cy, src_h - even(ch))))
    game = (even(gw), even(gh), even_pos((src_w - even(gw)) / 2), even_pos((src_h - even(gh)) / 2))
    return Layout(cam=cam, cam_h=cam_h, game=game, game_h=game_h)


# --------------------------------------------------------------------------
# "Is he actually there?" BRB / away / camera-off detection
# --------------------------------------------------------------------------
_FREEZE_RE = re.compile(r"freeze_(start|end): ?([\d.]+)")
_BLACK_RE = re.compile(r"black_start: ?([\d.]+) black_end: ?([\d.]+)")


def frozen_seconds(stderr: str, total: float) -> float:
    """Total frozen time from ffmpeg freezedetect output. A freeze that never
    ends runs to the end of the clip."""
    out, start = 0.0, None
    for kind, val in _FREEZE_RE.findall(stderr):
        v = float(val)
        if kind == "start":
            start = v
        elif start is not None:
            out += max(0.0, v - start)
            start = None
    if start is not None:
        out += max(0.0, total - start)
    return out


def black_seconds(stderr: str) -> float:
    return sum(max(0.0, float(b) - float(a)) for a, b in _BLACK_RE.findall(stderr))


def away_reason(path: Path, lay: Layout, duration: float, max_share: float = 0.35) -> str | None:
    """Why this clip shows a BRB/away screen, or None if he's on cam.
    A BRB screen is a still image, so the facecam area (and usually the whole
    frame) stops changing; a switched-off camera goes black."""
    cw, ch, cx, cy = lay.cam
    checks = [
        ("facecam frozen", f"fps=5,crop={cw}:{ch}:{cx}:{cy},freezedetect=n=-50dB:d=2", frozen_seconds),
        ("whole screen frozen (BRB screen)", "fps=5,freezedetect=n=-60dB:d=2", frozen_seconds),
        ("facecam black (camera off)", f"fps=5,crop={cw}:{ch}:{cx}:{cy},blackdetect=d=1:pix_th=0.08",
         lambda err, _t: black_seconds(err)),
    ]
    for label, vf, measure in checks:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vf", vf,
                            "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
        secs = measure(p.stderr, duration)
        if duration and secs / duration >= max_share:
            return f"{label} for {secs:.0f}s of {duration:.0f}s"
    return None


# --------------------------------------------------------------------------
# Captions (ASS, rendered by libass)
# --------------------------------------------------------------------------
@dataclass
class Word:
    start: float
    end: float
    text: str


def ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def ass_time(t: float) -> str:
    t = max(t, 0.0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def group_words(words: list[Word], max_words: int = 3, max_gap: float = 0.6) -> list[list[Word]]:
    """Short caption groups: break after 3 words, a pause, or sentence punctuation."""
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap
                    or cur[-1].text.rstrip().endswith((".", "!", "?"))):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[Word], hook: str, duration: float, cam_h: int,
              watermark: str | None = None) -> str:
    """Captions file: the hook/title line under the facecam for the whole clip, and
    word-by-word captions (current word in yellow) over the gameplay.
    `watermark` is an optional corner text; Lacy mode always passes None."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{FONT},58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,3,14,0,8,60,60,{cam_h + 36},1
Style: Cap,{FONT},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,70,70,420,1
Style: Mark,{FONT},36,&H80FFFFFF,&H80FFFFFF,&H80000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,3,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    end = ass_time(duration)
    lines = [f"Dialogue: 1,{ass_time(0)},{end},Hook,,0,0,0,,{ass_escape(hook)}"]
    if watermark:
        lines.append(f"Dialogue: 1,{ass_time(0)},{end},Mark,,0,0,0,,{ass_escape(watermark)}")
    for g in group_words(words):
        texts = [ass_escape(w.text.strip().upper()) for w in g]
        for i, w in enumerate(g):
            stop = g[i + 1].start if i + 1 < len(g) else max(w.end, w.start + 0.3)
            shown = " ".join(f"{{\\c&H00FFFF&}}{t}{{\\c&HFFFFFF&}}" if j == i else t
                             for j, t in enumerate(texts))
            lines.append(f"Dialogue: 0,{ass_time(w.start)},{ass_time(min(stop, duration))},"
                         f"Cap,,0,0,0,,{shown}")
    return head + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def render(src: Path, out: Path, lay: Layout, ass_file: Path | None) -> Path:
    vf = lay.filter()
    if ass_file is not None:
        vf += f",ass={ass_file.name}"  # relative: ffmpeg runs in the job folder
    vf += "[v]"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src.resolve()),
         "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart",
         str(out.resolve())], timeout=1800, cwd=(ass_file or out).parent)
    return out


def review_copy(src: Path, out: Path) -> Path:
    """Small 540x960 copy (about 5 MB a minute) that fits in a Discord message."""
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-vf", "scale=540:960", "-c:v", "libx264", "-preset", "veryfast", "-b:v", "550k",
         "-maxrate", "700k", "-bufsize", "1400k", "-c:a", "aac", "-b:a", "64k",
         "-movflags", "+faststart", str(out)], timeout=900)
    return out


# --------------------------------------------------------------------------
# Thumbnails: a frame from this clip, never the same picture twice
# --------------------------------------------------------------------------
def dhash(gray_9x8: bytes) -> int:
    """64-bit difference hash from a 9x8 greyscale image (row-major bytes)."""
    if len(gray_9x8) < 72:
        raise EditError("dhash needs 72 pixels")
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (gray_9x8[row * 9 + col] > gray_9x8[row * 9 + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def frame_hash(video: Path, t: float) -> int:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}",
                        "-i", str(video), "-frames:v", "1", "-vf", "scale=9:8,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True, timeout=60)
    if p.returncode != 0 or len(p.stdout) < 72:
        raise EditError(f"could not read a frame at {t:.1f}s")
    return dhash(p.stdout[:72])


def thumbnail_times(duration: float, peak: float | None) -> list[float]:
    """Candidate frames: around the chat peak first, then spread through the clip."""
    out: list[float] = []
    if peak is not None:
        out += [peak + d for d in (0, 1.5, -1.5, 3, -3, 5)]
    out += [duration * f for f in (0.5, 0.35, 0.65, 0.2, 0.8)]
    lo, hi = 1.0, max(duration - 0.5, 1.0)
    seen: list[float] = []
    for t in out:
        t = round(min(max(t, lo), hi), 2)
        if all(abs(t - s) > 0.4 for s in seen):
            seen.append(t)
    return seen


def pick_thumbnail(video: Path, out: Path, duration: float, peak: float | None,
                   used: list[int], min_distance: int = 10) -> int:
    """Save a JPEG of the first candidate frame that looks different from every
    thumbnail used before (hash distance >= min_distance). Returns its hash."""
    for t in thumbnail_times(duration, peak):
        try:
            h = frame_hash(video, t)
        except EditError:
            continue
        if all(hamming(h, u) >= min_distance for u in used):
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}",
                 "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)], timeout=60)
            return h
    raise EditError("every candidate frame looks like an earlier thumbnail")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Speech to text (English check + word timings)
# --------------------------------------------------------------------------
def transcribe(path: Path, model_name: str = "base") -> tuple[list[Word], str, float]:
    """Word timings, detected language and its probability, via faster-whisper
    (CPU, int8). Imported here so the chat bot never loads it."""
    from faster_whisper import WhisperModel  # heavy import, render subprocess only

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(path), word_timestamps=True, vad_filter=True)
    words = [Word(w.start, w.end, w.word) for seg in segments for w in (seg.words or [])
             if w.word.strip()]
    return words, info.language, float(info.language_probability)
