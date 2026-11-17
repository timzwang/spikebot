"""Live progress for long jobs (renders, #inspiration analysis, song downloads), shown in
Discord as one message with a bar that the bot keeps editing.

Writers (render subprocesses, the Lacy worker, the inspiration/music subprocesses) update
a small JSON file in data/progress/<id>.json. The Discord bot polls those files
(`ReviewBot.progress_loop`) and edits one message per job; the message is deleted when
the job finishes (the review card or reply takes its place) and kept, marked failed, if it
fails. Jobs nest: `span(lo, hi)` maps an inner 0..1 onto part of the outer bar, so a
review render inside a Lacy job just fills the Lacy job's last stretch.

Never raises into the caller: progress is cosmetic, a write problem must not stop a render.
The one deliberate exception is cancelling (/cancel or the bar's Cancel button): the bot
writes `<id>.cancel` next to the job file and sends SIGTERM to the worker. A worker that
called `arm_cancel()` turns that into `Cancelled` (and so does the next `update()`), which
job boundaries catch to clean up; a batch (Tokyo Girl daily, trend) then moves on to its
next edit. A worker that doesn't stop within `HARD_KILL_S` has its process group killed.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import signal
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("progress")
ENV = "SPIKEBOT_PROGRESS"         # a subprocess picks up the job file the bot made for it
STALE_S = 45 * 60                 # no update for this long while running = the process died
HARD_KILL_S = 30                  # a cancelled worker that hasn't stopped by then is killed
_current: contextvars.ContextVar = contextvars.ContextVar("spikebot_progress", default=None)
_mine: list[Path] = []            # job files this process has worked on (for the SIGTERM handler)


class Cancelled(BaseException):
    """Raised inside a worker whose job was cancelled. A BaseException so that the many
    `except Exception` failure handlers don't report a cancel as a failed render."""


def folder(data_dir: Path | str) -> Path:
    return Path(data_dir) / "progress"


class Job:
    def __init__(self, path: Path, data: dict):
        self.path, self.data = Path(path), data
        self.spans: list[tuple[float, float]] = [(0.0, 1.0)]
        self._written = 0.0
        self._checked = 0.0
        self._pid: int | None = None

    # ---- creating -------------------------------------------------------------
    @classmethod
    def create(cls, data_dir: Path | str, title: str, channel: str = "review", channel_id: int | None = None,
               icon: str = "🎬", now: float | None = None, meta: dict | None = None) -> "Job":
        """`meta` says what the job works on, for clean-up after a forced stop
        (e.g. {"item": review item id})."""
        now = now or time.time()
        jid = f"{int(now)}-{uuid.uuid4().hex[:6]}"
        prune(data_dir, now)
        job = cls(folder(data_dir) / f"{jid}.json",
                  {"id": jid, "title": title, "icon": icon, "channel": channel, "channel_id": channel_id,
                   "state": "running", "frac": 0.0, "stage": "starting", "detail": "",
                   "started": now, "updated": now, "meta": meta or {}})
        job.write(force=True)
        _mine.append(job.path)
        return job

    @classmethod
    def from_env(cls) -> "Job | None":
        p = os.environ.get(ENV)
        if not p:
            return None
        try:
            job = cls(Path(p), json.loads(Path(p).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        _mine.append(job.path)
        return job

    # ---- updating -------------------------------------------------------------
    def write(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._written < 1.0:
            return
        self._written = now
        self.data["updated"] = now
        pid = self._pid or os.getpid()
        self.data["pid"] = pid           # the process doing the work (the bot can see if it died)
        try:
            self.data["pgid"] = os.getpgid(pid)   # what a forced stop kills (with ffmpeg/Chrome children)
        except OSError:
            pass
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.debug("progress write failed: %s", e)

    def adopt(self, pid: int) -> None:
        """The bot made this job for a subprocess: record the subprocess as the worker
        straight away, so /cancel can reach it before its first update."""
        self._pid = int(pid)
        self.write(force=True)

    # ---- cancelling -----------------------------------------------------------
    @property
    def cancel_path(self) -> Path:
        return cancel_path(self.path)

    def cancel_requested(self) -> bool:
        return self.cancel_path.exists()

    def check(self) -> None:
        """Raise `Cancelled` if this job was cancelled (cheap: one stat, at most twice a second)."""
        now = time.time()
        if now - self._checked < 0.5:
            return
        self._checked = now
        if self.cancel_requested():
            raise Cancelled(self.data.get("id", ""))

    def update(self, frac: float | None = None, stage: str | None = None, detail: str | None = None) -> None:
        """`frac` is 0..1 inside the current span. Raises `Cancelled` once the job is cancelled."""
        self.check()
        changed = False
        if frac is not None:
            lo, hi = self.spans[-1]
            self.data["frac"] = round(max(self.data.get("frac", 0.0), lo + (hi - lo) * max(0.0, min(1.0, frac))), 4)
        if stage is not None and stage != self.data.get("stage"):
            self.data["stage"], changed = stage, True
            self.data["detail"] = ""
        if detail is not None:
            self.data["detail"] = detail
        self.write(force=changed)

    @contextlib.contextmanager
    def span(self, lo: float, hi: float):
        """Map 0..1 of the inner work onto lo..hi of the current span."""
        a, b = self.spans[-1]
        self.spans.append((a + (b - a) * lo, a + (b - a) * hi))
        try:
            self.update(0.0)
            yield self
        finally:
            self.update(1.0)
            self.spans.pop()

    def finish(self, ok: bool = True, text: str = "", state: str | None = None) -> None:
        """`state` "skipped" = nothing wrong, just not worth a clip (shown briefly).
        A cancelled job always ends as "cancelled", whoever finishes it."""
        if self.cancel_requested():
            state, ok, text = "cancelled", False, self._cancel_text()
        self.data.update(state=state or ("done" if ok else "failed"),
                         frac=1.0 if ok else self.data.get("frac", 0.0),
                         detail=text[:300] if text else self.data.get("detail", ""))
        self.write(force=True)

    def _cancel_text(self) -> str:
        try:
            by = json.loads(self.cancel_path.read_text(encoding="utf-8")).get("by")
        except (OSError, ValueError):
            by = None
        return f"cancelled by {by}" if by else "cancelled"


class _Null:
    """Stand-in when nothing is being tracked: every call is a no-op."""
    data: dict = {}

    def update(self, *a, **k) -> None:
        pass

    @contextlib.contextmanager
    def span(self, lo: float, hi: float):
        yield self

    def finish(self, *a, **k) -> None:
        pass

    def check(self) -> None:
        pass

    def cancel_requested(self) -> bool:
        return False


NULL = _Null()


def current():
    """The job being tracked in this context (or the one the bot handed this subprocess)."""
    job = _current.get()
    if job is None:
        job = Job.from_env()
        if job is not None:
            _current.set(job)
    return job or NULL


def active() -> bool:
    return current() is not NULL


def check() -> None:
    """For long loops with no progress to report: raise `Cancelled` if the job was cancelled."""
    current().check()


def cancel_path(job_path: Path | str) -> Path:
    return Path(job_path).with_suffix(".cancel")


def arm_cancel() -> None:
    """Call at the start of a worker CLI (render, analyse, ...): SIGTERM for a cancelled job
    raises `Cancelled` where the worker is (even inside subprocess.run, which then kills its
    child). A SIGTERM with no cancel pending (systemd stop) still ends the process."""
    if threading.current_thread() is not threading.main_thread():
        return

    def on_term(signum, frame):
        job = _current.get() or Job.from_env()
        if job is not None and job.cancel_requested():
            raise Cancelled(job.data.get("id", ""))
        if any(cancel_path(p).exists() for p in _mine):
            return   # a cancel for a job of ours that has just ended: nothing to stop
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGTERM, on_term)


@contextlib.contextmanager
def tracking(job: Job):
    token = _current.set(job)
    try:
        yield job
    finally:
        _current.reset(token)


# --------------------------------------------------------------------------
# Reading (the bot)
# --------------------------------------------------------------------------
def load_all(data_dir: Path | str) -> list[tuple[Path, dict]]:
    out = []
    d = folder(data_dir)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        if p.name.startswith("_"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if cancel_path(p).exists():
            d["cancel"] = True
        out.append((p, d))
    return out


# --------------------------------------------------------------------------
# Cancelling (the bot: /cancel and the bar's Cancel button)
# --------------------------------------------------------------------------
def running(data_dir: Path | str, now: float) -> list[tuple[Path, dict]]:
    """Jobs still working (not finished, not dead), oldest first."""
    return [(p, d) for p, d in load_all(data_dir) if state_of(d, now) == "running"]


def resolve(data_dir: Path | str, ref: str, now: float) -> tuple[Path, dict]:
    """The job for a run id (the short id shown on the bar, or the full id).
    Raises ValueError with a message for Tim when there is none or it's ambiguous."""
    ref = (ref or "").strip().strip("`").lower()
    if not ref:
        raise ValueError("give a run id (it's on the progress bar, e.g. `a1b2c3`)")
    hits = [(p, d) for p, d in load_all(data_dir)
            if (d.get("id") or p.stem) == ref or short(d.get("id") or p.stem) == ref]
    if not hits:
        raise ValueError(f"there's no run `{ref}` (it may have finished already)")
    if len(hits) > 1:
        raise ValueError(f"`{ref}` matches {len(hits)} runs; use the full id")
    p, d = hits[0]
    state = state_of(d, now)
    if state != "running":
        raise ValueError(f"run `{short(d.get('id') or p.stem)}` has already "
                         + {"done": "finished", "cancelled": "been cancelled"}.get(state, state))
    return p, d


def request_cancel(job_path: Path | str, by: str = "", now: float | None = None) -> bool:
    """Mark the job cancelled and ask its worker to stop (SIGTERM). Returns False if it was
    already cancelled. Never signals this process or its process group (the bot itself)."""
    mark = cancel_path(job_path)
    if mark.exists():
        return False
    mark.write_text(json.dumps({"by": by, "ts": now or time.time()}), encoding="utf-8")
    try:
        d = json.loads(Path(job_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    _signal(d, signal.SIGTERM, group=False)
    return True


def cancelled_for(data_dir: Path | str, key: str, value: str) -> bool:
    """Was the job working on `meta[key] == value` cancelled? (A worker killed by a forced
    stop can't record it itself, so its parent asks here before calling it a crash.)"""
    return any(d.get("cancel") and (d.get("meta") or {}).get(key) == value for _, d in load_all(data_dir))


def force_stop(job_path: Path | str) -> bool:
    """Kill a cancelled worker that ignored SIGTERM (stuck in C code: Whisper, librosa),
    with its whole process group (ffmpeg, Chrome). Returns True if something was killed."""
    try:
        d = json.loads(Path(job_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if d.get("state") != "running" or not d.get("pid") or not alive(d["pid"]):
        return False
    return _signal(d, getattr(signal, "SIGKILL", signal.SIGTERM), group=True)


def _signal(d: dict, sig: int, group: bool) -> bool:
    pid = d.get("pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1 or pid == os.getpid():
        return False   # still the bot's own write: the worker checks the marker on its first update
    try:
        own = os.getpgid(0)
        pgid = int(d.get("pgid") or 0)
        if group and pgid > 1 and pgid != own:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def prune(data_dir: Path | str, now: float, keep_s: float = 86400) -> None:
    """Drop job files older than a day (nobody showed them: e.g. the Discord bot is off)."""
    d = folder(data_dir)
    if not d.is_dir():
        return
    for p in [*d.glob("*.json"), *d.glob("*.cancel")]:
        try:
            if not p.name.startswith("_") and now - p.stat().st_mtime > keep_s:
                p.unlink()
        except OSError:
            pass


EIGHTHS = " ▏▎▍▌▋▊▉"


def bar(frac: float, width: int = 16) -> str:
    """Text bar (used when the rounded emoji bar isn't available), smooth to 1/8 of a cell."""
    frac = max(0.0, min(1.0, float(frac or 0)))
    eighths = int(round(frac * width * 8))
    full, part = divmod(eighths, 8)
    cells = "█" * full + (EIGHTHS[part] if part and full < width else "")
    return "`" + cells + "░" * (width - len(cells)) + "`"


# ---- the rounded bar: small pill-shaped pieces uploaded once as the bot's own emojis -----
SEGMENTS = 12          # pieces in a bar; each is empty, half or full (24 steps)
EMOJI_PREFIX = "spkbar1_"
FILL, TRACK = (88, 101, 242), (78, 80, 88)   # Discord blurple on grey


def _png(w: int, h: int, rgba: list[bytes]) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + row for row in rgba)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _piece(kind: str, level: int, size: int = 64) -> bytes:
    """One piece: kind l (rounded left end), m (middle) or r (rounded right end); level 0
    empty, 1 left half filled, 2 full. Edges anti-aliased by 4x4 supersampling."""
    top, bottom = size * 0.30, size * 0.70
    r = (bottom - top) / 2
    cy = (top + bottom) / 2
    split = {0: 0, 1: size / 2, 2: size}[level]
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            hit = 0
            for sy in range(4):
                for sx in range(4):
                    px, py = x + (sx + 0.5) / 4, y + (sy + 0.5) / 4
                    if not top <= py <= bottom:
                        continue
                    if kind == "l" and px < r + 1:
                        hit += (px - (r + 1)) ** 2 + (py - cy) ** 2 <= r * r
                    elif kind == "r" and px > size - r - 1:
                        hit += (px - (size - r - 1)) ** 2 + (py - cy) ** 2 <= r * r
                    else:
                        hit += 1
            colour = FILL if x + 0.5 <= split else TRACK
            row += bytes(colour) + bytes([round(255 * hit / 16)])
        rows.append(bytes(row))
    return _png(size, size, rows)


def bar_pieces() -> dict[str, bytes]:
    """{emoji name: PNG} for the 9 pieces of the rounded bar."""
    return {f"{EMOJI_PREFIX}{k}{lvl}": _piece(k, lvl) for k in "lmr" for lvl in (0, 1, 2)}


def emoji_bar(frac: float, emojis: dict[str, str]) -> str | None:
    """The rounded bar as emoji markup (None if the pieces aren't uploaded)."""
    if not emojis:
        return None
    units = int(round(max(0.0, min(1.0, float(frac or 0))) * SEGMENTS * 2))
    out = []
    for i in range(SEGMENTS):
        kind = "l" if i == 0 else ("r" if i == SEGMENTS - 1 else "m")
        name = f"{EMOJI_PREFIX}{kind}{max(0, min(2, units - 2 * i))}"
        if name not in emojis:
            return None
        out.append(emojis[name])
    return "".join(out)


def eta(d: dict, now: float) -> float | None:
    """Seconds left, from the pace so far (None until there's enough to go on)."""
    frac = float(d.get("frac") or 0)
    spent = now - float(d.get("started", now))
    if frac < 0.04 or spent < 8 or frac >= 1:
        return None
    left = spent * (1 - frac) / frac
    return left if left < 4 * 3600 else None


def clock(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError, OverflowError):
        return True
    return True


def short(jid: str) -> str:
    """The run id shown in Discord: the random part of the job id."""
    return (jid or "").rsplit("-", 1)[-1]


def state_of(d: dict, now: float) -> str:
    if d.get("state") == "running" and d.get("cancel") and d.get("pid") and not alive(d["pid"]):
        return "cancelled"   # stopped by /cancel (the worker was killed before it could say so)
    if d.get("state") == "running" and now - float(d.get("updated", now)) > STALE_S:
        return "stalled"
    if d.get("state") == "running" and d.get("pid") and not alive(d["pid"]):
        return "stalled"   # the worker died (restart, reboot, kill): don't show a frozen bar
    return d.get("state", "running")


def text(d: dict, now: float, render_bar=None) -> str:
    """The Discord message for one job. `render_bar(frac)` draws the bar (the rounded emoji
    bar in Discord); None or a None result = the text bar."""
    state = state_of(d, now)
    took = clock((d.get("updated") if state != "running" else now) - d.get("started", now))
    head = f"{d.get('icon', '🎬')} **{d.get('title', 'Working')}**"
    if state == "done":
        return f"{head}\n✅ Done in {took}"
    if state == "skipped":
        return f"{head}\n⏭ Skipped after {took}: {d.get('detail') or 'not usable'}"[:1900]
    if state == "cancelled":
        why = d.get("detail") if str(d.get("detail", "")).startswith("cancelled") else "cancelled"
        return f"{head}\n🛑 Stopped after {took}: {why}"[:1900]
    if state in ("failed", "stalled"):
        if state == "stalled":
            why = ("the process ended (bot restart or reboot?)" if d.get("pid") and not alive(d["pid"])
                   else "stopped responding")
        else:
            why = d.get("detail") or "failed"
        return f"{head}\n❌ {'Failed' if state == 'failed' else 'Stopped'} after {took}: {why}"[:1900]
    stage = d.get("stage") or "working"
    detail = f" ({d['detail']})" if d.get("detail") else ""
    pct = int(float(d.get("frac", 0)) * 100)
    if d.get("cancel"):
        stage, detail = "cancelling", ""
    run = f" · run `{short(d['id'])}`" if d.get("id") else ""
    left = eta(d, now) if not d.get("cancel") else None
    left_txt = f" · about {clock(left)} left" if left is not None else (" · estimating time left" if not
                                                                       d.get("cancel") else "")
    drawn = (render_bar(d.get("frac", 0)) if render_bar else None) or bar(d.get("frac", 0))
    return f"{head}\n{drawn} **{pct}%**\n{stage}{detail} · ⏱ {took}{left_txt}{run}"[:1900]
