"""GTA 5 vs GTA 6 comparison edits (Tim, 18 Nov: "then vs now" comparisons do well when the
difference is big, the edit is smooth and the captions are good).

Footage: official Rockstar media only. GTA 6 = the shared library (data/rockstar_media); GTA 5 =
Rockstar's original 2013 trailers and gameplay video in a hidden sub-folder of it
(data/rockstar_media/.gta5), so GTA 6 edits never see it. Never the PS5 "Expanded and
Enhanced" version: it looks too close to GTA 6 for a fair "13 years later".

Pairing: every shot is labelled once with a theme (beaches, faces, night city...) and a
quality score by Claude from one frame (cached in data/compare/labels.json). Each edit pairs
the same theme from both games, biggest-difference themes first, least-used shots first.

Layouts (per version): "stack" = GTA 5 on top, GTA 6 below, at the same time; "sequence" =
the GTA 5 shot, then the GTA 6 shot in the same 4:5 box. Corner labels "GTA 5 · 2013" /
"GTA 6 · 2026", the theme as a caption in the band above, a hook first and a question last.
Silent like every trailer edit (the sound is added in the app); rendered with ffmpeg.
"""
from __future__ import annotations

import json
import logging
import random
import subprocess
import time
from pathlib import Path

from . import editor
from . import tokyo_girl as TG

log = logging.getLogger("compare")

GTA5_DIR = ".gta5"   # inside the shared media dir: hidden, so GTA 6 edits never pick it
_NOT_REMASTER = "(?i)ps5|xbox series|expanded|enhanced|online|next.gen"
_GTA5 = "(?i)grand[^a-z0-9]+theft[^a-z0-9]+auto[^a-z0-9]+v([^a-z0-9i]|$)"
GTA5_SEARCHES = [
    {"query": "Grand Theft Auto V official trailer", "title": _GTA5 + ".*trailer", "exclude": _NOT_REMASTER,
     "max": 8},
    {"query": "Grand Theft Auto V official gameplay video", "title": _GTA5 + ".*gameplay", "exclude": _NOT_REMASTER},
]

# theme -> (caption on screen, how big the GTA 5 -> 6 difference usually looks: 3 = huge)
THEMES = {
    "faces": ("Faces", 3), "beach": ("Beaches", 3), "water": ("Water", 3), "city_night": ("City at night", 3),
    "crowd": ("Crowds", 3), "police": ("Police", 3), "animals": ("Wildlife", 3), "nature": ("Nature", 2),
    "city_day": ("Streets", 2), "skyline": ("Skyline", 2), "cars": ("Cars", 2), "driving": ("Driving", 2),
    "sunset": ("Sunsets", 2), "interior": ("Interiors", 2), "boats": ("Boats", 2), "weather": ("Weather", 2),
    "action": ("Action", 2), "aircraft": ("Planes & helicopters", 1),
}
HOOKS = ["GTA 5 vs *GTA 6*", "13 years of *upgrades*", "GTA 5 → *GTA 6*", "Then vs *now*"]
QUESTIONS = ["Which upgrade is the biggest?", "Which one shocked you most?", "Was it worth the 13 year wait?"]
LABEL5 = ("GTA 5 · 2013", "#5BC236")
LABEL6 = ("GTA 6 · 2026", "#FF4FA3")
EXPORT = "exports/compare"


def gta5_cfg(lcfg: dict) -> dict:
    """Tokyo Girl settings pointed at the GTA 5 library (its own sources, no GTA 6 links)."""
    return dict(lcfg, media_dir=str(Path(lcfg["media_dir"]) / GTA5_DIR), sources=[], searches=GTA5_SEARCHES,
                trailer_head_s=4.0, trailer_tail_s=8.0)


def prefixed(shots: list[TG.Shot]) -> list[TG.Shot]:
    """GTA 5 shots with their path under the shared media dir (one renderer for both games)."""
    import dataclasses
    return [dataclasses.replace(s, src=f"{GTA5_DIR}/{s.src}") for s in shots]


# --------------------------------------------------------------------------
# Labels (Claude, once per shot)
# --------------------------------------------------------------------------
LABEL_SYSTEM = """You label frames from official Grand Theft Auto trailers so an editor can pair the same
kind of shot from GTA 5 (2013) and GTA 6 (2026) side by side. For each frame give:
- theme: what the shot mainly shows, one of the allowed values ("other" when none fits, or when the frame is
  a logo, title card, text, black, or mostly blurred).
- quality 0-3: how good this frame is as a clear, readable example of its theme on a phone screen
  (3 = striking and obvious at a glance, 0 = unusable: dark, blurry, cluttered, a cutscene close-up of text).
Judge only what is visible. Never mark nudity, strip clubs or sexual content as anything but "other" with quality 0."""


def label_schema() -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": {"shots": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "theme": {"type": "string", "enum": [*THEMES, "other"]},
                               "quality": {"type": "integer"}},
                "required": ["id", "theme", "quality"]}}},
            "required": ["shots"]}


def thumb(media: Path, shot: TG.Shot, width: int = 384) -> bytes | None:
    """One JPEG frame from the middle of a shot (small: cheap to send)."""
    t = 0.0 if shot.kind == "image" else shot.start + shot.length / 2
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i",
                        str((media / shot.src).resolve()), "-frames:v", "1", "-vf", f"scale={width}:-2",
                        "-q:v", "6", "-f", "image2", "-c:v", "mjpeg", "-"], capture_output=True, timeout=60)
    return p.stdout if p.returncode == 0 and p.stdout else None


def load_labels(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def label_shots(shots: list[TG.Shot], media: Path, path: Path, base: Path | None = None, client=None,
                batch: int = 40, max_new: int = 600, ask=None) -> dict:
    """{shot key: {"theme", "quality"}} for every shot, asking Claude only about new ones.
    `ask(content) -> dict` replaces the API call in tests."""
    from . import ai, progress
    labels = load_labels(path)
    todo = [s for s in shots if s.key not in labels][:max_new]
    job = progress.current()
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        job.update(i / max(1, len(todo)), detail=f"labelling shots {i + 1}-{i + len(chunk)} of {len(todo)}")
        content: list[dict] = []
        ids = {}
        for k, s in enumerate(chunk):
            jpg = thumb(media, s)
            if not jpg:
                labels[s.key] = {"theme": "other", "quality": 0}
                continue
            ids[f"s{k}"] = s.key
            content.append({"type": "text", "text": f"id s{k}"})
            content.append(ai._image_block(jpg))
        if not ids:
            continue
        content.append({"type": "text", "text": f"Label all {len(ids)} frames above."})
        out = ask(content) if ask else ai.ask_json(LABEL_SYSTEM, content, label_schema(), base=base, max_tokens=6000,
                                                   client=client, effort="low", task="feedback")
        for row in out.get("shots") or []:
            key = ids.get(row.get("id"))
            if key:
                labels[key] = {"theme": row["theme"] if row.get("theme") in THEMES else "other",
                               "quality": max(0, min(3, int(row.get("quality") or 0)))}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(labels, indent=1), encoding="utf-8")   # saved per batch: a crash keeps the work
    return labels


# --------------------------------------------------------------------------
# Pairs and plan (pure)
# --------------------------------------------------------------------------
def by_theme(shots: list[TG.Shot], labels: dict, need_s: float) -> dict[str, list[TG.Shot]]:
    out: dict[str, list[TG.Shot]] = {}
    for s in shots:
        lb = labels.get(s.key) or {}
        if lb.get("theme") in THEMES and lb.get("quality", 0) >= 1 and s.length >= need_s:
            out.setdefault(lb["theme"], []).append(s)
    return out


def pick_pairs(v5: list[TG.Shot], v6: list[TG.Shot], labels: dict, n: int, need_s: float,
               rng: random.Random, uses: dict[str, int] | None = None,
               keep: list[dict] | None = None) -> list[tuple[str, TG.Shot, TG.Shot]]:
    """n (theme, GTA 5 shot, GTA 6 shot), one per theme: biggest-difference themes first (with some
    shuffling so edits differ), the best and least-used shots of each. `keep`: pairs of an earlier
    version ({theme, key5, key6}) to reuse where their shots still exist."""
    uses = uses or {}
    a, b = by_theme(v5, labels, need_s), by_theme(v6, labels, need_s)
    out: list[tuple[str, TG.Shot, TG.Shot]] = []
    keys = {s.key: s for s in v5 + v6}
    for p in keep or []:
        s5, s6 = keys.get(p.get("key5")), keys.get(p.get("key6"))
        if s5 and s6 and p.get("theme") in THEMES and min(s5.length, s6.length) >= need_s:
            out.append((p["theme"], s5, s6))
    themes = [t for t in THEMES if t in a and t in b and t not in {x[0] for x in out}]
    themes.sort(key=lambda t: THEMES[t][1] + rng.random() * 1.5, reverse=True)

    def best(pool: list[TG.Shot]) -> TG.Shot:
        top = max((labels[s.key]["quality"] * 2 - uses.get(s.key, 0)) for s in pool)
        return rng.choice([s for s in pool if labels[s.key]["quality"] * 2 - uses.get(s.key, 0) >= top - 1])
    for t in themes:
        if len(out) >= n:
            break
        out.append((t, best(a[t]), best(b[t])))
    if len(out) < 2:
        raise editor.EditError("not enough matching GTA 5 / GTA 6 shots yet: the libraries need labelling (needs"
                               " ANTHROPIC_API_KEY) or more official footage")
    return out[:n]


def pair_offset(shot: TG.Shot, dur: float, rng: random.Random) -> float:
    if shot.kind != "video":
        return 0.0
    return round(shot.start + (shot.length - dur) * rng.uniform(0.3, 0.7), 3)


def plan_compare(pairs: list[tuple[str, TG.Shot, TG.Shot]], layout: str, pair_s: float, hook: str,
                 question: str, rng: random.Random, intro: TG.Shot | None = None,
                 hook_s: float = 1.6, flash: bool = False) -> tuple[TG.Plan, list[dict]]:
    """(plan, corner labels). stack: one slot per pair (GTA 5 on top, GTA 6 below); sequence: the
    GTA 5 shot (40%) then the GTA 6 shot (60%). The hook plays over a GTA 6 shot first; the
    question sits on the last pair."""
    story = TG.Story("compare", "news", hook, [hook, question], None, [])
    slots, cards, labels = [], [], []
    t = 0.0
    if intro is not None:
        slots.append(TG.Slot(intro, pair_offset(intro, hook_s, rng), 4, 0.0, hook_s, 0, False, rng.choice((-1, 1))))
        cards.append((0.0, hook_s, hook))
        t = hook_s
    y5, y6 = TG.stack_tops()
    box = TG.box_top("box45")
    for i, (theme, s5, s6) in enumerate(pairs):
        cap = THEMES[theme][0]
        last = i == len(pairs) - 1
        if layout == "stack":
            slots.append(TG.Slot(s5, pair_offset(s5, pair_s, rng), 4, round(t, 3), pair_s, i + 1, flash and t > 0, 1,
                                 shot2=s6, offset2=pair_offset(s6, pair_s, rng)))
            labels += [{"start": t, "end": t + pair_s, "x": 36, "y": y5 + 24, "text": LABEL5[0], "colour": LABEL5[1]},
                       {"start": t, "end": t + pair_s, "x": 36, "y": y6 + 24, "text": LABEL6[0], "colour": LABEL6[1]}]
        else:
            d5 = round(pair_s * 0.4, 3)
            d6 = round(pair_s - d5, 3)
            slots.append(TG.Slot(s5, pair_offset(s5, d5, rng), 2, round(t, 3), d5, i + 1, False, 1))
            slots.append(TG.Slot(s6, pair_offset(s6, d6, rng), 2, round(t + d5, 3), d6, i + 1, flash, -1))
            labels += [{"start": t, "end": t + d5, "x": 36, "y": box + 24, "text": LABEL5[0], "colour": LABEL5[1]},
                       {"start": t + d5, "end": t + pair_s, "x": 36, "y": box + 24, "text": LABEL6[0],
                        "colour": LABEL6[1]}]
        cards.append((round(t, 3), round(t + pair_s, 3), question if last else cap))
        t += pair_s
    return TG.Plan(story, cards, slots, round(t, 3)), labels


def captions(themes: list[str]) -> dict[str, str]:
    names = ", ".join(THEMES[t][0].lower() for t in themes[:3])
    return {"instagram": f"gta 5 vs gta 6: {names} 🤯 #GTA6 #GTA5 #GTAVI #Rockstar #gaming",
            "tiktok": f"gta 5 vs gta 6: {names} 🤯 #GTA6 #GTA5 #GTAVI #fyp"}


# --------------------------------------------------------------------------
# Review variants (called by tokyo_girl.ReviewAdapter for items with source.kind "compare")
# --------------------------------------------------------------------------
LAYOUTS = ("stack", "stack", "sequence")   # A, B: stacked (the format that does well); C: one after the other
PAIR_S = {"slow": 3.4, "normal": 2.8, "fast": 2.2}


def libraries(adapter) -> tuple[list[TG.Shot], list[TG.Shot], dict]:
    """(GTA 5 shots, GTA 6 shots, labels), labelling new shots first."""
    from . import progress
    lcfg = adapter.lcfg
    media = Path(lcfg["media_dir"])
    job = progress.current()
    job.update(stage="Checking the GTA 5 library")
    v5 = prefixed(TG.Library(gta5_cfg(lcfg)).shots())
    v6 = [s for s in adapter.shots() if s.kind == "video"]
    if not v5:
        raise editor.EditError("no GTA 5 footage yet: it downloads with the daily fetch (official Rockstar"
                               " GTA V trailers)")
    job.update(stage="Matching GTA 5 and GTA 6 shots")
    labels = label_shots(v5 + v6, media, Path(lcfg["data_dir"]) / "compare" / "labels.json", adapter.base)
    return v5, v6, labels


def render_variants(adapter, item: dict, specs: list[dict], n: int, folder: Path, preview_bytes: int) -> list[dict]:
    from . import progress
    from . import style as st
    v5, v6, labels = libraries(adapter)
    state_path = Path(adapter.lcfg["state_file"])
    state = TG.load_state(state_path)
    uses = state.setdefault("uses", {})
    job = progress.current()
    out = []
    for vi, spec in enumerate(specs):
        with job.span(vi / len(specs), (vi + 1) / len(specs)):
            label = spec["label"]
            job.update(0.0, f"Version {label} ({vi + 1}/{len(specs)}): pairing shots")
            r = st.resolve(spec["style"])
            base_v = spec.get("from") or {}
            seed = base_v.get("seed") or random.Random(f"{item['id']}-{n}-{label}").randrange(1_000_000)
            rng = random.Random(seed)
            layout = base_v.get("layout") or LAYOUTS["ABC".index(label) % 3 if label in "ABC" else 0]
            pair_s = PAIR_S.get(spec["style"].get("pace") or "normal", 2.8)
            if r["pace"].get("shot_s"):
                pair_s = max(1.8, min(4.0, float(r["pace"]["shot_s"]) * 2))
            keep = base_v.get("pairs") if spec.get("keep_footage", True) else None
            pairs = pick_pairs(v5, v6, labels, 6, pair_s, rng, uses, keep)
            hook = (spec.get("texts") or {}).get("hook") or base_v.get("hook_card") or rng.choice(HOOKS)
            question = base_v.get("question") or rng.choice(QUESTIONS)
            heroes = [s for s in v6 if (labels.get(s.key) or {}).get("quality", 0) >= 3
                      and s.length >= 1.6 and s.key not in {p[2].key for p in pairs}]
            intro = rng.choice(heroes) if heroes else None
            plan, corner = plan_compare(pairs, layout, pair_s, hook, question, rng, intro,
                                        flash=r["transition"] == "flash")
            stem = f"r{n}_{label}"
            mp4 = folder / f"{stem}.mp4"
            # captions always on (Tim: good captions); the theme sits in the band above the picture
            style_r = dict(r, framing="stack" if layout == "stack" else "box45", position="high", text_y=None)
            job.update(0.05, f"Version {label} ({vi + 1}/{len(specs)}): rendering")
            with job.span(0.05, 0.9):
                TG.render_plan(plan, adapter.lcfg, mp4, st.grade_filter(r["grade"]), style_r, cards=plan.cards,
                               look=st.look_filter(r["look"], "tokyo_girl", TG.W, TG.H), labels=corner)
            job.update(0.9, f"Version {label} ({vi + 1}/{len(specs)}): preview")
            prev = editor.preview(mp4, folder / f"{stem}_preview.mp4", preview_bytes, plan.duration)
            for _, s5, s6 in pairs:
                for s in (s5, s6):
                    uses[s.key] = uses.get(s.key, 0) + 1
            themes = [p[0] for p in pairs]
            title = base_v.get("title") or f"GTA 5 vs GTA 6: {', '.join(THEMES[t][0].lower() for t in themes[:3])}"
            out.append({"label": label, "style": spec["style"], "preset": spec.get("preset"),
                        "experimental": spec.get("experimental", False), "story": "compare", "seed": seed,
                        "cards": [c[2] for c in plan.cards], "title": title[:95],
                        "description": f"{title}\n\nSide by side from official Rockstar trailers: GTA V (2013) and"
                                       " GTA VI (2026).\n\n#GTA6 #GTA5 #Shorts",
                        "tags": ["GTA 6", "GTA 5", "GTA 5 vs GTA 6", "Shorts"], "hook": title[:95],
                        "captions": base_v.get("captions") or captions(themes), "file": mp4.name,
                        "preview": prev.name, "duration": round(plan.duration, 1), "cuts": len(plan.slots) - 1,
                        "silent": True, "text_cards": len(plan.cards), "engine": "ffmpeg",
                        "footage": TG.footage_of(plan), "footage_note": None, "intro": "none",
                        "layout": layout, "hook_card": hook, "question": question,
                        "pairs": [{"theme": t, "key5": a.key, "key6": b.key} for t, a, b in pairs],
                        "music": None, "music_note": None})
    TG.save_state(state_path, state)
    return out


def not_ready(lcfg: dict) -> str | None:
    """Why a comparison can't be made yet (None = it can). Checked before any review item is
    created, so the queue never gets a failed one."""
    from . import ai
    if not TG.Library(gta5_cfg(lcfg)).shots():
        return "no GTA 5 footage yet: Rockstar's GTA V trailers download with the next daily fetch"
    if not any(s.kind == "video" for s in TG.Library(lcfg).shots()):
        return "no GTA 6 trailer footage yet"
    labels = Path(lcfg["data_dir"]) / "compare" / "labels.json"
    if not ai.available(lcfg.get("_base")) and not load_labels(labels):
        return "matching GTA 5 and GTA 6 shots needs ANTHROPIC_API_KEY in .env"
    return None


def make(lcfg: dict, n: int = 1, now: float | None = None) -> list[str]:
    """n comparison review items (3 versions each). Returns their ids. Raises EditError when
    it can't be made yet (`not_ready`), before creating anything."""
    from datetime import datetime
    from . import progress, review
    why = not_ready(lcfg)
    if why:
        raise editor.EditError(why)
    now = now or time.time()
    dt = datetime.fromtimestamp(now, TG.UK)
    store = review.Store(Path(lcfg["data_dir"]))
    cfg, base = lcfg.get("_cfg") or {}, lcfg.get("_base") or Path(".")
    adapter = TG.ReviewAdapter(cfg, base, lcfg)
    # labels the shots and checks enough themes match BEFORE any review item exists, so the daily
    # run never leaves a failed "GTA 5 vs GTA 6" in #ready-to-post (raises EditError instead)
    v5, v6, labels = libraries(adapter)
    pick_pairs(v5, v6, labels, 6, PAIR_S["slow"], random.Random(0))
    job = progress.current()
    ids = []
    for k in range(n):
        item = store.create("tokyo_girl", {"day": dt.date().isoformat(), "kind": "compare"}, now + k,
                            title="GTA 5 vs GTA 6" + (f" #{k + 1}" if n > 1 else ""))
        if progress.active():
            job.data.setdefault("meta", {})["item"] = item["id"]
        with job.span(k / n, (k + 1) / n):
            item = review.render_round(store, item["id"], cfg, base, {"kind": "initial"}, now=now + k, adapter=adapter)
        ids.append(item["id"])
    return ids


def ensure_gta5(lcfg: dict) -> int:
    """Download the official GTA 5 trailers once (more on the weekly refetch). Returns the shot count."""
    c5 = gta5_cfg(lcfg)
    if not TG.Library(c5).files():
        try:
            TG.fetch(c5)
        except Exception as e:  # noqa: BLE001 - a fetch problem must not stop the rest
            log.warning("GTA 5 fetch failed: %s", e)
    return len(TG.Library(c5).shots())

