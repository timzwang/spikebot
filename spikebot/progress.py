"""Live progress for long jobs (renders, #inspiration analysis, song downloads), shown in
Discord as one message with a bar that the bot keeps editing.

Writers (render subprocesses, the Lacy worker, the inspiration/music subprocesses) update
a small JSON file in data/progress/<id>.json. The Discord bot polls those files
(`ReviewBot.progress_loop`) and edits one message per job; the message is deleted when
the job finishes (the review card or reply takes its place) and kept, marked failed, if it
fails. Jobs nest: `span(lo, hi)` maps an inner 0..1 onto part of the outer bar, so a
review render inside a Lacy job just fills the Lacy job's last stretch.

Never raises into the caller: progress is cosmetic, a write problem must not stop a render.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import time
import uuid
from pathlib import Path

log = logging.getLogger("progress")
ENV = "SPIKEBOT_PROGRESS"         # a subprocess picks up the job file the bot made for it
STALE_S = 45 * 60                 # no update for this long while running = the process died
_current: contextvars.ContextVar = contextvars.ContextVar("spikebot_progress", default=None)


def folder(data_dir: Path | str) -> Path:
    return Path(data_dir) / "progress"


class Job:
    def __init__(self, path: Path, data: dict):
        self.path, self.data = Path(path), data
        self.spans: list[tuple[float, float]] = [(0.0, 1.0)]
        self._written = 0.0

    # ---- creating -------------------------------------------------------------
    @classmethod
    def create(cls, data_dir: Path | str, title: str, channel: str = "review", channel_id: int | None = None,
               icon: str = "🎬", now: float | None = None) -> "Job":
        now = now or time.time()
        jid = f"{int(now)}-{uuid.uuid4().hex[:6]}"
        prune(data_dir, now)
        job = cls(folder(data_dir) / f"{jid}.json",
                  {"id": jid, "title": title, "icon": icon, "channel": channel, "channel_id": channel_id,
                   "state": "running", "frac": 0.0, "stage": "starting", "detail": "",
                   "started": now, "updated": now})
        job.write(force=True)
        return job

    @classmethod
    def from_env(cls) -> "Job | None":
        p = os.environ.get(ENV)
        if not p:
            return None
        try:
            return cls(Path(p), json.loads(Path(p).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    # ---- updating -------------------------------------------------------------
    def write(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._written < 1.0:
            return
        self._written = now
        self.data["updated"] = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.debug("progress write failed: %s", e)

    def update(self, frac: float | None = None, stage: str | None = None, detail: str | None = None) -> None:
        """`frac` is 0..1 inside the current span."""
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
        """`state` "skipped" = nothing wrong, just not worth a clip (shown briefly)."""
        self.data.update(state=state or ("done" if ok else "failed"),
                         frac=1.0 if ok else self.data.get("frac", 0.0),
                         detail=text[:300] if text else self.data.get("detail", ""))
        self.write(force=True)


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
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return out


def prune(data_dir: Path | str, now: float, keep_s: float = 86400) -> None:
    """Drop job files older than a day (nobody showed them: e.g. the Discord bot is off)."""
    d = folder(data_dir)
    if not d.is_dir():
        return
    for p in d.glob("*.json"):
        try:
            if not p.name.startswith("_") and now - p.stat().st_mtime > keep_s:
                p.unlink()
        except OSError:
            pass


def bar(frac: float, width: int = 18) -> str:
    frac = max(0.0, min(1.0, float(frac or 0)))
    full = int(round(frac * width))
    return "█" * full + "░" * (width - full)


def clock(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def state_of(d: dict, now: float) -> str:
    if d.get("state") == "running" and now - float(d.get("updated", now)) > STALE_S:
        return "stalled"
    return d.get("state", "running")


def text(d: dict, now: float) -> str:
    """The Discord message for one job."""
    state = state_of(d, now)
    took = clock((d.get("updated") if state != "running" else now) - d.get("started", now))
    head = f"{d.get('icon', '🎬')} **{d.get('title', 'Working')}**"
    if state == "done":
        return f"{head}\n✅ Done in {took}"
    if state == "skipped":
        return f"{head}\n⏭ Skipped after {took}: {d.get('detail') or 'not usable'}"[:1900]
    if state in ("failed", "stalled"):
        why = d.get("detail") or ("stopped responding" if state == "stalled" else "failed")
        return f"{head}\n❌ {'Failed' if state == 'failed' else 'Stopped'} after {took}: {why}"[:1900]
    stage = d.get("stage") or "working"
    detail = f" ({d['detail']})" if d.get("detail") else ""
    pct = int(float(d.get("frac", 0)) * 100)
    return f"{head}\n`{bar(d.get('frac', 0))}` **{pct}%** · {stage}{detail} · ⏱ {took}"[:1900]
