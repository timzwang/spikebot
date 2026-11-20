"""Recover which trailer clips a rendered Tokyo Girl / trend edit used, from the video itself.

Edits made before clip lists were saved on each variant (or any finished edit Tim points at) can
still be re-rendered "with the same clips": the edit's cuts are found by scene detection, a few
frames of every shot are hashed (a gradient hash of the picture's centre, so a grade, grain or
text card barely moves it) and compared with frames of every file in the media library, sampled
twice a second and cached in data/tokyo_girl/frame_hashes.json. The best match gives the file,
the second in it and so the slot's clip and offset.

Needs numpy (installed with librosa). Pure ffmpeg otherwise; never downloads anything.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from . import editor

log = logging.getLogger("footage_match")

HW, HH = 17, 16           # gradient hash: 16x16 = 256 bits
SAMPLE_FPS = 2
MAX_DIST = 0.30           # worse than this share of differing bits = no match
CENTRE = 0.6              # share of the picture (each way) that is compared


def _frames(path: Path, vf: str, fps: float | None = SAMPLE_FPS, timeout: int = 1800):
    """Tiny grey frames as a numpy array (n, HH, HW) through `vf` (ends before scale)."""
    import numpy as np
    chain = (f"fps={fps}," if fps else "") + vf + f",scale={HW}:{HH}:flags=area,format=gray"
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", chain,
                        "-f", "rawvideo", "-"], capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise editor.EditError(f"ffmpeg could not read {path.name}: {p.stderr.decode(errors='replace')[-200:]}")
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    return buf[: len(buf) // (HW * HH) * HW * HH].reshape(-1, HH, HW)


def hashes(frames):
    """(n, HH, HW) grey -> (n, 256) bool gradient hashes."""
    import numpy as np
    f = frames.astype(np.int16)
    return (f[:, :, 1:] > f[:, :, :-1]).reshape(len(frames), -1)


def thumbs(frames):
    """(n, HH, HW) grey -> (n, 16) brightness layout (4x4 blocks), normalised so a grade or a
    brighter/darker copy matches: tells apart smooth shots whose gradients look alike."""
    import numpy as np
    f = frames[:, :16, :16].astype(np.float32).reshape(len(frames), 4, 4, 4, 4).mean(axis=(2, 4))
    f = f.reshape(len(frames), 16)
    f -= f.mean(axis=1, keepdims=True)
    return f / (f.std(axis=1, keepdims=True) + 8.0)


def source_crop(framing: str, aspect: float, crop: list[int] | None) -> str:
    """What part of the (letterbox-cropped) source the centre of the edit shows."""
    pre = f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]}," if crop else ""
    if framing == "fit":                 # the whole frame is shown: compare its centre
        w = CENTRE
    else:                                # fill: a 4:5 strip from the middle of a wide frame (edits are 4:5)
        from .shot_info import CROP_ASPECT
        w = CENTRE * min(1.0, CROP_ASPECT / aspect)
    return pre + f"crop=iw*{w:.4f}:ih*{CENTRE}:(iw-iw*{w:.4f})/2:ih*{(1 - CENTRE) / 2:.3f}"


def edit_crop(framing: str, aspect: float) -> str:
    """The centre of the edit's picture that matches `source_crop` (fit: inside the sharp band)."""
    if framing == "fit":
        band = 1 / aspect                # band height as a share of the width
        return (f"crop=iw*{CENTRE}:iw*{band * CENTRE:.4f}:iw*{(1 - CENTRE) / 2:.3f}:"
                f"(ih-iw*{band * CENTRE:.4f})/2")
    return f"crop=iw*{CENTRE}:ih*{CENTRE}:iw*{(1 - CENTRE) / 2:.3f}:ih*{(1 - CENTRE) / 2:.3f}"


class Index:
    """Frame hashes of every library file, per framing, cached by file signature."""

    def __init__(self, media_dir: Path, entries: dict, cache: Path):
        self.media, self.entries, self.cache = Path(media_dir), entries, Path(cache)

    def load(self, framing: str) -> dict:
        try:
            data = json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        import numpy as np
        out, changed = {}, False
        for rel, e in self.entries.items():
            key = f"{framing}{'' if framing == 'fit' else '@4x5'}|{rel}"   # 9:16-era hashes don't match
            sig = e.get("sig")
            row = data.get(key)
            if not row or row.get("sig") != sig or "thumb" not in row:
                aspect = (e["crop"][0] / e["crop"][1]) if e.get("crop") else e["w"] / max(e["h"], 1)
                vf = source_crop(framing, aspect, e.get("crop"))
                try:
                    fr = _frames(self.media / rel, vf, None if e.get("kind") == "image" else SAMPLE_FPS)
                except (editor.EditError, subprocess.TimeoutExpired) as err:
                    log.warning("frame hashes for %s failed: %s", rel, err)
                    continue
                h = hashes(fr)
                row = {"sig": sig, "aspect": round(aspect, 3), "kind": e.get("kind"),
                       "bits": ["".join("1" if b else "0" for b in x) for x in h],
                       "thumb": [[round(float(v), 2) for v in x] for x in thumbs(fr)]}
                data[key] = row
                changed = True
            out[rel] = {"aspect": row["aspect"], "kind": row["kind"],
                        "h": np.array([[c == "1" for c in s] for s in row["bits"]], dtype=bool),
                        "t": np.array(row["thumb"], dtype=np.float32).reshape(-1, 16)}
        if changed:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.cache)
        return out


EDIT_FPS = 10.0
MIN_RUN_S = 0.3           # shorter matches are noise (flashes, fades) and join a neighbour


def _popcount_dist(edit_bits, lib_bits, edit_th=None, lib_th=None):
    """Distance between every edit frame and every library frame (M x N), 0 = same: the share
    of differing gradient bits, blended with the brightness-layout difference when given."""
    import numpy as np
    e = np.packbits(edit_bits, axis=1)
    lib = np.packbits(lib_bits, axis=1)
    table = np.array([bin(x).count("1") for x in range(256)], dtype=np.uint16)
    out = np.empty((len(e), len(lib)), dtype=np.float32)
    for k in range(0, len(e), 32):   # in chunks: a big library stays small in memory
        x = np.bitwise_xor(e[k:k + 32, None, :], lib[None, :, :])
        d = table[x].sum(axis=2) / edit_bits.shape[1]
        if edit_th is not None:
            t = np.abs(edit_th[k:k + 32, None, :] - lib_th[None, :, :]).mean(axis=2)
            d = 0.6 * d + 0.4 * np.minimum(1.0, t / 1.2)
        out[k:k + 32] = d
    return out


def frame_matches(video: Path, lib: dict, framing: str) -> list[tuple[float, str | None, float, float]]:
    """(edit time, source file or None, source time, distance) for every edit frame."""
    import numpy as np
    groups: dict[float, list[str]] = {}
    for rel, v in lib.items():
        groups.setdefault(round(v["aspect"], 2), []).append(rel)
    best = None
    for aspect, rels in groups.items():
        ef = _frames(video, edit_crop(framing, aspect), fps=EDIT_FPS)
        eb, et = hashes(ef), thumbs(ef)
        owner, when, bits, ths = [], [], [], []
        for rel in rels:
            v = lib[rel]
            for j in range(len(v["h"])):
                owner.append(rel)
                when.append(0.0 if v["kind"] == "image" else j / SAMPLE_FPS)
            bits.append(v["h"])
            ths.append(v["t"])
        if not bits:
            continue
        d = _popcount_dist(eb, np.concatenate(bits), et, np.concatenate(ths))
        j = d.argmin(axis=1)
        dist = d[np.arange(len(d)), j]
        rows = [(i / EDIT_FPS, owner[j[i]], when[j[i]], float(dist[i])) for i in range(len(d))]
        best = rows if best is None else [a if a[3] <= b[3] else b for a, b in zip(best, rows)]
    return [(t, src if dd <= MAX_DIST else None, st, dd) for t, src, st, dd in (best or [])]


def runs(matches: list[tuple[float, str | None, float, float]], shot_of) -> list[dict]:
    """Group frame matches into shots: consecutive frames that land in the same library shot.
    `shot_of(src, src_time)` -> the Shot (or None)."""
    out: list[dict] = []
    for t, src, st, d in matches:
        shot = shot_of(src, st) if src else None
        key = shot.key if shot else None
        cur = out[-1] if out else None
        if cur and cur["key"] == key:
            cur["end"] = t + 1 / EDIT_FPS
            cur["at"].append((t, st))
            cur["d"].append(d)
        else:
            out.append({"key": key, "shot": shot, "src": src if shot else None, "start": t,
                        "end": t + 1 / EDIT_FPS, "at": [(t, st)], "d": [d]})
    changed = True   # noise: very short runs join a neighbour
    while changed and len(out) > 1:
        changed = False
        for n, r in enumerate(out):
            if r["end"] - r["start"] < MIN_RUN_S:
                if n > 0:
                    out[n - 1]["end"] = r["end"]
                else:
                    out[1]["start"] = r["start"]
                out.pop(n)
                changed = True
                break
    merged: list[dict] = []
    for r in out:
        if merged and merged[-1]["key"] == r["key"]:
            merged[-1]["end"] = r["end"]
            merged[-1]["at"] += r["at"]
            merged[-1]["d"] += r["d"]
        else:
            merged.append(r)
    return merged


def recover(video: Path, index: Index, shots: list, framing: str = "fill") -> list[dict]:
    """[{key, offset, pan, start, dur, src, dist}] per shot of `video`, in order; key None where
    nothing in the library matched."""
    lib = index.load(framing)
    if not lib:
        return []
    by_src: dict[str, list] = {}
    for s_ in shots:
        by_src.setdefault(s_.src, []).append(s_)

    def shot_of(src, st):
        cands = by_src.get(src) or []
        return next((x for x in cands if x.kind == "image" or x.start - 0.3 <= st <= x.end + 0.3), None)
    out = []
    for r in runs(frame_matches(video, lib, framing), shot_of):
        shot = r["shot"]
        off = 0.0
        if shot is not None and shot.kind == "video":
            starts = sorted(st - (t - r["start"]) for t, st in r["at"])
            off = round(min(max(starts[len(starts) // 2], shot.start), shot.end), 3)
        out.append({"key": r["key"], "offset": off, "pan": 1, "start": round(r["start"], 2),
                    "dur": round(r["end"] - r["start"], 2), "src": r["src"],
                    "dist": round(sorted(r["d"])[len(r["d"]) // 2], 3)})
    log.info("recovered %d/%d clips from %s", sum(1 for x in out if x["key"]), len(out), video.name)
    return out
