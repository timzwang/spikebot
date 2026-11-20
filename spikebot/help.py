"""/help: every Discord command, button and channel action, by category, searchable.

Pure (no discord import) so it is testable. `tests/test_help.py` checks every slash command
the bot registers is listed here, so a new command without a help line fails the tests.
"""
from __future__ import annotations

import re

# (key, heading shown in Discord, choice label for the category: option)
CATEGORIES = [
    ("style", "🎨 Editing style & rules", "Editing style & rules"),
    ("make", "🎬 Making & stopping edits", "Making & stopping edits"),
    ("presets", "🧩 Presets (#inspiration styles)", "Presets"),
    ("review", "✅ Review buttons (#ready-to-post)", "Review buttons"),
    ("channels", "📥 Channels you post in", "Channels you post in"),
    ("server", "🛠️ Server", "Server"),
]

# name, category, what it does, how to use it, extra search words
COMMANDS: list[dict] = [
    {"name": "/style", "cat": "style",
     "what": "Shows the style the editor has learned, or pins one setting so every edit uses it.",
     "how": "`/style` to view · `/style setting:pace value:fast` to pin · `value:auto` to let it learn again",
     "tags": "pin settings pace font colour grade captions learned"},
    {"name": "/feedback", "cat": "style",
     "what": "Tell the editor anything in plain words; it becomes a rule for every future edit.",
     "how": "`/feedback text:no captions unless he says something funny mode:Lacy clips` (mode is optional)",
     "tags": "rule always from now on lacy tokyo girl trend"},
    {"name": "/rules", "cat": "style",
     "what": "Every instruction the editor follows, numbered by category, with a Turn off/on button each.",
     "how": "`/rules` · `/rules search:captions` · `/rules disable:3, 5-7` · `/rules enable:4`",
     "tags": "list turn off on disable enable applies to search"},
    {"name": "/trend", "cat": "make",
     "what": "Makes trailer edits cut to your trending sounds; each lands in #ready-to-post with 3 versions.",
     "how": "`/trend` (every sound) · `/trend song:<name> per_song:2` (1-3 per sound)",
     "tags": "trending sound song music tokyo girl trailer"},
    {"name": "/compare", "cat": "make",
     "what": "GTA 5 vs GTA 6 comparison edits from official trailers: the same kind of shot from both games,"
             " labelled, with captions; each lands in #ready-to-post with 3 versions.",
     "how": "`/compare` · `/compare count:2` (1-3; one is also made every day)",
     "tags": "gta 5 vs 6 then now comparison side by side versus"},
    {"name": "/cancel", "cat": "make",
     "what": "Stops a running render, trend edit, Lacy clip, #inspiration analysis or sound download.",
     "how": "`/cancel` lists what's running · `/cancel run:<id from the progress bar>` · `/cancel run:all`",
     "tags": "stop kill abort progress bar render job"},
    {"name": "/preset apply", "cat": "presets",
     "what": "Re-renders a clip with a saved preset.",
     "how": "In the clip's thread: `/preset apply name:<preset>` (optional `variant:B`, or `item:<id>` elsewhere)",
     "tags": "rerender style inspiration"},
    {"name": "/preset list", "cat": "presets",
     "what": "All presets, most approved first, with what each one copies.",
     "how": "`/preset list`", "tags": "inspiration styles"},
    {"name": "/preset show", "cat": "presets",
     "what": "Every setting stored in one preset.",
     "how": "`/preset show name:<preset>`", "tags": "settings inspiration"},
    {"name": "/preset rename", "cat": "presets",
     "what": "Renames a preset (its approval stats move with it).",
     "how": "`/preset rename name:<preset> new_name:<new>`", "tags": "name"},
    {"name": "/preset edit", "cat": "presets",
     "what": "Changes one setting of a preset, or which parts it copies.",
     "how": "`/preset edit name:<preset> setting:pace value:fast` · `setting:copy value:pacing,colour`",
     "tags": "change setting saturation pace copy"},
    {"name": "/preset delete", "cat": "presets",
     "what": "Deletes a preset.", "how": "`/preset delete name:<preset>`", "tags": "remove"},
    {"name": "/preset combine", "cat": "presets",
     "what": "Builds a new preset from parts of others (e.g. pacing from one, colour from another).",
     "how": "`/preset combine new_name:<new> pacing:<preset> colour:<preset>` (also text, transitions, hook,"
            " structure, music_sync)",
     "tags": "merge mix parts pacing colour"},
    {"name": "Expand a video (drop-down)", "cat": "review",
     "what": "#ready-to-post is ONE message listing every video waiting for review. Pick one in the drop-down"
             " and it expands right there: its versions A/B/C (previews attached) and all the buttons below."
             " New clips and re-renders update that same message; nothing new is posted.",
     "how": "The drop-down on the review board in #ready-to-post · Collapse button to close it",
     "tags": "list board dropdown expand open menu queue"},
    {"name": "/campaigns", "cat": "make",
     "what": "Every clipping campaign joined: status, pay, budget spent, where it posts, required hashtags and"
             " its rules. Campaigns that run out of budget or end get no more clips automatically (checked"
             " every 30 min); end or resume one by hand.",
     "how": "`/campaigns` · `/campaigns end:Kai Cenat` · `/campaigns resume:Clix`",
     "tags": "budget programme rules hashtags streamer kai clix bobby kreekcraft end resume"},
    {"name": "Approve A/B/C", "cat": "review",
     "what": "Approves that version and posts it (YouTube/Instagram automatically, inside the daily caps and"
             " posting windows; TikTok and the rest go to #tiktok-queue). Nothing posts without this.",
     "how": "Button under each clip in #ready-to-post",
     "tags": "approve publish post upload youtube instagram tiktok"},
    {"name": "Redo (3 new)", "cat": "review",
     "what": "Throws away this round and renders 3 fresh versions.",
     "how": "Button under the clip", "tags": "rerender again new versions"},
    {"name": "Reject", "cat": "review",
     "what": "Drops the clip; nothing is posted.", "how": "Button under the clip", "tags": "delete no"},
    {"name": "Feedback", "cat": "review",
     "what": "Says what to change about this clip and re-renders it. Only this video, unless your wording"
             " asks for a rule (always / from now on / every / never).",
     "how": "Button under the clip, then type in the box", "tags": "change rerender fix this one"},
    {"name": "Edit caption", "cat": "review",
     "what": "Rewrites the title/captions before you approve.",
     "how": "Button under the clip, then edit in the box", "tags": "title caption text hashtags"},
    {"name": "Cancel (progress bar)", "cat": "make",
     "what": "Stops the job that progress bar belongs to, same as `/cancel`.",
     "how": "Button on any progress message", "tags": "stop kill progress"},
    {"name": "Turn off / Turn on · Applies to", "cat": "style",
     "what": "Switches one rule off or on, or picks which edits it applies to (Lacy clips, Tokyo Girl, trend).",
     "how": "Buttons and menus beside each rule in `/rules` or a /feedback reply",
     "tags": "rule toggle disable enable scope modes"},
    {"name": "#inspiration: a video", "cat": "channels",
     "what": "Analyses another creator's Short for its style and saves it as a preset (the video is deleted"
             " straight after; only numbers are kept).",
     "how": "Post a TikTok / Instagram / YouTube link or upload a video. Optional: `copy: pacing, colour, text,"
            " transitions, hook, structure, music-sync, look, framing` and `name: my-preset`",
     "tags": "reference style analyse preset link upload copy name"},
    {"name": "#inspiration: a sound", "cat": "channels",
     "what": "Adds a song to the music library for trend edits (beats and drop are measured; edits stay silent).",
     "how": "Paste a TikTok sound link (Share sound > Copy link), upload the audio, or `sound:` + a video link",
     "tags": "song music trending audio tiktok"},
    {"name": "/stats", "cat": "server",
     "what": "Refreshes the 📊 Stats channels now (they also update every 10 min) and replies with what"
             " YouTube and Instagram read. ▶️ YouTube shows the channel's views, subscribers and Shorts"
             " (YOUTUBE_API_KEY in .env); tell it which channel once if it can't find it.",
     "how": "`/stats` · `/stats youtube_channel:@yourhandle`", "tags": "numbers views counts youtube subscribers"},
    {"name": "/help", "cat": "server",
     "what": "This list.", "how": "`/help` · `/help category:Presets` · `/help search:caption`",
     "tags": "commands"},
]

SLASH = {c["name"].split()[0].lstrip("/") for c in COMMANDS if c["name"].startswith("/")}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^\w#/]+", text.lower()) if w]


def matches(cmd: dict, search: str) -> bool:
    """Every search word appears somewhere in the command's name, text, usage or tags."""
    hay = " ".join((cmd["name"], cmd["what"], cmd["how"], cmd.get("tags", ""))).lower()
    return all(w.lstrip("/") in hay for w in _words(search))


def select(category: str | None = None, search: str | None = None) -> list[dict]:
    return [c for c in COMMANDS if (not category or c["cat"] == category) and (not search or matches(c, search))]


def line(c: dict) -> str:
    return f"**{c['name']}**: {c['what']}\n↳ {c['how']}"


def help_text(category: str | None = None, search: str | None = None) -> str:
    """The /help reply: commands grouped under category headings."""
    found = select(category, search)
    head = "**Spikebot commands**"
    if category:
        head += f" · {dict((k, n) for k, _, n in CATEGORIES).get(category, category)}"
    if search:
        head += f" · matching “{search.strip()}”"
    if not found:
        cats = ", ".join(n for _, _, n in CATEGORIES)
        return f"{head}\nNothing matches. Try one word (e.g. `caption`, `stop`, `preset`) or a category: {cats}."
    out = [head]
    for key, heading, _ in CATEGORIES:
        rows = [c for c in found if c["cat"] == key]
        if rows:
            out.append(f"\n__{heading}__")
            out.extend(line(c) for c in rows)
    if not category and not search:
        out.append("\n-# Narrow it down: `/help category:` or `/help search:`. Only approvers can use the commands.")
    return "\n".join(out)


def chunks(text: str, limit: int = 1990) -> list[str]:
    """Split at line breaks into Discord-sized messages (a heading never ends a message)."""
    parts: list[str] = []
    cur = ""
    for ln in text.split("\n"):
        while len(ln) > limit:              # never happens with the lines above, but stay safe
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(ln[:limit])
            ln = ln[limit:]
        if cur and len(cur) + 1 + len(ln) > limit:
            tail = cur.rsplit("\n", 1)
            if len(tail) == 2 and tail[1].startswith("__") and tail[1].endswith("__"):
                parts.append(tail[0].rstrip("\n"))
                cur = tail[1] + "\n" + ln
                continue
            parts.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        parts.append(cur)
    return [p for p in parts if p.strip()]
