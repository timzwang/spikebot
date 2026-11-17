"""Named style presets from #inspiration: data/presets/<name>.json.

A preset holds *numbers about a style* (shot length, grade, text placement...),
never the reference video itself. Each part can be copied on its own:

    pacing, colour, text, transitions, hook, structure, music-sync

`to_style(preset, mode)` turns the copied parts into a variant style
(see style.py); `combine()` builds a new preset from parts of others
(e.g. pacing from one, colour from another).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import style as st

PARTS = ("pacing", "colour", "text", "transitions", "hook", "structure", "music-sync", "look")
ALIASES = {"pace": "pacing", "pacing": "pacing", "cuts": "pacing", "color": "colour",
           "colour": "colour", "grade": "colour", "text": "text", "captions": "text",
           "font": "text", "transition": "transitions", "transitions": "transitions",
           "hook": "hook", "hooks": "hook", "structure": "structure",
           "music": "music-sync", "music-sync": "music-sync", "music_sync": "music-sync",
           "musicsync": "music-sync", "beat": "music-sync", "beats": "music-sync",
           "sync": "music-sync", "look": "look", "effects": "look", "fx": "look",
           "cinematic": "look", "vibe": "look", "all": "all"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# which preset settings each part may write into a style
PART_KEYS = {
    "pacing": {"pace", "shot_s"},
    "colour": {"grade", "saturation", "contrast", "brightness", "warmth"},
    "text": {"position", "size", "colour", "font", "text_px", "text_y", "text_colour",
             "highlight", "outline", "shadow", "words"},
    "transitions": {"transition"},
    "hook": {"hook_style"},
    "structure": {"hook_s", "payoff", "ending"},
    "music-sync": set(),   # Tokyo Girl always cuts on the official sound's beat
    # artistic choices read from the frames by the AI (how much text, logo intro, film look)
    "look": {"text_level", "intro", "grain", "vignette", "letterbox"},
}


def parse_copy(note: str) -> list[str]:
    """'copy: pacing, colour' -> ['pacing', 'colour']. No note (or 'all') = every part."""
    m = re.search(r"(?i)\bcopy\s*:\s*([^\n]+)", note or "")
    if not m:
        return list(PARTS)
    out: list[str] = []
    for w in re.split(r"[,\s/+&]+|\band\b", m.group(1).lower()):
        p = ALIASES.get(w.strip(" .;"))
        if p == "all":
            return list(PARTS)
        if p and p not in out:
            out.append(p)
    return out or list(PARTS)


def parse_name(note: str) -> str | None:
    m = re.search(r"(?i)\bname\s*:\s*([^\n,;]+?)(?=\s+copy\s*:|[\n,;]|$)", note or "")
    return clean_name(m.group(1)) if m else None


def clean_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-_")[:40]
    if not NAME_RE.match(n):
        raise ValueError("preset names are letters, numbers, - and _ (max 40)")
    return n


class Presets:
    def __init__(self, folder: Path | str):
        self.dir = Path(folder)

    def path(self, name: str) -> Path:
        return self.dir / f"{clean_name(name)}.json"

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json")) if self.dir.exists() else []

    def all(self) -> dict[str, dict]:
        out = {}
        for n in self.names():
            try:
                out[n] = self.load(n)
            except (ValueError, OSError):
                continue
        return out

    def load(self, name: str) -> dict:
        p = self.path(name)
        if not p.exists():
            raise ValueError(f"no preset called {name!r}")
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, preset: dict, overwrite: bool = True) -> Path:
        preset["name"] = clean_name(preset["name"])
        p = self.path(preset["name"])
        if p.exists() and not overwrite:
            raise ValueError(f"preset {preset['name']!r} already exists")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(preset, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        return p

    def unique_name(self, base: str) -> str:
        base = clean_name(base)[:34]
        if not self.path(base).exists():
            return base
        n = 2
        while self.path(f"{base}-{n}").exists():
            n += 1
        return f"{base}-{n}"

    def rename(self, old: str, new: str) -> dict:
        p = self.load(old)
        new = clean_name(new)
        if self.path(new).exists():
            raise ValueError(f"preset {new!r} already exists")
        p["name"] = new
        self.save(p)
        self.path(old).unlink()
        return p

    def delete(self, name: str) -> None:
        self.load(name)
        self.path(name).unlink()

    def edit(self, name: str, setting: str, value: str) -> dict:
        """Change one setting. `copy` takes a part list; anything else is a style
        setting (pace, grade, saturation, text_px, ending...)."""
        p = self.load(name)
        if setting == "copy":
            p["copy"] = parse_copy("copy: " + value)
            return self._saved(p)
        part = next((pt for pt, keys in PART_KEYS.items() if setting in keys), None)
        if part is None:
            raise ValueError(f"unknown setting {setting!r}; try one of: copy, "
                             + ", ".join(sorted(set().union(*PART_KEYS.values()))))
        if setting in ("ending",):
            if value not in ("none", "question", "cta", "loop"):
                raise ValueError("ending must be none, question, cta or loop")
            checked = value
        elif setting in ("hook_style",):
            checked = value.strip().lower()
        else:
            # validate through the style rules (options or numeric ranges)
            probe = st.set_value({}, setting, value, "lacy")
            checked = probe.get(setting, (probe.get("custom") or {}).get(setting))
        p.setdefault("settings", {}).setdefault(part, {})[setting] = checked
        if part not in p.setdefault("copy", []):
            p["copy"].append(part)
        p.setdefault("edits", []).append({"ts": time.time(), "setting": setting, "value": checked})
        return self._saved(p)

    def _saved(self, p: dict) -> dict:
        self.save(p)
        return p

    def combine(self, new: str, parts: dict[str, str]) -> dict:
        """{part: preset name} -> a new preset copying only those parts."""
        if not parts:
            raise ValueError("pick at least one part to take from another preset")
        out = {"name": clean_name(new), "created": time.time(), "source": {"combined": parts},
               "copy": [], "settings": {}, "analysis": {}}
        if self.path(out["name"]).exists():
            raise ValueError(f"preset {out['name']!r} already exists")
        for part, name in parts.items():
            if part not in PARTS:
                raise ValueError(f"unknown part {part!r}")
            src = self.load(name)
            out["settings"][part] = dict((src.get("settings") or {}).get(part) or {})
            out["copy"].append(part)
        self.save(out)
        return out


def nearest(options: dict[str, float], value: float) -> str:
    return min(options, key=lambda k: abs(options[k] - value))


def to_style(preset: dict, mode: str) -> dict:
    """The copied parts of a preset as a (partial) style for this mode."""
    out: dict = {}
    custom: dict = {}
    opts = st.dims(mode)
    sets = preset.get("settings") or {}
    for part in preset.get("copy") or PARTS:
        for k, v in (sets.get(part) or {}).items():
            if k not in PART_KEYS.get(part, set()) or v is None:
                continue
            if k == "ending":
                if mode == "lacy":
                    out["ending"] = "none" if v == "loop" else v
            elif k in opts:
                if v in opts[k]:
                    out[k] = v
            elif k in st.CUSTOM_KEYS or k in st.CUSTOM_STR:
                custom[k] = v
    if custom:
        out["custom"] = custom
    return out


def summary(preset: dict, profile_row: dict | None = None) -> str:
    """Readable lines for /preset show and /preset list."""
    sets = preset.get("settings") or {}
    lines = [f"**{preset['name']}** · copies: {', '.join(preset.get('copy') or PARTS)}"]
    src = preset.get("source") or {}
    if src.get("url"):
        lines.append(f"from <{src['url']}>")
    elif src.get("combined"):
        lines.append("combined from " + ", ".join(f"{p} ← {n}" for p, n in src["combined"].items()))
    for part in PARTS:
        vals = sets.get(part)
        if vals:
            lines.append(f"• {part}: " + ", ".join(f"{k}={v}" for k, v in vals.items()))
    art = preset.get("art") or {}
    for g in (art.get("what_makes_it_good") or [])[:4]:
        lines.append(f"★ {g}")
    if profile_row:
        lines.append(f"used {profile_row.get('shown', 0)}× · approved {profile_row.get('approved', 0)}"
                     f" · rejected {profile_row.get('rejected', 0)}")
    return "\n".join(lines)
