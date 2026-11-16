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
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height:format=duration", "-of", "json", str(path)], timeout=60)
    j = json.loads(p.stdout)
    streams = j.get("streams") or []
    s = next((x for x in streams if x.get("codec_type") == "video"), {})
    return {"width": int(s.get("width", 0)), "height": int(s.get("height", 0)),
            "duration": float((j.get("format") or {}).get("duration") or 0),
            "audio": any(x.get("codec_type") == "audio" for x in streams)}


# --------------------------------------------------------------------------
# Layout: facecam stacked above gameplay
# --------------------------------------------------------------------------
@dataclass
class Layout:
    cam: tuple[int, int, int, int]    # crop w, h, x, y in the source
    cam_h: int                        # facecam height in the output
    game: tuple[int, int, int, int]   # gameplay crop w, h, x, y in the source
    game_h: int                       # gameplay height in the output

    def filter(self, src: str = "[0:v]") -> str:
        cw, ch, cx, cy = self.cam
        gw, gh, gx, gy = self.game
        return (f"{src}split=2[a][b];"
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


def caption_margin(position: str, cam_h: int, size: int, text_y: float | None = None) -> int:
    """MarginV (bottom-aligned) for the word captions. Kept out of the bottom 320 px
    (TikTok/Reels/Shorts buttons) and clear of the facecam + two-line hook box
    (room for a two-line caption below it)."""
    top_limit = OUT_H - cam_h - 300 - 2 * size     # caption block starts >= 300 px under the cam
    if text_y is not None:
        m = OUT_H * (1 - float(text_y)) - size
    elif position == "high":
        m = top_limit
    elif position == "middle":
        m = OUT_H - (cam_h + OUT_H) / 2 - size
    else:
        m = 420
    return int(max(320, min(m, top_limit)))


END_TEXT = {"question": "Would you have done the same?", "cta": "Follow for daily Lacy GTA clips"}


def build_ass(words: list[Word], hook: str, duration: float, cam_h: int,
              watermark: str | None = None, style: dict | None = None) -> str:
    """Captions file: the hook/title line under the facecam, and word-by-word
    captions (current word highlighted) over the gameplay.
    `style` is style.resolve() output (font, size, position, colours, words per
    caption, hook length, end card); None = the original look.
    `watermark` is an optional corner text; Lacy mode always passes None."""
    r = style or {}
    font = r.get("font") or FONT
    scale = float(r.get("scale") or 1.0)
    cap = int(r.get("text_px") or 84 * scale)
    hook_px = int(58 * scale)
    outline = int(r["outline"]) if r.get("outline") is not None else 7
    shadow = int(r["shadow"]) if r.get("shadow") is not None else 2
    from .style import ass_colour
    main = ass_colour(r.get("text_colour") or "#FFFFFF")
    hi = ass_colour(r.get("highlight") or "#FFFF00")[4:]  # BBGGRR for inline \\c&H..&
    margin = caption_margin(r.get("position") or "low", cam_h, cap, r.get("text_y"))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font},{hook_px},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,3,14,0,8,60,60,{cam_h + 36},1
Style: Cap,{font},{cap},{main},{main},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,70,70,{margin},1
Style: End,{font},{int(72 * scale)},&H00FFFFFF,&H00FFFFFF,&H30000000,&H30000000,-1,0,0,0,100,100,0,0,3,20,0,5,80,80,0,1
Style: Mark,{FONT},36,&H80FFFFFF,&H80FFFFFF,&H80000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,3,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    end = ass_time(duration)
    hook_end = ass_time(min(duration, float(r["hook_s"]))) if r.get("hook_s") else end
    lines = [f"Dialogue: 1,{ass_time(0)},{hook_end},Hook,,0,0,0,,{ass_escape(hook)}"]
    if watermark:
        lines.append(f"Dialogue: 1,{ass_time(0)},{end},Mark,,0,0,0,,{ass_escape(watermark)}")
    ending = END_TEXT.get(r.get("ending") or "")
    end_at = max(0.0, duration - 2.5) if ending else duration
    if ending:
        lines.append(f"Dialogue: 2,{ass_time(end_at)},{end},End,,0,0,0,,{ass_escape(ending)}")
    for g in group_words(words, max_words=int((r.get("pace") or {}).get("words") or 3)):
        texts = [ass_escape(w.text.strip().upper()) for w in g]
        for i, w in enumerate(g):
            if w.start >= end_at:
                break
            stop = g[i + 1].start if i + 1 < len(g) else max(w.end, w.start + 0.3)
            shown = " ".join(f"{{\\c&H{hi}&}}{t}{{\\c&H{main[4:]}&}}" if j == i else t
                             for j, t in enumerate(texts))
            lines.append(f"Dialogue: 0,{ass_time(w.start)},{ass_time(min(stop, end_at))},"
                         f"Cap,,0,0,0,,{shown}")
    return head + "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Pacing: which parts of the segment to keep (window around the peak + jump cuts)
# --------------------------------------------------------------------------
def keep_segments(words: list[Word], duration: float, window_s: float, peak: float | None,
                  max_gap_s: float | None, payoff: float | None = None,
                  pad: float = 0.25, peak_keep: float = 3.0) -> list[tuple[float, float]]:
    """[(start, end)] of the source to keep, in order.
    1. A window of `window_s` placed so the chat peak lands at `payoff` of the way
       through (default 55%).
    2. With `max_gap_s`, silences longer than that are cut out (jump cuts), but the
       few seconds around the peak are always kept: the reaction may be laughter
       that has no words."""
    payoff = 0.55 if payoff is None else float(payoff)
    window_s = min(window_s, duration)
    if peak is None:
        peak = duration * 0.55
    start = min(max(0.0, peak - window_s * payoff), max(0.0, duration - window_s))
    end = min(duration, start + window_s)
    if not max_gap_s:
        return [(round(start, 3), round(end, 3))]
    spans = [(max(start, w.start - pad), min(end, w.end + pad)) for w in words
             if w.end > start and w.start < end]
    spans.append((max(start, peak - peak_keep), min(end, peak + peak_keep)))
    spans.sort()
    merged: list[list[float]] = []
    for a, b in spans:
        if b <= a:
            continue
        if merged and a - merged[-1][1] <= max_gap_s:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = [(round(a, 3), round(b, 3)) for a, b in merged if b - a >= 0.4]
    return out or [(round(start, 3), round(end, 3))]


def remap_words(words: list[Word], segments: list[tuple[float, float]]) -> list[Word]:
    """Word timings on the cut timeline (words outside the kept parts are dropped)."""
    out = []
    offset = 0.0
    for a, b in segments:
        for w in words:
            if a <= w.start < b:
                out.append(Word(round(offset + w.start - a, 3), round(offset + min(w.end, b) - a, 3), w.text))
        offset += b - a
    return out


def cut_length(segments: list[tuple[float, float]]) -> float:
    return sum(b - a for a, b in segments)


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


def render_variant(src: Path, out: Path, lay: Layout, ass_file: Path | None, src_w: int, src_h: int,
                   segments: list[tuple[float, float]], grade: str = "", transition: str = "cut",
                   audio: bool = True) -> Path:
    """One review variant: keep `segments` of the source (jump cuts), optional
    zoom punch-in on every other segment or a white flash at each cut, then the
    facecam layout, colour grade and captions."""
    parts, labels = [], []
    for i, (a, b) in enumerate(segments):
        v = f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS"
        if transition == "zoom" and i % 2 == 1:
            v += f",scale={even(src_w * 1.12)}:{even(src_h * 1.12)},crop={src_w}:{src_h}"
        if transition == "flash" and i > 0:
            v += ",fade=t=in:st=0:d=0.12:color=white"
        parts.append(v + f"[sv{i}]")
        labels.append(f"[sv{i}]")
        if audio:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[sa{i}]")
            labels.append(f"[sa{i}]")
    n = len(segments)
    parts.append("".join(labels) + f"concat=n={n}:v=1:a={1 if audio else 0}[cv]" + ("[ca]" if audio else ""))
    vf = lay.filter("[cv]")
    if grade:
        vf += "," + grade
    if ass_file is not None:
        vf += f",ass={ass_file.name}"
    graph = ";".join(parts) + ";" + vf + "[v]"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src.resolve()),
         "-filter_complex", graph, "-map", "[v]", *(["-map", "[ca]"] if audio else []),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         *(["-c:a", "aac", "-b:a", "160k", "-ar", "48000"] if audio else []),
         "-movflags", "+faststart", str(out.resolve())], timeout=1800, cwd=(ass_file or out).parent)
    return out


def preview(src: Path, out: Path, max_bytes: int, duration: float | None = None) -> Path:
    """540x960 copy squeezed under max_bytes (three of them go in one Discord message)."""
    duration = duration or probe(src)["duration"] or 60.0
    total_kbps = max_bytes * 8 * 0.88 / 1000 / max(duration, 1.0)
    has_audio = probe(src).get("audio")
    a_kbps = 48 if has_audio else 0
    v_kbps = max(120, int(total_kbps - a_kbps))
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-vf", "scale=540:960", "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{v_kbps}k",
         "-maxrate", f"{int(v_kbps * 1.2)}k", "-bufsize", f"{v_kbps * 2}k",
         *(["-c:a", "aac", "-b:a", f"{a_kbps}k", "-ac", "1"] if has_audio else ["-an"]),
         "-movflags", "+faststart", str(out)], timeout=900)
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
