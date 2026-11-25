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
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("editor")

OUT_W, OUT_H = 1080, 1920
FULL_H = 1350        # no facecam: a 4:5 video with the whole stream frame (nothing cropped off, Tim 20 Nov)
FONT = "DejaVu Sans"  # fonts-dejavu-core on Ubuntu (install.sh)
# On-screen text looks like TikTok's own (Tim, 20 Nov): TikTok Sans (SIL OFL, bundled in
# spikebot/fonts and handed to libass with fontsdir=, so no install is needed).
FONTS_DIR = Path(__file__).resolve().parent / "fonts"
CAPTION_FONT = "TikTok Sans"
CAPTION_HOLD_S = 1.5   # captions and the hook stay this much longer, so there's time to read them
# Text over the picture sits on a see-through dark box (Tim, 24 Nov: white text with only an outline
# "clashes and blends in" on bright or busy footage). `text_box` = the box's opacity (0 = outline only).
TEXT_BOX = 0.55
BOX_PAD = 14            # px of box around the letters


def box_colour(opacity: float) -> str:
    """ASS colour (&HAABBGGRR) of the black text box at this opacity (0-1)."""
    alpha = round(255 * (1.0 - max(0.0, min(1.0, float(opacity)))))
    return f"&H{alpha:02X}000000"


def text_box(r: dict | None) -> float:
    v = (r or {}).get("text_box")
    return TEXT_BOX if v is None else max(0.0, min(1.0, float(v)))


def ass_filter(ass_name: str) -> str:
    """The ffmpeg `ass` filter for a captions file in the working folder, with the bundled fonts."""
    if FONTS_DIR.is_dir() and ":" not in str(FONTS_DIR) and "'" not in str(FONTS_DIR):
        return f"ass=filename={ass_name}:fontsdir={FONTS_DIR}"
    return f"ass={ass_name}"


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
class VodNotReady(EditError):
    """The VOD doesn't reach the clip yet (the stream is still live and Twitch/Kick haven't
    written that part): try again later, it isn't a failure."""


def hls_segments(text: str, base_url: str) -> tuple[list[tuple[float, float, str]], str | None, bool]:
    """([(start s, length s, absolute uri)], init-segment uri or None, ended?) from a media playlist."""
    from urllib.parse import urljoin
    out, t, dur, init, ended = [], 0.0, None, None, False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            try:
                dur = float(line[8:].split(",")[0])
            except ValueError:
                dur = None
        elif line.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', line)
            init = urljoin(base_url, m.group(1)) if m else None
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line and not line.startswith("#") and dur is not None:
            out.append((t, dur, urljoin(base_url, line)))
            t += dur
            dur = None
    return out, init, ended


def cut_playlist(segs: list[tuple[float, float, str]], init: str | None, start_s: float,
                 duration_s: float) -> tuple[str, float]:
    """(a finished playlist with only the segments covering [start, start+duration], seconds to skip
    inside its first segment). A finished playlist is what lets ffmpeg seek: a live-VOD playlist
    (no ENDLIST, the stream is still going) makes ffmpeg jump to the live edge instead."""
    keep = covering(segs, start_s, duration_s)
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", f"#EXT-X-TARGETDURATION:{int(max(x[1] for x in keep)) + 1}",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    if init:
        lines.append(f'#EXT-X-MAP:URI="{init}"')
    for _t, d, uri in keep:
        lines += [f"#EXTINF:{d:.3f},", uri]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n", max(0.0, start_s - keep[0][0])


def covering(segs: list[tuple[float, float, str]], start_s: float, duration_s: float) -> list:
    end = start_s + duration_s
    keep = [x for x in segs if x[0] + x[1] > start_s and x[0] < end]
    if not keep:
        raise VodNotReady("the VOD has no part covering this moment yet")
    return keep


def _get_bytes(url: str, timeout: float = 60) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - VOD segments yt-dlp resolved
        return r.read()


def download_segments(keep: list[tuple[float, float, str]], init: str | None, folder: Path,
                      get_bytes=None, tries: int = 3) -> tuple[list[str], str | None]:
    """Fetch the covering segments (and the fMP4 init segment Twitch's newer VODs use) into `folder`,
    4 at a time, each retried. Returns (local names in order, init name or None). ffmpeg then
    reads only local files: no live playlists, proxies or half-loaded remote segments."""
    import concurrent.futures as cf
    get_bytes = get_bytes or _get_bytes
    folder.mkdir(parents=True, exist_ok=True)

    def one(job: tuple[str, str]) -> str:
        url, name = job
        last = None
        for _ in range(tries):
            try:
                data = get_bytes(url)
                if data:
                    (folder / name).write_bytes(data)
                    return name
            except Exception as e:  # noqa: BLE001 - CDN hiccup: try again
                last = e
        raise VodNotReady(f"VOD segment unavailable ({type(last).__name__ if last else 'empty'})")
    ext = lambda u: Path(u.split("?")[0]).suffix or ".ts"   # noqa: E731
    jobs = [(u, f"seg{i:04d}{ext(u)}") for i, (_t, _d, u) in enumerate(keep)]
    if init:
        jobs.append((init, f"init{ext(init)}"))
    with cf.ThreadPoolExecutor(4) as ex:
        names = list(ex.map(one, jobs))
    return (names[:-1], names[-1]) if init else (names, None)


def _get_text(url: str, timeout: float = 30) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - a VOD playlist yt-dlp resolved
        return r.read().decode("utf-8", errors="replace")


def fetch_segment(vod_url: str, start_s: float, duration_s: float, out: Path, get_text=_get_text,
                  get_bytes=None) -> Path:
    """Cut [start, start+duration] out of a VOD without downloading the whole thing:
    yt-dlp resolves the HLS playlist, only the segments covering the range are fetched and ffmpeg
    copies the exact range out of them. Raises VodNotReady while the (still live) VOD doesn't reach
    the end of the clip yet."""
    # yt-dlp from this venv (systemd's PATH doesn't include venv/bin)
    # 1080p60 when the stream had it (Tim: clips at 60 fps): resolution first, then frame rate
    p = run([sys.executable, "-m", "yt_dlp", "-g", "-f", "best[height<=1080]/best", "-S", "res:1080,fps",
             "--no-warnings", vod_url], timeout=120)
    stream = p.stdout.strip().splitlines()[0] if p.stdout.strip() else ""
    if not stream:
        raise EditError("yt-dlp returned no stream URL (VOD private or sub-only?)")
    start_s = max(start_s, 0.0)
    src, skip, tmp = stream, start_s, None
    if ".m3u8" in stream.split("?")[0]:
        try:
            text = get_text(stream)
        except Exception as e:  # noqa: BLE001 - a playlist hiccup: try again later
            raise VodNotReady(f"VOD playlist unreadable ({type(e).__name__})") from e
        segs, init, ended = hls_segments(text, stream)
        have = segs[-1][0] + segs[-1][1] if segs else 0.0
        if not ended and have < start_s + duration_s + 2:
            raise VodNotReady(f"the VOD reaches {have / 60:.0f} min so far; this clip ends at"
                              f" {(start_s + duration_s) / 60:.0f} min")
        if segs:
            keep = covering(segs, start_s, duration_s)
            skip = max(0.0, start_s - keep[0][0])
            tmp = out.parent / f".{out.stem}_parts"
            names, init_name = download_segments(keep, init, tmp, get_bytes)
            local, _ = cut_playlist([(t, d, n) for (t, d, _u), n in zip(keep, names)], init_name,
                                    keep[0][0], duration_s + skip)
            src = str((tmp / "local.m3u8").resolve())
            Path(src).write_text(local, encoding="utf-8")
    try:
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-protocol_whitelist", "file,http,https,tcp,tls,crypto,httpproxy", "-ss", f"{skip:.2f}",
             "-i", src, "-t", f"{duration_s:.2f}", "-c", "copy", "-movflags", "+faststart",
             str(out)], timeout=600)
    except subprocess.TimeoutExpired as e:   # slow CDN: worth another go later
        raise VodNotReady("the VOD download timed out") from e
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    if not out.exists() or out.stat().st_size < 10_000:
        # the VOD's newest segments can still be half-written: wait and try again (job waiting_vod,
        # retried every 10 min while the VOD wait lasts), never a hard failure
        out.unlink(missing_ok=True)
        raise VodNotReady("the VOD segment came back empty")
    return out


def probe(path: Path) -> dict:
    """Width, height and duration of a video."""
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,width,height,avg_frame_rate,r_frame_rate:format=duration", "-of", "json",
             str(path)], timeout=60)
    j = json.loads(p.stdout)
    streams = j.get("streams") or []
    s = next((x for x in streams if x.get("codec_type") == "video"), {})
    return {"width": int(s.get("width", 0)), "height": int(s.get("height", 0)),
            "fps": frame_rate(s.get("avg_frame_rate")) or frame_rate(s.get("r_frame_rate")),
            "duration": float((j.get("format") or {}).get("duration") or 0),
            "audio": any(x.get("codec_type") == "audio" for x in streams)}


def frame_rate(text) -> float | None:
    """"60000/1001" -> 59.94; "0/0", "" or junk -> None."""
    try:
        a, _, b = str(text or "").partition("/")
        v = float(a) / float(b or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return round(v, 3) if v > 0 else None


MAX_FPS = 60        # Lacy output: 60 fps, or the VOD's own rate when it is lower (#39)
DEFAULT_FPS = 30    # source rate unknown: never invent frames
CRF = 18            # final encodes (Tim, 18 Nov: CRF 18, #39)


def out_fps(src_fps: float | None) -> float:
    """Output frame rate for a source: its own rate up to 60 (59.94 stays 59.94)."""
    if not src_fps or src_fps <= 0:
        return DEFAULT_FPS
    return min(float(src_fps), MAX_FPS)


def fps_arg(fps: float) -> str:
    """ffmpeg rate text: 59.94 -> 60000/1001, 29.97 -> 30000/1001, else the number."""
    for whole in (24, 30, 60):
        if abs(fps - whole * 1000 / 1001) < 0.01:
            return f"{whole * 1000}/1001"
    return f"{fps:g}"


# --------------------------------------------------------------------------
# Layout: facecam stacked above gameplay
# --------------------------------------------------------------------------
@dataclass
class Layout:
    cam: tuple[int, int, int, int] | None   # facecam crop w, h, x, y in the source; None = no facecam
    cam_h: int                        # facecam height in the output (0 without one)
    game: tuple[int, int, int, int]   # gameplay crop w, h, x, y in the source
    game_h: int                       # gameplay height in the output
    fps: float = DEFAULT_FPS          # output frame rate (out_fps of the source)
    out_h: int = OUT_H                # 1920 with a facecam, FULL_H (4:5) without
    top: int = 0                      # no facecam: where the picture starts (black band above it)

    @property
    def picture(self) -> tuple[int, int] | None:
        """No facecam: (top, bottom) of the stream picture inside the 4:5 frame."""
        return None if self.cam else (self.top, self.top + self.game_h)

    def filter(self, src: str = "[0:v]") -> str:
        gw, gh, gx, gy = self.game
        if self.cam is None:   # the whole frame, scaled to the width, black bands above and below
            return (f"{src}crop={gw}:{gh}:{gx}:{gy},scale={OUT_W}:{self.game_h},setsar=1,"
                    f"pad={OUT_W}:{self.out_h}:0:{self.top}:black,fps={fps_arg(self.fps)}")
        cw, ch, cx, cy = self.cam
        return (f"{src}split=2[a][b];"
                f"[a]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{self.cam_h},setsar=1[cam];"
                f"[b]crop={gw}:{gh}:{gx}:{gy},scale={OUT_W}:{self.game_h},setsar=1[game];"
                f"[cam][game]vstack=inputs=2,fps={fps_arg(self.fps)}")


CAM_SHARE = 1 / 3   # facecam = top third, gameplay = bottom two thirds (Tim's rule)


def full_layout(src_w: int, src_h: int, src_fps: float | None = None) -> Layout:
    """No facecam (none found, or the campaign needs the whole frame such as KreekCraft's overlay):
    the whole stream frame, nothing cropped, in a 1080x1350 (4:5) video. The picture sits in the
    middle; the hook goes in the band above it and the captions in the band below, so no text
    covers the game or anything shown in it."""
    pic_h = even(OUT_W * src_h / src_w)
    game = (even(src_w), even(src_h), 0, 0)
    if pic_h > FULL_H - 2 * 160:            # a taller-than-16:9 source: trim top/bottom evenly
        pic_h = FULL_H - 2 * 160
        gh = even(src_w * pic_h / OUT_W)
        game = (even(src_w), gh, 0, even_pos((src_h - gh) / 2))
    top = even_pos((FULL_H - pic_h) / 2)
    return Layout(cam=None, cam_h=0, game=game, game_h=pic_h, fps=out_fps(src_fps), out_h=FULL_H, top=top)


def layout(src_w: int, src_h: int, facecam: dict | None, cam_share: float = CAM_SHARE,
           src_fps: float | None = None) -> Layout:
    """Facecam box (fractions of the source frame: x, y, w, h) on top, filling exactly
    `cam_share` of the height (default 1/3); gameplay centre-cropped to fill the rest
    (2/3). The facecam box is trimmed around its centre to the right shape, so the
    face is never squashed. The facecam is on screen for the whole clip.
    `facecam` None = there is no facecam: full_layout (4:5, whole frame), never a fake panel."""
    if not facecam:
        return full_layout(src_w, src_h, src_fps)
    cx, cy = facecam["x"] * src_w, facecam["y"] * src_h
    cw, ch = facecam["w"] * src_w, facecam["h"] * src_h
    if cw < 16 or ch < 16:
        raise EditError("facecam box is too small; check lacy.facecam in config")
    cam_h = even(OUT_H * cam_share)
    want = OUT_W / cam_h                     # output aspect of the facecam strip
    if cw / ch > want:                       # box too wide: trim the sides
        new_cw = ch * want
        cx += (cw - new_cw) / 2
        cw = new_cw
    else:                                    # box too tall: trim top and bottom
        new_ch = cw / want
        cy += (ch - new_ch) / 2
        ch = new_ch
    game_h = OUT_H - cam_h
    gh = src_h
    gw = gh * OUT_W / game_h
    if gw > src_w:
        gw = src_w
        gh = gw * game_h / OUT_W
    cam = (even(cw), even(ch), even_pos(min(cx, src_w - even(cw))), even_pos(min(cy, src_h - even(ch))))
    game = (even(gw), even(gh), even_pos((src_w - even(gw)) / 2), even_pos((src_h - even(gh)) / 2))
    return Layout(cam=cam, cam_h=cam_h, game=game, game_h=game_h, fps=out_fps(src_fps))


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
    frame) stops changing; a switched-off camera goes black.
    Without a facecam only the whole screen is checked."""
    if lay.cam is None:
        checks = [("whole screen frozen (BRB screen)", "fps=5,freezedetect=n=-60dB:d=2", frozen_seconds),
                  ("screen black", "fps=5,blackdetect=d=1:pix_th=0.08", lambda err, _t: black_seconds(err))]
        return _away(path, checks, duration, max_share)
    cw, ch, cx, cy = lay.cam
    checks = [
        ("facecam frozen", f"fps=5,crop={cw}:{ch}:{cx}:{cy},freezedetect=n=-50dB:d=2", frozen_seconds),
        ("whole screen frozen (BRB screen)", "fps=5,freezedetect=n=-60dB:d=2", frozen_seconds),
        ("facecam black (camera off)", f"fps=5,crop={cw}:{ch}:{cx}:{cy},blackdetect=d=1:pix_th=0.08",
         lambda err, _t: black_seconds(err)),
    ]
    return _away(path, checks, duration, max_share)


def mean_luma(path: Path, every_s: float = 1.0) -> float | None:
    """The picture's average brightness, 0 (black) to 1 (white), sampled every `every_s` seconds
    (stream clips: dark footage is lifted, style.stream_grade). None when it can't be measured."""
    vf = f"fps=1/{max(0.2, every_s):g},scale=96:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG"
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(path), "-vf", vf,
                            "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    vals = [float(v) for v in re.findall(r"lavfi\.signalstats\.YAVG=([\d.]+)", p.stderr)]
    if not vals:
        return None
    y = sum(vals) / len(vals)
    return round(max(0.0, min(1.0, (y - 16.0) / 219.0)), 3)   # limited range 16-235


def _away(path: Path, checks: list, duration: float, max_share: float) -> str | None:
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


CAP_MAX_CHARS = 18   # one caption = one line at 84 px on a 1080 px frame (no wrapping = no jumping)


def group_words(words: list[Word], max_words: int = 3, max_gap: float = 0.6,
                max_chars: int = CAP_MAX_CHARS) -> list[list[Word]]:
    """Short caption groups: break after 3 words, a pause, sentence punctuation, or when the
    line would get too long to stay on one line."""
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        width = len(" ".join(x.text.strip() for x in cur + [w]))
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap or width > max_chars
                    or cur[-1].text.rstrip().endswith((".", "!", "?"))):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def caption_margin(position: str, cam_h: int, size: int, text_y: float | None = None,
                   frame_h: int = OUT_H) -> int:
    """MarginV (bottom-aligned) for the word captions. Kept out of the bottom 320 px
    (TikTok/Reels/Shorts buttons) and clear of the facecam + two-line hook box
    (room for a two-line caption below it)."""
    top_limit = frame_h - cam_h - 300 - 2 * size     # caption block starts >= 300 px under the cam
    if text_y is not None:
        m = frame_h * (1 - float(text_y)) - size
    elif position == "high":
        m = top_limit
    elif position == "middle":
        m = frame_h - (cam_h + frame_h) / 2 - size
    else:
        m = 420
    return int(max(320, min(m, top_limit)))


# End cards. No "follow ..." card: calls to action go in the post caption, not the video (Tim, 20 Nov),
# so an old "cta" ending renders as no end card.
END_TEXT = {"question": "Would you have done the same?"}


MINIMAL_HOOK_S = 2.5          # "minimal" text: the hook is a short opener (+ CAPTION_HOLD_S to read it)
FOCUS_BEFORE, FOCUS_AFTER = 5.0, 2.5   # "minimal": captions only this close to the payoff


# Words that carry no meaning on their own: a caption made only of these adds nothing
# (laughing, "bro", "what", "no no no"), so it is cut.
FILLER = {
    "uh", "um", "umm", "er", "ah", "ahh", "oh", "ohh", "ooh", "huh", "hmm", "mm", "ha", "haha", "hahaha",
    "heh", "lol", "lmao", "bro", "bruh", "dude", "man", "yo", "yeah", "yea", "yes", "ya", "no", "nah",
    "nope", "ok", "okay", "like", "what", "wait", "dawg", "chat", "yup", "wow", "whoa", "damn", "god",
    "oh my god", "omg", "wha", "so", "and", "the", "a", "i", "it", "is", "go", "come", "on", "bro's",
    "let's", "lets", "right", "well", "hey", "aye", "ayy", "sheesh", "gg",
}
MIN_MEANINGFUL_WORDS = 3   # fewer meaningful words than this in the shown lines = no captions


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9']+", "", text.lower())


def meaningful(word: str) -> bool:
    t = _norm(word)
    return len(t) > 1 and t not in FILLER and not re.fullmatch(r"(ha)+h?|a+h+|o+h+|(no)+", t)


def worth_captioning(words: list[Word]) -> bool:
    """Do these words say anything? Pure laughter / filler / one-word reactions don't."""
    return sum(1 for w in words if meaningful(w.text)) >= MIN_MEANINGFUL_WORDS


def caption_focus(peak: float | None) -> tuple[float, float] | None:
    return None if peak is None else (max(0.0, peak - FOCUS_BEFORE), peak + FOCUS_AFTER)


def caption_times(groups: list[list[Word]], end_at: float, hold: float = CAPTION_HOLD_S,
                  min_s: float = 1.0) -> list[tuple[float, float]]:
    """(start, end) of each caption line: on as its first word is said, off `hold` seconds after
    its last word (time to read it before the reaction), never past the next line's start, so
    two lines are never on screen at once."""
    out = []
    for k, g in enumerate(groups):
        a = g[0].start
        b = max(g[-1].end + hold, a + min_s)
        if k + 1 < len(groups):
            b = min(b, groups[k + 1][0].start)
        out.append((a, min(b, end_at)))
    return out


def build_ass(words: list[Word], hook: str, duration: float, cam_h: int,
              watermark: str | None = None, style: dict | None = None,
              focus: tuple[float, float] | None = None, lead: float = 0.0, lead_text: str = "",
              frame_h: int = OUT_H, picture: tuple[int, int] | None = None) -> str:
    """Captions file: the hook/title line under the facecam, and the spoken words as captions
    over the gameplay.
    Look (Tim, 20 Nov): TikTok's own text style. TikTok Sans; the hook is black text on a white
    box (TikTok's "background" text), captions are white with a thin black outline, in sentence
    case. Nothing moves, bounces or fades: a line appears, stays CAPTION_HOLD_S after its last
    word so it can be read, then just disappears.
    `style` is style.resolve() output (size, position, words per caption, hook length, end card,
    text_level); None = the defaults.
    text_level "none": nothing at all. "minimal": the hook for 2.5 s (+ the hold) and captions
    only for words inside `focus` (the lines around the payoff); no focus = no captions.
    "full": the hook all the way through and every word.
    `watermark` is an optional corner text; Lacy mode always passes None.
    `lead` seconds of teaser play before the clip (`duration` is the clip's own length): the
    teaser shows `lead_text` (the voiceover line) in the hook box, then everything else runs
    as usual, `lead` seconds later.
    `frame_h` / `picture`: the 4:5 no-facecam layout (Layout.out_h / Layout.picture): the hook sits
    in the black band above the picture and the captions in the band below it."""
    r = style or {}
    if lead > 0:
        words = [Word(w.start + lead, w.end + lead, w.text) for w in words]
        focus = (focus[0] + lead, focus[1] + lead) if focus else None
        duration += lead
    level = r.get("text_level") or "full"
    font = CAPTION_FONT
    scale = float(r.get("scale") or 1.0)
    cap = int(r.get("text_px") or 76 * scale)
    hook_px = int(56 * scale)
    outline = int(r["outline"]) if r.get("outline") is not None else 4
    shadow = int(r["shadow"]) if r.get("shadow") is not None else 1
    from .style import ass_colour
    main = ass_colour(r.get("text_colour") or "#FFFFFF")
    if picture:
        p_top, p_bottom = picture
        hook_style = f"Style: Hook,{font},{hook_px},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,16,0,2,60,60,{frame_h - p_top + 34},1"
        cap_y = min(frame_h - 40, p_bottom + 48)
        pos = f"{{\\an8\\pos({OUT_W // 2},{cap_y})\\q2}}"
        cap_margin = 0
    else:
        hook_style = f"Style: Hook,{font},{hook_px},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,16,0,8,60,60,{cam_h + 36},1"
        cap_margin = caption_margin(r.get("position") or "low", cam_h, cap, r.get("text_y"), frame_h)
        pos = f"{{\\an2\\pos({OUT_W // 2},{frame_h - cap_margin})\\q2}}"
    box = text_box(r)
    if box > 0 and not picture:   # over the gameplay: on a see-through box (in the 4:5 band: plain)
        bc = box_colour(box)
        cap_style = (f"Style: Cap,{font},{cap},{main},{main},{bc},{bc},-1,0,0,0,100,100,0,0,3,{BOX_PAD},0,2,70,70,"
                     f"{cap_margin},1")
    else:
        cap_style = (f"Style: Cap,{font},{cap},{main},{main},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},"
                     f"{shadow},2,70,70,{cap_margin},1")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {frame_h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{hook_style}
{cap_style}
Style: End,{font},{int(68 * scale)},&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,20,0,5,80,80,0,1
Style: Mark,{FONT},36,&H80FFFFFF,&H80FFFFFF,&H80000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,3,30,30,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    end = ass_time(duration)
    if level == "none":
        return head
    if r.get("hook_s"):
        hook_end = ass_time(min(duration, lead + float(r["hook_s"]) + CAPTION_HOLD_S))
    elif level == "minimal":
        hook_end = ass_time(min(duration, lead + MINIMAL_HOOK_S + CAPTION_HOLD_S))
    else:
        hook_end = end
    lines = [f"Dialogue: 1,{ass_time(lead)},{hook_end},Hook,,0,0,0,,{ass_escape(hook)}"] if hook.strip() else []
    if lead > 0 and lead_text.strip():
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(lead)},Hook,,0,0,0,,{ass_escape(lead_text)}")
    if watermark:
        lines.append(f"Dialogue: 1,{ass_time(0)},{end},Mark,,0,0,0,,{ass_escape(watermark)}")
    ending = END_TEXT.get(r.get("ending") or "")
    end_at = max(0.0, duration - 2.5) if ending else duration
    if ending:
        lines.append(f"Dialogue: 2,{ass_time(end_at)},{end},End,,0,0,0,,{ass_escape(ending)}")
    if level == "minimal":
        words = [w for w in words if focus and focus[0] <= w.start <= focus[1]]
    # captions only when they add value: none at all if the lines are just reactions,
    # and any single caption made only of filler ("bro", "haha", "no no") is cut
    groups = group_words(words, max_words=int((r.get("pace") or {}).get("words") or 3))
    if not worth_captioning(words):
        groups = []
    groups = [g for g in groups if any(meaningful(w.text) for w in g) and g[0].start < end_at]
    # Every caption sits at exactly the same spot: a fixed \\pos (libass never shifts positioned
    # lines), one line only (groups are short and wrapping is off), and no two captions on screen
    # at once (each ends when the next starts).
    for g, (a, b) in zip(groups, caption_times(groups, end_at)):
        if b - a < 0.02:
            continue
        text = sentence_case(" ".join(ass_escape(w.text.strip()) for w in g))
        lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,{pos}{text}")
    return head + "\n".join(lines) + "\n"


def sentence_case(text: str) -> str:
    """TikTok-style caption case: as said, first letter capitalised, shouting kept only for
    short words already in capitals ("NO WAY" stays loud)."""
    t = text.strip()
    if not t:
        return t
    if t.isupper() and len(t) > 12:
        t = t.lower()
    return t[0].upper() + t[1:]


# --------------------------------------------------------------------------
# Pacing: which parts of the segment to keep (window around the peak + jump cuts)
# --------------------------------------------------------------------------
def keep_segments(words: list[Word], duration: float, window_s: float, peak: float | None,
                  max_gap_s: float | None, payoff: float | None = None,
                  pad: float = 0.25, peak_keep: float = 3.0, lead_in: float = 0.0) -> list[tuple[float, float]]:
    """[(start, end)] of the source to keep, in order.
    1. A window of `window_s` placed so the chat peak lands at `payoff` of the way
       through (default 55%); `lead_in` seconds are added before it (lead_in_s: the context
       before the hook) and the start snaps back to where that sentence began.
    2. With `max_gap_s`, silences longer than that are cut out (jump cuts), but the
       few seconds around the peak are always kept: the reaction may be laughter
       that has no words."""
    payoff = 0.55 if payoff is None else float(payoff)
    window_s = min(window_s, duration)
    if peak is None:
        peak = duration * 0.55
    start = min(max(0.0, peak - window_s * payoff), max(0.0, duration - window_s))
    end = min(duration, start + window_s)
    start = max(0.0, start - max(0.0, float(lead_in or 0)))
    start, end = snap_window(words, start, end, duration, pad)
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


def snap_window(words: list[Word], start: float, end: float, duration: float, pad: float = 0.25,
                phrase_gap: float = 0.45, max_extend: float = 4.0) -> tuple[float, float]:
    """Move the window's edges so nobody is cut off mid-word or mid-sentence: the start goes
    back to where the phrase it falls in began, the end runs on until the phrase it falls in
    is finished (a phrase = words less than `phrase_gap` apart), each by at most `max_extend` s."""
    ws = sorted(words, key=lambda w: w.start)
    lo, hi = max(0.0, start - max_extend), min(duration, end + max_extend)
    # start: step back over words that touch it or run into it without a pause
    s0 = start
    for w in reversed(ws):
        if w.start >= s0 or w.end < lo:
            continue
        if w.end + phrase_gap >= s0:
            s0 = max(lo, w.start - pad)
        else:
            break
    # end: run on over words that straddle it or follow it without a pause
    e0 = end
    for w in ws:
        if w.end <= e0 - 0.0 and w.end <= end:
            continue
        if w.start > hi:
            break
        if w.start - phrase_gap <= e0:
            e0 = min(hi, w.end + pad)
        else:
            break
    return round(max(0.0, s0), 3), round(min(duration, e0), 3)


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
        vf += "," + ass_filter(ass_file.name)  # relative: ffmpeg runs in the job folder
    vf += "[v]"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src.resolve()),
         "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF), "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart",
         str(out.resolve())], timeout=1800, cwd=(ass_file or out).parent)
    return out


LOGO_INSET = 28   # a sponsor logo's distance from the top-right corner


def render_variant(src: Path, out: Path, lay: Layout, ass_file: Path | None, src_w: int, src_h: int,
                   segments: list[tuple[float, float]], grade: str = "", transition: str = "cut",
                   audio: bool = True, sfx_cues: list | None = None,
                   teaser: tuple[float, float] | None = None, voice: Path | None = None,
                   logo: Path | None = None, logo_w: int = 320) -> Path:
    """One review variant: keep `segments` of the source (jump cuts), optional
    zoom punch-in on every other segment or a white flash at each cut, then the
    facecam layout, colour grade and captions.
    `teaser` (source start, end): that moment plays first, with the clip's sound ducked under
    the `voice` file (the AI voiceover hook), then a white flash into the clip itself. The
    captions file and `sfx_cues` must already be on that longer timeline.
    `logo` (a sponsor's PNG, e.g. ClipUp): `logo_w` px wide, aspect kept, top-right corner for the
    whole clip, drawn last so captions never cover it."""
    parts, labels = [], []
    if teaser is not None and audio:
        ta, tb = teaser
        parts.append(f"[0:v]trim=start={ta:.3f}:end={tb:.3f},setpts=PTS-STARTPTS[tv]")
        clip_a = (f"[0:a]atrim=start={ta:.3f}:end={tb:.3f},asetpts=PTS-STARTPTS,"
                  "aformat=sample_rates=48000:channel_layouts=stereo")
        if voice is not None:
            ms = int(VOICE_DELAY_S * 1000)
            parts.append(clip_a + f",volume={TEASER_DUCK}[tg]")
            parts.append(f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,adelay={ms}|{ms},apad[vo]")
            parts.append("[tg][vo]amix=inputs=2:normalize=0:duration=first[ta]")
        else:
            parts.append(clip_a + "[ta]")
        labels += ["[tv]", "[ta]"]
    else:
        teaser, voice = None, None
    for i, (a, b) in enumerate(segments):
        v = f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS"
        if transition == "zoom" and i % 2 == 1:
            v += f",scale={even(src_w * 1.12)}:{even(src_h * 1.12)},crop={src_w}:{src_h}"
        if (transition == "flash" and i > 0) or (teaser is not None and i == 0):
            v += ",fade=t=in:st=0:d=0.12:color=white"
        parts.append(v + f"[sv{i}]")
        labels.append(f"[sv{i}]")
        if audio:
            parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[sa{i}]")
            labels.append(f"[sa{i}]")
    n = len(segments) + (1 if teaser is not None else 0)
    parts.append("".join(labels) + f"concat=n={n}:v=1:a={1 if audio else 0}[cv]" + ("[ca]" if audio else ""))
    vf = lay.filter("[cv]")
    if grade:
        vf += "," + grade
    if ass_file is not None:
        vf += "," + ass_filter(ass_file.name)
    extra_inputs: list[str] = ["-i", str(Path(voice).resolve())] if voice is not None else []
    if audio and sfx_cues:
        for c in sfx_cues:
            extra_inputs += ["-i", str(Path(c.file).resolve())]
    if logo is not None:
        k = 1 + len(extra_inputs) // 2
        extra_inputs += ["-loop", "1", "-i", str(Path(logo).resolve())]
        vf += (f"[pre];[{k}:v]scale={even(logo_w)}:-2,format=rgba[lg];"
               f"[pre][lg]overlay=W-w-{LOGO_INSET}:{LOGO_INSET}:shortest=1:format=auto")
    graph = ";".join(parts) + ";" + vf + "[v]"
    amap = "[ca]"
    if audio and sfx_cues:
        from .sfx import mix_graph
        graph += ";" + mix_graph(sfx_cues, 1 + (voice is not None))
        amap = "[am]"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src.resolve()), *extra_inputs,
         "-filter_complex", graph, "-map", "[v]", *(["-map", amap] if audio else []),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF), "-pix_fmt", "yuv420p",
         *(["-c:a", "aac", "-b:a", "160k", "-ar", "48000"] if audio else []),
         "-movflags", "+faststart", str(out.resolve())], timeout=1800, cwd=(ass_file or out).parent)
    return out


# --------------------------------------------------------------------------
# Teaser opening (experimental): the clip's big moment first, under an AI voiceover hook
# --------------------------------------------------------------------------
TEASER_MIN_S, TEASER_MAX_S = 2.5, 4.5   # how long the teaser plays before the clip
TEASER_DUCK = 0.35                      # the clip's own sound under the voiceover
VOICE_DELAY_S = 0.12                    # the voice starts just after the first frame
REACTION_BEFORE, REACTION_AFTER = 6.0, 1.5   # chat reacts late: the loud moment is before the peak


def teaser_length(voice_s: float | None) -> float:
    """Seconds of teaser: the voiceover plus a beat, kept between TEASER_MIN_S and TEASER_MAX_S."""
    want = (voice_s or 0.0) + VOICE_DELAY_S + 0.45
    return round(min(TEASER_MAX_S, max(TEASER_MIN_S, want)), 2)


def loudness(samples, rate: int = 16000, hop_s: float = 0.1) -> list[float]:
    """RMS level per `hop_s` window of mono float samples."""
    import numpy as np
    hop = max(1, int(rate * hop_s))
    n = len(samples) // hop
    if n == 0:
        return []
    x = np.asarray(samples[: n * hop], dtype=np.float32).reshape(n, hop)
    return [float(v) for v in np.sqrt((x * x).mean(axis=1))]


def teaser_window(levels: list[float], peak: float | None, duration: float, length: float,
                  hop_s: float = 0.1) -> tuple[float, float]:
    """(start, end) of the source for the teaser: centred a little before the loudest moment
    (the streamer's reaction) in the few seconds before chat's peak, which lags the action.
    No audio levels: just before the peak; no peak either: the middle. Pure."""
    peak = duration * 0.55 if peak is None else peak
    centre = max(0.0, peak - 2.0)
    if levels:
        lo = max(0, int((peak - REACTION_BEFORE) / hop_s))
        hi = min(len(levels), int((peak + REACTION_AFTER) / hop_s) + 1)
        if hi > lo:
            # smooth over 0.5 s so one click doesn't win
            k = 5
            sm = [sum(levels[max(lo, i - k // 2):min(hi, i + k // 2 + 1)]) for i in range(lo, hi)]
            best = max(range(len(sm)), key=lambda i: sm[i])
            if sm[best] > 0:
                centre = (lo + best) * hop_s
    length = min(length, duration)
    start = min(max(0.0, centre - length * 0.55), max(0.0, duration - length))
    return round(start, 3), round(start + length, 3)


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
WHISPER_RATE = 16000


def decode_audio(path: Path, rate: int = WHISPER_RATE):
    """The file's audio as mono float32 at `rate` Hz (what Whisper wants), decoded by ffmpeg.
    A file without audio gives an empty array."""
    import numpy as np
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                        "-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                       capture_output=True, timeout=600)
    if p.returncode != 0:
        err = p.stderr.decode(errors="replace").strip()
        if "does not contain any stream" in err or "matches no streams" in err:
            return np.zeros(0, dtype=np.float32)
        raise EditError(f"could not decode the audio: {err[-300:]}")
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def transcribe(path: Path, model_name: str = "base") -> tuple[list[Word], str, float]:
    """Word timings, detected language and its probability, via faster-whisper
    (CPU, int8). Imported here so the chat bot never loads it."""
    from faster_whisper import WhisperModel  # heavy import, render subprocess only

    audio = decode_audio(path)
    if not len(audio):
        return [], "none", 0.0
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    # The audio is decoded by ffmpeg, never by faster-whisper: its decoder goes through PyAV,
    # whose API keeps changing under it (PyAV 19 dropped `metadata_errors` and every render
    # crashed). A numpy array skips PyAV completely.
    segments, info = model.transcribe(audio, word_timestamps=True, vad_filter=True)
    words = [Word(w.start, w.end, w.word) for seg in segments for w in (seg.words or [])
             if w.word.strip()]
    return words, info.language, float(info.language_probability)
