"""Beat map of the song Tim will post a Tokyo Girl edit with, so cuts land on its real
beats and the drop instead of a fixed 120 BPM grid.

The song file is only *measured*: edits stay silent (Tim adds the licensed sound in
the TikTok / Instagram / YouTube app), so nothing of the song is ever burned in.
Each review card says where in the song to start the sound so it lines up.

Songs live in data/music/ (upload an audio file in #inspiration, or copy one in).
The newest song is used unless `tokyo_girl.song` names one. Analysis (librosa,
imported lazily in the render subprocess) is cached in data/music/library.json.

CLI:
  python -m spikebot.music add FILE [--name NAME]   copy into data/music and analyse
  python -m spikebot.music list
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import statistics
import sys
from pathlib import Path

log = logging.getLogger("music")
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"}
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
        row = analyse(path) | {"name": path.stem, "file": path.name, "size": st_.st_size,
                               "mtime": int(st_.st_mtime)}
        idx[key] = row
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx), encoding="utf-8")
        tmp.replace(self.index)
        return row

    def current(self, name: str | None = None) -> dict | None:
        """The named song, else the newest one; None if there are none."""
        files = self.files()
        if not files:
            return None
        if name:
            pick = [p for p in files if p.stem == name or p.name == name]
            if not pick:
                raise MusicError(f"no song called {name!r} in {self.root}")
            return self.get(pick[0])
        return self.get(max(files, key=lambda p: p.stat().st_mtime))

    def add(self, src: Path, name: str | None = None) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        dest = self.root / f"{clean_name(name or src.stem)}{src.suffix.lower()}"
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        try:
            return self.get(dest)
        except MusicError:
            dest.unlink(missing_ok=True)
            raise


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


def mmss(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


def note(song: dict, start_s: float) -> str:
    """The line on the review card / caption file telling Tim how to line up the sound."""
    return (f"🎵 Sound: **{song['name']}** — start it at **{mmss(start_s)}** "
            f"(drop lands at {mmss(song['drop_s'] - start_s)} in the edit, {song['bpm']:.0f} BPM)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m spikebot.music")
    ap.add_argument("--config", default="config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("file")
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
        try:
            row = lib.add(Path(args.file), args.name)
            res = {"ok": True, "name": row["name"], "bpm": row["bpm"], "drop_s": row["drop_s"],
                   "duration": row["duration"], "start_s": start_for(row)}
        except MusicError as e:
            res = {"ok": False, "error": str(e)}
        if args.json:
            Path(args.json).write_text(json.dumps(res), encoding="utf-8")
        print(res)
        return 0 if res["ok"] else 1
    for p in sorted(lib.files(), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            r = lib.get(p)
            print(f"{r['name']}: {r['bpm']:.0f} BPM, drop at {mmss(r['drop_s'])}, {r['duration']:.0f}s")
        except MusicError as e:
            print(f"{p.name}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
