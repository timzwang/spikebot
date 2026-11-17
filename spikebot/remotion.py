"""Remotion renderer for Tokyo Girl edits: the same plan (shots on the beat, text cards,
logo intro, grade/look) rendered with real motion design instead of plain ffmpeg:
eased push-ins and drifts, spring punch-ins with motion blur, eased white flashes,
word-by-word kinetic text, a logo that resolves from blur and scales away into the
edit, film grain, vignette and cinema bars.

The React project is in remotion/ (`npm ci` there; install.sh / auto_update.sh do it).
Remotion downloads its own headless Chrome on the first render. If Node or the project
isn't installed, or a render fails, the ffmpeg renderer is used instead, so an edit is
never lost. Output is always silent (--muted), like the ffmpeg path.

Licence: Remotion is free for individuals and companies of up to 3 people.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("remotion")
ROOT = Path(__file__).resolve().parent.parent / "remotion"
TEXT_Y = {"high": 0.27, "middle": 0.44, "low": 0.6}


class RemotionError(RuntimeError):
    pass


def node_bin(root: Path = ROOT) -> str | None:
    """Folder with `node`: the system's, or the copy setup_remotion.sh puts next to the app."""
    local = root.parent / "node" / "bin"
    if (local / "node").exists():
        return str(local)
    found = shutil.which("node")
    return str(Path(found).parent) if found else None


def available(root: Path = ROOT) -> bool:
    return node_bin(root) is not None and (root / "node_modules" / ".bin" / "remotion").exists()


def props_for(plan, lcfg: dict, r: dict, cards: list[tuple[float, float, str]], logo: Path | None,
              intro_s: float, media: Path) -> tuple[dict, dict[str, Path]]:
    """(props for the TrailerEdit composition, {public name: real file})."""
    files: dict[str, Path] = {}
    names: dict[str, str] = {}

    def public(path: Path) -> str:
        key = str(path.resolve())
        if key not in names:
            names[key] = f"m{len(names):03d}{path.suffix.lower()}"
            files[names[key]] = path.resolve()
        return names[key]

    shots = []
    for sl in plan.slots:
        sh = sl.shot
        shots.append({"src": public(media / sh.src), "kind": sh.kind, "start": round(sl.start, 4),
                      "dur": round(sl.dur, 4), "offset": round(sl.offset, 3), "pan": sl.pan,
                      "punch": bool(sl.punch), "flash": bool(sl.flash), "srcW": sh.w, "srcH": sh.h,
                      "crop": list(sh.crop) if sh.crop else None})
    g = r.get("grade") or {}
    look = r.get("look") or {}
    scale = float(r.get("scale") or 1.0)
    y = r.get("text_y")
    text = {"font": r.get("font") or lcfg.get("font") or "DejaVu Sans",
            "sizePx": int(r.get("text_px") or lcfg.get("font_size", 104) * scale * 0.92),
            "colour": r.get("text_colour") or "#FFFFFF", "highlight": r.get("highlight") or "#FFE500",
            "y": float(y) if y is not None else TEXT_Y.get(r.get("position") or "high", 0.27)}
    props = {"fps": 30, "width": 1080, "height": 1920, "duration": round(plan.duration, 4),
             "shots": shots,
             "cards": [{"from": a, "to": b, "text": t, "ask": bool(plan.cards) and t == plan.cards[-1][2]
                        and len(plan.cards) > 1} for a, b, t in cards],
             "logo": {"src": public(logo), "until": round(intro_s, 4)} if logo and intro_s else None,
             "look": {"saturation": g.get("saturation", 1.0), "contrast": g.get("contrast", 1.0),
                      "brightness": g.get("brightness", 0.0), "warmth": g.get("warmth", 0.0),
                      "grain": float(look.get("grain") or 0), "vignette": bool(look.get("vignette")),
                      "letterbox": bool(look.get("letterbox"))},
             "text": text}
    return props, files


# "Rendered 120/900" (older CLI) or "Rendering frames ━━━━ 120/900" (4.x, non-TTY prints lines)
FRAMES_RE = re.compile(r"Render\w*(?:\s+frames)?\D*?(\d+)\s*/\s*(\d+)")


def run_with_progress(cmd: list[str], cwd: Path, env: dict, timeout: float) -> tuple[int, str]:
    """Run the Remotion CLI, feeding "Rendered 120/900" lines to the live progress bar.
    Returns (exit code, last output)."""
    from . import progress
    job = progress.current()
    tail: list[str] = []
    start = time.time()
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    except OSError as e:
        raise RemotionError(f"could not start remotion: {e}") from e
    assert p.stdout is not None
    for line in p.stdout:
        tail = (tail + [line.rstrip()])[-40:]
        m = FRAMES_RE.search(line)
        if m and int(m.group(2)):
            done, total = int(m.group(1)), int(m.group(2))
            job.update(0.95 * done / total, detail=f"frame {done}/{total}")
        if time.time() - start > timeout:
            p.kill()
            raise RemotionError(f"render took over {timeout / 60:.0f} min")
    try:
        code = p.wait(timeout=max(1, timeout - (time.time() - start)))
    except subprocess.TimeoutExpired as e:
        p.kill()
        raise RemotionError(f"render took over {timeout / 60:.0f} min") from e
    return code, "\n".join(tail)


def render(plan, lcfg: dict, out: Path, r: dict, cards, logo: Path | None = None, intro_s: float = 0.0,
           root: Path = ROOT, timeout: float = 2400) -> Path:
    """Render one edit with Remotion. Raises RemotionError (the caller falls back to ffmpeg)."""
    if not available(root):
        raise RemotionError("Remotion isn't installed (node + remotion/node_modules)")
    props, files = props_for(plan, lcfg, r, cards, logo, intro_s, Path(lcfg["media_dir"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(lcfg.get("data_dir") or out.parent) / ".remotion"   # same disk as the media: hard links
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r-", dir=work) as tmp:
        pub = Path(tmp) / "public"
        pub.mkdir()
        for name, real in files.items():   # Remotion doesn't follow symlinks in its public folder
            try:
                os.link(real, pub / name)
            except OSError:
                shutil.copyfile(real, pub / name)
        pj = Path(tmp) / "props.json"
        pj.write_text(json.dumps(props), encoding="utf-8")
        part = Path(tmp) / "edit.mp4"
        cmd = [str(root / "node_modules" / ".bin" / "remotion"), "render", "src/index.ts", "TrailerEdit",
               str(part), f"--props={pj}", f"--public-dir={pub}", "--muted", "--codec=h264", "--crf=19",
               "--log=info"]
        browser = os.environ.get("REMOTION_BROWSER") or lcfg.get("remotion_browser")
        if browser:   # a Chrome already on the machine, instead of Remotion's own download
            cmd.append(f"--browser-executable={browser}")
        env = dict(os.environ, PATH=f"{node_bin(root)}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        code, text = run_with_progress(cmd, root, env, timeout)
        if code != 0 or not part.exists():
            raise RemotionError(f"remotion failed ({code}): {text.strip()[-400:]}")
        # faststart + guarantee there is no audio stream at all
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(part), "-an",
                        "-c:v", "copy", "-movflags", "+faststart", str(out)], check=True, timeout=300)
    return out
