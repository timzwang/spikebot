"""Discord approval bot (discord.py, bot token DISCORD_BOT_TOKEN).

Runs inside the main bot process (Bot.run) and never takes it down:

  #ready-to-post  each review round: one message with the 3 previews (A/B/C), a card per
                  variant (title, YT/IG/TikTok captions, style), and buttons:
                  Approve A/B/C · Redo · Reject · Feedback · Edit caption.
                  A thread per clip holds re-renders, errors and the live links.
  #tiktok-queue   the approved file + captions for manual posting (TikTok, and any
                  platform not automated for that mode).
  #inspiration    links or uploads -> style analysis -> preset (spikebot/inspiration.py).
  /style          preferred settings (learned + pinned), pin or unpin them.
  /rules          every instruction from /feedback, Feedback and /style as one numbered
                  list by category; turn entries off/on (menus, buttons, or disable:3,5-7).
  /preset         apply | list | show | rename | edit | delete | combine.
  /help           every command and button by category (category:, search:).
  /cancel         stop a running job by its run id (renders, trend edits, Lacy clips,
                  #inspiration analysis, sound downloads, queued re-renders); every
                  progress bar also has a Cancel button.

Only approvers (review.approver_ids, or server managers if empty) can press buttons or
use the commands. Layouts are built as plain dicts by pure functions (tested); the
discord.py glue is kept thin.
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import ai
from . import feedback as fb
from . import help as help_
from . import presets as presets_
from . import progress, review
from . import style as st

log = logging.getLogger("discord_bot")

MODE_LABEL = {"lacy": ("Lacy", 0x9146FF), "tokyo_girl": ("Tokyo Girl", 0xFF4FA3)}
TREND_LABEL = ("Trending sound", 0x1DB954)
COMPARE_LABEL = ("GTA 5 vs GTA 6", 0x5BC236)


def mode_label(item: dict) -> tuple[str, int]:
    """Card label: trailer edits cut to a trending sound share Tokyo Girl's renderer and style
    but are not Tokyo Girl edits."""
    if (item.get("source") or {}).get("kind") == "trend":
        return TREND_LABEL
    if (item.get("source") or {}).get("kind") == "compare":
        return COMPARE_LABEL
    src = item.get("source") or {}
    if item["mode"] == "lacy" and src.get("streamer") and src["streamer"] != "Lacy":   # Adin Ross, Seal...
        return src["streamer"], (0x53FC18 if src.get("platform") == "kick" else 0x9146FF)
    return MODE_LABEL.get(item["mode"], (item["mode"], 0x5865F2))
VARIANT_DOT = {"A": "🟢", "B": "🔵", "C": "🟠", "P": "🟣"}   # which column is which, at a glance
SEP = "|"


# --------------------------------------------------------------------------
# Pure layout helpers (tested without Discord)
# --------------------------------------------------------------------------
def cid(action: str, item_id: str, n: int = 0, label: str = "") -> str:
    return SEP.join(["rv", action, item_id, str(n), label])


def parse_cid(custom_id: str) -> dict | None:
    parts = (custom_id or "").split(SEP)
    if len(parts) != 5 or parts[0] != "rv":
        return None
    return {"action": parts[1], "item": parts[2], "round": int(parts[3] or 0), "label": parts[4]}


def code(text: str, limit: int = 380) -> str:
    t = (text or "").replace("```", "'''")
    if len(t) > limit:
        t = t[:limit - 1] + "…"
    return f"```{t}```"


def ts(t: float, style: str = "f") -> str:
    return f"<t:{int(t)}:{style}>"


def vod_time(link: str | None) -> str | None:
    """"1:02:03" from a VOD link's ?t= (Twitch 1h02m03s / Kick seconds)."""
    m = re.search(r"[?&]t=(?:(\d+)h)?(?:(\d+)m)?(\d+)s?\b", link or "")
    if not m:
        return None
    sec = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
    h, rest = divmod(sec, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def song_line(v: dict) -> str | None:
    """"🎵 Tokyo Girl\n▶ 0:42 · [sound](link)" (where to start the sound) for a silent edit."""
    m = v.get("music") or {}
    if not m.get("song"):
        return None
    start = float(m.get("start_s") or 0)
    link = re.search(r"<(https?://[^>]+)>", v.get("music_note") or "")
    return (f"🎵 {m['song']}\n▶ **{int(start // 60)}:{int(start % 60):02d}**"
            + (f" · [sound]({link.group(1)})" if link else ""))


def variant_cell(item: dict, v: dict) -> str:
    """One column of the card: length, the sound (start time) or the clip title."""
    lines = [f"**{v.get('duration', '?')}s**" + (" · ✏️" if v.get("edited") else "")
             + ((" · 🎙️ teaser" if v["teaser"].get("voice") else " · 🎞️ teaser") if v.get("teaser") else "")]
    song = song_line(v)
    if song:
        lines.append(song)
    elif v.get("silent"):
        lines.append("🎵 add the sound in the app")
    if v.get("intro_note"):     # e.g. no Rockstar Games Presents shot yet: the logo was used
        lines.append("⚠️ " + v["intro_note"][:160])
    if v.get("glitch_note"):    # the glitch check fixed (or couldn't fix) a stray cut
        lines.append("🛠️ " + v["glitch_note"][:160])
    if item["mode"] == "lacy" and v.get("title"):
        lines.append(f"“{v['title'][:90]}”")
    return "\n".join(lines)[:1000]


def round_payload(item: dict, rnd: dict, extra: str = "", entry_on: dict | None = None) -> dict:
    """Message for one review round, kept short so several can be reviewed at once: one header
    line (who / category / VOD), then ONE embed with A | B | C side by side as columns (length,
    sound + start, or the clip title), and the buttons. Nothing about styles or settings."""
    label, colour = mode_label(item)
    src = item.get("source") or {}
    kind = {"redo": " · redo", "feedback": " · after feedback", "preset": " · preset"}.get(rnd["kind"], "")
    bits = [f"**{label}**" + (f" · {src['category'].replace('Grand Theft Auto ', 'GTA ')}"
                              if src.get("category") else "")]
    if src.get("vod_link"):
        t = vod_time(src["vod_link"])
        bits.append(f"[VOD{' at ' + t if t else ''}](<{src['vod_link']}>)")
    head = [" · ".join(bits) + kind + f" · `{item['id']}`"]
    if rnd.get("comment") and rnd["kind"] != "initial":
        head.append(rnd["comment"][:300])
    elif rnd.get("comment") and "🔞" in rnd["comment"]:
        head.append(rnd["comment"][rnd["comment"].index("🔞"):][:300])
    if any(v.get("safety") == "unchecked" for v in rnd["variants"]):
        head.append("⚠️ Nudity check couldn't run: look before approving (it re-checks before posting).")
    if extra:
        head.append(extra)
    fields = [{"name": f"{VARIANT_DOT.get(v['label'][0], '⚪')} {v['label']}" + (" 🧪" if v.get("experimental") else ""),
               "value": variant_cell(item, v), "inline": True} for v in rnd["variants"][:24]]
    embeds = [{"color": colour, "fields": fields}] if fields else []
    return {"content": "\n".join(head)[:1900], "embeds": embeds,
            "rows": buttons(item, rnd),
            "allowed_mentions": {"parse": []}}


def buttons(item: dict, rnd: dict, disabled: bool = False) -> list[list[dict]]:
    n = rnd["n"]
    labels = [v["label"] for v in rnd["variants"]]
    row1 = [{"label": f"Approve {lab}", "style": "success", "custom_id": cid("approve", item["id"], n, lab),
             "disabled": disabled} for lab in labels]
    row2 = [{"label": "Redo (3 new)", "style": "secondary", "custom_id": cid("redo", item["id"], n), "disabled": disabled},
            {"label": "Reject", "style": "danger", "custom_id": cid("reject", item["id"], n), "disabled": disabled},
            {"label": "Feedback", "style": "primary", "custom_id": cid("feedback", item["id"], n), "disabled": disabled},
            {"label": "Edit caption", "style": "secondary", "custom_id": cid("edit", item["id"], n), "disabled": disabled}]
    return [row1, row2]


# ---- the rules list (/rules) ------------------------------------------------------
def entry_button(n: int, on: bool, item_id: str = "-", rnd: int = 0) -> dict:
    return {"label": f"Turn off #{n}" if on else f"Turn on #{n}", "style": "secondary" if on else "success",
            "custom_id": cid("rule", item_id, rnd, str(n)), "disabled": False}


def entry_buttons(numbers: list[int], on: dict, item_id: str = "-", rnd: int = 0) -> list[list[dict]]:
    """On/off buttons for the entries a piece of feedback added (5 per row, max 3 rows)."""
    bs = [entry_button(n, on.get(n, True), item_id, rnd) for n in numbers[:15]]
    return [bs[i:i + 5] for i in range(0, len(bs), 5)]


def flip_rows(rows: list[list[dict]], n: int, on: bool) -> list[list[dict]]:
    """The same buttons with entry n's button showing its new state."""
    out = []
    for row in rows:
        new = []
        for b in row:
            d = parse_cid(b.get("custom_id", "")) if b.get("custom_id") else None
            if d and d["action"] == "rule" and d["label"] == str(n):
                b = entry_button(n, on, d["item"], d["round"])
            new.append(b)
        out.append(new)
    return out


def replaced_by(entries: list[dict]) -> dict[int, int]:
    """Enabled setting entries that a newer enabled one fully overrides: n -> newer n."""
    out: dict[int, int] = {}
    live = [e for e in entries if e.get("on", True) and e["kind"] == "setting"]
    for i, e in enumerate(live):
        for later in live[i + 1:]:
            if later["setting"] == e["setting"] and set(st.entry_modes(e)) <= set(st.entry_modes(later)):
                out[e["n"]] = later["n"]
    return out


def rules_text(entries: list[dict]) -> str:
    """The whole list as plain text (the fallback attachment)."""
    lines = []
    for key, name, _ in st.CATEGORIES:
        es = [e for e in entries if st.category_of(e) == key]
        if es:
            lines.append(name)
            for g in rule_groups(es):
                title, what = rule_title(g["e"])
                nums = ", ".join(f"#{n}" for n in g["ns"])
                lines.append(f"  {nums} {'ON ' if g['on'] else 'OFF'} {title}" + (f"  ({what})" if what else ""))
            lines.append("")
    return "\n".join(lines) or "No rules yet."


# Discord "Components V2" message: every rule is a section (text + its own button).
MAX_COMPONENTS = 40     # per message, nested ones included
MAX_TEXT = 3800         # Discord allows 4000 characters of text per message


SCOPE_LABEL = {"lacy": "Stream clips (Lacy, Adin Ross, Seal)", "tokyo_girl": "Tokyo Girl edits", "trend": "Trend edits"}


def rule_groups(entries: list[dict]) -> list[dict]:
    """Entries that say the same thing (style.rule_key) merged into one line, in the order the
    first of them was saved: {"e": newest entry, "ns": all numbers, "on": any of them on}."""
    groups: dict[tuple, dict] = {}
    for e in entries:
        g = groups.setdefault(st.rule_key(e), {"e": e, "ns": [], "on": False})
        g["ns"].append(e["n"])
        if e["n"] >= g["e"]["n"]:
            g["e"] = e
        g["on"] = g["on"] or e.get("on", True)
    return list(groups.values())


def rule_title(e: dict) -> tuple[str, str]:
    """(headline, explanation) of one rule in plain English."""
    if e.get("kind") == "setting":
        title, what = st.describe_setting(e.get("setting"), e.get("value"))
        return title, what
    return e.get("text", ""), ""


def rule_search_text(e: dict) -> str:
    title, what = rule_title(e)
    return " ".join([title, what, st.entry_text(e), st.CATEGORY_NAME.get(st.category_of(e), "")]).lower()


def rule_line(e: dict, over: dict[int, int], ns: list[int] | None = None, on: bool | None = None) -> str:
    """One rule: its number(s) and plain name, and underneath what it does."""
    ns = ns or [e["n"]]
    on = e.get("on", True) if on is None else on
    nums = " · ".join(f"#{n}" for n in ns)
    title, what = rule_title(e)
    sub = f"\n-# {what}" if what else ""
    if len(ns) > 1:
        sub += f"\n-# saved {len(ns)} times, shown once"
    if not on:
        why = f"_{e['upgraded']}_" if e.get("upgraded") else "_off_"
        return f"~~**{nums}** {title}~~ · {why}{sub}"[:500]
    tag = f" · _replaced by #{over[e['n']]}_" if e["n"] in over else ""
    if e.get("status") == "needs_code":
        tag += " · _needs new editor code" + (f" (issue #{e['issue']})" if e.get("issue") else "") + \
            (f", {e['partly']}" if e.get("partly") else "") + "_"
    elif e.get("status") == "not_editor":
        tag += " · _not about the edit itself_"
    elif e.get("kind") in ("note", "unsupported") and not e.get("checked"):
        tag += " · _being turned into settings…_"
    return f"**{nums}** {title}{tag}{sub}"[:500]


PER_RULE = 5   # components per rule: section + text + button, row + "applies to" menu


def rule_block(g: dict, over: dict[int, int], where: str, page: int) -> dict:
    """One rule line (merged duplicates included): a Turn off / Turn on button beside it and an
    "Applies to" menu (ticks for Lacy / Tokyo Girl / Trend edits; only the kinds where the
    setting exists). `where` = "-" for the /rules pages, "s:<words>" for a search, else the
    dot-joined numbers of a feedback reply."""
    if "ns" not in g:                       # a bare entry
        g = {"e": g, "ns": [g["n"]], "on": g.get("on", True)}
    e, on = g["e"], g["on"]
    label = ".".join(str(n) for n in g["ns"])
    current = st.entry_modes(e)
    opts = st.valid_modes(e.get("setting"), e.get("value"))
    return {"type": "rule", "n": e["n"], "ns": g["ns"], "content": rule_line(e, over, g["ns"], on),
            "button": {"label": "Turn off" if on else "Turn on", "style": "secondary" if on else "success",
                       "custom_id": cid("rulep", where, page, label), "disabled": False},
            "scope": {"type": "select", "custom_id": cid("rulem", where, page, label),
                      "placeholder": "Applies to…", "min": 1, "max": len(opts),
                      "options": [{"label": f"Applies to {SCOPE_LABEL[m]}", "value": m, "default": m in current}
                                  for m in opts]}}


def search_where(query: str) -> str:
    """The `where` of a search page (kept in the buttons' ids, so it survives clicks)."""
    q = re.sub(r"\s+", " ", (query or "").replace(SEP, " ")).strip().lower()[:40]
    return f"s:{q}" if q else "-"


def find_rules(groups: list[dict], query: str) -> list[dict]:
    words = (query or "").lower().split()
    if not words:
        return groups
    return [g for g in groups if all(w in rule_search_text(g["e"]) or w.lstrip("#") in
                                     [str(n) for n in g["ns"]] for w in words)]


def rules_pages(entries: list[dict], query: str = "") -> list[list[tuple[str, dict]]]:
    """Merge duplicates, filter by `query`, split the list (category order) into pages that
    fit one message. Each page is [(category key, group)]."""
    over = replaced_by(entries)
    groups = find_rules(rule_groups(entries), query)
    flat = [(key, g) for key, _, _ in st.CATEGORIES for g in groups if st.category_of(g["e"]) == key]
    pages: list[list[tuple[str, dict]]] = []
    page: list[tuple[str, dict]] = []
    comps, chars, last = 6, 400, None      # header + nav row (4) reserved; header text
    for key, g in flat:
        line = len(rule_line(g["e"], over, g["ns"], g["on"]))
        need_c = PER_RULE + (1 if key != last else 0)
        need_t = line + (len(st.CATEGORY_NAME[key]) + 8 if key != last else 0)
        if page and (comps + need_c > MAX_COMPONENTS or chars + need_t > MAX_TEXT):
            pages.append(page)
            page, comps, chars, last = [], 6, 400, None
            need_c = PER_RULE + 1
            need_t = line + len(st.CATEGORY_NAME[key]) + 8
        page.append((key, g))
        comps, chars, last = comps + need_c, chars + need_t, key
    if page:
        pages.append(page)
    return pages


def rules_page(entries: list[dict], page: int = 0, notice: str = "", query: str = "") -> dict:
    """One page of /rules as layout blocks: header, category headings, one block per rule
    (duplicates merged) with its Turn off / Turn on button, and Prev / Next. `query` = search."""
    groups = rule_groups(entries)
    on = sum(1 for g in groups if g["on"])
    where = search_where(query)
    query = where[2:] if where != "-" else ""
    pages = rules_pages(entries, query)
    page = max(0, min(page, len(pages) - 1)) if pages else 0
    head = (f"## Your rules\n{on} on · {len(groups) - on} off · for each setting the newest rule that's on"
            " wins; turning one off brings back the one before it. Find one with `/rules search:`.")
    if query:
        n_found = sum(len(p) for p in pages)
        head += f"\n🔎 **{n_found}** matching “{query}” · `/rules` shows them all."
    if notice:
        head += f"\n{notice}"
    blocks: list[dict] = [{"type": "text", "content": head[:400]}]
    if not entries:
        blocks[0]["content"] += "\nNo rules yet: use `/feedback` or the Feedback button on a clip."
        return {"page": 0, "pages": 1, "blocks": blocks}
    if not pages:
        blocks[0]["content"] += "\nNothing matches. Try another word (e.g. `cut`, `text`, `colour`, `logo`)."
        return {"page": 0, "pages": 1, "blocks": blocks}
    over = replaced_by(entries)
    last = None
    for key, g in pages[page]:
        if key != last:
            blocks.append({"type": "text", "content": f"### {st.CATEGORY_NAME[key]}"})
            last = key
        blocks.append(rule_block(g, over, where, page))
    if len(pages) > 1:
        blocks.append({"type": "row", "buttons": [
            {"label": "◀ Prev", "style": "primary", "custom_id": cid("rulesp", where, page - 1),
             "disabled": page == 0},
            {"label": f"Page {page + 1}/{len(pages)}", "style": "secondary",
             "custom_id": cid("rulesp", where, page, "here"), "disabled": True},
            {"label": "Next ▶", "style": "primary", "custom_id": cid("rulesp", where, page + 1),
             "disabled": page >= len(pages) - 1}]})
    return {"page": page, "pages": len(pages), "blocks": blocks}


def rules_subset(entries: list[dict], numbers: list[int], head: str) -> dict:
    """The rules one piece of feedback just saved (the /feedback reply, or under a
    re-render), each with its button and "Applies to" menu, so the scope is explicit."""
    keep = [e for e in entries if e["n"] in set(numbers)][:6]     # 6 x 5 components + text
    where = ".".join(str(e["n"]) for e in keep) or "-"
    over = replaced_by(entries)
    blocks = [{"type": "text", "content": head[:1500]}] + [rule_block(e, over, where, 0) for e in keep]
    if len(numbers) > len(keep):
        blocks.append({"type": "text", "content": f"_…and {len(numbers) - len(keep)} more: see `/rules`._"})
    return {"page": 0, "pages": 1, "blocks": blocks}


def picker(action: str, item: dict, rnd: dict) -> list[list[dict]]:
    """'Which variant?' buttons for Feedback / Edit caption."""
    return [[{"label": f"{'Feedback on' if action == 'fbpick' else 'Edit'} {v['label']}", "style": "primary",
              "custom_id": cid(action, item["id"], rnd["n"], v["label"]), "disabled": False}
             for v in rnd["variants"]]]


def cancel_buttons(jid: str) -> list[list[dict]]:
    """The Cancel button under a running job's progress bar."""
    return [[{"label": "Cancel", "style": "danger", "custom_id": cid("cancel", jid), "disabled": False}]]


QUEUED = "q-"   # run id of a queued (not started) re-render: q-<item id>


def run_rows(running: list[dict], queued: list[dict], now: float) -> list[tuple[str, str]]:
    """(run id, one-line description) for /cancel: running jobs, then queued re-renders."""
    rows = []
    for d in running:
        pct = int(float(d.get("frac", 0)) * 100)
        took = progress.clock(now - d.get("started", now))
        rows.append((progress.short(d.get("id", "")),
                     f"{d.get('icon', '🎬')} {d.get('title', 'Working')} · {pct}% · {took}"
                     + (" · cancelling" if d.get("cancel") else "")))
    for it in queued:
        kind = (it.get("request") or {}).get("kind", "re-render")
        rows.append((QUEUED + it["id"], f"⏳ {it.get('title') or it['id']} · {kind} (queued)"))
    return rows


def manual_payload(item: dict, v: dict, platforms: list[str], link: str | None = None,
                   path_note: str | None = None) -> dict:
    label, colour = mode_label(item)
    names = [review.NAMES[p] for p in platforms]
    lines = [f"📱 **{label}** `{item['id']}` approved ({v['label']}) · post on **{', '.join(names)}**"]
    if v.get("silent"):
        lines.append(v["music_note"] if v.get("music_note")
                     else "Silent edit: add the official Tokyo Girl sound in the app.")
    caps = v.get("captions") or {}
    blocks = []
    if "tiktok" in platforms:
        blocks.append(("TikTok caption", caps.get("tiktok", "")))
    if "instagram" in platforms:
        blocks.append(("Instagram caption", caps.get("instagram", "")))
    if "youtube" in platforms:
        blocks.append(("YouTube title", v.get("title", "")))
    job = (item.get("source") or {}).get("job_id")
    if item["mode"] == "lacy" and job:
        flags = " ".join(f"--{p} URL" for p in platforms if p in ("tiktok", "instagram"))
        blocks.append(("Then log the links (Content Rewards)",
                       f"cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy posted {job} {flags}"))
    content = "\n".join(lines)
    for k, text in blocks:
        content += f"\n{k}:\n```{text.replace('```', '')}```"
    out = {"content": content[:1990], "allowed_mentions": {"parse": []}}
    if link:
        out["embeds"] = [{"title": "Download the file", "url": link, "color": colour,
                          "description": "Too big to attach; link works for 7 days."}]
    elif path_note:
        out["embeds"] = [{"title": "Too big for Discord", "color": colour,
                          "description": f"Copy it from the server:\n`{path_note}`"}]
    return out


# The line under each channel's name (Discord's channel topic): how to use it and what lands
# there. Set by `ReviewBot.ensure_topics` on start, only when it differs. Keys are found by
# config id (`rcfg` field) or, for the webhook channels, by name.
CHANNEL_TOPICS = {
    "channel_id": "Clips waiting for you, 3 versions (A/B/C) each. Approve publishes; Redo, Feedback or Reject"
                  " otherwise. Each clip's thread takes /preset apply. Nothing posts without Approve.",
    "tiktok_channel_id": "Approved videos to post by hand on TikTok/Instagram: download, copy the caption, post."
                         " Lacy clips: log the links with the command shown.",
    "inspiration_channel_id": "Post a Short link or upload one to copy its style (optional copy: / name: /"
                              " type: edit, clip or other). TikTok sound link, audio file or sound: + link = a new"
                              " trend sound. Videos are deleted after analysis.",
    "moments": "Chat spikes from watched streams: streamer, score and a VOD link at the moment. Read-only.",
    "status": "Bot health: restarts, streamers live/offline, chat down/back, Kick key errors, low disk. Read-only.",
    "daily-summary": "22:00 UK daily: moments per streamer, top 3 of the last 24 h, server CPU. Read-only.",
    "announcements": "New rules, what the rule compiler built, videos going live, blocked videos, deploy notes."
                     " Read-only.",
}
BY_NAME = ("moments", "status", "daily-summary", "announcements")


def topic_plan(channels, rcfg: dict) -> list[tuple[object, str]]:
    """(channel, topic) for every channel whose topic should change. Config ids win; the
    webhook channels are matched by name (emoji/prefixes allowed, e.g. "📈-daily-summary"). Pure."""
    ids = {rcfg.get(k): k for k in CHANNEL_TOPICS if k.endswith("channel_id") and rcfg.get(k)}
    out, done = [], set()
    for ch in channels:
        name = (getattr(ch, "name", "") or "").lower()
        key = ids.get(getattr(ch, "id", None))
        if key is None:
            core = re.sub(r"[^a-z0-9-]+", "", name).strip("-")
            core = "announcements" if core.endswith("announcement") else core
            key = next((k for k in BY_NAME if core == k or core.endswith("-" + k)), None)
        if key is None or key in done:
            continue
        done.add(key)
        if (getattr(ch, "topic", None) or "") != CHANNEL_TOPICS[key]:
            out.append((ch, CHANNEL_TOPICS[key]))
    return out


def style_overview(profile: st.Profile, presets: dict[str, dict]) -> str:
    """/style text: what variant A uses now, per mode, with scores and pins."""
    lines = ["**Preferred settings** (variant A uses these; 📌 = pinned with /style)"]
    for mode in ("lacy", "tokyo_girl"):
        pref = profile.preferred(mode)
        pins = profile.pinned(mode)
        bits = []
        for dim, val in pref.items():
            key = st.stat_key(dim, mode)
            pinned = pins.get(dim) == val
            row = profile.data["stats"].get(key, {}).get(val, {})
            bits.append(f"{dim} **{val}**{' 📌' if pinned else ''} ({row.get('approved', 0):g}/{row.get('shown', 0):g})")
        lines.append(f"__{MODE_LABEL.get(mode, (mode,))[0]}__: " + " · ".join(bits))
    intents = profile.data["feedback"]["intents"]
    if intents:
        top = sorted(intents.items(), key=lambda kv: kv[1], reverse=True)[:6]
        lines.append("**Recurring feedback**: " + ", ".join(f"{k} ×{v}" for k, v in top))
    if presets:
        ranked = profile.top_presets(list(presets), n=5)
        rows = profile.data["presets"]
        lines.append("**Presets by approvals**: " + ", ".join(
            f"{n} ({rows.get(n, {}).get('approved', 0)}/{rows.get(n, {}).get('shown', 0)})" for n in ranked))
    decided = [d for d in profile.data["decisions"] if d["action"] in ("approve", "reject")]
    if decided:
        appr = sum(1 for d in decided if d["action"] == "approve")
        lines.append(f"Decisions logged: {len(profile.data['decisions'])} ({appr} approved, {len(decided) - appr} rejected)")
    lines.append("Pin: `/style setting:<name> value:<option>` · unpin: `value:auto`.")
    return "\n".join(lines)[:1990]


STYLE_SETTINGS = ["grade", "pace", "font", "size", "position", "colour", "transition", "ending", "opening",
                  "text_level", "intro", "motion", "framing", "media", "hook:lacy", "hook:tokyo_girl",
                  "hook:trend"]


def style_options(setting: str) -> list[str]:
    if setting.startswith("hook:"):
        return list(st.HOOKS.get(setting[5:], ()))
    both = list(st.dims("lacy").get(setting, ())) + list(st.dims("tokyo_girl").get(setting, ()))
    return list(dict.fromkeys(both))   # e.g. transition: Tokyo Girl also has fade


def progress_env(job) -> dict | None:
    """Environment for a subprocess that should update `job`'s bar (None = inherit)."""
    if job is None:
        return None
    import os
    return dict(os.environ, **{progress.ENV: str(job.path)})


# GTA 6 unlocks at midnight local time: 00:00 GMT Thu 19 Nov 2026 in the UK (US 05:00 UK).
# review.stats.gta6_launch overrides it (ISO time with offset).
GTA6_LAUNCH = "2026-11-19T00:00:00+00:00"
STAT_KEYS = ("live", "moments", "review", "approved", "views", "likes", "followers", "uptime", "gta6")
RENAME_WINDOW_S = 610.0   # Discord: 2 renames per channel per 10 min (+10 s margin)
RENAME_GAP_S = 305.0      # so normally one rename per channel every ~5 min


def uptime_text(seconds: float) -> str:
    s = int(max(0, seconds))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m:02d}m"


def compact(n) -> str:
    """1234 -> 1.2K, 1234567 -> 1.23M (YouTube-style)."""
    n = int(n or 0)
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = n / div
            txt = f"{v:.2f}" if v < 10 else f"{v:.1f}" if v < 100 else f"{v:.0f}"
            return txt.rstrip("0").rstrip(".") + suf
    return str(n)


def launch_text(now: float, launch: str = GTA6_LAUNCH) -> str:
    """Countdown to the UK unlock to the minute, then time since launch."""
    at = datetime.fromisoformat(launch)
    left = at.timestamp() - now
    uk = at.astimezone(review.UK)
    when = f"{uk:%a} {uk.day} {uk:%b %H:%M}"
    if left > 86400:
        d, h = int(left // 86400), int(left % 86400 // 3600)
        return f"🎮 GTA 6 in {d}d {h}h ({when} UK)"
    if left > 0:
        h, m = int(left // 3600), int(left % 3600 // 60)
        return f"🎮 GTA 6 in {h}h {m:02d}m ({when} UK)"
    since = -left
    if since < 86400:
        h, m = int(since // 3600), int(since % 3600 // 60)
        return f"🎮 GTA 6 out now: {h}h {m:02d}m in"
    return f"🎮 GTA 6 out: day {int(since // 86400) + 1}"


def stat_names(snap: dict, now: float, launch: str = GTA6_LAUNCH) -> dict[str, str]:
    """Channel names for the Stats category. Only what Discord can show in a name
    (max 100 chars); keys missing from `snap` are left out."""
    out = {}
    if "live" in snap:
        out["live"] = f"🟢 Live streamers: {snap['live']}/{snap.get('watching', '?')}"
        if snap.get("unchecked"):
            out["live"] += f" ({snap['unchecked']} unchecked)"
    if "moments_today" in snap:
        out["moments"] = f"⚡ Moments today: {snap['moments_today']}"
    if "to_review" in snap:
        out["review"] = f"🎬 Waiting for review: {snap['to_review']}"
    if "approved_today" in snap:
        out["approved"] = f"✅ Approved today: {snap['approved_today']}"
    social = snap.get("social") or {}
    for key, icon, label in (("views", "👁", "Views"), ("likes", "❤️", "Likes"),
                             ("followers", "👥", "Followers")):
        row = social.get(key)
        if row and row.get("platforms"):
            out[key] = f"{icon} {label}: {compact(row['total'])} ({'+'.join(row['platforms'])})"
    if snap.get("started"):
        out["uptime"] = f"⏱ Bot uptime: {uptime_text(now - snap['started'])}"
    out["gta6"] = launch_text(now, launch)
    return {k: v[:100] for k, v in out.items()}


def renames_due(names: dict[str, str], current: dict[str, str], history: dict[str, list],
                now: float, burst: bool = False) -> list[str]:
    """Stat keys whose channel name is out of date and may be renamed now. Discord allows
    2 renames per channel per 10 min: never more than that in any 10 min (`history` = recent
    rename times per key), and normally spaced ~5 min apart; `burst` (/stats) only applies
    the 2-per-10-min rule."""
    out = []
    for k in STAT_KEYS:
        if k not in names or k not in current or current[k] == names[k]:
            continue
        recent = [t for t in history.get(k) or [] if now - t < RENAME_WINDOW_S]
        if len(recent) >= 2 or (not burst and recent and now - max(recent) < RENAME_GAP_S):
            continue
        out.append(k)
    return out


def note_rename(history: dict[str, list], key: str, now: float) -> None:
    history[key] = [t for t in history.get(key) or [] if now - t < RENAME_WINDOW_S][-1:] + [now]


def _no_mentions():
    try:
        import discord
        return discord.AllowedMentions.none()
    except ImportError:
        return None


def trend_slots_due(times: list[str], done: dict, now: float) -> tuple[str | None, list[str]]:
    """(slot to run now, slots to mark skipped). Only the latest passed slot of today runs,
    so a restart late in the day doesn't pile up every missed edit at once."""
    dt = datetime.fromtimestamp(now, review.UK)
    day = dt.strftime("%Y-%m-%d")
    already = set(done.get(day, []))
    passed = sorted(t for t in times if t <= dt.strftime("%H:%M") and t not in already)
    if not passed:
        return None, []
    return passed[-1], passed[:-1]


DISCORD_EPOCH_MS = 1420070400000


def snowflake_ts(sid) -> float:
    """Unix time a Discord id was made (a thread's id = when it was created)."""
    return ((int(sid) >> 22) + DISCORD_EPOCH_MS) / 1000


def stale_threads(threads, bot_id: int, now: float, hours: float) -> list:
    """The bot's own open threads with no message for `hours` (created time if empty)."""
    out = []
    for th in threads:
        if getattr(th, "archived", False) or getattr(th, "owner_id", None) != bot_id:
            continue
        last = snowflake_ts(th.id)
        if getattr(th, "last_message_id", None):
            last = max(last, snowflake_ts(th.last_message_id))
        if now - last >= hours * 3600:
            out.append(th)
    return out


# --------------------------------------------------------------------------
# The bot
# --------------------------------------------------------------------------
class ReviewBot:
    def __init__(self, cfg: dict, config_path: str | Path, notifier=None):
        self.cfg = cfg
        self.config_path = str(config_path)
        self.base = Path(config_path).resolve().parent if Path(config_path).suffix else Path(config_path)
        self.rcfg = review.settings(cfg)
        dd = Path(cfg.get("data_dir", "data"))
        self.data_dir = dd if dd.is_absolute() else self.base / dd
        self.store = review.Store(self.data_dir)
        self.notifier = notifier
        self.client = None
        self.render_q: asyncio.Queue | None = None
        self.queued: set[str] = set()
        self.wake = asyncio.Event()
        self.publish_lock = asyncio.Lock()   # one publish pass at a time: never double-upload
        self.profile_lock = threading.Lock()   # /feedback writes the style profile from a thread
        self.pending_uploads: dict[int, dict] = {}   # inspiration thread id -> name/copy
        self.started = False
        self._publisher = None
        self._archive_checked = 0.0
        self._trend_running = False
        self.rules_wake = asyncio.Event()
        self._progress_lock = asyncio.Lock()
        self.bar_emojis: dict[str, str] = {}   # rounded progress bar pieces (the bot's own emojis)
        self.force_stopped: set[int] = set()   # pids /cancel had to kill (their parent shouldn't call it a crash)
        self.progress_msgs: dict[str, dict] = {}   # job id -> {msg, text, ended}
        self.stats_source = None   # main.Bot sets this: -> {live, watching, moments_today, started}
        self.started_at = time.time()
        self._stats_warned = False
        self.social: dict = {}     # socialstats.refresh() -> views/likes/followers totals

    # ---- helpers ------------------------------------------------------------
    @property
    def publisher(self):
        if self._publisher is None:
            self._publisher = review.Publisher(self.cfg, self.base, self.store)
        return self._publisher

    def allowed(self, user) -> bool:
        ids = self.rcfg["approver_ids"]
        if ids:
            return user.id in ids
        perms = getattr(user, "guild_permissions", None)
        return bool(perms and (perms.manage_guild or perms.administrator))

    async def channel(self, channel_id: int):
        if not channel_id:
            return None
        ch = self.client.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.client.fetch_channel(channel_id)
            except Exception as e:  # noqa: BLE001
                log.warning("channel %s not reachable: %s", channel_id, e)
                return None
        return ch

    async def announcements(self):
        """#announcements: the configured id, else the server's text channel with "announcement"
        in its name. None if there isn't one (callers fall back to #status)."""
        cid_ = self.rcfg.get("announcements_channel_id")
        if cid_:
            return await self.channel(cid_)
        if getattr(self, "_announce_id", None):
            return await self.channel(self._announce_id)
        guilds = ([self.client.get_guild(self.rcfg["guild_id"])] if self.rcfg.get("guild_id")
                  else list(getattr(self.client, "guilds", []) or []))
        for g in guilds:
            for ch in getattr(g, "text_channels", []) or []:
                if "announcement" in (ch.name or "").lower():
                    self._announce_id = ch.id
                    return ch
        return None

    async def ensure_topics(self) -> int:
        """Write the how-to line under each channel name (CHANNEL_TOPICS). Needs Manage Channels;
        never raises. Returns how many were changed."""
        guilds = ([self.client.get_guild(self.rcfg["guild_id"])] if self.rcfg.get("guild_id")
                  else list(getattr(self.client, "guilds", []) or []))
        n = 0
        for g in [g for g in guilds if g is not None]:
            for ch, topic in topic_plan(getattr(g, "text_channels", []) or [], self.rcfg):
                try:
                    await ch.edit(topic=topic)
                    n += 1
                except Exception as e:  # noqa: BLE001 - e.g. no Manage Channels permission
                    log.warning("couldn't set the description of #%s: %s", getattr(ch, "name", "?"), e)
        return n

    async def announce(self, key: str, text: str) -> None:
        """Key news for Tim (new rules, what the rule compiler built, videos going live, blocked
        videos, what a deploy changed) goes to #announcements; #status keeps bot health.
        Never raises."""
        import discord
        try:
            ch = await self.announcements()
            if ch is not None:
                await ch.send(text[:1990], allowed_mentions=discord.AllowedMentions.none())
                return
        except Exception as e:  # noqa: BLE001
            log.warning("#announcements post failed: %s", e)
        if self.notifier:
            await self.notifier.send(key, text[:1900], kind="start")

    async def announce_new_rules(self, seen: int) -> int:
        """Post rules added since entry `seen` (/feedback, or Feedback said "always") to
        #announcements. Returns the newest entry number."""
        new = [e for e in self.store.profile().entries() if e["n"] > seen]
        if new:
            lines = [f"**#{e['n']}** {st.entry_text(e, where=False)} → {st.modes_label(st.entry_modes(e))}"
                     for e in new[:15]]
            await self.announce(f"newrules:{new[-1]['n']}", "📝 **New rule" + ("s" if len(new) > 1 else "")
                                + "** (turn off with /rules):\n" + "\n".join(lines))
            seen = max(e["n"] for e in new)
        return seen

    async def say(self, item: dict, text: str, file=None) -> None:
        """Message in the clip's thread (or #ready-to-post if it has none)."""
        ch = await self.channel(item.get("thread_id") or self.rcfg["channel_id"])
        if ch is None:
            log.info("review %s: %s", item["id"], text)
            return
        import discord
        kw = {"allowed_mentions": discord.AllowedMentions.none()}
        if file is not None:
            kw["file"] = file
        try:
            await ch.send(text[:1990], **kw)
        except Exception as e:  # noqa: BLE001
            log.warning("review %s: thread message failed: %s", item["id"], e)

    def view(self, rows: list[list[dict]]):
        import discord
        v = discord.ui.View(timeout=None)
        styles = {"success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger,
                  "primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary}
        for r, row in enumerate(rows):
            for b in row:
                if b.get("type") == "select":
                    v.add_item(discord.ui.Select(
                        custom_id=b["custom_id"], placeholder=b.get("placeholder"), min_values=1,
                        max_values=max(1, b.get("max", 1)), row=r,
                        options=[discord.SelectOption(label=o["label"], value=o["value"],
                                                      description=o.get("description")) for o in b["options"]]))
                    continue
                if b.get("url"):
                    v.add_item(discord.ui.Button(label=b["label"], url=b["url"], row=r))
                    continue
                v.add_item(discord.ui.Button(label=b["label"], style=styles[b["style"]], custom_id=b["custom_id"],
                                             disabled=b["disabled"], row=r))
        return v

    def layout(self, spec: dict):
        """A Components V2 view (text with a button beside it) from rules_page blocks."""
        import discord
        styles = {"success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger,
                  "primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary}

        def button(b):
            return discord.ui.Button(label=b["label"], style=styles[b["style"]], custom_id=b["custom_id"],
                                     disabled=b["disabled"])
        v = discord.ui.LayoutView(timeout=None)
        for blk in spec["blocks"]:
            if blk["type"] == "text":
                v.add_item(discord.ui.TextDisplay(blk["content"]))
            elif blk["type"] == "rule":
                v.add_item(discord.ui.Section(discord.ui.TextDisplay(blk["content"]), accessory=button(blk["button"])))
                sc = blk.get("scope")
                if sc and sc["options"]:
                    v.add_item(discord.ui.ActionRow(discord.ui.Select(
                        custom_id=sc["custom_id"], placeholder=sc["placeholder"], min_values=sc["min"],
                        max_values=sc["max"],
                        options=[discord.SelectOption(label=o["label"], value=o["value"], default=o["default"])
                                 for o in sc["options"]])))
            elif blk["type"] == "row":
                v.add_item(discord.ui.ActionRow(*[button(b) for b in blk["buttons"]]))
        return v

    @staticmethod
    def rows_of(message) -> list[list[dict]]:
        """A sent message's buttons as layout dicts (to re-post them with one changed)."""
        rows = []
        for ar in getattr(message, "components", None) or []:
            row = []
            for c in getattr(ar, "children", []):
                if getattr(c, "url", None):
                    row.append({"label": c.label, "url": c.url})
                elif getattr(c, "custom_id", None) and hasattr(c, "style"):
                    row.append({"label": c.label or "", "style": getattr(c.style, "name", "secondary"),
                                "custom_id": c.custom_id, "disabled": bool(c.disabled)})
            if row:
                rows.append(row)
        return rows

    @staticmethod
    def embeds(dicts: list[dict]):
        import discord
        return [discord.Embed.from_dict(d) for d in dicts]

    # ---- life cycle -----------------------------------------------------------
    async def run_forever(self) -> None:
        """Connect and stay connected. Never raises (a bad token is reported once)."""
        try:
            import discord
        except ImportError:
            log.warning("discord.py is not installed: pip install -r requirements.txt")
            return
        while True:
            self.client = self.build_client()
            try:
                await self.client.start(self.rcfg["token"])
            except discord.LoginFailure:
                log.error("DISCORD_BOT_TOKEN was refused by Discord; approval bot is off")
                if self.notifier:
                    await self.notifier.send("discord_token", "🤖 The Discord approval bot token was refused."
                                             " Reset it in the Developer Portal and update DISCORD_BOT_TOKEN in .env.")
                return
            except discord.PrivilegedIntentsRequired:
                log.error("enable MESSAGE CONTENT INTENT for the bot in the Developer Portal")
                if self.notifier:
                    await self.notifier.send("discord_intent", "🤖 Turn on **Message Content Intent** for the"
                                             " approval bot (Developer Portal > Bot), then restart spikebot.")
                return
            except Exception as e:  # noqa: BLE001
                log.warning("discord bot stopped: %s; reconnecting in 60s", e)
            finally:
                try:
                    await self.client.close()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(60)

    def build_client(self):
        import discord
        from discord import app_commands
        intents = discord.Intents.default()
        intents.message_content = True  # links and "copy:" notes in #inspiration
        client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        tree = app_commands.CommandTree(client)
        self.register_commands(tree)
        bot = self

        @client.event
        async def on_ready():
            log.info("discord approval bot online as %s", client.user)
            try:
                if bot.rcfg["guild_id"]:
                    g = discord.Object(id=bot.rcfg["guild_id"])
                    tree.copy_global_to(guild=g)
                    await tree.sync(guild=g)
                    # Old global copies (with old options) would show twice and fail when picked:
                    # the server's own copies are the only ones needed. Cleared on Discord's side
                    # only, so the tree (and a later on_ready) still has every command.
                    await client.http.bulk_upsert_global_commands(client.application_id, [])
                else:
                    await tree.sync()
            except Exception as e:  # noqa: BLE001
                log.warning("slash command sync failed: %s", e)
            asyncio.create_task(bot.ensure_bar_emojis())
            asyncio.create_task(bot.ensure_topics())
            if not bot.started:
                bot.started = True
                bot.render_q = asyncio.Queue()
                for coro in (bot.scan_loop(), bot.render_worker(), bot.publish_loop(), bot.trend_loop(),
                             bot.progress_loop(), bot.stats_loop(), bot.social_loop(), bot.rules_loop(), bot.run_deploy_requests()):
                    asyncio.create_task(coro)

        @tree.error
        async def on_command_error(interaction, error):
            await bot.report_error(interaction, getattr(error, "original", error),
                                   f"/{getattr(interaction.command, 'name', '?')}")

        @client.event
        async def on_interaction(interaction):
            if interaction.type == discord.InteractionType.component:
                data = parse_cid((interaction.data or {}).get("custom_id", ""))
                if data:
                    try:
                        await bot.on_button(interaction, data)
                    except Exception as e:  # noqa: BLE001 - say what went wrong instead of "interaction failed"
                        await bot.report_error(interaction, e, f"button {data['action']}")

        @client.event
        async def on_message(message):
            try:
                await bot.on_message(message)
            except Exception as e:  # noqa: BLE001
                log.warning("inspiration message failed: %s", e)
        return client

    # ---- loops ----------------------------------------------------------------
    async def scan_loop(self, every: float = 15) -> None:
        while True:
            try:
                await self.scan(time.time())
            except Exception as e:  # noqa: BLE001
                log.warning("review scan: %s", e)
            try:
                await self.archive_inspiration(time.time())
            except Exception as e:  # noqa: BLE001
                log.warning("inspiration archive: %s", e)
            try:
                await asyncio.wait_for(self.wake.wait(), every)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def scan(self, now: float) -> None:
        for item in self.store.all():
            st_ = item["status"]
            if st_ in ("open", "approved"):
                for rnd in item["rounds"]:
                    if rnd["status"] == "ready" and not rnd.get("message_id"):
                        try:
                            await self.post_round(item["id"], rnd["n"])
                        except Exception as e:  # noqa: BLE001 - tell Tim, keep retrying
                            log.warning("review %s: posting round %s failed: %s", item["id"], rnd["n"], e)
                            if self.notifier:
                                await self.notifier.send(f"post_failed:{item['id']}:{rnd['n']}",
                                                         f"🎬 Round {rnd['n']} of `{item['id']}` is rendered but"
                                                         f" couldn't be posted: {type(e).__name__}: {e}"[:400],
                                                         kind="lacy")
                        item = self.store.load(item["id"])
            req = item.get("request") or {}
            if req.get("status") == "failed" and not req.get("told"):
                await self.say(item, f"⚠️ Re-render failed: {req.get('error')}")
                with self.store.edit(item["id"]) as it:
                    it["request"]["told"] = True
            if req.get("status") == "queued" and item["id"] not in self.queued and st_ == "open":
                self.queued.add(item["id"])
                self.render_q.put_nowait(item["id"])
            if st_ == "failed" and not item.get("told") and "🔞" in (item.get("error") or ""):
                await self.announce(f"blocked:{item['id']}", f"🔞 `{item['id']}` ({mode_label(item)[0]}) was blocked"
                                    f" and deleted before review: {item.get('error')}")
                with self.store.edit(item["id"]) as it:
                    it["told"] = True
            elif st_ == "failed" and not item.get("told"):
                if self.notifier:
                    await self.notifier.send(f"review_failed:{item['id']}",
                                             f"🎬 `{item['id']}` could not be rendered: {item.get('error')}", kind="lacy")
                with self.store.edit(item["id"]) as it:
                    it["told"] = True
            if review.due_to_expire(item, now):
                await self.expire(item["id"], now)
            if st_ == "approved":
                if (item.get("publish") or {}).get("manual", {}).get("status") == "queued":
                    await self.post_manual(item["id"])
                if review.publish_finished(self.store.load(item["id"])):
                    await self.complete(item["id"])

    async def archive_inspiration(self, now: float, every: float = 600) -> None:
        """Archive the bot's #inspiration threads after `inspiration_archive_hours` without a
        message: they leave the channel list and sit under the channel's Threads button.
        Posting in one reopens it."""
        hours = float(self.rcfg.get("inspiration_archive_hours") or 0)
        if hours <= 0 or now - self._archive_checked < every:
            return
        self._archive_checked = now
        me = getattr(self.client, "user", None)
        ch = await self.channel(self.rcfg["inspiration_channel_id"])
        if me is None or ch is None:
            return
        for th in stale_threads(list(getattr(ch, "threads", None) or []), me.id, now, hours):
            try:
                await th.edit(archived=True)
                log.info("archived #inspiration thread %s", getattr(th, "name", th.id))
            except Exception as e:  # noqa: BLE001 - missing permission etc.
                log.warning("could not archive thread %s: %s", th.id, e)

    async def render_worker(self) -> None:
        """Re-renders one at a time in a subprocess (CPU heavy, keeps the bot responsive)."""
        while True:
            item_id = await self.render_q.get()
            try:
                if (self.store.load(item_id).get("request") or {}).get("status") != "queued":
                    continue   # cancelled while it waited
                p = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "spikebot.review", "--config", self.config_path, "render", item_id,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                out, _ = await asyncio.wait_for(p.communicate(), 3600)
                log.info("review render %s: %s", item_id, out.decode(errors="replace")[-300:].strip())
                item = self.store.load(item_id)
                if (item.get("request") or {}).get("status") == "rendering":  # crashed, or force-stopped
                    if p.pid in self.force_stopped or progress.cancelled_for(self.data_dir, "item", item_id):
                        review.cancel_render(self.store, item_id)
                    else:
                        with self.store.edit(item_id) as it:
                            it["request"].update(status="failed", error=out.decode(errors="replace")[-300:])
            except Exception as e:  # noqa: BLE001
                log.warning("review render %s: %s", item_id, e)
            finally:
                self.queued.discard(item_id)
                self.wake.set()

    async def publish_loop(self, every: float = 60) -> None:
        while True:
            try:
                await self.publish_due(time.time())
            except Exception as e:  # noqa: BLE001
                log.warning("publish loop: %s", e)
            await asyncio.sleep(every)

    async def publish_due(self, now: float) -> None:
        async with self.publish_lock:
            await self._publish_due(now)

    async def _publish_due(self, now: float) -> None:
        for item_id, platform in self.publisher.due(now):
            row = await asyncio.to_thread(self.publisher.publish, item_id, platform, now)
            item = self.store.load(item_id)
            name = review.NAMES[platform]
            if row["status"] == "done":
                extra = f" ({row['note']})" if row.get("note") else ""
                await self.say(item, f"✅ **{name}** is live: {row.get('url')}{extra}")
                await self.announce(f"live:{item_id}:{platform}",
                                    f"✅ {mode_label(item)[0]} `{item_id}` is live on **{name}**: {row.get('url')}")
            elif row["status"] == "failed":
                await self.say(item, f"❌ **{name}** failed {row.get('attempts')}×, giving up: {row.get('error')}")
                if self.notifier:
                    await self.notifier.send(f"publish_failed:{item_id}:{platform}",
                                             f"❌ `{item_id}` {name} upload failed: {row.get('error')}", kind="lacy")
            elif row["status"] == "waiting" and row.get("told") != row.get("reason"):
                when = review.next_window(self.rcfg["windows"].get(platform) or [], now)
                await self.say(item, f"⏳ **{name}** queued: {row.get('reason')}."
                               + (f" Next window {ts(when, 'R')}." if when > now else " Retrying hourly."))
                with self.store.edit(item_id) as it:
                    it["publish"][platform]["told"] = row.get("reason")
            elif row["status"] == "retry":
                await self.say(item, f"🔁 **{name}** failed ({row.get('error')}); retrying in a minute.")
        self.wake.set()

    # ---- posting rounds ---------------------------------------------------------
    async def post_round(self, item_id: str, n: int) -> None:
        import discord
        item = self.store.load(item_id)
        rnd = review.round_of(item, n)
        payload = round_payload(item, rnd, entry_on=self.entry_states())
        d = self.store.dir(item_id)

        def files():   # fresh each try: a File can only be sent once
            return [discord.File(d / v["preview"], filename=f"{v['label']}_{item_id}.mp4")
                    for v in rnd["variants"] if (d / v["preview"]).exists()]
        main = await self.channel(self.rcfg["channel_id"])
        target = (await self.channel(item["thread_id"])) if item.get("thread_id") else None
        in_thread = target is not None
        if target is None:      # no thread yet, or it can't be reached: the channel itself
            target = main
        if target is None:
            raise RuntimeError("neither the clip's thread nor #ready-to-post can be reached")
        kw = dict(content=payload["content"], embeds=self.embeds(payload["embeds"]),
                  view=self.view(payload["rows"]), allowed_mentions=discord.AllowedMentions.none())

        async def send(where):
            try:
                return await where.send(files=files(), **kw)
            except discord.HTTPException as e:
                if e.status != 413:
                    raise
                links = await asyncio.to_thread(self.preview_links, item, rnd)
                kw2 = dict(kw, content=(payload["content"] + "\nPreviews (too big to attach): "
                                        + " · ".join(f"[{k}]({u})" for k, u in links.items()))[:1990] if links else
                           payload["content"] + "\nPreviews too big to attach and no bucket set: approve from the cards.")
                return await where.send(**kw2)
        try:
            msg = await send(target)
        except Exception as e:  # noqa: BLE001 - e.g. an archived/locked thread: post in the channel instead
            if not in_thread or main is None:
                raise
            log.warning("review %s: could not post round %s in its thread (%s): posting in the channel", item_id, n, e)
            target, in_thread = main, False
            msg = await send(target)
        if in_thread and main is not None and rnd.get("n", n) > 1:
            # re-renders post in the thread: say so in the channel too, so nothing is missed
            try:
                labels = ", ".join(v["label"] for v in rnd["variants"])
                await main.send(f"🎬 **{labels}** of `{item_id}` is ready: {msg.jump_url}",
                                allowed_mentions=discord.AllowedMentions.none())
            except Exception as e:  # noqa: BLE001
                log.warning("review %s: could not post the pointer: %s", item_id, e)
        with self.store.edit(item_id) as it:
            r = review.round_of(it, n)
            r["message_id"], r["channel_id"], r["status"] = msg.id, target.id, "posted"
        if not item.get("thread_id"):
            try:
                th = await msg.create_thread(name=(item.get("title") or item_id)[:95], auto_archive_duration=4320)
                with self.store.edit(item_id) as it:
                    it["thread_id"] = th.id
            except Exception as e:  # noqa: BLE001
                log.warning("review %s: could not create a thread: %s", item_id, e)
        if rnd.get("entries"):   # rules this feedback saved: what each applies to, editable
            try:
                where = await self.channel(self.store.load(item_id).get("thread_id") or target.id)
                head = (f"📝 Saved from your feedback on this {st.MODE_NAME[review.style_mode(item)]} clip."
                        " Change the ticks to choose what each applies to, or turn it off.")
                await where.send(view=self.layout(rules_subset(list(self.store.profile().entries()),
                                                               rnd["entries"], head)))
            except Exception as e:  # noqa: BLE001 - the rules are saved either way
                log.warning("review %s: could not post the saved rules: %s", item_id, e)

    async def repost_latest(self, item_id: str) -> None:
        """Send a copy of the item's newest rendered round (previews + the same buttons) to
        #ready-to-post itself. Nothing in the item changes."""
        import discord
        item = self.store.load(item_id)
        rnd = next((r for r in reversed(item["rounds"]) if r.get("variants")), None)
        main = await self.channel(self.rcfg["channel_id"])
        if rnd is None or main is None:
            raise RuntimeError("no rendered round" if rnd is None else "#ready-to-post can't be reached")
        payload = round_payload(item, rnd, entry_on=self.entry_states())
        d = self.store.dir(item_id)
        files = [discord.File(d / v["preview"], filename=f"{v['label']}_{item_id}.mp4")
                 for v in rnd["variants"] if (d / v["preview"]).exists()]
        await main.send(content=payload["content"][:1990], embeds=self.embeds(payload["embeds"]), files=files,
                        view=self.view(payload["rows"]), allowed_mentions=discord.AllowedMentions.none())

    def preview_links(self, item: dict, rnd: dict) -> dict[str, str]:
        from . import delivery
        dcfg = delivery.settings(self.cfg, self.base)
        if not dcfg["gcs_bucket"]:
            return {}
        up = delivery.GCSUploader(dcfg["gcs_bucket"], dcfg["gcs_key_file"])
        d = self.store.dir(item["id"])
        return {v["label"]: up.upload(d / v["preview"], f"videos/review/{item['id']}_{v['preview']}", 2)
                for v in rnd["variants"]}

    async def close_round_messages(self, item: dict, note: str) -> None:
        """Grey out the buttons on every posted round of the item."""
        for rnd in item["rounds"]:
            if not rnd.get("message_id"):
                continue
            ch = await self.channel(rnd.get("channel_id"))
            if ch is None:
                continue
            try:
                msg = await ch.fetch_message(rnd["message_id"])
                content = (msg.content + f"\n{note}")[:1990] if note not in msg.content else msg.content
                await msg.edit(content=content, view=self.view(buttons(item, rnd, disabled=True)))
            except Exception as e:  # noqa: BLE001
                log.warning("review %s: could not update round %s: %s", item["id"], rnd["n"], e)

    # ---- decisions ----------------------------------------------------------------
    async def on_button(self, interaction, data: dict) -> None:
        if not self.allowed(interaction.user):
            await interaction.response.send_message("Only Tim can review clips.", ephemeral=True)
            return
        if data["action"] == "cancel":
            await self.cancel_command(interaction, data["item"])
            return
        if data["action"] in ("rule", "rulep", "rulem", "rulesp", "rules_off", "rules_on"):
            await self.on_rules_click(interaction, data)
            return
        try:
            item = self.store.load(data["item"])
        except (FileNotFoundError, ValueError):
            await interaction.response.send_message("That clip no longer exists.", ephemeral=True)
            return
        a = data["action"]
        try:
            if a == "approve":
                await self.do_approve(interaction, item, data["round"], data["label"])
            elif a == "reject":
                await self.do_reject(interaction, item, data["round"])
            elif a == "redo":
                await self.do_redo(interaction, item, data["round"])
            elif a in ("feedback", "edit"):
                rnd = review.round_of(item, data["round"])
                review.require_open(item)
                pick = "fbpick" if a == "feedback" else "edpick"
                if len(rnd["variants"]) == 1:
                    await self.open_modal(interaction, pick, item, rnd["n"], rnd["variants"][0]["label"])
                else:
                    await interaction.response.send_message("Which variant?", ephemeral=True,
                                                            view=self.view(picker(pick, item, rnd)))
            elif a in ("fbpick", "edpick"):
                review.require_open(item)
                await self.open_modal(interaction, a, item, data["round"], data["label"])
        except ValueError as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)

    async def do_approve(self, interaction, item: dict, n: int, label: str) -> None:
        await interaction.response.defer(thinking=False)
        available = await asyncio.to_thread(self.publisher.available)
        with self.store.edit(item["id"]) as it:
            review.approve(it, n, label, self.rcfg, available)
        item = self.store.load(item["id"])
        profile = self.store.profile()
        profile.decide("approve", review.style_mode(item), item["id"], review.round_of(item, n)["variants"], label,
                       extra={"by": interaction.user.id})
        profile.save()
        await self.close_round_messages(item, f"✅ **Approved {label}** by {interaction.user.display_name}")
        auto = [review.NAMES[p] for p in item["publish"] if p != "manual"]
        manual = [review.NAMES[p] for p in item["publish"]["manual"]["platforms"]]
        await self.say(item, f"✅ Approved **{label}**. Auto: {', '.join(auto) or 'none'} (posting windows and"
                             f" daily caps apply). Manual via #tiktok-queue: {', '.join(manual)}.")
        self.wake.set()
        asyncio.create_task(self.publish_due(time.time()))

    async def do_reject(self, interaction, item: dict, n: int) -> None:
        review.require_open(item)
        await interaction.response.defer(thinking=False)
        profile = self.store.profile()
        profile.decide("reject", review.style_mode(item), item["id"], review.round_of(item, n)["variants"],
                       extra={"by": interaction.user.id})
        profile.save()
        item = await asyncio.to_thread(review.finish, self.store, self.cfg, self.base, item["id"], "rejected",
                                       None, "rejected in Discord")
        await self.close_round_messages(item, f"🗑️ **Rejected** by {interaction.user.display_name}")

    async def do_redo(self, interaction, item: dict, n: int) -> None:
        with self.store.edit(item["id"]) as it:
            review.request(it, "redo", round=n)
        profile = self.store.profile()
        profile.decide("redo", review.style_mode(item), item["id"], review.round_of(item, n)["variants"])
        profile.save()
        await interaction.response.send_message("🔄 Rendering 3 new variants; they'll appear in the thread.",
                                                ephemeral=True)
        await self.say(item, "🔄 Redo requested: rendering 3 new variants…")
        self.wake.set()

    async def open_modal(self, interaction, kind: str, item: dict, n: int, label: str) -> None:
        import discord
        bot = self
        v = review.variant_of(item, n, label)
        if kind == "fbpick":
            class Feedback(discord.ui.Modal, title=f"Feedback on {label}"):
                notes = discord.ui.TextInput(label="What should change?", style=discord.TextStyle.paragraph,
                                             max_length=800, placeholder="bigger text, faster, more colour,"
                                             " hook: Lacy did NOT see this coming")

                async def on_submit(self, inter):
                    try:
                        with bot.store.edit(item["id"]) as it:
                            review.request(it, "feedback", round=n, label=label, notes=str(self.notes))
                    except ValueError as e:
                        await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                        return
                    await inter.response.send_message("📝 Got it, re-rendering with your notes.", ephemeral=True)
                    await bot.say(item, f"📝 Feedback on **{label}**: {str(self.notes)[:400]}\nRe-rendering…")
                    bot.wake.set()
            await interaction.response.send_modal(Feedback())
            return

        caps = v.get("captions") or {}

        class Edit(discord.ui.Modal, title=f"Edit caption {label}"):
            title_in = discord.ui.TextInput(label="YouTube title", default=v.get("title", "")[:100], max_length=100)
            ig = discord.ui.TextInput(label="Instagram caption", style=discord.TextStyle.paragraph,
                                      default=caps.get("instagram", "")[:2000], max_length=2000)
            tt = discord.ui.TextInput(label="TikTok caption", style=discord.TextStyle.paragraph,
                                      default=caps.get("tiktok", "")[:2000], max_length=2000)

            async def on_submit(self, inter):
                try:
                    adapter = review.adapter_for(item["mode"], bot.cfg, bot.base)
                    with bot.store.edit(item["id"]) as it:
                        review.edit_caption(it, n, label, str(self.title_in), str(self.ig), str(self.tt),
                                            adapter.validate)
                except ValueError as e:
                    await inter.response.send_message(f"⚠️ Not saved: {e}", ephemeral=True)
                    return
                await inter.response.send_message("✏️ Saved. The card shows the new text.", ephemeral=True)
                await bot.refresh_round(item["id"], n)
                profile = bot.store.profile()
                profile.data["decisions"].append({"ts": time.time(), "action": "edit_caption", "mode": item["mode"],
                                                  "item": item["id"], "chosen": label})
                profile.save()
        await interaction.response.send_modal(Edit())

    async def refresh_round(self, item_id: str, n: int) -> None:
        item = self.store.load(item_id)
        rnd = review.round_of(item, n)
        ch = await self.channel(rnd.get("channel_id"))
        if ch is None or not rnd.get("message_id"):
            return
        payload = round_payload(item, rnd)
        try:
            msg = await ch.fetch_message(rnd["message_id"])
            await msg.edit(content=payload["content"], embeds=self.embeds(payload["embeds"]),
                           view=self.view(payload["rows"]))
        except Exception as e:  # noqa: BLE001
            log.warning("review %s: card refresh failed: %s", item_id, e)

    async def expire(self, item_id: str, now: float) -> None:
        item = self.store.load(item_id)
        rnd = review.latest_round(item)
        profile = self.store.profile()
        profile.decide("expire", review.style_mode(item), item_id, rnd["variants"] if rnd else [], now=now)
        profile.save()
        item = await asyncio.to_thread(review.finish, self.store, self.cfg, self.base, item_id, "expired", now,
                                       "expired after 48 h without approval")
        await self.close_round_messages(item, "⌛ **Expired** (not approved within 48 h)")

    async def post_manual(self, item_id: str) -> None:
        """#tiktok-queue: the approved file + captions for the manual platforms."""
        import discord
        item = self.store.load(item_id)
        a = item["approved"]
        v = review.variant_of(item, a["round"], a["label"])
        platforms = item["publish"]["manual"]["platforms"]
        export = await asyncio.to_thread(review.tiktok_export, self.cfg, self.base, self.store, item)
        path = review.make_final(self.store, item)
        ch = await self.channel(self.rcfg["tiktok_channel_id"])
        status, link_text = "done", ""
        if ch is None:
            status = "no-channel"
            link_text = f"#tiktok-queue isn't set up; the file is on the server at `{export or path}`"
        else:
            limit = getattr(getattr(ch, "guild", None), "filesize_limit", 10 * 1024 * 1024) - 64 * 1024
            try:
                if path.stat().st_size <= limit:
                    p = manual_payload(item, v, platforms)
                    msg = await ch.send(p["content"], file=discord.File(path, filename=f"{item_id}.mp4"),
                                        allowed_mentions=discord.AllowedMentions.none())
                else:
                    link = await asyncio.to_thread(self.big_file_link, item, path)
                    p = manual_payload(item, v, platforms, link=link, path_note=None if link else str(export or path))
                    msg = await ch.send(p["content"], embeds=self.embeds(p.get("embeds", [])),
                                        allowed_mentions=discord.AllowedMentions.none())
                link_text = f"📱 Manual posting ({', '.join(review.NAMES[x] for x in platforms)}): {msg.jump_url}"
            except Exception as e:  # noqa: BLE001
                status = "retry"
                link_text = f"⚠️ Couldn't post to #tiktok-queue ({e}); retrying."
        with self.store.edit(item_id) as it:
            it["publish"]["manual"]["status"] = "done" if status in ("done", "no-channel") else "queued"
            it["publish"]["manual"]["export"] = str(export) if export else None
        await self.say(item, link_text)

    def big_file_link(self, item: dict, path: Path) -> str | None:
        from . import delivery
        dcfg = delivery.settings(self.cfg, self.base)
        if not dcfg["gcs_bucket"]:
            return None
        up = delivery.GCSUploader(dcfg["gcs_bucket"], dcfg["gcs_key_file"])
        return up.upload(path, f"videos/{item['mode']}/{item['id']}.mp4", int(dcfg["link_days"]))

    async def complete(self, item_id: str) -> None:
        item = self.store.load(item_id)
        lines = []
        for p, row in item["publish"].items():
            if p == "manual":
                continue
            lines.append(f"{review.NAMES[p]}: {row.get('url') or row.get('status')}")
        await asyncio.to_thread(review.finish, self.store, self.cfg, self.base, item_id, "done")
        await self.say(item, "🏁 Done. " + (" · ".join(lines) if lines else "Manual platforms only."))

    # ---- #inspiration -------------------------------------------------------------
    async def on_message(self, message) -> None:
        import discord
        if message.author.bot or not self.rcfg["inspiration_channel_id"]:
            return
        ch = message.channel
        in_thread = getattr(ch, "parent_id", None) is not None   # threads have a parent channel
        home = ch.parent_id if in_thread else ch.id
        if home != self.rcfg["inspiration_channel_id"]:
            return
        if not self.allowed(message.author):
            return
        from . import inspiration
        songs = [a for a in message.attachments
                 if (a.content_type or "").startswith("audio/")
                 or a.filename.lower().endswith((".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"))]
        from . import music
        try:
            sound_links = await asyncio.to_thread(music.song_links, message.content)
        except Exception:  # noqa: BLE001 - a bad link must not stop #inspiration
            log.exception("song link check failed")
            sound_links = []
        videos = [a for a in message.attachments
                  if (a.content_type or "").startswith("video/") or a.filename.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))]
        urls = [u for u in inspiration.links(message.content) if u not in {o for o, _ in sound_links}]
        if songs or sound_links:
            await self.add_songs(message, songs, [r for _, r in sound_links])
            if not videos and not urls:
                return
        if not videos and not urls:
            if not in_thread:
                await message.reply("Send a TikTok / Instagram / YouTube link or upload a video. For a song,"
                                    " paste a TikTok sound link (Share sound > Copy link), upload the audio,"
                                    " or put `sound:` before a link to a video that uses it. Optional notes:"
                                    " `copy: pacing, colour, text, transitions, hook, structure, music-sync, look, framing`,"
                                    " `name: my-preset`, `type: edit / clip / other` (else I work it out), and anything about what you like in it.",
                                    mention_author=False)
            return
        pending = self.pending_uploads.pop(ch.id, None) if in_thread else None
        kind = inspiration.kind_from_note(message.content) or (pending or {}).get("kind")
        copy = presets_.parse_copy(message.content) if "copy:" in message.content.lower() or not pending \
            else pending["copy"]
        if in_thread:
            thread = ch
        else:
            thread = await message.create_thread(name=f"Inspiration {datetime.now():%d %b %H:%M}"[:95],
                                                 auto_archive_duration=1440)
        jobs = [("url", u) for u in urls] + [("file", a) for a in videos]
        for i, (what, src) in enumerate(jobs):
            name = presets_.parse_name(message.content) or (pending or {}).get("name")
            if not name or len(jobs) > 1:
                plat = inspiration.platform_of(src) if what == "url" else "upload"
                name = f"{name or plat}-{datetime.now():%m%d}" + (f"-{i + 1}" if len(jobs) > 1 else "")
            await thread.send(f"🔎 Analysing{' ' + src if what == 'url' else ' your upload'} → preset"
                              f" `{name}` (copies: {', '.join(copy)})…", suppress_embeds=True)
            path = None
            if what == "file":
                tmp = self.data_dir / "inspiration" / "uploads"
                tmp.mkdir(parents=True, exist_ok=True)
                path = tmp / f"{uuid.uuid4().hex}{Path(src.filename).suffix or '.mp4'}"
                await src.save(path)
            note = re.sub(r"https?://\S+|\b(copy|name|type|kind)\s*:[^\n]*", " ", message.content or "",
                          flags=re.I).strip()
            res = await self.analyse(name, copy, url=src if what == "url" else None, file=path, note=note,
                                     channel_id=thread.id, kind=kind)
            if res.get("ok"):
                f = discord.File(res["strip"], filename=f"{res['name']}_strip.jpg") if res.get("strip") else None
                await thread.send(res["text"], file=f) if f else await thread.send(res["text"])
                if res.get("art"):
                    await thread.send(res["art"])
                if res.get("strip"):
                    Path(res["strip"]).unlink(missing_ok=True)
            elif res.get("ask_upload"):
                self.pending_uploads[thread.id] = {"name": name, "copy": copy, "kind": kind}
                await thread.send(f"⚠️ I couldn't download that link ({res.get('error')}).\n"
                                  "**Please upload the video file directly here in this thread** and I'll analyse it.")
            elif res.get("cancelled"):
                await thread.send("🛑 Cancelled: nothing was saved.")
            else:
                await thread.send(f"⚠️ Analysis failed: {res.get('error')}")

    async def add_songs(self, message, songs, urls=()) -> None:
        """Audio in #inspiration (an upload, a TikTok sound link, or `sound:` + a video link)
        = the song to cut Tokyo Girl edits to. Only measured (beats, drop); edits stay silent
        and Tim adds the sound in the app."""
        given = presets_.parse_name(message.content)
        tmp = self.data_dir / "music" / ".incoming"
        tmp.mkdir(parents=True, exist_ok=True)
        jobs = [("file", a) for a in songs] + [("url", u) for u in urls]
        for kind, src in jobs:
            path = None
            if kind == "file":
                path = tmp / f"{uuid.uuid4().hex}{Path(src.filename).suffix.lower() or '.mp3'}"
                await src.save(path)
                args = [str(path), "--name", given or Path(src.filename).stem]
            else:
                try:
                    await message.add_reaction("🎵")   # "fetching", the reply follows
                except Exception:  # noqa: BLE001 - missing permission is fine
                    pass
                args = ["--url", src] + (["--name", given] if given else [])
            out = tmp / f"{uuid.uuid4().hex}.json"
            pj = self.progress_job("Adding a sound" + (f" · {given}" if given else ""), message.channel.id, "🎵")
            res = {}
            try:
                p = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "spikebot.music", "--config", self.config_path, "add", *args,
                    "--json", str(out), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, env=progress_env(pj), start_new_session=True)
                pj.adopt(p.pid)
                text, _ = await asyncio.wait_for(p.communicate(), 600)
                res = json.loads(out.read_text(encoding="utf-8")) if out.exists() else \
                    {"ok": False, "error": text.decode(errors="replace")[-200:]}
                if pj.cancel_requested():
                    res = {"ok": False, "cancelled": True, "error": "cancelled"}
            except asyncio.TimeoutError:
                res = {"ok": False, "error": "analysis took over 10 minutes"}
            finally:
                if path:
                    path.unlink(missing_ok=True)
                out.unlink(missing_ok=True)
                pj.finish(bool(res.get("ok")), res.get("error") or "")
            if res.get("ok") and res.get("tokyo_girl"):
                from .music import mmss
                await message.reply(
                    f"🎵 Got the Tokyo Girl song: {res['bpm']:.0f} BPM, drop at {mmss(res['drop_s'])}. Tokyo Girl"
                    f" cuts now land on its beats and the drop; each card says where to start the sound (about"
                    f" {mmss(res['start_s'])}). The edits stay silent: add the sound in the app.",
                    mention_author=False)
            elif res.get("ok"):
                from .music import mmss
                await message.reply(
                    f"🎵 Saved **{res['name']}** as a trending sound: {res['bpm']:.0f} BPM, drop at"
                    f" {mmss(res['drop_s'])}. It won't touch the Tokyo Girl edits, which always use the Tokyo Girl"
                    " song. (If this *is* the Tokyo Girl sound, post it again with `name: tokyo-girl`.)",
                    mention_author=False)
            elif res.get("cancelled"):
                await message.reply("🛑 Cancelled: that sound wasn't added.", mention_author=False)
            else:
                await message.reply(f"⚠️ Couldn't read that song: {res.get('error')}", mention_author=False)

    # ---- stats channels -------------------------------------------------------------
    def stats_snapshot(self, now: float) -> dict:
        snap = {"started": self.started_at}
        if not self.social:
            from . import socialstats
            self.social = socialstats.load(self.data_dir)   # last totals, kept across restarts
        snap["social"] = (self.social or {}).get("totals") or {}
        if self.stats_source:
            try:
                snap.update(self.stats_source(now))
            except Exception as e:  # noqa: BLE001 - stats are cosmetic
                log.warning("stats source: %s", e)
        day = datetime.fromtimestamp(now, review.UK).date()
        items = self.store.all()
        snap["to_review"] = sum(1 for it in items if it.get("status") == "open")
        snap["approved_today"] = sum(
            1 for it in items if (it.get("approved") or {}).get("ts")
            and datetime.fromtimestamp(it["approved"]["ts"], review.UK).date() == day)
        return snap

    async def stats_loop(self) -> None:
        await asyncio.sleep(20)          # let the cache fill after connecting
        while True:
            try:
                await self.stats_tick(time.time())
            except Exception as e:  # noqa: BLE001
                log.warning("stats channels: %s", e)
            # Checked every minute; each channel is renamed at most once per ~5 min (Discord's
            # limit is 2 per 10 min), so a change shows within a minute unless that channel
            # was just renamed.
            await asyncio.sleep(60.0)

    async def social_loop(self) -> None:
        """Views / likes / followers across YouTube and Instagram (spikebot/socialstats.py)."""
        from . import socialstats
        await asyncio.sleep(30)
        while True:
            every = float((self.rcfg.get("stats") or {}).get("social_every_s") or 900)
            try:
                if (self.rcfg.get("stats") or {}).get("enabled"):
                    self.social = await asyncio.to_thread(
                        socialstats.refresh, self.cfg, self.base, self.data_dir)
            except Exception as e:  # noqa: BLE001 - stats are cosmetic
                log.warning("social stats: %s", e)
            await asyncio.sleep(max(300.0, every))

    async def stats_tick(self, now: float, force: bool = False) -> bool:
        """True when every stats channel is in place (renames may still be waiting for
        Discord's per-channel limit). `force` (/stats) skips the 5 min spacing, never the limit."""
        import discord
        scfg = self.rcfg.get("stats") or {}
        if not scfg.get("enabled"):
            return True
        guild = self.client.get_guild(self.rcfg["guild_id"]) if self.rcfg["guild_id"] else None
        if guild is None:
            ch = await self.channel(self.rcfg["channel_id"])
            guild = getattr(ch, "guild", None)
        if guild is None:
            return False
        path = self.data_dir / "discord_stats.json"
        try:
            ids = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ids = {}
        renamed = {k: v for k, v in (ids.get("renamed") or {}).items() if isinstance(v, list)}
        ids["renamed"] = renamed
        names = stat_names(self.stats_snapshot(now), now, scfg.get("gta6_launch") or GTA6_LAUNCH)
        try:
            cat = guild.get_channel(ids.get("category") or 0)
            if cat is None:
                cat = next((c for c in guild.categories if c.name == scfg.get("category", "📊 Stats")), None)
            if cat is None:
                cat = await guild.create_category(
                    scfg.get("category", "📊 Stats"), position=0,
                    overwrites={guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=True),
                                guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True,
                                                                      view_channel=True)},
                    reason="spikebot stats")
            ids["category"] = cat.id
            chans = {}
            for key in STAT_KEYS:
                if key not in names:
                    continue
                ch = guild.get_channel(ids.get(key) or 0)
                if ch is None:
                    ch = await guild.create_voice_channel(names[key], category=cat, reason="spikebot stats")
                    ids[key] = ch.id
                chans[key] = ch
            for key in renames_due(names, {k: c.name for k, c in chans.items()}, renamed, now, burst=force):
                await chans[key].edit(name=names[key], reason="spikebot stats")
                note_rename(renamed, key, now)
            self._stats_warned = False
            return True
        except discord.Forbidden:
            if not self._stats_warned:
                self._stats_warned = True
                log.warning("stats channels: the bot needs the Manage Channels permission")
                if self.notifier:
                    await self.notifier.send("stats_perm", "📊 Stats channels need the bot's **Manage Channels**"
                                             " permission (Server Settings > Roles > the bot's role).")
            return False
        finally:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(ids), encoding="utf-8")

    # ---- live progress bars (spikebot/progress.py) -----------------------------
    async def ensure_bar_emojis(self) -> None:
        """Upload the rounded progress bar pieces once as the bot's own (application) emojis.
        They work in every server without Manage Emojis; on any problem the text bar is used."""
        try:
            have = {e.name: e for e in await self.client.fetch_application_emojis()}
            for name, png in progress.bar_pieces().items():
                if name not in have:
                    have[name] = await self.client.create_application_emoji(name=name, image=png)
            self.bar_emojis = {n: str(e) for n, e in have.items() if n.startswith(progress.EMOJI_PREFIX)}
            log.info("rounded progress bar ready (%d pieces)", len(self.bar_emojis))
        except Exception as e:  # noqa: BLE001 - cosmetic: the text bar still works
            log.warning("rounded progress bar unavailable, using the text bar: %s", e)

    def render_bar(self, frac: float) -> str | None:
        return progress.emoji_bar(frac, self.bar_emojis)

    async def progress_loop(self, every: float = 2) -> None:
        while True:
            try:
                await self.progress_tick(time.time())
            except Exception as e:  # noqa: BLE001
                log.warning("progress: %s", e)
            await asyncio.sleep(every)

    def _progress_index(self) -> Path:
        return progress.folder(self.data_dir) / "_messages.json"

    async def progress_tick(self, now: float) -> None:
        """One message per running job, edited as it moves; deleted when done (the card or
        reply replaces it), kept when it fails, shown for 2 minutes when skipped or cancelled.
        Running bars carry a Cancel button."""
        async with self._progress_lock:   # the loop and /cancel's poke must not both post a bar
            await self._progress_tick(now)

    async def _progress_tick(self, now: float) -> None:
        try:
            index = json.loads(self._progress_index().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = {}
        jobs = progress.load_all(self.data_dir)
        # Discord allows about 5 message edits per 5 s in a channel: with several bars in one
        # channel each is edited less often (2 s with one or two bars, 4 s with three or four...)
        per_channel: dict = {}
        for _, d in jobs:
            if progress.state_of(d, now) == "running":
                c = d.get("channel_id") or self.rcfg["channel_id"]
                per_channel[c] = per_channel.get(c, 0) + 1
        for path, d in jobs:
            jid, state = d.get("id") or path.stem, progress.state_of(d, now)
            text = progress.text(d, now, self.render_bar)
            rec = self.progress_msgs.get(jid)
            gap = 2.0 * max(1, -(-per_channel.get(d.get("channel_id") or self.rcfg["channel_id"], 1) // 2))
            if rec is None and jid in index:           # bot restarted mid-job: reuse the message
                ch = await self.channel(index[jid][0])
                try:
                    rec = {"msg": await ch.fetch_message(index[jid][1]), "text": ""} if ch else None
                except Exception:  # noqa: BLE001 - deleted meanwhile
                    rec = None
            if rec is None:
                if state == "done" or (state == "running" and now - d.get("started", now) < 4):
                    if state == "done":
                        path.unlink(missing_ok=True)
                    continue                            # quick jobs never need a bar
                cid = d.get("channel_id") or self.rcfg["channel_id"]
                ch = await self.channel(cid)
                if ch is None:
                    continue
                kw = {"view": self.view(cancel_buttons(jid))} if state == "running" else {}
                rec = {"msg": await ch.send(text, allowed_mentions=_no_mentions(), **kw), "text": text,
                       "button": state == "running", "edited": now}
                index[jid] = [cid, rec["msg"].id]
            elif text != rec["text"] and (state != "running" or now - rec.get("edited", 0) >= gap - 0.05):
                rec["edited"] = now
                kw = {}
                if state != "running" and rec.get("button", True):
                    kw["view"], rec["button"] = None, False     # finished: the Cancel button goes
                await rec["msg"].edit(content=text, **kw)
                rec["text"] = text
            self.progress_msgs[jid] = rec
            end = None
            if state == "done":
                await rec["msg"].delete()
                end = True
            elif state in ("skipped", "cancelled"):
                rec.setdefault("ended", now)
                if now - rec["ended"] >= 120:
                    await rec["msg"].delete()
                    end = True
            elif state in ("failed", "stalled"):
                end = True
            if end:
                path.unlink(missing_ok=True)
                self.progress_msgs.pop(jid, None)
                index.pop(jid, None)
        live = {d.get("id") or p.stem for p, d in progress.load_all(self.data_dir)}
        index = {k: v for k, v in index.items() if k in live}
        try:
            self._progress_index().parent.mkdir(parents=True, exist_ok=True)
            self._progress_index().write_text(json.dumps(index), encoding="utf-8")
        except OSError:
            pass

    def progress_job(self, title: str, channel_id: int | None, icon: str) -> "progress.Job":
        return progress.Job.create(self.data_dir, title, channel="custom", channel_id=channel_id, icon=icon)

    # ---- /cancel and the progress bar's Cancel button -------------------------------
    def runs(self, now: float) -> list[tuple[str, str]]:
        """Everything that can be cancelled now: (run id, description)."""
        running = [d for _, d in progress.running(self.data_dir, now)]
        queued = [it for it in self.store.all()
                  if it.get("status") == "open" and (it.get("request") or {}).get("status") == "queued"]
        return run_rows(running, queued, now)

    async def cancel_run(self, ref: str, by: str, now: float | None = None,
                         grace: float = progress.HARD_KILL_S, poll: float = 1.0) -> str:
        """Cancel one run (short id from the bar, full job id, or q-<item> for a queued
        re-render). Returns the message for Tim; raises ValueError if there's nothing to cancel."""
        now = now or time.time()
        ref = (ref or "").strip().strip("`")
        if ref.lower().startswith(QUEUED):
            item_id = ref[len(QUEUED):]
            try:
                item = self.store.load(item_id)
            except (FileNotFoundError, ValueError):
                raise ValueError(f"there's no clip `{item_id}`") from None
            if (item.get("request") or {}).get("status") != "queued":
                raise ValueError(f"`{item_id}` has no queued re-render (it may have started: cancel it by the"
                                 " run id on its progress bar)")
            review.cancel_render(self.store, item_id, now, why=f"queued re-render cancelled by {by}")
            self.queued.discard(item_id)
            await self.say(self.store.load(item_id), f"🛑 Queued re-render cancelled by {by}.")
            return f"🛑 Cancelled the queued re-render of **{item.get('title') or item_id}**."
        path, d = progress.resolve(self.data_dir, ref, now)
        rid, title = progress.short(d.get("id") or path.stem), d.get("title", "job")
        if not progress.request_cancel(path, by, now):
            return f"⏳ `{rid}` ({title}) is already being cancelled."
        self.progress_poke()
        # The worker normally stops within a second or two (SIGTERM -> Cancelled -> clean-up).
        waited = 0.0
        while waited < grace:
            await asyncio.sleep(poll)
            waited += poll
            if self._job_state(path, time.time()) != "running":
                return f"🛑 Stopped **{title}** (run `{rid}`)."
        # Still going (stuck in C code: Whisper, librosa): clean up for it, then kill it.
        meta = d.get("meta") or {}
        if meta.get("item"):
            try:
                review.cancel_render(self.store, meta["item"], why=f"force-stopped by {by}")
            except (FileNotFoundError, ValueError):
                pass
        try:
            pid = int(json.loads(path.read_text(encoding="utf-8")).get("pid") or 0)
        except (OSError, ValueError):
            pid = 0
        if progress.force_stop(path):
            self.force_stopped.add(pid)
            return f"🛑 Force-stopped **{title}** (run `{rid}`): it didn't stop within {grace:.0f}s."
        if self._job_state(path, time.time()) != "running":
            return f"🛑 Stopped **{title}** (run `{rid}`)."
        return (f"⏳ Asked **{title}** (run `{rid}`) to stop; it will at its next step. Its bar will say when"
                " it has.")

    def _job_state(self, path: Path, now: float) -> str:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "gone"
        d["cancel"] = True
        return progress.state_of(d, now)

    def progress_poke(self) -> None:
        """Update the bars soon (shows "cancelling" without waiting for the 5 s loop)."""
        if self.started:
            asyncio.create_task(self._poke())

    async def _poke(self) -> None:
        try:
            await self.progress_tick(time.time())
        except Exception as e:  # noqa: BLE001 - cosmetic
            log.debug("progress poke: %s", e)

    async def cancel_command(self, inter, run: str | None) -> None:
        """/cancel [run] and the Cancel button. No run: list what's running."""
        now = time.time()
        if not run:
            rows = self.runs(now)
            if not rows:
                await inter.response.send_message("Nothing is running right now.", ephemeral=True)
                return
            lines = [f"`{rid}` {desc}" for rid, desc in rows[:20]]
            await inter.response.send_message(
                "**Running now** (cancel one with `/cancel run:<id>`, or `/cancel run:all`):\n"
                + "\n".join(lines)[:1800], ephemeral=True)
            return
        by = getattr(inter.user, "display_name", None) or str(inter.user.id)
        refs = [rid for rid, _ in self.runs(now)] if run.strip().lower() == "all" else [run]
        if not refs:
            await inter.response.send_message("Nothing is running right now.", ephemeral=True)
            return
        if len(refs) == 1:   # check the id first so a typo gets a quick answer
            try:
                if not refs[0].lower().startswith(QUEUED):
                    progress.resolve(self.data_dir, refs[0], now)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
        await inter.response.send_message(f"🛑 Cancelling {len(refs)} run{'s' if len(refs) > 1 else ''}…",
                                          ephemeral=True)

        async def one(ref: str) -> str:
            try:
                return await self.cancel_run(ref, by)
            except ValueError as e:
                return f"⚠️ {e}"
            except Exception as e:  # noqa: BLE001 - never crash the bot on a command
                log.warning("cancel %s: %s", ref, e)
                return f"⚠️ Couldn't cancel `{ref}`: {e}"

        async def go():
            results = await asyncio.gather(*(one(r) for r in refs))   # several runs stop in parallel
            self.wake.set()
            try:
                await inter.followup.send("\n".join(results)[:1990], ephemeral=True)
            except Exception as e:  # noqa: BLE001
                log.warning("cancel reply: %s", e)
        asyncio.create_task(go())

    async def trend_loop(self, every: float = 60) -> None:
        """Scheduled trending-sound edits (review.trend.times, UK): one edit per slot."""
        while True:
            try:
                await self.trend_tick(time.time())
            except Exception as e:  # noqa: BLE001
                log.warning("trend schedule: %s", e)
            await asyncio.sleep(every)

    async def trend_tick(self, now: float) -> str | None:
        tcfg = self.rcfg.get("trend") or {}
        if not tcfg.get("enabled") or self._trend_running:
            return None
        path = self.data_dir / "trend" / "schedule.json"
        try:
            done = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            done = {}
        slot, skipped = trend_slots_due(list(tcfg.get("times") or []), done, now)
        if not slot:
            return None
        day = datetime.fromtimestamp(now, review.UK).strftime("%Y-%m-%d")
        done = {day: sorted(set(done.get(day, [])) | {slot, *skipped})}   # keep only today
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(done), encoding="utf-8")
        log.info("scheduled trending-sound edit (%s UK)%s", slot,
                 f", skipped missed {', '.join(skipped)}" if skipped else "")
        self._trend_running = True
        asyncio.create_task(self.run_trend(None, "auto", 1))
        return slot

    async def run_daily(self, per_day: int = 0) -> None:
        """`python -m spikebot.tokyo_girl daily` in a subprocess (what the 06:47 timer runs);
        `per_day` raises today's target."""
        cmd = [sys.executable, "-m", "spikebot.tokyo_girl", "--config", self.config_path, "daily"] + \
            (["--per-day", str(per_day)] if per_day else [])
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT, start_new_session=True)
            try:
                out, _ = await asyncio.wait_for(p.communicate(), 3 * 3600)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
                raise RuntimeError("Tokyo Girl top-up took over 3 h and was stopped")
            log.info("tokyo girl daily: %s", out.decode(errors="replace")[-500:].strip())
            self.wake.set()
        except Exception as e:  # noqa: BLE001 - never crash the bot
            log.warning("Tokyo Girl top-up failed: %s", e)

    async def run_trend(self, inter, song: str | None, per_song: int) -> None:
        """`python -m spikebot.tokyo_girl trend` in a subprocess; the scan loop posts the rounds."""
        try:
            await self.run_edits(inter, ["trend", "-n", str(per_song)] + (["--song", song] if song else []),
                                 "Trending-sound edits" + (f" · {song}" if song and song != "auto" else ""), "🎵",
                                 "trending-sound edit", skip_if="no trending sounds")
        finally:
            if song == "auto":
                self._trend_running = False

    async def run_compare(self, inter, n: int = 1) -> None:
        """`python -m spikebot.tokyo_girl compare`: GTA 5 vs GTA 6 comparison edits."""
        await self.run_edits(inter, ["compare", "-n", str(n)], "GTA 5 vs GTA 6" + (f" ×{n}" if n > 1 else ""),
                             "🆚", "comparison edit")

    async def run_edits(self, inter, args: list[str], title: str, icon: str, what: str,
                        skip_if: str | None = None) -> None:
        """A `spikebot.tokyo_girl` batch in a subprocess, with one progress bar from the start (library
        check, sounds/labels, every edit): the renders alone only showed one after the library's
        nudity check, which can take many minutes. `skip_if`: output text that means "nothing to do"."""
        cmd = [sys.executable, "-m", "spikebot.tokyo_girl", "--config", self.config_path] + args
        where = getattr(inter, "channel_id", None) if inter is not None else None
        pj = self.progress_job(title, where, icon)
        ok, why, state = False, "", None
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT, env=progress_env(pj),
                                                     start_new_session=True)
            pj.adopt(p.pid)
            try:
                out, _ = await asyncio.wait_for(p.communicate(), 3 * 3600)
            except asyncio.TimeoutError:
                p.kill()   # don't leave a hung render running forever
                await p.wait()
                raise RuntimeError(f"{what} render took over 3 h and was stopped")
            text = out.decode(errors="replace")
            log.info("%s: %s", what, text[-500:].strip())
            self.wake.set()
            ok = p.returncode == 0
            bad = [ln for ln in text.splitlines() if "FAILED" in ln or "Error" in ln or "error" in ln]
            why = (bad or text.strip().splitlines() or ["?"])[-1][:300]
            skipped = bool(skip_if) and skip_if in text
            if skipped:
                state = "skipped"
            if p.pid in self.force_stopped:
                log.info("%s stopped by /cancel", what)
            elif p.returncode != 0 and inter is None:
                if self.notifier and not skipped:
                    await self.notifier.send(f"{args[0]}_failed", f"{icon} Scheduled {what} failed: "
                                             + (text.strip().splitlines() or ["?"])[-1][:300], kind="lacy")
            elif p.returncode != 0:
                await inter.followup.send((f"⚠️ Some {what}s failed:\n"
                                           + ("\n".join(bad[-5:]) or text[-300:]))[:1990])
        except Exception as e:  # noqa: BLE001 - never crash the bot on a command
            log.warning("%s failed: %s", what, e)
            why = str(e)[:300]
            try:
                if inter is not None:
                    await inter.followup.send(f"⚠️ {what.capitalize()}s failed: {e}"[:1990])
            except Exception:  # noqa: BLE001
                pass
        finally:
            pj.finish(ok, "" if ok else why, state)

    async def analyse(self, name: str, copy: list[str], url: str | None = None, file: Path | None = None,
                      note: str = "", channel_id: int | None = None, kind: str | None = None) -> dict:
        out = self.data_dir / "inspiration" / f"result_{uuid.uuid4().hex}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "spikebot.inspiration", "--config", self.config_path, "analyse",
               "--name", name, "--copy", ",".join(copy), "--json", str(out), "--note", note[:800]]
        cmd += ["--url", url] if url else ["--file", str(file)]
        if kind:
            cmd += ["--kind", kind]
        pj = self.progress_job(f"Studying `{name}`", channel_id, "🔎") if channel_id else None
        res: dict = {}
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT, env=progress_env(pj),
                                                     start_new_session=True)
            if pj:
                pj.adopt(p.pid)
            text, _ = await asyncio.wait_for(p.communicate(), 1200)
            if out.exists():
                res = json.loads(out.read_text(encoding="utf-8"))
            else:
                res = {"ok": False, "error": text.decode(errors="replace")[-300:]}
            if pj and pj.cancel_requested():
                res = {"ok": False, "cancelled": True, "error": "cancelled"}
            return res
        except asyncio.TimeoutError:
            res = {"ok": False, "error": "analysis took over 20 minutes"}
            return res
        finally:
            if pj:   # a failed download asks for an upload: that's not a failure to keep on screen
                pj.finish(bool(res.get("ok")) or bool(res.get("ask_upload")), res.get("error") or "")
            out.unlink(missing_ok=True)
            if file is not None:
                Path(file).unlink(missing_ok=True)

    def learn_feedback(self, text: str, mode: str = "all", client=None) -> tuple[dict, list[str]]:
        """/feedback: read it (Claude, or the rule parser), save a rule, pin its settings.
        Runs in a thread (the API call takes a few seconds)."""
        with self.profile_lock:
            profile = self.store.profile()
            rule, problems = fb.learn(text, mode, profile, self.base, client=client)
            profile.save()
        self.rules_wake.set()   # any note it saved is compiled straight away
        return rule, problems

    # ---- rule compiler: notes -> settings / new-code requests (spikebot/rules.py) ---------
    def compile_rules(self, client=None, now: float | None = None) -> list[dict]:
        """One pass: read pending notes against the current editor, apply the verdicts, export
        what still needs code and file issues for it. Blocking (runs in a thread)."""
        from . import rules as rules_
        version, results = rules_.decide(self.store.profile(), self.base, client=client)
        changes = []
        with self.profile_lock:
            profile = self.store.profile()   # fresh: a render may have saved in between
            if results:
                changes = rules_.apply(profile, version, results, now=now)
            token = rules_.github_token(self.base)
            repo = (self.rcfg.get("rules") or {}).get("github_repo") or rules_.REPO
            for e in rules_.needs_code(profile.entries()):
                last = e.get("issue_try")
                if token and not e.get("issue") and (last is None or (now or time.time()) - last > 3600):
                    e["issue_try"] = now or time.time()
                    try:
                        e["issue"] = rules_.file_issue(e, token, repo)
                        for c in changes:
                            if c["n"] == e["n"]:
                                c["issue"] = e["issue"]
                    except (OSError, ValueError, KeyError) as err:
                        log.warning("rule #%s: could not file a GitHub issue: %s", e["n"], err)
            profile.save()
            rules_.export(profile.entries(), self.data_dir)
        return changes

    @staticmethod
    def compile_notice(changes: list[dict], token: bool) -> str:
        lines = []
        for c in changes:
            short = (c.get("text") or "")[:80]
            if c["verdict"] == "settings" and c["added"] and not c.get("needs"):
                lines.append(f"✅ Rule #{c['n']} ({short}) is now applied: " + ", ".join(f"#{n}" for n in c["added"]))
            elif c["verdict"] == "built_in":
                lines.append(f"✅ Rule #{c['n']} ({short}): the editor already does this")
            elif c["verdict"] == "needs_code":
                where = (f"filed as issue #{c['issue']}, the daily Claude Code job builds it" if c.get("issue")
                         else "queued in data/rules/needs_code.json" + ("" if token else
                              " (add GITHUB_TOKEN to .env so it files itself as an issue)"))
                lines.append(f"🛠️ Rule #{c['n']} ({short}) needs new editor code: {where}")
        return "\n".join(lines)

    async def rules_loop(self, every: float = 120) -> None:
        """Compile saved rules: at start (every note already saved), after each /feedback, and
        every few minutes (Feedback-button notes, newly deployed editor settings)."""
        from . import rules as rules_
        backoff = every
        seen = max((e["n"] for e in self.store.profile().entries()), default=0)   # announce only new ones
        while True:
            try:
                seen = await self.announce_new_rules(seen)
            except Exception as e:  # noqa: BLE001
                log.warning("announcing new rules failed: %s", e)
            try:
                changes = await asyncio.to_thread(self.compile_rules)
                backoff = every
                text = self.compile_notice(changes, bool(rules_.github_token(self.base)))
                if text:
                    log.info("rules compiled:\n%s", text)
                    await self.announce(f"rules:{time.time():.0f}", text)
            except ai.AIError as e:
                backoff = min(backoff * 2, 3600)
                log.warning("rule compiler: %s (next try in %.0f s)", e, backoff)
            except Exception as e:  # noqa: BLE001 - never take the bot down
                backoff = min(backoff * 2, 3600)
                log.warning("rule compiler failed: %s", e)
            try:
                await asyncio.wait_for(self.rules_wake.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            self.rules_wake.clear()

    # ---- one-off requests shipped with a deploy (spikebot/deploy_requests.json) -------------
    async def run_deploy_requests(self, now: float | None = None, path: Path | None = None) -> list[str]:
        """Run each request in the deployed code once, after the restart, if it hasn't expired.
        Only whitelisted actions: `trend`, `daily` (top up today's Tokyo Girl edits), and `feedback` (a Feedback re-render of one version,
        as if Tim had pressed the button: {item, round, label, notes}; the result posts in that
        item's thread in #ready-to-post). Done ids live in data/deploy_requests.json."""
        path = path or Path(__file__).with_name("deploy_requests.json")
        state = self.data_dir / "deploy_requests.json"
        now = now or time.time()
        try:
            reqs = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        try:
            done = set(json.loads(state.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            done = set()
        ran = []
        for r in reqs if isinstance(reqs, list) else []:
            rid = str(r.get("id") or "")
            if not rid or rid in done or r.get("action") not in ("trend", "feedback", "repost", "daily", "announce"):
                continue
            done.add(rid)   # once, even if it is stale or fails
            until = r.get("until")
            if until and now > datetime.fromisoformat(until).timestamp():
                log.info("deploy request %s expired", rid)
                continue
            log.info("deploy request %s: %s", rid, r["action"])
            ran.append(rid)
            if r["action"] == "trend":
                asyncio.create_task(self.run_trend(None, r.get("song"), int(r.get("per_song") or 1)))
            elif r["action"] == "announce":   # what a deploy changed, for Tim
                await self.announce(f"deploy:{rid}", str(r.get("text") or ""))
            elif r["action"] == "daily":   # top today's Tokyo Girl edits up to per_day now
                asyncio.create_task(self.run_daily(int(r.get("per_day") or 0)))
            elif r["action"] == "repost":
                try:
                    await self.repost_latest(r["item"])
                except Exception as e:  # noqa: BLE001
                    log.warning("deploy request %s failed: %s", rid, e)
                    if self.notifier:
                        await self.notifier.send(f"deploy_request:{rid}",
                                                 f"🎬 Couldn't re-post `{r.get('item')}`: {e}", kind="lacy")
            elif r["action"] == "feedback":
                try:
                    if not (self.store.dir(r["item"]) / "item.json").exists():
                        raise ValueError("no such review item")
                    with self.store.edit(r["item"]) as it:
                        review.request(it, "feedback", now, round=int(r["round"]), label=str(r["label"]),
                                       notes=str(r["notes"]))
                    self.wake.set()   # the scan loop queues the render; the round posts in the thread
                except Exception as e:  # noqa: BLE001 - closed item, a render already running...
                    log.warning("deploy request %s failed: %s", rid, e)
                    if self.notifier:
                        await self.notifier.send(f"deploy_request:{rid}",
                                                 f"🎬 Re-render `{r.get('item')}` {r.get('label')} could not start: {e}",
                                                 kind="lacy")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(sorted(done)), encoding="utf-8")
        return ran

    async def report_error(self, inter, error: BaseException, what: str) -> None:
        """Log the traceback and tell Tim what failed (Discord's own message says nothing)."""
        log.error("%s failed", what, exc_info=(type(error), error, error.__traceback__))
        text = f"⚠️ {what} failed: {type(error).__name__}: {error}"[:1900]
        try:
            if inter.response.is_done():
                await inter.followup.send(text, ephemeral=True)
            else:
                await inter.response.send_message(text, ephemeral=True)
        except Exception as e:  # noqa: BLE001 - the interaction may have expired
            log.warning("could not report the error to Discord: %s", e)

    # ---- the rules list ----------------------------------------------------------------
    def entry_states(self) -> dict[int, bool]:
        return {e["n"]: e.get("on", True) for e in self.store.profile().entries()}

    def toggle_entries(self, numbers: list[int], on: bool) -> tuple[list[int], list[int], list[dict]]:
        """Switch entries; returns (changed, not found, all entries). Thread-safe with /feedback."""
        with self.profile_lock:
            profile = self.store.profile()
            changed, missing = profile.set_enabled(numbers, on)
            profile.save()
            return changed, missing, list(profile.entries())

    @staticmethod
    def toggle_notice(changed: list[int], missing: list[int], on: bool) -> str:
        bits = []
        if changed:
            bits.append(("✅ Turned on " if on else "⛔ Turned off ") + ", ".join(f"#{n}" for n in changed))
        if missing:
            bits.append("No entry " + ", ".join(f"#{n}" for n in missing))
        return " · ".join(bits) or ("Already on." if on else "Already off.")

    async def on_rules_click(self, inter, data: dict) -> None:
        """Turn off / Turn on next to a rule in /rules (and Prev / Next), and the buttons under
        a /feedback reply or a feedback re-render card."""
        act = data["action"]
        await inter.response.defer()   # answer within Discord's 3 s; the message is edited below
        if act == "rulesp":
            entries = list((await asyncio.to_thread(self.store.profile)).entries())
            await inter.edit_original_response(view=self.layout(self.rules_view(entries, data, "")))
            return
        if act in ("rules_off", "rules_on"):   # menus on /rules messages from before the buttons
            values = [int(v) for v in (getattr(inter, "data", None) or {}).get("values") or [] if str(v).isdigit()]
            changed, missing, _ = await asyncio.to_thread(self.toggle_entries, values, act == "rules_on")
            await inter.followup.send(self.toggle_notice(changed, missing, act == "rules_on")
                                      + " · run `/rules` for the new list with a button on each rule.", ephemeral=True)
            return
        ns = [int(x) for x in str(data["label"]).split(".") if x.isdigit()]   # merged duplicates: all of them
        prof = await asyncio.to_thread(self.store.profile)
        group = [x for x in (prof.entry(k) for k in ns) if x]
        if not group:
            await inter.followup.send(f"No entry #{data['label']}.", ephemeral=True)
            return
        e = max(group, key=lambda x: x["n"])
        n = e["n"]
        if act == "rulem":   # "Applies to" ticks
            values = [v for v in (getattr(inter, "data", None) or {}).get("values") or [] if v in st.STYLE_MODES]
            try:
                for x in group:
                    modes, entries = await asyncio.to_thread(self.scope_entry, x["n"], values)
                nums = ", ".join(f"#{x['n']}" for x in group)
                notice = f"✅ {nums} now applies to **{st.modes_label(modes)}**"
            except ValueError as err:
                entries, notice = list(self.store.profile().entries()), f"⚠️ {err}"
            await inter.edit_original_response(view=self.layout(self.rules_view(entries, data, notice)))
            return
        on = not any(x.get("on", True) for x in group)
        changed, missing, entries = await asyncio.to_thread(self.toggle_entries, [x["n"] for x in group], on)
        notice = self.toggle_notice(changed, missing, on) + f": {rule_title(e)[0]}"
        if act == "rulep":
            await inter.edit_original_response(view=self.layout(self.rules_view(entries, data, notice)))
            return
        rows = flip_rows(self.rows_of(inter.message), n, on)
        await inter.edit_original_response(view=self.view(rows))
        await inter.followup.send(notice, ephemeral=True)

    def scope_entry(self, n: int, modes: list[str]) -> tuple[list[str], list[dict]]:
        with self.profile_lock:
            profile = self.store.profile()
            ok = profile.set_modes(n, modes)
            profile.save()
            return ok, list(profile.entries())

    @staticmethod
    def rules_view(entries: list[dict], data: dict, notice: str) -> dict:
        """Redraw what was clicked: a /rules page, or a feedback reply's own rules."""
        if data["item"] in ("-", ""):
            return rules_page(entries, data["round"], notice)
        if data["item"].startswith("s:"):
            return rules_page(entries, data["round"], notice, data["item"][2:])
        nums = [int(x) for x in data["item"].split(".") if x.isdigit()]
        return rules_subset(entries, nums, notice)

    def saved_rules_view(self, rule: dict, head: str) -> dict:
        return rules_subset(list(self.store.profile().entries()), [e["n"] for e in rule["entries"]], head)

    async def rules_command(self, inter, disable: str | None = None, enable: str | None = None,
                            search: str | None = None) -> None:
        """/rules: the list with a button beside every rule. Answers straight away (no
        "thinking"); disable:/enable: switch several at once first."""
        notice = []
        if disable or enable:
            await inter.response.defer(thinking=True)   # switching may wait for a /feedback being saved
        for text, on in ((disable, False), (enable, True)):
            if text:
                nums = st.parse_numbers(text)
                if not nums:
                    notice.append(f"⚠️ No numbers in `{text}` (e.g. `3, 5-7`).")
                    continue
                changed, missing, _ = await asyncio.to_thread(self.toggle_entries, nums, on)
                notice.append(self.toggle_notice(changed, missing, on))
        entries = list(self.store.profile().entries())
        await self.send_rules(inter, rules_page(entries, 0, " · ".join(notice), search or ""), entries)

    async def send_rules(self, inter, spec: dict, entries: list[dict]) -> None:
        """Send one page; if Discord refuses it, send the whole list as a text file instead."""
        import io

        import discord

        async def send(**kw):
            if inter.response.is_done():
                await inter.followup.send(**kw)
            else:
                await inter.response.send_message(**kw)
        try:
            await send(view=self.layout(spec))
        except discord.HTTPException as e:
            log.warning("/rules refused by Discord (%s %s): %d entries, %d blocks", e.status, e.text[:200],
                        len(entries), len(spec["blocks"]))
            await send(content="Discord wouldn't show the list with buttons, so here it is as a file. Turn"
                               " entries off with `/rules disable:3, 5-7`.",
                       file=discord.File(io.BytesIO(rules_text(entries).encode()), filename="rules.txt"))

    # ---- slash commands -------------------------------------------------------------
    def register_commands(self, tree) -> None:
        import discord
        from discord import app_commands
        bot = self

        async def guard(inter) -> bool:
            if not bot.allowed(inter.user):
                await inter.response.send_message("Only Tim can use this.", ephemeral=True)
                return False
            return True

        async def preset_names(inter, current: str):
            names = bot.store.presets().names()
            return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n][:25]

        async def setting_choices(inter, current: str):
            return [app_commands.Choice(name=s, value=s) for s in STYLE_SETTINGS if current.lower() in s][:25]

        async def value_choices(inter, current: str):
            setting = getattr(inter.namespace, "setting", "") or ""
            opts = style_options(setting) + ["auto"]
            return [app_commands.Choice(name=o, value=o) for o in opts if current.lower() in o][:25]

        @tree.command(name="style", description="Show the learned style settings, or pin one")
        @app_commands.autocomplete(setting=setting_choices, value=value_choices)
        async def style_cmd(inter: discord.Interaction, setting: str | None = None, value: str | None = None):
            if not await guard(inter):
                return
            profile = bot.store.profile()
            if setting:
                if setting not in STYLE_SETTINGS:
                    await inter.response.send_message(f"Unknown setting. Try: {', '.join(STYLE_SETTINGS)}",
                                                      ephemeral=True)
                    return
                if value not in (None, "auto") and value not in style_options(setting):
                    await inter.response.send_message(f"{setting} options: {', '.join(style_options(setting))}, auto",
                                                      ephemeral=True)
                    return
                with bot.profile_lock:
                    profile = bot.store.profile()
                    entry = profile.pin(setting, value)
                    profile.save()
            text = style_overview(profile, bot.store.presets().all())
            if setting:
                text = (f"📌 {setting} → **{value}** (rule #{entry['n']})\n" if value not in (None, "auto")
                        else f"↩️ {setting} back to learning (rule #{entry['n']})\n") + text
            await inter.response.send_message(text[:1990], ephemeral=False)

        @tree.command(name="feedback", description="Tell the editor anything (plain words); it applies to every future edit")
        @app_commands.describe(text="What to change, in your own words",
                               mode="Which edits it's about (default: all)")
        @app_commands.choices(mode=[app_commands.Choice(name="All edits", value="all"),
                                    app_commands.Choice(name="Lacy clips", value="lacy"),
                                    app_commands.Choice(name="Tokyo Girl trailer edits", value="tokyo_girl"),
                                    app_commands.Choice(name="Trend edits (trending sounds)", value="trend")])
        async def feedback_cmd(inter: discord.Interaction, text: str, mode: str = "all"):
            if not await guard(inter):
                return
            await inter.response.defer(thinking=True)
            try:
                rule, problems = await asyncio.to_thread(bot.learn_feedback, text, mode)
            except Exception as e:  # noqa: BLE001 - never crash the bot on a command
                log.warning("/feedback failed: %s", e)
                await inter.followup.send(f"⚠️ Couldn't save that: {e}"[:1990])
                return
            msg = ["📝 Saved for every future edit" + (" (read by Claude)" if rule.get("engine") == "claude"
                                                      else " (rule parser: set ANTHROPIC_API_KEY for full understanding)"),
                   fb.format_rule(rule)]
            if problems:
                msg.append("Skipped: " + " | ".join(p[:100] for p in problems[:3]))
            if not rule["entries"]:
                msg.append("Nothing changed: try saying what to do differently, e.g. *no captions unless he says"
                           " something funny*.")
            if rule["entries"]:
                msg.append("Each rule below says what it applies to: change the ticks, or turn it off."
                           " `/rules` shows the whole list.")
                await inter.followup.send(view=bot.layout(bot.saved_rules_view(rule, "\n".join(msg))))
            else:   # discord.py refuses view=None on a follow-up
                await inter.followup.send("\n".join(msg)[:1990])

        @tree.command(name="trend", description="Make trailer edits cut to your trending sounds (#ready-to-post)")
        @app_commands.describe(song="Only this sound (default: every trending sound)", per_song="Edits per sound (1-3)")
        async def trend_cmd(inter: discord.Interaction, song: str | None = None, per_song: int = 1):
            if not await guard(inter):
                return
            per_song = max(1, min(3, per_song))
            await inter.response.send_message(
                f"🎬 Making trending-sound edits ({per_song} per sound{', ' + song if song else ''}). Each one"
                " appears here in #ready-to-post with 3 versions when it's rendered (a few minutes each).")
            asyncio.create_task(bot.run_trend(inter, song, per_song))

        @tree.command(name="compare", description="GTA 5 vs GTA 6 comparison edits (#ready-to-post, 3 versions each)")
        @app_commands.describe(count="How many comparison edits (1-3)")
        async def compare_cmd(inter: discord.Interaction, count: int = 1):
            if not await guard(inter):
                return
            count = max(1, min(3, count))
            await inter.response.send_message(
                f"🆚 Making {count} GTA 5 vs GTA 6 comparison edit{'s' if count > 1 else ''}. The first one also"
                " downloads Rockstar's GTA 5 trailers and matches the shots, so it can take a while; the bar below"
                " shows where it is.")
            asyncio.create_task(bot.run_compare(inter, count))

        @tree.command(name="stats", description="Refresh the 📊 Stats channels now")
        async def stats_cmd(inter: discord.Interaction):
            if not await guard(inter):
                return
            await inter.response.send_message("📊 Refreshing the stats channels (Discord allows 2 renames per"
                                              " channel every 10 minutes, so a name may lag).", ephemeral=True)

            async def go():
                try:
                    if not await bot.stats_tick(time.time(), force=True):
                        await inter.followup.send("⚠️ Couldn't update them: check the bot has **Manage Channels**.",
                                                  ephemeral=True)
                except Exception as e:  # noqa: BLE001
                    log.warning("/stats: %s", e)
            asyncio.create_task(go())

        async def run_choices(inter, current: str):
            rows = bot.runs(time.time())
            opts = [app_commands.Choice(name=f"{rid} · {desc}"[:100], value=rid) for rid, desc in rows
                    if current.lower() in f"{rid} {desc}".lower()][:24]
            if len(rows) > 1 and "all".startswith(current.lower()):
                opts.append(app_commands.Choice(name=f"all ({len(rows)} runs)", value="all"))
            return opts

        @tree.command(name="cancel", description="Stop a running render, trend edit, Lacy clip, analysis or download")
        @app_commands.describe(run="Run id from the progress bar (leave empty to list what's running; 'all' = every run)")
        @app_commands.autocomplete(run=run_choices)
        async def cancel_cmd(inter: discord.Interaction, run: str | None = None):
            if not await guard(inter):
                return
            await bot.cancel_command(inter, run)

        @tree.command(name="rules", description="Every instruction the editor follows, numbered; turn any off or on")
        @app_commands.describe(search="Words to find, e.g. cut, text, colour, logo, #12",
                               disable="Numbers to turn off, e.g. 3, 5-7", enable="Numbers to turn back on")
        async def rules_cmd(inter: discord.Interaction, search: str | None = None, disable: str | None = None,
                            enable: str | None = None):
            if not await guard(inter):
                return
            await bot.rules_command(inter, disable, enable, search)

        @tree.command(name="help", description="Every command and button, what it does and how to use it")
        @app_commands.describe(category="Only one group of commands", search="Words to find, e.g. caption, stop, preset")
        @app_commands.choices(category=[app_commands.Choice(name=label, value=key)
                                        for key, _, label in help_.CATEGORIES])
        async def help_cmd(inter: discord.Interaction, category: str | None = None, search: str | None = None):
            parts = help_.chunks(help_.help_text(category, search))
            await inter.response.send_message(parts[0], ephemeral=True)
            for p in parts[1:]:
                await inter.followup.send(p, ephemeral=True)

        group = app_commands.Group(name="preset", description="Style presets from #inspiration")

        @group.command(name="apply", description="Re-render a clip with a preset (use it in the clip's thread)")
        @app_commands.autocomplete(name=preset_names)
        async def apply(inter: discord.Interaction, name: str, variant: str | None = None, item: str | None = None):
            if not await guard(inter):
                return
            it = None
            if item:
                try:
                    it = bot.store.load(item)
                except (FileNotFoundError, ValueError):
                    pass
            else:
                it = next((x for x in bot.store.all() if x.get("thread_id") == inter.channel_id), None)
            if it is None:
                await inter.response.send_message("Use this inside a #ready-to-post clip thread, or give `item:`.",
                                                  ephemeral=True)
                return
            rnd = review.latest_round(it)
            label = (variant or rnd["variants"][0]["label"]).upper()
            try:
                bot.store.presets().load(name)
                review.variant_of(it, rnd["n"], label)
                with bot.store.edit(it["id"]) as x:
                    review.request(x, "preset", round=rnd["n"], label=label, name=name)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            await inter.response.send_message(f"🎨 Re-rendering **{label}** with preset `{name}`…")
            bot.wake.set()

        @group.command(name="list", description="All presets, most approved first")
        async def list_(inter: discord.Interaction):
            if not await guard(inter):
                return
            ps = bot.store.presets().all()
            if not ps:
                await inter.response.send_message("No presets yet: post a link in #inspiration.", ephemeral=True)
                return
            prof = bot.store.profile()
            rows = prof.data["presets"]
            names = prof.top_presets(list(ps), n=len(ps))
            text = "\n".join(f"• **{n}** ({', '.join(ps[n].get('copy') or [])}) · approved "
                             f"{rows.get(n, {}).get('approved', 0)}/{rows.get(n, {}).get('shown', 0)}" for n in names)
            await inter.response.send_message(text[:1990])

        @group.command(name="show", description="Settings of one preset")
        @app_commands.autocomplete(name=preset_names)
        async def show(inter: discord.Interaction, name: str):
            if not await guard(inter):
                return
            try:
                p = bot.store.presets().load(name)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            row = bot.store.profile().data["presets"].get(p["name"])
            await inter.response.send_message(presets_.summary(p, row)[:1990])

        @group.command(name="rename", description="Rename a preset")
        @app_commands.autocomplete(name=preset_names)
        async def rename(inter: discord.Interaction, name: str, new_name: str):
            if not await guard(inter):
                return
            try:
                p = bot.store.presets().rename(name, new_name)
                prof = bot.store.profile()
                if name in prof.data["presets"]:
                    prof.data["presets"][p["name"]] = prof.data["presets"].pop(name)
                    prof.save()
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            await inter.response.send_message(f"Renamed `{name}` → `{p['name']}`.")

        @group.command(name="edit", description="Change one setting (e.g. pace fast, saturation 1.3, copy pacing,colour)")
        @app_commands.autocomplete(name=preset_names)
        async def edit(inter: discord.Interaction, name: str, setting: str, value: str):
            if not await guard(inter):
                return
            try:
                p = bot.store.presets().edit(name, setting, value)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            await inter.response.send_message(presets_.summary(p)[:1990])

        @group.command(name="delete", description="Delete a preset")
        @app_commands.autocomplete(name=preset_names)
        async def delete(inter: discord.Interaction, name: str):
            if not await guard(inter):
                return
            try:
                bot.store.presets().delete(name)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            await inter.response.send_message(f"Deleted preset `{name}`.")

        @group.command(name="combine", description="New preset from parts of others (e.g. pacing from one, colour from another)")
        @app_commands.autocomplete(pacing=preset_names, colour=preset_names, text=preset_names,
                                   transitions=preset_names, hook=preset_names, structure=preset_names,
                                   music_sync=preset_names)
        async def combine(inter: discord.Interaction, new_name: str, pacing: str | None = None,
                          colour: str | None = None, text: str | None = None, transitions: str | None = None,
                          hook: str | None = None, structure: str | None = None, music_sync: str | None = None):
            if not await guard(inter):
                return
            parts = {k: v for k, v in (("pacing", pacing), ("colour", colour), ("text", text),
                                        ("transitions", transitions), ("hook", hook), ("structure", structure),
                                        ("music-sync", music_sync)) if v}
            try:
                p = bot.store.presets().combine(new_name, parts)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            await inter.response.send_message(presets_.summary(p)[:1990])

        tree.add_command(group)
