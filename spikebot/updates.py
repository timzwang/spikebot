"""#updates: every change to the bot, one short line each (Tim, 20 Nov).

The changelog is spikebot/updates.json (newest last): {id, date (UK, YYYY-MM-DD), title, text}.
Add an entry with every change Tim would notice; the deploy restarts the bot and
`ReviewBot.updates_loop` posts whatever hasn't been posted yet, oldest first, as embeds (up to
10 per message, one colour per day). Posted ids are kept in data/updates_posted.json, so a
restart never posts twice. Pure apart from `load` / the state helpers.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

FILE = Path(__file__).resolve().parent / "updates.json"
PER_MESSAGE = 10               # Discord: embeds per message
DAY_COLOURS = (0x5865F2, 0x57F287, 0xFEE75C, 0xEB459E, 0xED4245, 0x3BA55C, 0xFAA61A)
CHANNEL_NAME = "updates"


def load(path: Path = FILE) -> list[dict]:
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in rows if r.get("id") and r.get("title")]


def pending(rows: list[dict], posted: set[str]) -> list[dict]:
    """Entries not posted yet, oldest first (stable within a day)."""
    return sorted((r for r in rows if r["id"] not in posted), key=lambda r: r.get("date") or "")


def day_label(d: str) -> str:
    """"2026-11-20" -> "Fri 20 Nov"."""
    try:
        return date.fromisoformat(d).strftime("%a %-d %b")
    except ValueError:
        return d


def embed(row: dict) -> dict:
    d = row.get("date") or ""
    try:
        colour = DAY_COLOURS[date.fromisoformat(d).toordinal() % len(DAY_COLOURS)]
    except ValueError:
        colour = DAY_COLOURS[0]
    return {"title": f"🆕 {row['title']}"[:256], "description": (row.get("text") or "")[:400],
            "color": colour, "footer": {"text": day_label(d)}}


def messages(rows: list[dict]) -> list[dict]:
    """Discord payloads: a date heading when the day changes, then up to 10 embeds per message
    (a message never mixes two days)."""
    out: list[dict] = []
    cur_day: str | None = None
    cur: list[dict] = []

    def flush():
        if cur:
            out.append({"content": f"**📅 {day_label(cur_day)}**", "embeds": [embed(r) for r in cur],
                        "ids": [r["id"] for r in cur], "allowed_mentions": {"parse": []}})
    for r in rows:
        if r.get("date") != cur_day or len(cur) >= PER_MESSAGE:
            flush()
            cur_day, cur = r.get("date"), []
        cur.append(r)
    flush()
    return out


class Posted:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "updates_posted.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, st: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        tmp.replace(self.path)
