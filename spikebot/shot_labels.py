"""Vision labels for every trailer shot: what each shot shows, measured once per file.

Tim's rules need to know what is IN a shot, which numbers alone can't tell: never strip-club,
twerking or sexually suggestive footage; only clips that fit the edit (action, atmosphere),
never filler; an impactful closing shot (someone walking away, a car driving into the
distance); calm / ambient shots; the "Rockstar Games Presents" shot over the ocean and
skyscrapers (and never the plain black title card).

Each shot of a library video (the segments between its scene cuts, trailer head and tail
included) becomes one small image of 3 frames side by side; up to 16 go in one Claude
request (Sonnet, structured output). About 1p per 16 shots, once per file: the answers are
cached in library.json (`labels`) by file signature, so nothing is sent again unless the
file changes or VERSION goes up. Without ANTHROPIC_API_KEY nothing happens and every rule
falls back to the measured numbers (motion, colour, detail).

Only frames of official Rockstar trailers are sent, never anything else.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("shot_labels")

VERSION = 1
BATCH = 16
RETRY_S = 6 * 3600       # a failed file is tried again after this long
TILE_W = 256

TAGS = ("action", "atmosphere", "scenery", "establishing", "ocean", "beach", "city", "skyline", "night",
        "sunset", "car", "driving", "driving_away", "walking_away", "character", "couple", "crowd",
        "party", "gunfight", "chase", "police", "boat", "aircraft", "animal", "interior", "close_up",
        "stripclub", "twerking", "pole_dance", "suggestive",
        "title_card", "text_overlay", "rockstar_presents", "black_card", "logo", "filler")
UNSAFE = {"stripclub", "twerking", "pole_dance", "suggestive"}
FILLER = {"title_card", "text_overlay", "logo", "black_card", "filler"}
# vision tags -> the story tags tokyo_girl matches footage on
STORY_TAGS = {"car": "cars", "driving": "cars", "driving_away": "cars", "chase": "cars", "boat": "cars",
              "ocean": "city", "beach": "city", "city": "city", "skyline": "city", "night": "city",
              "sunset": "city", "scenery": "city", "establishing": "city", "couple": "couple"}

SCHEMA = {
    "type": "object",
    "properties": {"shots": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "i": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string", "enum": list(TAGS)}},
            "action": {"type": "number"}, "calm": {"type": "number"}, "impact_end": {"type": "number"},
            "subject_x": {"type": "number"}, "subject_w": {"type": "number"},
        },
        "required": ["i", "tags", "action", "calm", "impact_end", "subject_x", "subject_w"],
        "additionalProperties": False}}},
    "required": ["shots"], "additionalProperties": False,
}

SYSTEM = """You label shots of official Rockstar Games GTA VI trailers for an automated Shorts editor.
Each image is ONE shot: three frames from its start, middle and end, side by side (left to right).
For every shot give:
- tags: every tag from the list that clearly applies.
  action = fast or dramatic movement (chase, fight, jump, crash, gunfire); atmosphere = mood shot
  (lighting, weather, slow scenery); scenery / establishing = wide view of a place; driving_away = a
  vehicle moving away from the camera into the distance; walking_away = a person walking away from the
  camera; stripclub / twerking / pole_dance / suggestive = any strip club, twerking, pole dancing or
  sexualised framing of bodies (be strict: when in doubt, tag it); title_card = mostly text on screen;
  text_overlay = on-screen text over footage; rockstar_presents = the words "Rockstar Games Presents"
  are on screen; black_card = a plain black or near-black background with only text or a logo;
  logo = a game or company logo; filler = nothing worth showing (blurry, transition, near-empty frame).
- action, calm: 0 to 1 (how action-packed / how calm and ambient the shot is).
- impact_end: 0 to 1, how good this shot would be as the LAST shot of an edit (someone walking away
  from the camera, a car driving off into the distance, a wide closing view = high).
- subject_x: 0 to 1, horizontal centre of the main subject (0 = left edge, 0.5 = centre).
- subject_w: 0 to 1, width of the main subject or action as a share of the frame width (a person in
  medium shot about 0.2; two people at opposite edges about 0.9; an even landscape 0.3).
Answer for every shot index you were given."""


class LabelError(RuntimeError):
    pass


def segments(entry: dict, min_len: float = 0.5) -> list[tuple[float, float]]:
    """Every shot of a video between its scene cuts (head and tail included)."""
    dur = float(entry.get("duration") or 0)
    if entry.get("kind") != "video" or dur <= 0:
        return []
    bounds = [0.0] + sorted(c for c in entry.get("cuts") or [] if 0 < c < dur) + [dur]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a >= min_len:
            out.append((round(a, 3), round(b, 3)))
    return out


def tile(path: Path, a: float, b: float, out: Path) -> bytes:
    """One JPEG: 3 frames of the shot (start, middle, end) side by side."""
    ts = [a + (b - a) * q for q in (0.15, 0.5, 0.85)]
    inp: list[str] = []
    for t in ts:
        inp += ["-ss", f"{t:.3f}", "-i", str(path)]
    graph = ";".join(f"[{k}:v]scale={TILE_W}:-2,setsar=1[f{k}]" for k in range(3)) + ";[f0][f1][f2]hstack=3"
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *inp,
                        "-filter_complex", graph, "-frames:v", "1", "-q:v", "6", str(out)],
                       capture_output=True, timeout=120)
    if p.returncode != 0 or not out.exists():
        raise LabelError(f"frame grab failed at {a:.1f}s: {p.stderr.decode(errors='replace')[-160:]}")
    return out.read_bytes()


def clean(row: dict) -> dict:
    def unit(v) -> float:
        try:
            return round(max(0.0, min(1.0, float(v))), 2)
        except (TypeError, ValueError):
            return 0.0
    tags = sorted({t for t in row.get("tags") or [] if t in TAGS})
    return {"tags": tags, "action": unit(row.get("action")), "calm": unit(row.get("calm")),
            "impact_end": unit(row.get("impact_end")), "subject_x": unit(row.get("subject_x", 0.5)),
            "subject_w": unit(row.get("subject_w", 0.3))}


def _ask_default(base: Path | None):
    from . import ai

    def ask(content: list[dict]) -> dict:
        try:
            return ai.ask_json(SYSTEM, content, SCHEMA, base=base, max_tokens=6000, effort="low", task="labels")
        except ai.AIError as e:
            raise LabelError(str(e)) from e
    return ask


def label_file(path: Path, entry: dict, base: Path | None = None, ask=None, workers: int = 4) -> list[list]:
    """[[start, end, labels], ...] for every shot of a video. Raises LabelError."""
    from . import ai
    segs = segments(entry)
    if not segs:
        return []
    ask = ask or _ask_default(base)
    with tempfile.TemporaryDirectory() as tmp:
        imgs = [tile(Path(path), a, b, Path(tmp) / f"t{i:04d}.jpg") for i, (a, b) in enumerate(segs)]
    batches = [list(range(k, min(k + BATCH, len(segs)))) for k in range(0, len(segs), BATCH)]

    def run(idx: list[int]) -> dict[int, dict]:
        content: list[dict] = []
        for j, i in enumerate(idx):
            content.append({"type": "text", "text": f"Shot {j}:"})
            content.append(ai._image_block(imgs[i]))
        content.append({"type": "text", "text": f"Label shots 0 to {len(idx) - 1}."})
        out = ask(content)
        got = {}
        for row in (out or {}).get("shots") or []:
            j = row.get("i")
            if isinstance(j, int) and 0 <= j < len(idx):
                got[idx[j]] = clean(row)
        if len(got) < len(idx):
            raise LabelError(f"labels missing for {len(idx) - len(got)} shot(s)")
        return got

    labels: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for got in pool.map(run, batches):
            labels.update(got)
    return [[a, b, labels[i]] for i, (a, b) in enumerate(segs)]


def needs_labels(entry: dict, now: float | None = None) -> bool:
    if entry.get("kind") != "video":
        return False
    lab = entry.get("labels")
    if isinstance(lab, dict) and lab.get("v") == VERSION:
        return False
    tried = entry.get("labels_tried")
    return tried is None or (now or time.time()) - float(tried) >= RETRY_S


def ensure(path: Path, entry: dict, rel: str, base: Path | None, ask=None, now: float | None = None) -> bool:
    """Label `entry` in place when it needs it. Never raises; True when labels were added."""
    from . import ai
    if not needs_labels(entry, now):
        return False
    if ask is None and not ai.available(base):
        return False
    try:
        log.info("labelling the shots of %s (once, vision)", rel)
        rows = label_file(path, entry, base, ask)
    except Exception as e:  # noqa: BLE001 - try again later; the edit falls back to numbers
        log.warning("shot labels for %s failed: %s", rel, e)
        entry["labels_tried"] = now or time.time()
        return False
    entry["labels"] = {"v": VERSION, "shots": rows}
    entry.pop("labels_tried", None)
    unsafe = sum(1 for _, _, lab in rows if UNSAFE & set(lab["tags"]))
    if unsafe:
        log.warning("%s: %d shot(s) labelled strip club / twerking / suggestive, never used", rel, unsafe)
    return True


def for_range(entry: dict, s: float, e: float) -> dict | None:
    """The labels of the shot that overlaps [s, e] the most (None when not labelled)."""
    lab = entry.get("labels")
    if not isinstance(lab, dict):
        return None
    best, best_ov = None, 0.0
    for a, b, row in lab.get("shots") or []:
        ov = min(b, e) - max(a, s)
        if ov > best_ov:
            best, best_ov = row, ov
    return best


def presents_segments(entry: dict) -> list[tuple[float, float, str]]:
    """(start, end, "scenery" | "black") of the shots showing "Rockstar Games Presents"."""
    lab = entry.get("labels")
    if not isinstance(lab, dict):
        return []
    out = []
    for a, b, row in lab.get("shots") or []:
        tags = set(row.get("tags") or [])
        if "rockstar_presents" not in tags:
            continue
        if "black_card" in tags or ("title_card" in tags and not tags & PLACES):
            out.append((a, b, "black"))
        else:
            out.append((a, b, "scenery"))
    return out


PLACES = {"ocean", "city", "skyline", "scenery", "establishing", "beach", "sunset", "night", "atmosphere"}
