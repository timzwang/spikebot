"""Beat map of the song Tim will post a Tokyo Girl edit with, so cuts land on its real
beats and the drop instead of a fixed 120 BPM grid.

The song file is only *measured*: edits stay silent (Tim adds the licensed sound in
the TikTok / Instagram / YouTube app), so nothing of the song is ever burned in.
Each review card says where in the song to start the sound so it lines up.

Songs live in data/music/ (upload an audio file in #inspiration, paste a TikTok sound
link, or copy one in). A TikTok sound link (tiktok.com/music/..., or the vm.tiktok.com
link from "Share sound") is fetched from the sound page; any TikTok/Instagram/YouTube
link with `sound:` in the message has just its audio track pulled (the video is never
kept).
Tokyo Girl edits only ever use the Tokyo Girl song (`tokyo_girl.song`, default
"tokyo-girl": a file data/music/tokyo-girl.*). Every other song is a trending sound kept in
the library for other edits; it never replaces the Tokyo Girl song. Analysis (librosa,
imported lazily in the render subprocess) is cached in data/music/library.json.

CLI:
  python -m spikebot.music add FILE [--name NAME]   copy into data/music and analyse
  python -m spikebot.music add --url URL [--name NAME]   fetch a sound link, then the same
  python -m spikebot.music list
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("music")
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"}
MAX_SONG_S = 600
MAX_SONG_BYTES = 40 * 1024 * 1024
SHORT_HOSTS = ("vm.tiktok.com", "vt.tiktok.com")
SOUND_HINT = re.compile(r"\b(song|sound|audio)\s*:", re.I)
PLAY_URL_RE = re.compile(r'"playUrl"\s*:\s*"(https?:[^"]+)"')
TOKYO_GIRL_SONG = "tokyo-girl"
BUILD_BEATS = 4   # the edit starts one bar before the drop: logo / build-up, then the drop hits


class MusicError(RuntimeError):
    pass


def clean_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return n[:40] or "song"


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def find_drop(times, energy, beats: list[float], skip_s: float = 4.0, window_s: float = 2.0) -> float:
    """The drop: where the loudness jumps the most (average of the next `window_s`
    minus the previous `window_s`), after the first `skip_s`, snapped to a beat."""
    import numpy as np
    times = np.asarray(times, dtype=float)
    energy = np.asarray(energy, dtype=float)
    if len(times) < 4 or not beats:
        return beats[0] if beats else 0.0
    hop = float(times[1] - times[0])
    w = max(1, int(window_s / hop))
    csum = np.concatenate([[0.0], np.cumsum(energy)])
    best_t, best = None, -1e9
    for i in range(w, len(energy) - w):
        if times[i] < skip_s:
            continue
        after = (csum[i + w] - csum[i]) / w
        before = (csum[i] - csum[i - w]) / w
        if after - before > best:
            best, best_t = after - before, float(times[i])
    if best_t is None:
        return beats[0]
    return min(beats, key=lambda b: abs(b - best_t))


def fill_beats(beats: list[float]) -> list[float]:
    """Beat trackers often miss a quiet intro (or a breakdown): extend back to the start
    and fill any gap longer than 1.5 beats with the median beat length."""
    if len(beats) < 2:
        return beats
    step = statistics.median([b - a for a, b in zip(beats, beats[1:])])
    out = [beats[0]]
    while out[0] - step >= -0.02:
        out.insert(0, round(max(0.0, out[0] - step), 3))
    for b in beats[1:]:
        while b - out[-1] > 1.5 * step:
            out.append(round(out[-1] + step, 3))
        out.append(b)
    return out


def analyse(path: Path) -> dict:
    """{bpm, beats[s], drop_s, duration}. Raises MusicError."""
    try:
        import librosa
        import numpy as np
    except ImportError as e:
        raise MusicError("librosa is not installed (pip install -r requirements.txt)") from e
    try:
        y, sr = librosa.load(str(path), sr=22050, mono=True)
    except Exception as e:  # noqa: BLE001 - unreadable/odd audio
        raise MusicError(f"could not read {Path(path).name}: {e}") from e
    if len(y) < sr * 5:
        raise MusicError("the song is shorter than 5 seconds")
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    beats = [round(float(b), 3) for b in np.atleast_1d(beats)]
    if len(beats) < 8:
        raise MusicError("no steady beat found in that audio")
    beats = fill_beats(beats)
    rms = librosa.feature.rms(y=y)[0]
    times = librosa.times_like(rms, sr=sr)
    drop = find_drop(times, rms, beats)
    bpm = float(np.atleast_1d(tempo)[0])
    return {"bpm": round(bpm, 1), "beats": beats, "drop_s": round(drop, 3),
            "duration": round(len(y) / sr, 2)}


# --------------------------------------------------------------------------
# Library
# --------------------------------------------------------------------------
class Library:
    def __init__(self, folder: Path | str):
        self.root = Path(folder)
        self.index = self.root / "library.json"

    def files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [p for p in self.root.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT]

    def _load(self) -> dict:
        try:
            return json.loads(self.index.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def get(self, path: Path) -> dict:
        """Cached analysis of one song (re-analysed if the file changed)."""
        idx = self._load()
        st_ = path.stat()
        key = path.name
        row = idx.get(key)
        if row and row.get("size") == st_.st_size and row.get("mtime") == int(st_.st_mtime):
            return row
        keep = {k: row[k] for k in ("url",) if row and row.get(k)}
        row = analyse(path) | {"name": path.stem, "file": path.name, "size": st_.st_size,
                               "mtime": int(st_.st_mtime)} | keep
        idx[key] = row
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx), encoding="utf-8")
        tmp.replace(self.index)
        return row

    def current(self, name: str | None = None) -> dict | None:
        """The named song (matched like clean_name, so "Tokyo Girl" = tokyo-girl.mp3), else
        the newest one; None if there are none."""
        files = self.files()
        if not files:
            return None
        if name:
            pick = [p for p in files if p.name == name or clean_name(p.stem) == clean_name(name)]
            if not pick:
                raise MusicError(f"no song called {name!r} in {self.root}")
            return self.get(pick[0])
        return self.get(max(files, key=lambda p: p.stat().st_mtime))

    def set_meta(self, file: str, **meta) -> None:
        """Extra fields on a song's row (e.g. the sound link it came from)."""
        idx = self._load()
        if file in idx:
            idx[file].update({k: v for k, v in meta.items() if v})
            tmp = self.index.with_suffix(".tmp")
            tmp.write_text(json.dumps(idx), encoding="utf-8")
            tmp.replace(self.index)

    def trending(self, tokyo_song: str = TOKYO_GIRL_SONG) -> list[dict]:
        """Every song except the Tokyo Girl one, newest first (unreadable files skipped)."""
        out = []
        for p in sorted(self.files(), key=lambda p: p.stat().st_mtime, reverse=True):
            if clean_name(p.stem) == clean_name(tokyo_song):
                continue
            try:
                out.append(self.get(p))
            except MusicError as e:
                log.warning("skipping %s: %s", p.name, e)
        return out

    def add(self, src: Path, name: str | None = None, replace: bool = False) -> dict:
        """Copy a song in. A different song with the same name gets -2, -3... instead of
        overwriting it (many TikTok sounds are just "original sound"); the same file again
        reuses the existing one. `replace=True` overwrites (the Tokyo Girl song)."""
        self.root.mkdir(parents=True, exist_ok=True)
        stem, ext = clean_name(name or src.stem), src.suffix.lower()
        dest = self.root / f"{stem}{ext}"
        if not replace:
            data = src.read_bytes()
            n = 1
            while True:
                same = [p for p in self.files() if p.stem == dest.stem]
                if not same:
                    break
                if any(p.read_bytes() == data for p in same):
                    dest = next(p for p in same if p.read_bytes() == data)
                    break
                n += 1
                dest = self.root / f"{stem}-{n}{ext}"
        else:
            for p in self.files():           # one Tokyo Girl song, whatever the format
                if p.stem == stem and p.suffix.lower() != ext:
                    p.unlink()
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        try:
            return self.get(dest)
        except MusicError:
            dest.unlink(missing_ok=True)
            raise


# --------------------------------------------------------------------------
# Sound links (TikTok sound page, or the audio track of a video)
# --------------------------------------------------------------------------
def is_sound_page(url: str) -> bool:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return (host == "tiktok.com" or host.endswith(".tiktok.com")) and u.path.startswith("/music/")


def resolve_short(url: str, timeout: float = 10.0) -> str:
    """vm.tiktok.com/xyz -> where it redirects (a sound page or a video). Other links unchanged;
    on any failure the original link comes back."""
    if (urlparse(url).hostname or "").lower() not in SHORT_HOSTS:
        return url
    try:
        from curl_cffi import requests as http  # noqa: N813
        r = http.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True)
        return str(r.url) or url
    except Exception as e:  # noqa: BLE001 - network/Cloudflare: keep the link as it was
        log.info("could not resolve %s: %s", url, e)
        return url


def song_links(text: str, resolve=None) -> list[tuple[str, str]]:
    """(link as posted, link to fetch) for every song link in a #inspiration message:
    TikTok sound pages always; any allowed link when the message says `sound:`/`song:`."""
    from .inspiration import links
    resolve = resolve or resolve_short
    hint = bool(SOUND_HINT.search(text or ""))
    out = []
    for u in links(text):
        r = resolve(u)
        if hint or is_sound_page(r):
            out.append((u, r))
    return out


def sound_title(url: str) -> str:
    """tiktok.com/music/Tokyo-Girl-7301234567890123456 -> "Tokyo Girl"."""
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-?\d{8,}$", "", slug)
    return slug.replace("-", " ").strip() or "tiktok sound"


def sound_meta(html: str) -> str:
    """"Title - author" from a TikTok sound page; for "original sound" the author is what
    tells sounds apart. Empty if the page doesn't say."""
    def field(key):
        m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % key, html or "")
        try:
            return json.loads(f'"{m.group(1)}"').strip() if m else ""
        except ValueError:
            return ""
    title, author = field("title"), field("authorName")
    return f"{title} - {author}" if title and author and title.lower().startswith("original sound") \
        else title


def play_url(html: str) -> str | None:
    m = PLAY_URL_RE.search(html or "")
    if not m:
        return None
    return json.loads(f'"{m.group(1)}"')   # undo \u002F escapes


def _fetch_sound_page(url: str, folder: Path) -> tuple[Path, str]:
    from curl_cffi import requests as http  # noqa: N813
    try:
        page = http.get(url, impersonate="chrome", timeout=20)
        src = play_url(page.text) if page.status_code == 200 else None
        title = sound_meta(page.text) if page.status_code == 200 else ""
        if not src or urlparse(src).scheme != "https":
            raise MusicError("TikTok didn't hand over the sound file")
        r = http.get(src, impersonate="chrome", timeout=60, headers={"Referer": "https://www.tiktok.com/"})
    except MusicError:
        raise
    except Exception as e:  # noqa: BLE001
        raise MusicError(f"couldn't reach TikTok ({type(e).__name__})") from e
    if r.status_code != 200 or len(r.content) < 20_000:
        raise MusicError(f"TikTok's sound file didn't download (HTTP {r.status_code})")
    if len(r.content) > MAX_SONG_BYTES:
        raise MusicError("that sound file is too big")
    path = folder / "sound.mp3"
    path.write_bytes(r.content)
    return path, title


def _fetch_audio_track(url: str, folder: Path) -> tuple[Path, str]:
    """Only the audio of a TikTok/Instagram/YouTube video (yt-dlp -x; the video is deleted)."""
    from .inspiration import host_ok
    if not host_ok(url):
        raise MusicError("only TikTok, Instagram and YouTube links are accepted")
    title_file = folder / "title.txt"
    try:
        p = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "-q",
                            "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
                            "--match-filter", f"duration <= {MAX_SONG_S}", "--max-filesize", "300M",
                            "--print-to-file", "%(track,title)s", str(title_file),
                            "-o", str(folder / "sound.%(ext)s"), url],
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as e:
        raise MusicError("the download took over 5 minutes") from e
    files = [f for f in folder.glob("sound.*") if f.suffix.lower() in AUDIO_EXT]
    if p.returncode != 0 or not files:
        err = (p.stderr or p.stdout).strip().splitlines()
        raise MusicError(err[-1][:300] if err else "yt-dlp could not fetch it")
    for f in folder.glob("sound.*"):
        if f not in files:
            f.unlink(missing_ok=True)
    title = title_file.read_text(encoding="utf-8").strip().splitlines()[0] if title_file.exists() else ""
    return files[0], title or "sound"


def fetch(url: str, folder: Path) -> tuple[Path, str]:
    """Download the song behind a link into `folder` -> (audio file, suggested name)."""
    url = resolve_short(url)
    if is_sound_page(url):
        try:
            path, title = _fetch_sound_page(url, folder)
            return path, title or sound_title(url)
        except MusicError as e:
            raise MusicError(f"{e}. Send a link to any TikTok that uses this sound with `sound:` in "
                             "front of it, or upload the audio file") from e
    return _fetch_audio_track(url, folder)


# --------------------------------------------------------------------------
# Using a beat map
# --------------------------------------------------------------------------
def start_for(song: dict, build_beats: int = BUILD_BEATS) -> float:
    """Where in the song the edit starts: `build_beats` beats before the drop (or the
    first beat if the drop is too early)."""
    beats = song["beats"]
    i = min(range(len(beats)), key=lambda k: abs(beats[k] - song["drop_s"]))
    return beats[max(0, i - build_beats)]


def grid(song: dict, start_s: float, need_s: float) -> list[float]:
    """Beat times relative to `start_s` (0.0 first), extended past the song's last beat
    with its median beat length so a plan never runs out of beats."""
    beats = [b - start_s for b in song["beats"] if b >= start_s - 1e-6]
    if len(beats) < 2:
        raise MusicError("not enough beats after the start point")
    gaps = [b - a for a, b in zip(song["beats"], song["beats"][1:])]
    step = statistics.median(gaps)
    out = [round(b - beats[0], 4) for b in beats]
    while out[-1] < need_s + 4 * step:
        out.append(round(out[-1] + step, 4))
    return out


def window(song: dict, min_s: float, max_s: float) -> tuple[float, float]:
    """(start in the song, longest the edit can be) so the edit never runs past the end of
    the sound. Starts a bar before the drop when there's room; otherwise early enough to fit
    at least `min_s` (or the whole sound if it's shorter than that)."""
    dur = float(song["duration"])
    start = start_for(song)
    if dur - start < min_s:
        fits = [b for b in song["beats"] if b <= dur - min_s]
        start = fits[-1] if fits else song["beats"][0]
    return start, max(1.0, min(max_s, dur - start))


def mmss(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


def note(song: dict, start_s: float) -> str:
    """The line on the review card / caption file telling Tim how to line up the sound."""
    drop = song["drop_s"] - start_s
    where = f"drop lands at {mmss(drop)} in the edit" if drop >= 0 else "after the drop"
    link = f" · <{song['url']}>" if song.get("url") else ""
    return (f"🎵 Sound: **{song['name']}** — start it at **{mmss(start_s)}** "
            f"({where}, {song['bpm']:.0f} BPM){link}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m spikebot.music")
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("file", nargs="?")
    a.add_argument("--url")
    a.add_argument("--name")
    a.add_argument("--json")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from . import tokyo_girl
    from .review import load_cfg
    cfg, base = load_cfg(args.config)
    lib = Library(tokyo_girl.settings(cfg, base)["music_dir"])
    if args.cmd == "add":
        if not args.file and not args.url:
            ap.error("add needs FILE or --url")
        tg_song = tokyo_girl.settings(cfg, base).get("song") or TOKYO_GIRL_SONG
        from . import progress
        progress.arm_cancel()
        try:
            job = progress.current()
            if args.url:
                job.update(0.05, "Downloading the sound")
                lib.root.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=lib.root) as tmp:
                    path, title = fetch(args.url, Path(tmp))
                    name = args.name or title
                    job.update(0.5, "Finding the beats and the drop")
                    row = lib.add(path, name, replace=clean_name(name) == clean_name(tg_song))
                lib.set_meta(row["file"], url=args.url)
            else:
                name = args.name or Path(args.file).stem
                job.update(0.3, "Finding the beats and the drop")
                row = lib.add(Path(args.file), name, replace=clean_name(name) == clean_name(tg_song))
            res = {"ok": True, "name": row["name"], "bpm": row["bpm"], "drop_s": row["drop_s"],
                   "duration": row["duration"], "start_s": start_for(row),
                   "tokyo_girl": clean_name(row["name"]) == clean_name(tg_song)}
        except MusicError as e:
            res = {"ok": False, "error": str(e)}
        except progress.Cancelled:
            res = {"ok": False, "cancelled": True, "error": "cancelled"}
        if args.json:
            Path(args.json).write_text(json.dumps(res), encoding="utf-8")
        print(res)
        return 0 if res["ok"] else 1
    for p in sorted(lib.files(), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            r = lib.get(p)
            print(f"{r['name']}: {r['bpm']:.0f} BPM, drop at {mmss(r['drop_s'])}, {r['duration']:.0f}s"
                  + (" (Tokyo Girl song)" if clean_name(r["name"]) == TOKYO_GIRL_SONG else " (trending sound)")
                  + (f" {r['url']}" if r.get("url") else ""))
        except MusicError as e:
            print(f"{p.name}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
