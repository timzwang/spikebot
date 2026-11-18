"""Style settings for the 3 review variants, the learning profile, and feedback parsing.

Pure logic (no ffmpeg, no Discord), so it is unit-tested with a seeded RNG.

A *style* is a small dict of named choices, one per dimension, plus optional exact
`custom` values (from an #inspiration preset or from feedback):

    {"grade": "punchy", "pace": "fast", "font": "montserrat", "size": "l",
     "position": "middle", "colour": "classic", "transition": "zoom",
     "hook": "funny", "ending": "none", "custom": {"saturation": 1.32}}

`resolve(style)` turns it into concrete render numbers. The learning profile
(data/style_profile.json) counts, per option, how often it was shown, approved,
passed over (another variant won) and rejected. Variant A uses the best-scoring
options, B is built on a preset Tim has approved (or sampled from the profile if
there are no presets), C is the experimental one (least-tried options).
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------
GRADES = {  # ffmpeg eq + colorbalance
    "natural": {"saturation": 1.0, "contrast": 1.0, "brightness": 0.0, "warmth": 0.0},
    "clean": {"saturation": 1.12, "contrast": 1.04, "brightness": 0.02, "warmth": 0.0},
    "punchy": {"saturation": 1.3, "contrast": 1.12, "brightness": 0.0, "warmth": 0.0},
    "vivid": {"saturation": 1.5, "contrast": 1.06, "brightness": 0.02, "warmth": 0.0},
    "muted": {"saturation": 0.82, "contrast": 1.04, "brightness": 0.0, "warmth": 0.0},
    "moody": {"saturation": 0.9, "contrast": 1.22, "brightness": -0.04, "warmth": -0.05},
    "warm": {"saturation": 1.15, "contrast": 1.06, "brightness": 0.0, "warmth": 0.08},
    "cool": {"saturation": 1.08, "contrast": 1.08, "brightness": 0.0, "warmth": -0.08},
}
PACES = {
    # lacy: clip length around the peak, longest silence kept (None = no jump cuts),
    #       words per caption; tokyo_girl: beats-per-shot patterns.
    "slow": {"window_s": 59, "max_gap_s": None, "words": 4, "bars": "slow"},
    "normal": {"window_s": 42, "max_gap_s": 1.2, "words": 3, "bars": "normal"},
    "fast": {"window_s": 28, "max_gap_s": 0.5, "words": 2, "bars": "fast"},
}
FONTS = {  # key -> fontconfig family (install.sh installs these)
    "dejavu": "DejaVu Sans",
    "montserrat": "Montserrat",
    "roboto": "Roboto",
    "narrow": "Liberation Sans Narrow",
}
SIZES = {"s": 0.85, "m": 1.0, "l": 1.15, "xl": 1.3}
POSITIONS = ("high", "middle", "low")
COLOURS = {  # main text, highlighted word
    "classic": ("#FFFFFF", "#FFE500"),
    "lime": ("#FFFFFF", "#7CFF3A"),
    "ice": ("#FFFFFF", "#3ADFFF"),
    "fire": ("#FFE500", "#FF4A3A"),
    "pink": ("#FFFFFF", "#FF5FC8"),
}
TRANSITIONS = ("cut", "flash", "zoom")
TG_TRANSITIONS = TRANSITIONS + ("fade",)   # Tokyo Girl also has a soft crossfade
# Camera movement added by the editor: "none" = every shot completely still (no pans,
# push-ins, punch-in zooms or animated stills), "subtle" = half the drift, "full" = as designed.
MOTIONS = ("none", "subtle", "full")
# Tokyo Girl framing of the 16:9 trailers in the 9:16 frame: "fill" crops the sides to fill
# the screen, "fit" shows the whole frame (nothing cut off) over a blurred copy of itself.
FRAMINGS = ("fill", "fit")
# Tokyo Girl source media: "clips" = trailer video only (never screenshots), "mixed" = both.
MEDIA = ("clips", "mixed")
# How much on-screen text an edit carries. "none": no captions, no hook box, no end
# card (a clean edit); "minimal": a short hook at the start and captions only where
# the payoff is said (Lacy) / only the opening card (Tokyo Girl); "full": every word
# (Lacy) / every story card (Tokyo Girl).
TEXT_LEVELS = ("none", "minimal", "full")
# Tokyo Girl also has "plain": one short factual line (e.g. "GTA 6 is almost here...", the
# release countdown), never a question or a story card.
TG_TEXT_LEVELS = TEXT_LEVELS + ("plain",)
# Tokyo Girl intro: the official GTA VI logo builds up and bursts away on the drop, or a
# plain "N days until GTA 6" countdown line.
INTROS = ("none", "logo", "countdown")
ENDINGS = ("none", "question", "cta")
HOOKS = {
    "lacy": ("matched", "funny", "shock", "hype", "neutral"),
    "tokyo_girl": ("news", "theory", "meme", "couple"),
    "trend": ("news", "theory", "meme", "couple"),   # trending-sound edits: Tokyo Girl's editor
}
# The kinds of edit a rule can apply to. "trend" = trailer edits cut to a trending sound: the
# same editor as Tokyo Girl, but its own rules (each rule says which kinds it applies to).
STYLE_MODES = ("lacy", "tokyo_girl", "trend")
TG_LIKE = ("tokyo_girl", "trend")
DEFAULT = {"grade": "natural", "pace": "normal", "font": "dejavu", "size": "m",
           "position": "low", "colour": "classic", "transition": "flash",
           "ending": "none", "text_level": "minimal", "intro": "none", "motion": "full",
           "framing": "fill", "media": "mixed"}
TG_ONLY = ("intro", "framing", "media")   # dimensions only Tokyo Girl has
# Not varied between A/B/C: a rule, not a style to explore. Default unless Tim sets it.
FIXED = ("media",)

# Exact values a preset or feedback can set (shown and editable with /preset edit).
CUSTOM_KEYS = {
    "saturation": (0.3, 2.0), "contrast": (0.6, 1.6), "brightness": (-0.25, 0.25),
    "warmth": (-0.2, 0.2), "text_px": (36, 160), "text_y": (0.15, 0.85),
    "outline": (0, 14), "shadow": (0, 8), "hook_s": (1.0, 60.0), "payoff": (0.2, 0.9),
    "shot_s": (0.2, 6.0), "words": (1, 6),
    # film look: grain strength, vignette on/off, cinema bars on/off (Tokyo Girl only)
    "grain": (0, 30), "vignette": (0, 1), "letterbox": (0, 1),
    "sfx": (0, 1),   # sound-effect volume on Lacy clips (0 = off, default 0.35)
    # Tokyo Girl timing: longest edit (seconds, logo intro included), fewest beats a shot is
    # held (2+ keeps a clip's action intact), logo build-up before the drop (s), the logo's
    # largest width as a share of the screen, crossfade length (s)
    "max_s": (8, 60), "hold_beats": (1, 8), "logo_s": (1, 8), "logo_size": (0.2, 0.9), "fade_s": (0.1, 1.5),
}
TG_CUSTOM = ("max_s", "hold_beats", "logo_s", "logo_size", "fade_s")
CUSTOM_STR = {"text_colour", "highlight", "hook_style"}


def dims(mode: str) -> dict[str, tuple[str, ...]]:
    """Every dimension a variant varies in for this mode, with its options."""
    tg = mode in TG_LIKE
    out = {"grade": tuple(GRADES), "pace": tuple(PACES), "font": tuple(FONTS),
           "size": tuple(SIZES), "position": POSITIONS, "colour": tuple(COLOURS),
           "transition": TG_TRANSITIONS if tg else TRANSITIONS, "hook": HOOKS.get(mode, ("matched",)),
           "text_level": TG_TEXT_LEVELS if tg else TEXT_LEVELS, "motion": MOTIONS}
    if mode == "lacy":
        out["ending"] = ENDINGS
    if tg:
        out["intro"] = INTROS
        out["framing"] = FRAMINGS
        out["media"] = MEDIA
    return out


def stat_key(dim: str, mode: str) -> str:
    """Hook tones only make sense per mode; the look is shared across modes."""
    return f"hook:{mode}" if dim == "hook" else dim


def default_style(mode: str) -> dict:
    s = dict(DEFAULT)
    s["hook"] = HOOKS.get(mode, ("matched",))[0]
    if mode != "lacy":
        s.pop("ending")
    if mode not in TG_LIKE:
        for k in TG_ONLY:
            s.pop(k)
    return s


# --------------------------------------------------------------------------
# Resolve to numbers
# --------------------------------------------------------------------------
def hex_ok(v: str) -> bool:
    return bool(re.fullmatch(r"#?[0-9A-Fa-f]{6}", (v or "").strip()))


def ass_colour(hexrgb: str, alpha: int = 0) -> str:
    """#RRGGBB -> ASS &HAABBGGRR."""
    h = hexrgb.strip().lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def resolve(style: dict) -> dict:
    """Concrete numbers for the renderers. Unknown names fall back to defaults."""
    c = dict(style.get("custom") or {})
    g = dict(GRADES.get(style.get("grade"), GRADES["natural"]))
    for k in ("saturation", "contrast", "brightness", "warmth"):
        if k in c:
            g[k] = float(c[k])
    p = dict(PACES.get(style.get("pace"), PACES["normal"]))
    if "words" in c:
        p["words"] = int(c["words"])
    if "shot_s" in c:
        p["shot_s"] = float(c["shot_s"])
    text, hi = COLOURS.get(style.get("colour"), COLOURS["classic"])
    motion = style.get("motion") if style.get("motion") in MOTIONS else "full"
    transition = style.get("transition") if style.get("transition") in TG_TRANSITIONS else "flash"
    if motion == "none" and transition == "zoom":
        transition = "cut"   # a punch-in zoom is camera movement

    def num(k, default):
        return float(c[k]) if c.get(k) is not None else default
    return {
        "grade": g, "pace": p,
        "font": FONTS.get(style.get("font"), FONTS["dejavu"]),
        "scale": SIZES.get(style.get("size"), 1.0),
        "text_px": c.get("text_px"),
        "position": style.get("position") if style.get("position") in POSITIONS else "low",
        "text_y": c.get("text_y"),
        "text_colour": c["text_colour"] if hex_ok(c.get("text_colour", "")) else text,
        "highlight": c["highlight"] if hex_ok(c.get("highlight", "")) else hi,
        "outline": c.get("outline"), "shadow": c.get("shadow"),
        "transition": transition, "motion": motion,
        "framing": style.get("framing") if style.get("framing") in FRAMINGS else "fill",
        "media": style.get("media") if style.get("media") in MEDIA else "mixed",
        "max_s": num("max_s", None), "hold_beats": int(num("hold_beats", 1)),
        "logo_s": num("logo_s", None), "logo_size": num("logo_size", 0.78), "fade_s": num("fade_s", 0.45),
        "ending": style.get("ending") if style.get("ending") in ENDINGS else "none",
        "hook_s": c.get("hook_s"), "payoff": c.get("payoff"),
        "hook_style": c.get("hook_style"),
        "text_level": style.get("text_level") if style.get("text_level") in TG_TEXT_LEVELS else "full",
        "intro": style.get("intro") if style.get("intro") in INTROS else "none",
        "look": {"grain": float(c.get("grain") or 0), "vignette": bool(c.get("vignette")),
                 "letterbox": bool(c.get("letterbox"))},
    }


def grade_filter(g: dict) -> str:
    """ffmpeg filter for a grade ('' when it is neutral)."""
    parts = []
    if any(abs(g.get(k, d) - d) > 1e-3 for k, d in (("saturation", 1), ("contrast", 1), ("brightness", 0))):
        parts.append(f"eq=saturation={g['saturation']:.3f}:contrast={g['contrast']:.3f}"
                     f":brightness={g['brightness']:.3f}")
    w = g.get("warmth", 0.0)
    if abs(w) > 1e-3:  # warm = more red / less blue in mids and highlights
        parts.append(f"colorbalance=rm={w:.3f}:bm={-w:.3f}:rh={w / 2:.3f}:bh={-w / 2:.3f}")
    return ",".join(parts)


def look_filter(look: dict, mode: str, width: int = 1080, height: int = 1920) -> str:
    """Film-look ffmpeg filters (grain, vignette, cinema bars) for a resolved style.
    Cinema bars only in Tokyo Girl: the Lacy layout needs the full frame."""
    parts = []
    if (look or {}).get("vignette"):
        parts.append("vignette=angle=PI/5")
    g = float((look or {}).get("grain") or 0)
    if g > 0:
        parts.append(f"noise=alls={int(round(g))}:allf=t")
    if (look or {}).get("letterbox") and mode == "tokyo_girl":
        bar = int(height * 0.11) // 2 * 2
        parts.append(f"drawbox=x=0:y=0:w={width}:h={bar}:color=black:t=fill,"
                     f"drawbox=x=0:y={height - bar}:w={width}:h={bar}:color=black:t=fill")
    return ",".join(parts)


def describe(style: dict) -> str:
    """One line for the Discord card."""
    c = style.get("custom") or {}
    bits = [f"grade **{style.get('grade')}**", f"pace **{style.get('pace')}**",
            f"text **{style.get('font')}/{style.get('size')}/{style.get('position')}/{style.get('colour')}**",
            f"transition **{style.get('transition')}**", f"hook **{style.get('hook')}**"]
    if style.get("text_level"):
        bits.append(f"on-screen text **{style['text_level']}**")
    if style.get("intro") not in (None, "none"):
        bits.append(f"intro **{style['intro']}**")
    for k in ("motion", "framing", "media"):
        if style.get(k) and style.get(k) != DEFAULT.get(k):
            bits.append(f"{k} **{style[k]}**")
    if style.get("ending") not in (None, "none"):
        bits.append(f"ending **{style['ending']}**")
    if c:
        bits.append("custom " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    return " · ".join(bits)


# --------------------------------------------------------------------------
# Learning profile: data/style_profile.json
# --------------------------------------------------------------------------
def _blank() -> dict:
    return {"version": 2, "stats": {}, "presets": {}, "decisions": [],
            "feedback": {"notes": [], "intents": {}}, "entries": []}


# The rules list (/rules): every instruction Tim has given, one numbered entry each, grouped
# by category. Pins are *derived* from it: for each setting the newest enabled entry wins,
# whether it was for every mode or one mode, so switching an entry off falls back to the
# one before it.
CATEGORIES = (
    ("look", "🎨 Colour & look", ("grade", "saturation", "contrast", "brightness", "warmth", "grain",
                                 "vignette", "letterbox")),
    ("pace", "✂️ Pace & cuts", ("pace", "shot_s", "payoff", "transition")),
    ("text", "🔤 Text & captions", ("text_level", "font", "size", "position", "colour", "text_px", "text_y",
                                   "outline", "shadow", "words", "text_colour", "highlight")),
    ("hook", "🪝 Hook, intro & ending", ("hook", "hook_s", "hook_style", "intro", "ending")),
    ("sound", "🔊 Sound", ("sfx",)),
    ("note", "📝 Notes (the editor can't apply these yet)", ()),
    ("unsupported", "🚫 Asked for, not possible yet", ()),
)
CATEGORY_NAME = {k: name for k, name, _ in CATEGORIES}
MODE_NAME = {"all": "all edits", "lacy": "Lacy", "tokyo_girl": "Tokyo Girl", "trend": "Trend edits"}


def expand_modes(mode) -> list[str]:
    """"all" / one mode / a list -> the list of modes (in STYLE_MODES order)."""
    if mode in (None, "all"):
        return list(STYLE_MODES)
    ms = [mode] if isinstance(mode, str) else list(mode)
    return [m for m in STYLE_MODES if m in ms]


def entry_modes(e: dict) -> list[str]:
    """Which kinds of edit an entry applies to. Entries saved before trend edits had their
    own rules said "tokyo_girl" for both, so they keep applying to both."""
    if e.get("modes"):
        return expand_modes(e["modes"])
    m = e.get("mode", "all")
    return list(TG_LIKE) if m == "tokyo_girl" else expand_modes(m)


def modes_label(modes: list[str]) -> str:
    ms = expand_modes(modes)
    return "all edits" if ms == list(STYLE_MODES) else ", ".join(MODE_NAME[m] for m in ms)


def valid_modes(setting: str | None, value) -> list[str]:
    """Kinds of edit where this setting (with this value) exists; every kind for notes."""
    if not setting:
        return list(STYLE_MODES)
    if value is None:
        return [m for m in STYLE_MODES if setting in dims(m) or setting in CUSTOM_KEYS or setting in CUSTOM_STR]
    out = []
    for m in STYLE_MODES:
        if setting in TG_CUSTOM and m not in TG_LIKE:
            continue
        try:
            set_value(default_style(m), setting, str(value), m)
        except ValueError:
            continue
        out.append(m)
    return out


def category_of(entry: dict) -> str:
    if entry.get("kind") in ("note", "unsupported"):
        return entry["kind"]
    for key, _, settings in CATEGORIES:
        if entry.get("setting") in settings:
            return key
    return "look"


def entry_text(e: dict, where: bool = True) -> str:
    """One line for an entry (without its number); `where` adds "(Lacy)" etc. when it isn't for all."""
    ms = entry_modes(e)
    where = "" if ms == list(STYLE_MODES) or not where else f" ({modes_label(ms)})"
    if e.get("kind") == "setting":
        val = "auto (let it learn)" if e.get("value") is None else e["value"]
        if isinstance(val, float) and val.is_integer():
            val = int(val)
        return f"{e['setting']} = {val}{where}"
    return f"{e.get('text', '')}{where}"


def _pin_target(key: str) -> tuple[str, str]:
    """"grade" -> ("grade", "all"); "hook:lacy" -> ("hook", "lacy"); "hook:trend" -> ("hook", "trend")."""
    if ":" in key:
        a, b = key.split(":", 1)
        return a, b
    return key, "all"


class Profile:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.data = self.load()

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return _blank()
        d = _blank() | d
        if not d.get("entries") and (d.get("rules") or d.get("pinned") or d.get("custom_pins")):
            d["entries"] = migrate(d)
        if NOTES_UPGRADE not in (d.get("upgrades") or []):
            upgrade_notes(d["entries"])
            d["upgrades"] = list(d.get("upgrades") or []) + [NOTES_UPGRADE]
        return d

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ---- counters -----------------------------------------------------------
    def _bump(self, key: str, opt: str, field: str, by: float = 1.0) -> None:
        row = self.data["stats"].setdefault(key, {}).setdefault(
            opt, {"shown": 0, "approved": 0, "passed": 0, "rejected": 0})
        row[field] = round(row.get(field, 0) + by, 3)

    def _count(self, mode: str, style: dict, field: str, by: float = 1.0) -> None:
        for dim in dims(mode):
            if style.get(dim) is not None:
                self._bump(stat_key(dim, mode), str(style[dim]), field, by)

    def _preset(self, name: str | None, field: str) -> None:
        if name:
            row = self.data["presets"].setdefault(name, {"shown": 0, "approved": 0, "rejected": 0,
                                                         "last_used": 0})
            row[field] = row.get(field, 0) + 1

    def shown(self, mode: str, variants: list[dict], now: float | None = None) -> None:
        for v in variants:
            self._count(mode, v["style"], "shown")
            self._preset(v.get("preset"), "shown")
            if v.get("preset"):
                self.data["presets"][v["preset"]]["last_used"] = now or time.time()

    def decide(self, action: str, mode: str, item_id: str, variants: list[dict],
               chosen: str | None = None, now: float | None = None, extra: dict | None = None) -> None:
        """approve (chosen wins, the rest are passed over), reject (all rejected),
        redo (all passed over), expire (logged only)."""
        for v in variants:
            if action == "approve":
                field = "approved" if v["label"] == chosen else "passed"
            elif action == "reject":
                field = "rejected"
            elif action == "redo":
                field = "passed"
            else:
                field = None
            if field:
                self._count(mode, v["style"], field)
                if v.get("preset") and field in ("approved", "rejected"):
                    self._preset(v["preset"], field)
        self.data["decisions"].append({
            "ts": now or time.time(), "action": action, "mode": mode, "item": item_id,
            "chosen": chosen, "variants": [{"label": v["label"], "style": v["style"],
                                            "preset": v.get("preset")} for v in variants],
            **(extra or {})})
        self.data["decisions"] = self.data["decisions"][-500:]

    def feedback(self, item_id: str, label: str, text: str, intents: list[str],
                 before: dict, after: dict, mode: str, now: float | None = None) -> None:
        """Notes count against the old settings and (half) for the new ones."""
        fb = self.data["feedback"]
        fb["notes"].append({"ts": now or time.time(), "item": item_id, "variant": label,
                            "text": text[:500], "intents": intents})
        fb["notes"] = fb["notes"][-300:]
        for i in intents:
            fb["intents"][i] = fb["intents"].get(i, 0) + 1
        for dim in dims(mode):
            if before.get(dim) != after.get(dim):
                if before.get(dim) is not None:
                    self._bump(stat_key(dim, mode), str(before[dim]), "rejected", 0.5)
                if after.get(dim) is not None:
                    self._bump(stat_key(dim, mode), str(after[dim]), "approved", 0.5)

    # ---- preferences ----------------------------------------------------------
    def score(self, key: str, opt: str) -> float:
        """Smoothed approval rate: approvals count fully, being passed over a
        little, rejection a lot."""
        r = self.data["stats"].get(key, {}).get(opt, {})
        good = 1 + r.get("approved", 0)
        bad = 1 + 0.35 * r.get("passed", 0) + r.get("rejected", 0)
        return good / (good + bad)

    def best(self, mode: str, dim: str, options: tuple[str, ...]) -> str:
        key = stat_key(dim, mode)
        d = default_style(mode).get(dim)
        return max(options, key=lambda o: (self.score(key, o), o == d))

    # ---- the rules list (pins are derived from it) ------------------------------------
    def _effective(self, mode: str) -> dict:
        """setting -> value from the newest enabled setting entry for every mode or this mode.
        Values that aren't options here are skipped (the one before it counts)."""
        opts = dims(mode)
        out: dict = {}
        for e in self.data.get("entries") or []:
            if not e.get("on", True) or e.get("kind") != "setting" or mode not in entry_modes(e):
                continue
            k, v = e["setting"], e.get("value")
            if v is not None:
                if k in opts and v not in opts[k]:
                    continue
                if k not in opts and k not in CUSTOM_KEYS and k not in CUSTOM_STR:
                    continue
            out[k] = v
        return {k: v for k, v in out.items() if v is not None}

    def pinned(self, mode: str) -> dict:
        """Option settings fixed for this mode (all three variants use them)."""
        opts = dims(mode)
        return {k: v for k, v in self._effective(mode).items() if k in opts}

    def custom_pins(self, mode: str) -> dict:
        """Exact values (e.g. saturation 1.2, grain 8) for every edit of this mode."""
        return {k: v for k, v in self._effective(mode).items() if k in CUSTOM_KEYS or k in CUSTOM_STR}

    def entries(self) -> list[dict]:
        return self.data.setdefault("entries", [])

    def entry(self, n: int) -> dict | None:
        return next((e for e in self.entries() if e["n"] == n), None)

    def add_entry(self, kind: str, mode, setting: str | None = None, value=None, text: str = "",
                  said: str = "", source: str = "feedback", now: float | None = None) -> dict:
        """`mode`: "all", one of STYLE_MODES, or a list of them."""
        es = self.entries()
        ms = expand_modes(mode) or list(STYLE_MODES)
        e = {"n": max([x["n"] for x in es] or [0]) + 1, "ts": now or time.time(), "kind": kind,
             "mode": "all" if ms == list(STYLE_MODES) else ms[0], "modes": ms, "setting": setting, "value": value,
             "text": text[:300], "said": said[:500], "source": source, "on": True}
        es.append(e)
        return e

    def set_modes(self, n: int, modes: list[str]) -> list[str]:
        """Change which kinds of edit entry n applies to (only kinds where its setting exists).
        Returns the modes it now has. Raises ValueError."""
        e = self.entry(n)
        if e is None:
            raise ValueError(f"no entry #{n}")
        ok = [m for m in expand_modes(modes) if m in valid_modes(e.get("setting"), e.get("value"))]
        if not ok:
            raise ValueError(f"#{n} ({e.get('setting')}) can't apply to {modes_label(modes)}")
        e["modes"] = ok
        e["mode"] = "all" if ok == list(STYLE_MODES) else ok[0]
        return ok

    def add_rule(self, text: str, summary: str, mode: str, settings: list[dict], notes: list[str],
                 unsupported: list[str], now: float | None = None, source: str = "feedback") -> dict:
        """Append one feedback's settings, notes and "can't do" items to the rules list.
        `settings` are validated {"mode", "setting", "value"} rows (value None = back to auto).
        Returns {"summary", "mode", "entries": [...]} (what was added)."""
        added = [self.add_entry("setting", r.get("modes") or r["mode"], r["setting"], r["value"], said=text,
                                source=source, now=now) for r in settings]
        added += [self.add_entry("note", mode, text=n, said=text, source=source, now=now) for n in notes[:12]]
        added += [self.add_entry("unsupported", mode, text=u, said=text, source=source, now=now)
                  for u in unsupported[:12]]
        return {"summary": summary[:300], "mode": mode, "entries": added,
                "settings": [{"mode": e["mode"], "modes": e["modes"], "setting": e["setting"], "value": e["value"]}
                             for e in added if e["kind"] == "setting"],
                "notes": [e["text"] for e in added if e["kind"] == "note"]}

    def set_enabled(self, numbers, on: bool) -> tuple[list[int], list[int]]:
        """Switch entries on/off. Returns (changed, not found)."""
        changed, missing = [], []
        for n in numbers:
            e = self.entry(n)
            if e is None:
                missing.append(n)
            elif e.get("on", True) != on:
                e["on"] = on
                changed.append(n)
        return changed, missing

    def taste(self, mode: str | None = None, n: int = 40) -> list[str]:
        """Tim's enabled instructions (for the AI reading his next feedback)."""
        out = []
        for e in self.entries()[-n * 2:]:
            if not e.get("on", True) or e["kind"] == "unsupported":
                continue
            if mode and mode not in entry_modes(e):
                continue
            out.append(entry_text(e))
        return out[-n:]

    def pin(self, setting: str, value: str | None, now: float | None = None) -> dict:
        """/style: "grade" or "hook:lacy" -> a new entry (value None / "auto" = learn it again)."""
        dim, mode = _pin_target(setting)
        v = None if value in (None, "", "auto") else value
        return self.add_entry("setting", mode, dim, v, said=f"/style {setting} {value or 'auto'}",
                              source="/style", now=now)

    def preferred(self, mode: str) -> dict:
        """What variant A would use now."""
        pins = self.pinned(mode)
        return {d: pins.get(d) or self.best(mode, d, opts) for d, opts in dims(mode).items()}

    def top_presets(self, names: list[str], n: int = 3) -> list[str]:
        """Presets ranked by approvals (then approval rate), most-approved first."""
        p = self.data["presets"]

        def rank(name):
            r = p.get(name, {})
            return (r.get("approved", 0), (1 + r.get("approved", 0)) / (2 + r.get("shown", 0)))
        return sorted(names, key=rank, reverse=True)[:n]

    def next_preset(self, names: list[str]) -> str | None:
        """Rotate through the most-approved presets: the one used longest ago."""
        top = self.top_presets(names)
        if not top:
            return None
        return min(top, key=lambda n: self.data["presets"].get(n, {}).get("last_used", 0))


# --------------------------------------------------------------------------
# Picking the 3 variants
# --------------------------------------------------------------------------
def pick_variants(profile: Profile, mode: str, rng: random.Random,
                  presets: dict[str, dict] | None = None, avoid: list[dict] | None = None,
                  n: int = 3) -> list[dict]:
    """[{label, style, preset, experimental}] for A, B, C.

    A: best-scoring option per dimension (pins win).
    B: built on a preset (most-approved presets in rotation); without presets,
       weighted-random from the profile.
    C: experimental: least-shown options, kept different from A and B.
    Unpinned dimensions are kept different between the variants where there are
    enough options, so every round differs in grade, pace, text and hook.
    `avoid` (styles from an earlier round, for Redo) is steered away from too."""
    from . import presets as presets_
    options = dims(mode)
    pins = profile.pinned(mode)
    avoid = avoid or []
    out: list[dict] = []
    preset_name = profile.next_preset(list(presets or {})) if presets else None
    for i in range(n):
        label = "ABC"[i]
        style: dict = {}
        preset = None
        experimental = (i == 2)
        base: dict = {}
        if i == 1 and preset_name:
            preset = preset_name
            base = presets_.to_style(presets[preset_name], mode)
        for dim, opts in options.items():
            if dim in pins and pins[dim] in opts:
                style[dim] = pins[dim]
                continue
            if dim in FIXED:
                style[dim] = DEFAULT[dim]
                continue
            if dim == "ending" and base.get("ending") not in opts:
                # end cards ("Would you have done the same?") add nothing unless Tim asks
                # for one (/feedback or /style) or a preset he chose has one
                style[dim] = "none"
                continue
            taken = {v["style"].get(dim) for v in out}
            fresh = [o for o in opts if o not in taken] or list(opts)
            unseen = [o for o in fresh if all(a.get(dim) != o for a in avoid)] or fresh
            key = stat_key(dim, mode)
            if base.get(dim) in opts and (base[dim] in fresh or len(opts) < n):
                style[dim] = base[dim]
            elif i == 0:
                style[dim] = max(unseen, key=lambda o: (profile.score(key, o), o == DEFAULT.get(dim)))
            elif experimental:
                shown = profile.data["stats"].get(key, {})
                # explore, but never with something Tim has turned down more than he's chosen it
                liked = [o for o in unseen if shown.get(o, {}).get("rejected", 0)
                         <= shown.get(o, {}).get("approved", 0)] or unseen
                least = min(shown.get(o, {}).get("shown", 0) for o in liked)
                style[dim] = rng.choice([o for o in liked if shown.get(o, {}).get("shown", 0) == least])
            else:
                weights = [profile.score(key, o) ** 2 for o in unseen]
                style[dim] = rng.choices(unseen, weights=weights)[0]
        custom = dict(base.get("custom") or {}) | profile.custom_pins(mode)
        if custom:
            style["custom"] = custom
        out.append({"label": label, "style": style, "preset": preset, "experimental": experimental})
    return out


# --------------------------------------------------------------------------
# Feedback notes -> setting changes (rule based: free, predictable, testable)
# --------------------------------------------------------------------------
def _step(options: tuple[str, ...] | list[str], cur: str, by: int) -> str:
    opts = list(options)
    i = opts.index(cur) if cur in opts else len(opts) // 2
    return opts[max(0, min(len(opts) - 1, i + by))]


SAT_ORDER = sorted(GRADES, key=lambda k: GRADES[k]["saturation"])
CON_ORDER = sorted(GRADES, key=lambda k: GRADES[k]["contrast"])
RULES: list[tuple[str, str]] = [  # (intent, regex)
    ("text_bigger", r"\b(bigger|larger|increase|more readable)\b.*\b(text|font|captions?|words?)\b|\b(text|font|captions?)\b.*\b(bigger|larger|too small)\b"),
    ("text_smaller", r"\b(smaller|reduce)\b.*\b(text|font|captions?|words?)\b|\b(text|font|captions?)\b.*\b(smaller|too big|too large)\b"),
    ("text_higher", r"\b(text|captions?)\b.*\b(higher|up|top)\b|\b(move|put)\b.*\b(up|higher|top)\b"),
    ("text_lower", r"\b(text|captions?)\b.*\b(lower|down|bottom)\b|\b(move|put)\b.*\b(down|lower|bottom)\b"),
    ("text_middle", r"\b(text|captions?)\b.*\b(middle|centre|center)\b|\b(centre|center) (the )?(text|captions?)\b"),
    ("faster", r"\b(faster|quicker|snappier|more cuts|too slow|tighter|shorter)\b"),
    ("slower", r"\b(slower|calmer|fewer cuts|too fast|too quick|longer)\b"),
    ("more_colour", r"\b(more|extra|boost)\b.*\b(colou?r|saturat\w*|vibran\w*|pop)\b|\b(punchier|too dull|washed out|vivid)\b"),
    ("less_colour", r"\b(less|tone down|reduce)\b.*\b(colou?r|saturat\w*)\b|\b(too (saturated|colou?rful)|oversaturated)\b"),
    ("more_contrast", r"\bmore contrast\b|\b(too flat)\b"),
    ("less_contrast", r"\bless contrast\b|\btoo (dark|contrasty|harsh)\b"),
    ("warmer", r"\bwarmer\b|\btoo (cold|blue)\b"),
    ("cooler", r"\bcooler\b|\btoo (warm|orange|yellow)\b"),
    ("no_flash", r"\bno (white )?flash(es)?\b|\btoo many flashes\b"),
    ("flash", r"\b(add|more) flash(es)?\b"),
    ("zoom", r"\b(add|more) (zoom|punch)\w*\b|\bzoom (ins?|cuts?)\b"),
    ("no_zoom", r"\bno zoom\w*\b"),
    ("cta", r"\b(add|end with) (a )?(cta|call to action|follow)\b"),
    ("question", r"\b(end|finish) (with|on) a question\b"),
    ("no_ending", r"\bno (end card|ending|cta)\b"),
    ("no_text", r"\b(no|remove|drop|without|get rid of|ditch|kill)\b.*\b(captions?|subtitles?|text|words)\b"
                r"|\b(captions?|text)\b.*\b(slop|pointless|unnecessary|not needed|useless|meaningless)\b"),
    ("less_text", r"\b(less|fewer|minimal|only key|only important)\b.*\b(captions?|text)\b"),
    ("full_text", r"\b(caption everything|full captions|every word|more captions)\b"),
    ("logo_intro", r"\blogo\b.*\b(intro|start|open\w*|fade\w*)\b|\b(intro|start|open\w*)\b.*\blogo\b"),
    ("static", r"\b(no|without|stop|remove)\b.*\b(camera )?(movement|motion|panning|pans?|push.?ins?|zooms?|drift\w*|shake)\b"
               r"|\b(static|still) (shots?|camera|frames?)\b|\bkeep (every|each|all|the) shots? (still|static)\b"),
    ("clips_only", r"\b(no|never|without|stop using|avoid)\b.*\b(screenshots?|stills?|images?|photos?)\b"
                   r"|\b(only|just) (real )?(trailer )?(video )?clips\b"),
    ("fit_frame", r"\b(crop\w*|cut(ting)? off|cropped)\b.*\b(details?|text|content|subjects?|edges?)\b"
                  r"|\b(full|whole) (frame|composition|shot)\b|\bsafe area\b"),
    ("crossfade", r"\b(cross.?fades?|dissolves?|softer (transitions?|cuts?)|smoother transitions?)\b"),
]
COLOUR_WORDS = {"yellow": "classic", "green": "lime", "lime": "lime", "blue": "ice", "cyan": "ice",
                "red": "fire", "orange": "fire", "pink": "pink", "purple": "pink"}
QUOTED = re.compile(r"[\"“](.+?)[\"”]")


def parse_feedback(text: str, mode: str) -> tuple[dict, dict, list[str], list[str]]:
    """(style changes as {dim: 'up'/'down'/value}, text changes {'hook': ...},
    recognised intents, sentences nothing matched)."""
    changes: dict = {}
    texts: dict = {}
    intents: list[str] = []
    unknown: list[str] = []
    for raw in re.split(r"[\n;]+|(?<=[.!?])\s+", text or ""):
        s = raw.strip()
        if not s:
            continue
        m = re.match(r"(?i)^\s*(hook|title|text)\s*[:=]\s*(.+)$", s)
        if m:
            texts["hook"] = m.group(2).strip().strip("\"“”")
            intents.append("new_hook")
            continue
        low = s.lower()
        hit = False
        for intent, rx in RULES:
            if re.search(rx, low):
                intents.append(intent)
                hit = True
        for word, colour in COLOUR_WORDS.items():
            if re.search(rf"\b{word}\b", low) and re.search(r"\b(text|captions?|highlight|words?|colou?r)\b", low):
                changes["colour"] = colour
                intents.append(f"colour_{colour}")
                hit = True
                break
        for key, fam in FONTS.items():
            if key in low or fam.lower() in low:
                changes["font"] = key
                intents.append(f"font_{key}")
                hit = True
                break
        q = QUOTED.search(s)
        if q and "new_hook" not in intents:
            texts["hook"] = q.group(1).strip()
            intents.append("new_hook")
            hit = True
        if not hit:
            unknown.append(s)
    for intent in intents:
        if intent == "text_bigger":
            changes["size"] = "up"
        elif intent == "text_smaller":
            changes["size"] = "down"
        elif intent == "text_higher":
            changes["position"] = "up"
        elif intent == "text_lower":
            changes["position"] = "down"
        elif intent == "text_middle":
            changes["position"] = "middle"
        elif intent == "faster":
            changes["pace"] = "up"
        elif intent == "slower":
            changes["pace"] = "down"
        elif intent == "more_colour":
            changes["saturation"] = "up"
        elif intent == "less_colour":
            changes["saturation"] = "down"
        elif intent == "more_contrast":
            changes["contrast"] = "up"
        elif intent == "less_contrast":
            changes["contrast"] = "down"
        elif intent in ("warmer", "cooler"):
            changes["grade"] = "warm" if intent == "warmer" else "cool"
        elif intent in ("no_flash", "no_zoom"):
            changes["transition"] = "cut"
        elif intent in ("flash", "zoom"):
            changes["transition"] = intent
        elif intent in ("cta", "question") and mode == "lacy":
            changes["ending"] = intent
        elif intent == "no_ending" and mode == "lacy":
            changes["ending"] = "none"
        elif intent in ("no_text", "less_text", "full_text"):
            changes["text_level"] = {"no_text": "none", "less_text": "minimal", "full_text": "full"}[intent]
        elif intent == "logo_intro" and mode == "tokyo_girl":
            changes["intro"] = "logo"
        elif intent == "static":
            changes["motion"] = "none"
            if changes.get("transition") == "zoom":
                changes["transition"] = "cut"
        elif intent == "clips_only" and mode == "tokyo_girl":
            changes["media"] = "clips"
        elif intent == "fit_frame" and mode == "tokyo_girl":
            changes["framing"] = "fit"
        elif intent == "crossfade" and mode == "tokyo_girl":
            changes["transition"] = "fade"
    return changes, texts, list(dict.fromkeys(intents)), unknown


def apply_changes(style: dict, changes: dict) -> dict:
    """New style dict with the parsed feedback applied."""
    s = json.loads(json.dumps(style))
    c = s.setdefault("custom", {})
    for dim, val in changes.items():
        if dim == "size":
            s["size"] = _step(tuple(SIZES), s.get("size", "m"), 1 if val == "up" else -1)
            c.pop("text_px", None)
        elif dim == "position":
            if val == "middle":
                s["position"] = "middle"
            else:  # POSITIONS run top to bottom
                s["position"] = _step(POSITIONS, s.get("position", "low"), -1 if val == "up" else 1)
            c.pop("text_y", None)
        elif dim == "pace":
            s["pace"] = _step(tuple(PACES), s.get("pace", "normal"), 1 if val == "up" else -1)
            c.pop("shot_s", None)
        elif dim in ("saturation", "contrast"):
            g = resolve(s)["grade"]
            factor = (1.15 if dim == "saturation" else 1.08) ** (1 if val == "up" else -1)
            lo, hi = CUSTOM_KEYS[dim]
            c[dim] = round(min(hi, max(lo, g[dim] * factor)), 3)
        else:
            s[dim] = val
            if dim == "colour":
                c.pop("text_colour", None)
                c.pop("highlight", None)
            if dim == "grade":
                for k in ("saturation", "contrast", "brightness", "warmth"):
                    c.pop(k, None)
    if not c:
        s.pop("custom", None)
    return s


def set_value(style: dict, setting: str, value: str, mode: str) -> dict:
    """Set one setting by name (used by /preset edit and /style). Raises ValueError."""
    s = json.loads(json.dumps(style))
    opts = dims(mode)
    if setting in opts:
        if value not in opts[setting]:
            raise ValueError(f"{setting} must be one of: {', '.join(opts[setting])}")
        s[setting] = value
        return s
    if setting in CUSTOM_KEYS:
        lo, hi = CUSTOM_KEYS[setting]
        try:
            v = float(value)
        except ValueError:
            raise ValueError(f"{setting} needs a number ({lo} to {hi})") from None
        if not lo <= v <= hi:
            raise ValueError(f"{setting} must be between {lo} and {hi}")
        s.setdefault("custom", {})[setting] = v
        return s
    if setting in CUSTOM_STR:
        if setting != "hook_style" and not hex_ok(value):
            raise ValueError(f"{setting} needs a colour like #FFE500")
        s.setdefault("custom", {})[setting] = ("#" + value.lstrip("#").upper()) if setting != "hook_style" else value
        return s
    raise ValueError(f"unknown setting {setting!r}")


def parse_numbers(text: str) -> list[int]:
    """"3, 5-7 12" -> [3, 5, 6, 7, 12] (a range covers at most 500 numbers)."""
    out: list[int] = []
    for lo, hi, one in re.findall(r"(\d+)\s*(?:-|–|to)\s*(\d+)|(\d+)", text or ""):
        a, b = (int(lo), int(hi)) if lo else (int(one), int(one))
        a, b = min(a, b), max(a, b)
        for n in range(a, min(b, a + 499) + 1):
            if n not in out:
                out.append(n)
    return out


# Notes saved before the editor had these settings -> real settings (once, on load). Each
# rule: (pattern, [(setting, value, mode or None = the note's own mode)]). A note that
# matches becomes those setting entries and is switched off; an empty list means the editor
# now does it by itself (e.g. the logo builds over a calm clip and bursts on the drop).
NOTES_UPGRADE = "notes-to-settings-1"
_TG = "tokyo_girl"
NOTE_RULES: list[tuple[str, list[tuple[str, object, str | None]]]] = [
    (r"camera movement|virtual camera|fully static|completely static|shots? (fully |completely )?static"
     r"|no (camera )?(panning|zoom)|animated stills|artificial movement|random camera",
     [("motion", "none", None)]),
    (r"screenshots?|still images|animating still", [("media", "clips", _TG)]),
    (r"\bcrop|safe area|cut off|cut-off|full composition|full frame|9:16|cropping", [("framing", "fit", _TG)]),
    (r"\b24[- ]?second|24 seconds", [("max_s", 24.0, _TG)]),
    (r"two or more beats|cut away mid-action|longer holds|chopped", [("hold_beats", 2.0, _TG)]),
    (r"slower and softer|softer|crossfade|dissolve", [("transition", "fade", _TG)]),
    (r"logo gradually|build the gta vi logo|expand it quickly", [("intro", "logo", _TG), ("logo_s", 4.0, _TG),
                                                               ("logo_size", 0.6, _TG)]),
    (r"no captions, hook text or on-screen text|captions must be blank", [("text_level", "none", None)]),
    (r"fade out 1-2 seconds sooner|beat drop can be matched|ambient atmosphere trailer clip"
     r"|logo is a deliberate exception|logo intro remains the one allowed exception", []),
]
# "Not possible yet" items the editor can now do.
NOW_POSSIBLE = (r"logo intro stays|fade-out timing|zoom or crop|crop setting|clips over screenshots|source media mix"
                r"|prefer video clips|camera movement|static-shot|16:9|safe-area|safe area|4-second|gradual reveal"
                r"|beat drop|logo expansion|total video length|clip start times|crossfades?|dissolves|framing works"
                r"|approved phrase list|no-screenshots")


def upgrade_notes(entries: list[dict], now: float | None = None) -> list[dict]:
    """Turn notes that the editor can now enforce into setting entries (appended, so they
    are the newest and win) and switch the notes, and the "not possible" items that are
    possible now, off. Returns the added entries."""
    added: list[dict] = []
    done: dict[tuple, dict] = {}
    for e in list(entries):
        if not e.get("on", True):
            continue
        text = (e.get("text") or "").lower()
        if e.get("kind") == "unsupported" and re.search(NOW_POSSIBLE, text):
            e["on"], e["upgraded"] = False, "possible now"
            continue
        if e.get("kind") != "note":
            continue
        hits = [row for rx, rows in NOTE_RULES if re.search(rx, text) for row in rows]
        matched = any(re.search(rx, text) for rx, _ in NOTE_RULES)
        if not matched:
            continue
        nums = []
        for setting, value, mode in hits:
            key = (mode or e.get("mode", "all"), setting, value)
            if key not in done:
                new = {"n": max([x["n"] for x in entries] or [0]) + 1, "ts": now or time.time(), "kind": "setting",
                       "mode": key[0], "setting": setting, "value": value, "text": "",
                       "said": e.get("text", "")[:500], "source": "from your notes", "on": True}
                entries.append(new)
                added.append(new)
                done[key] = new
            nums.append(done[key]["n"])
        e["on"] = False
        e["upgraded"] = ("now " + ", ".join(f"#{n}" for n in dict.fromkeys(nums))) if nums else "built in now"
    return added


def migrate(d: dict) -> list[dict]:
    """Version 1 profiles (rules with pins + /style pins) -> the numbered rules list.
    Rules are replayed oldest first so the newest instruction wins; /style pins that no rule
    explains go first. Old fields are left in the file, unused."""
    entries: list[dict] = []

    def add(**kw):
        entries.append({"n": len(entries) + 1, "ts": kw.pop("ts", 0), "on": True, "said": "", "text": "",
                        "setting": None, "value": None, "source": "migrated", **kw})
    covered = {(r_["mode"], r_["setting"]) for r in d.get("rules") or [] for r_ in r.get("settings") or []}
    for key, v in (d.get("pinned") or {}).items():
        dim, mode = _pin_target(key)
        if (mode, dim) not in covered:
            add(kind="setting", mode=mode, setting=dim, value=v, source="/style")
    for mode, bucket in (d.get("custom_pins") or {}).items():
        for k, v in (bucket or {}).items():
            if (mode, k) not in covered:
                add(kind="setting", mode=mode, setting=k, value=v, source="/style")
    for r in d.get("rules") or []:
        said, ts_ = r.get("text", ""), r.get("ts", 0)
        for row in r.get("settings") or []:
            add(kind="setting", mode=row["mode"], setting=row["setting"], value=row.get("value"), said=said, ts=ts_,
                source=r.get("source", "feedback"))
        for n in r.get("notes") or []:
            add(kind="note", mode=r.get("mode", "all"), text=n, said=said, ts=ts_, source=r.get("source", "feedback"))
        for u in r.get("unsupported") or []:
            add(kind="unsupported", mode=r.get("mode", "all"), text=u, said=said, ts=ts_,
                source=r.get("source", "feedback"))
    return entries


def apply_settings(rows: list[dict], default_mode: str = "all") -> tuple[list[dict], list[str]]:
    """Validate [{"mode" or "modes", "setting", "value"}] (from /feedback or the AI) against the
    real options of each kind of edit. A row keeps only the kinds where the setting exists
    (ending: Lacy; framing, intro...: Tokyo Girl and trend edits). Returns (clean rows with
    "modes" and "mode", reasons for the ones dropped)."""
    ok, bad = [], []
    for row in rows or []:
        setting = str(row.get("setting") or "").strip()
        value = str(row.get("value") if row.get("value") is not None else "").strip()
        want = row.get("modes") or row.get("mode")
        if not (want == "all" or (isinstance(want, str) and want in STYLE_MODES) or isinstance(want, list)):
            want = default_mode
        asked = expand_modes(want)
        modes = [m for m in asked if m in valid_modes(setting, value)]
        if not modes:
            elsewhere = valid_modes(setting, value)
            if elsewhere:
                bad.append(f"{setting} only applies to {modes_label(elsewhere)}, not {modes_label(asked)}")
                continue
            try:   # the reason from the first kind asked for
                set_value(default_style(asked[0] if asked else "lacy"), setting, value, asked[0] if asked else "lacy")
                bad.append(f"{setting} = {value} isn't possible for {modes_label(asked)}")
            except ValueError as e:
                bad.append(str(e))
            continue
        if setting in CUSTOM_KEYS:
            v: float | str = float(value)
        elif setting in CUSTOM_STR and setting != "hook_style":
            v = "#" + value.lstrip("#").upper()
        else:
            v = value
        ok.append({"mode": "all" if modes == list(STYLE_MODES) else modes[0], "modes": modes,
                   "setting": setting, "value": v})
    return ok, bad


# --------------------------------------------------------------------------
# Hook styles (shared with #inspiration): what kind of opening line is it?
# --------------------------------------------------------------------------
def hook_style(text: str) -> str:
    t = (text or "").strip().lower()
    if not t:
        return "none"
    if t.startswith("pov"):
        return "pov"
    if re.match(r"^(wait|watch|stay) (for|till|until|to the end)", t) or "wait for" in t:
        return "wait"
    if t.endswith("?") or re.match(r"^(who|what|why|how|would|is|are|do|did|can|which)\b", t):
        return "question"
    if re.search(r"\b\d+\s*(days?|hours?)\b", t):
        return "countdown"
    if re.match(r"^(nobody|no one|everyone|when|this is|the moment)\b", t):
        return "reveal"
    return "statement"


def ending_style(last_text: str, loops: bool) -> str:
    t = (last_text or "").strip().lower()
    if re.search(r"\b(follow|subscribe|like|comment|link in bio|part 2|share)\b", t):
        return "cta"
    if t.endswith("?"):
        return "question"
    return "loop" if loops else "none"
