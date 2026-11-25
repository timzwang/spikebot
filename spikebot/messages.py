"""Discord message layouts for #moments, #status and #daily-summary.

Pure formatting, no networking, so every layout is testable. Each builder
returns a webhook payload dict (embeds) that `post_discord()` sends as-is.

Times use Discord's <t:unix:style> markup, which every viewer sees in their own
local time. `plain()` turns those back into UK time for the log.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

UK = ZoneInfo("Europe/London")

GREY, ORANGE, RED = 0x95A5A6, 0xE67E22, 0xE74C3C
GREEN, BLUE, BLURPLE = 0x2ECC71, 0x3498DB, 0x5865F2

# Colours for #status alerts, by alert kind.
STATUS_COLOURS = {"start": BLUE, "live": GREEN, "offline": GREY, "reconnected": GREEN,
                  "disconnected": RED, "key_error": RED, "disk": ORANGE, "lacy": ORANGE}

NO_MENTIONS = {"parse": []}  # chat text must never ping @everyone


def score_colour(score: float) -> int:
    """Grey below 3, orange 3 to 5, red above 5."""
    if score > 5:
        return RED
    return ORANGE if score >= 3 else GREY


def score_icon(score: float) -> str:
    if score > 5:
        return "🔥"
    return "⚡" if score >= 3 else "💬"


def ts(unix: float, style: str = "t") -> str:
    """Discord timestamp: t = 02:53, f = 16 November 2026 02:53, R = 5 minutes ago."""
    return f"<t:{int(unix)}:{style}>"


_TS_RE = re.compile(r"<t:(\d+):([tTdDfFR])>")
_TS_FMT = {"t": "%H:%M", "T": "%H:%M:%S", "d": "%d/%m/%Y", "D": "%d %b %Y",
           "f": "%a %d %b %H:%M", "F": "%a %d %b %Y %H:%M", "R": "%a %d %b %H:%M"}


def plain(payload: str | dict) -> str:
    """One-line log version of a message, with Discord timestamps as UK time."""
    if isinstance(payload, dict):
        parts = [payload.get("content") or ""]
        for e in payload.get("embeds", []):
            parts += [e.get("title") or "", e.get("description") or ""]
        text = "\n".join(p for p in parts if p)
    else:
        text = payload

    def sub(m):
        dt = datetime.fromtimestamp(int(m.group(1)), UK)
        return dt.strftime(_TS_FMT[m.group(2)]) + " " + dt.strftime("%Z")
    return _TS_RE.sub(sub, text).replace("\n", " | ")


_MD_RE = re.compile(r"([\\*_~`|>\[\]()#-])")


def escape(text: str) -> str:
    """Escape Discord markdown in chat text."""
    return _MD_RE.sub(r"\\\1", text)


def multiplier(peak: float, baseline: float) -> float:
    return round(peak / max(baseline, 0.2), 1)


def vod_offset(seconds: float, platform: str) -> str:
    """Query string for a VOD at `seconds` in: Twitch ?t=1h02m03s, Kick ?t=3723."""
    s = max(int(seconds), 0)
    if platform == "twitch":
        return f"?t={s // 3600}h{s % 3600 // 60:02d}m{s % 60:02d}s"
    return f"?t={s}"


def live_url(platform: str, channel: str) -> str:
    return f"https://kick.com/{channel}" if platform == "kick" else f"https://www.twitch.tv/{channel}"


def embed(description: str, colour: int, title: str | None = None, url: str | None = None) -> dict:
    e: dict = {"description": description[:4000], "color": colour}
    if title:
        e["title"] = title[:256]
    if url:
        e["url"] = url
    return {"embeds": [e], "allowed_mentions": NO_MENTIONS}


# --------------------------------------------------------------------------
# #moments
# --------------------------------------------------------------------------
def vibe_line(rec: dict) -> str | None:
    words = rec.get("vibe") or []
    if words:
        return " · ".join(escape(w) for w in words[:4])
    # older records / chat without hype words: the most repeated short messages
    short = [s for s in rec.get("sample") or [] if s][:3]
    if not short:
        return None
    return " · ".join(f'"{escape(s[:40])}"' for s in short)


def moment(rec: dict, display: str) -> dict:
    """The #moments alert. `rec` is the stored spike record; optional keys
    (vod_url, stream_offset_start, category, vibe) are left out when missing."""
    score = rec["score"]
    mult = multiplier(rec["peak_rate"], rec["baseline"])
    title = f"{score_icon(score)} {display} · {mult}x chat spike · score {score}"

    offset = rec.get("stream_offset_start")
    if rec.get("vod_url"):
        link = rec["vod_url"]
        watch = f"**[▶ Watch moment]({link})**"
        if offset:
            watch += f" · {offset} into stream"
    else:
        link = live_url(rec["platform"], rec["channel"])
        watch = f"**[▶ Open stream]({link})** · VOD not ready"
        if offset:
            watch += f", moment at {offset}"

    lines = [watch, "",
             f"**Chat** {rec['peak_rate']}/s (normal {rec['baseline']}/s)",
             f"**Chatters** {rec['unique_users']} · {rec['clip_mentions']} said \"clip\""]
    vibe = vibe_line(rec)
    if vibe:
        lines.append(f"**Vibe** {vibe}")
    if rec.get("rank") and rec.get("rank_of", 0) > 1:
        lines.append(f"**Rank** #{rec['rank']} of {rec['rank_of']} moments across all streamers (last hour)")

    prog = rec.get("programme")
    foot = [prog if prog else "⚠️ **No programme: check permission before posting**",
            rec["platform"].capitalize()]
    if rec.get("category"):
        foot.append(escape(rec["category"]))
    foot.append(ts(rec["onset"]))
    lines += ["", " · ".join(foot)]
    return embed("\n".join(lines), score_colour(score), title=title, url=link)


# --------------------------------------------------------------------------
# Lacy mode (#moments): review card and upload confirmation
# --------------------------------------------------------------------------
APPROVE_CMD = "cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy"


def lacy_review(job: dict, uploads_today: int, max_uploads: int) -> dict:
    """Card for a rendered Lacy Short, posted with the small preview video."""
    lines = [f"**Title** {escape(job.get('title') or '')}",
             f"**Game** {escape(job.get('category') or '')} · {job.get('duration', '?')}s"
             f" · chat score {job.get('score')}"]
    if job.get("vod_link"):
        lines.append(f"**[▶ Source VOD]({job['vod_link']})**")
    lines += ["", "Check before approving: no cutscene, ending or story spoiler (Rockstar policy),"
              " Lacy on screen, no gambling.",
              f"Uploads today: **{uploads_today}/{max_uploads}**"]
    if job.get("reason"):
        lines.append(f"⚠️ {escape(job['reason'])}")
    lines += ["", f"`{APPROVE_CMD} approve {job['id']}`",
              f"`{APPROVE_CMD} reject {job['id']}`", "", ts(job["onset"], "f")]
    return embed("\n".join(lines), BLURPLE, title="🎬 Lacy Short ready for review")


def lacy_uploaded(job: dict) -> dict:
    lines = [f"**{escape(job.get('title') or '')}**", job.get("youtube_url") or "",
             "Logged to lacy_submissions.csv: paste the link into Content Rewards."]
    if job.get("reason"):
        lines.append(f"⚠️ {escape(job['reason'])}")
    return embed("\n".join(lines), GREEN, title="✅ Lacy Short uploaded",
                 url=job.get("youtube_url"))


# --------------------------------------------------------------------------
# #status
# --------------------------------------------------------------------------
def status(kind: str, text: str) -> dict:
    return embed(text, STATUS_COLOURS.get(kind, GREY))


# --------------------------------------------------------------------------
# #daily-summary
# --------------------------------------------------------------------------
def summary(rows: list[dict], channels: list[tuple[str, str]], now: float,
            names: dict | None = None, window_s: float = 86400, cpu: dict | None = None,
            cores: int | None = None, filtered: dict | None = None, ai_line: str | None = None) -> dict:
    """rows: candidate moments in the window (platform, channel, score, onset and,
    when known, peak_rate, baseline, vod_url). channels: every tracked (platform, name)."""
    names = names or {}

    def name(k):
        return names.get(k) or f"{k[0]}/{k[1]}"

    counts = {c: 0 for c in channels}
    for r in rows:
        k = (r["platform"], r["channel"])
        counts[k] = counts.get(k, 0) + 1
    lines = [f"{ts(now - window_s, 'f')} to {ts(now, 't')}",
             f"**{len(rows)}** moments sent to #moments", ""]
    for k, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"• {name(k)}: {c}")
    top = sorted(rows, key=lambda r: r["score"], reverse=True)[:3]
    lines += ["", "**Top 3 by score**"]
    if not top:
        lines.append("No moments today.")
    for i, r in enumerate(top, 1):
        bits = [f"**{name((r['platform'], r['channel']))}**", f"score {r['score']}"]
        if r.get("peak_rate") is not None and r.get("baseline") is not None:
            bits.append(f"{multiplier(r['peak_rate'], r['baseline'])}x")
        bits.append(ts(r["onset"]))
        if r.get("vod_url"):
            bits.append(f"[▶ VOD]({r['vod_url']})")
        elif r.get("stream_offset_start"):
            bits.append(f"stream time {r['stream_offset_start']}")
        lines.append(f"{i}. " + " · ".join(bits))
    f = filtered or {}
    if f.get("chat") or f.get("clips"):
        bits = []
        if f.get("chat"):
            bits.append(f"{f['chat']} chat surge{'s' if f['chat'] != 1 else ''} caused only by chat answering the"
                        " streamer, subs or raids (no alert, no clip)")
        if f.get("clips"):
            bits.append(f"{f['clips']} clip{'s' if f['clips'] != 1 else ''} judged low impact before editing")
        lines += ["", "🧹 **Filtered out**: " + "; ".join(bits)]
    if cpu:
        lines += ["", cpu_line(cpu, cores)]
    if ai_line:   # what the Claude API cost today (spikebot/ai.py ledger)
        lines += ["", ai_line]
    return embed("\n".join(lines), BLURPLE, title="📊 Daily summary")


def cpu_line(cpu: dict, cores: int | None = None) -> str:
    """The server's CPU over the day and whether a bigger VM looks worth it."""
    from .status import duration
    busy = cpu["busy_s"]
    head = (f"**Server CPU**{f' ({cores} cores)' if cores else ''}: average {min(cpu['avg'], 9.99):.0%},"
            f" peak {min(cpu['peak'], 9.99):.0%} at {ts(cpu['peak_at'], 't')}, maxed out for "
            + (duration(busy) if busy else "no time at all"))
    if cpu["upgrade"]:
        return head + ("\n⚠️ Maxed out for a long time today. If stream clips reached #ready-to-post late,"
                       " a bigger VM (paid from trial credit, scaled back before 14 Feb) would help.")
    return head + "\n✅ Plenty of headroom: no upgrade needed."
