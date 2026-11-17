"""Sound effects for Lacy clips: a whoosh into each cut, a riser into the payoff and a
hit on it, mixed quietly under his voice. That polish is a big part of why human edits
feel edited.

Files come from Tim (royalty-free packs, e.g. Pixabay sound effects): drop them in
data/sfx/whoosh/, data/sfx/hit/ and data/sfx/riser/ (mp3/wav/ogg). No files = no SFX,
nothing breaks. Tokyo Girl edits stay silent (the sound is added in the app), so they
never get SFX. Volume: custom setting `sfx` (0 = off, default 0.35), via /feedback.
"""
from __future__ import annotations

import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

KINDS = ("whoosh", "hit", "riser")
EXT = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}
DEFAULT_VOLUME = 0.35
MAX_WHOOSHES = 6


@dataclass
class Cue:
    file: Path
    at: float        # seconds into the clip
    volume: float


def library(folder: Path) -> dict[str, list[Path]]:
    out = {}
    for k in KINDS:
        d = Path(folder) / k
        out[k] = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in EXT) if d.is_dir() else []
    return out


def length(path: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                        str(path)], capture_output=True, text=True, timeout=30)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def cues(segments: list[tuple[float, float]], peak: float | None, transition: str, lib: dict[str, list[Path]],
         rng: random.Random, volume: float = DEFAULT_VOLUME, length_of=length) -> list[Cue]:
    """Where each effect goes on the cut timeline. Whooshes only on visible cuts (zoom /
    flash transitions; plain jump cuts stay clean), ending just after the cut."""
    if volume <= 0:
        return []
    out: list[Cue] = []
    total = sum(b - a for a, b in segments)
    if transition in ("zoom", "flash") and lib.get("whoosh"):
        t, cuts = 0.0, []
        for a, b in segments[:-1]:
            t += b - a
            cuts.append(t)
        if len(cuts) > MAX_WHOOSHES:
            step = len(cuts) / MAX_WHOOSHES
            cuts = [cuts[int(i * step)] for i in range(MAX_WHOOSHES)]
        for c in cuts:
            f = rng.choice(lib["whoosh"])
            out.append(Cue(f, max(0.0, c - min(0.35, length_of(f) * 0.6)), volume * 0.8))
    if peak is not None and 0.5 < peak < total:
        if lib.get("riser"):
            f = rng.choice(lib["riser"])
            n = length_of(f)
            if 0.5 <= n <= peak:
                out.append(Cue(f, peak - n, volume * 0.7))
        if lib.get("hit"):
            out.append(Cue(rng.choice(lib["hit"]), peak, volume))
    return sorted(out, key=lambda c: c.at)


def mix_graph(cues_: list[Cue], first_input: int, voice: str = "[ca]", out: str = "[am]") -> str:
    """ffmpeg filter that delays each effect to its time and mixes them under the voice
    (the voice keeps its level: normalize=0, length = the voice's)."""
    if not cues_:
        return f"{voice}anull{out}"
    parts, labels = [], []
    for i, c in enumerate(cues_):
        ms = int(round(c.at * 1000))
        parts.append(f"[{first_input + i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                     f"adelay={ms}|{ms},volume={c.volume:.3f}[fx{i}]")
        labels.append(f"[fx{i}]")
    parts.append(f"{voice}aformat=sample_rates=48000:channel_layouts=stereo[v0a]")
    parts.append(f"[v0a]{''.join(labels)}amix=inputs={len(labels) + 1}:normalize=0:duration=first{out}")
    return ";".join(parts)
