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
ENDINGS = ("none", "question", "cta")
HOOKS = {
    "lacy": ("matched", "funny", "shock", "hype", "neutral"),
    "tokyo_girl": ("news", "theory", "meme", "couple"),
}
DEFAULT = {"grade": "natural", "pace": "normal", "font": "dejavu", "size": "m",
           "position": "low", "colour": "classic", "transition": "flash",
           "ending": "none"}

# Exact values a preset or feedback can set (shown and editable with /preset edit).
CUSTOM_KEYS = {
    "saturation": (0.3, 2.0), "contrast": (0.6, 1.6), "brightness": (-0.25, 0.25),
    "warmth": (-0.2, 0.2), "text_px": (36, 160), "text_y": (0.15, 0.85),
    "outline": (0, 14), "shadow": (0, 8), "hook_s": (1.0, 60.0), "payoff": (0.2, 0.9),
    "shot_s": (0.2, 6.0), "words": (1, 6),
}
CUSTOM_STR = {"text_colour", "highlight", "hook_style"}


def dims(mode: str) -> dict[str, tuple[str, ...]]:
    """Every dimension a variant varies in for this mode, with its options."""
    out = {"grade": tuple(GRADES), "pace": tuple(PACES), "font": tuple(FONTS),
           "size": tuple(SIZES), "position": POSITIONS, "colour": tuple(COLOURS),
           "transition": TRANSITIONS, "hook": HOOKS.get(mode, ("matched",))}
    if mode == "lacy":
        out["ending"] = ENDINGS
    return out


def stat_key(dim: str, mode: str) -> str:
    """Hook tones only make sense per mode; the look is shared across modes."""
    return f"hook:{mode}" if dim == "hook" else dim


def default_style(mode: str) -> dict:
    s = dict(DEFAULT)
    s["hook"] = HOOKS.get(mode, ("matched",))[0]
    if mode != "lacy":
        s.pop("ending")
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
        "transition": style.get("transition") if style.get("transition") in TRANSITIONS else "flash",
        "ending": style.get("ending") if style.get("ending") in ENDINGS else "none",
        "hook_s": c.get("hook_s"), "payoff": c.get("payoff"),
        "hook_style": c.get("hook_style"),
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


def describe(style: dict) -> str:
    """One line for the Discord card."""
    c = style.get("custom") or {}
    bits = [f"grade **{style.get('grade')}**", f"pace **{style.get('pace')}**",
            f"text **{style.get('font')}/{style.get('size')}/{style.get('position')}/{style.get('colour')}**",
            f"transition **{style.get('transition')}**", f"hook **{style.get('hook')}**"]
    if style.get("ending") not in (None, "none"):
        bits.append(f"ending **{style['ending']}**")
    if c:
        bits.append("custom " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    return " · ".join(bits)


# --------------------------------------------------------------------------
# Learning profile: data/style_profile.json
# --------------------------------------------------------------------------
def _blank() -> dict:
    return {"version": 1, "pinned": {}, "stats": {}, "presets": {}, "decisions": [],
            "feedback": {"notes": [], "intents": {}}}


class Profile:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.data = self.load()

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return _blank()
        return _blank() | d

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

    def pinned(self, mode: str) -> dict:
        """/style pins. Hook pins are per mode ("hook:lacy")."""
        p = dict(self.data["pinned"])
        out = {k: v for k, v in p.items() if ":" not in k}
        if f"hook:{mode}" in p:
            out["hook"] = p[f"hook:{mode}"]
        if out.get("hook") not in HOOKS.get(mode, ()):
            out.pop("hook", None)
        return out

    def pin(self, setting: str, value: str | None) -> None:
        if value in (None, "", "auto"):
            self.data["pinned"].pop(setting, None)
        else:
            self.data["pinned"][setting] = value

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
                least = min(shown.get(o, {}).get("shown", 0) for o in unseen)
                style[dim] = rng.choice([o for o in unseen if shown.get(o, {}).get("shown", 0) == least])
            else:
                weights = [profile.score(key, o) ** 2 for o in unseen]
                style[dim] = rng.choices(unseen, weights=weights)[0]
        if base.get("custom"):
            style["custom"] = dict(base["custom"])
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
