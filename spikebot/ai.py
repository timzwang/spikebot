"""Claude API helpers: read Tim's feedback in plain words, and study what makes a
reference Short look good.

Optional. Without ANTHROPIC_API_KEY (environment, or .env next to config.json) every
caller falls back to the rule-based parser (style.parse_feedback) and the measured
numbers only. The `anthropic` package is imported lazily, so the chat bot never
needs it.

Models (Tim, 24 Nov: "it's burning through my credits"): each task has its own model (`TASKS`).
Sonnet 5.5 for the writing and judgement Tim sees (stream-clip titles/hooks, /feedback, the rule
compiler, #inspiration); Haiku 5.5 (a twentieth of Sonnet's price) for the high-volume checks
(shot labels, GTA 5 vs 6 labels, impact ratings, which lines are game audio). Env overrides:
SPIKEBOT_AI_MODEL (#inspiration), SPIKEBOT_FEEDBACK_MODEL (/feedback, rules), SPIKEBOT_COPY_MODEL
(titles, hooks), SPIKEBOT_CHEAP_MODEL (labels, impact, speakers).

Spend: every request is logged with its tokens and cost in data/ai_usage.json (per UK day and
task) and shown in #daily-summary. A daily budget (`ai.daily_usd` in config.json or
SPIKEBOT_AI_DAILY_USD, default $0.75) stops background work (labelling, the rule compiler) at
half of it and clip copy / impact checks at all of it; callers then fall back to their
non-AI path. Requests Tim starts himself (/feedback, #inspiration) always run.

Privacy/policy: only frames and numbers are sent, never the reference file itself,
and the model is told not to copy on-screen words, speech or footage. Nothing it
returns is ever burned into our edits as their wording.
"""
from __future__ import annotations

import base64
import fcntl
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import style as st

log = logging.getLogger("ai")

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
MODEL = SONNET            # #inspiration study (was Opus at high effort: several times the cost)
FEEDBACK_MODEL = SONNET   # /feedback, the review Feedback button, the rule compiler
COPY_MODEL = SONNET       # stream-clip titles, captions, hooks (what viewers read)
CHEAP_MODEL = HAIKU       # shot labels, impact ratings, game-audio lines (high volume)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models with a server-side refusal fallback (Haiku has none: the fallback params would 400)
FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")

# task -> (env var that overrides the model, default model, budget tier)
# tiers: "tim" = Tim asked for it (never blocked); "clip" = per stream clip (blocked at the daily
# budget); "background" = labelling / rule compiling (blocked at half of it)
TASKS = {
    "study": ("SPIKEBOT_AI_MODEL", MODEL, "tim"),
    "feedback": ("SPIKEBOT_FEEDBACK_MODEL", FEEDBACK_MODEL, "tim"),
    "rules": ("SPIKEBOT_FEEDBACK_MODEL", FEEDBACK_MODEL, "background"),
    "copy": ("SPIKEBOT_COPY_MODEL", COPY_MODEL, "clip"),
    "voice": ("SPIKEBOT_COPY_MODEL", COPY_MODEL, "clip"),
    "impact": ("SPIKEBOT_CHEAP_MODEL", CHEAP_MODEL, "clip"),
    "speakers": ("SPIKEBOT_CHEAP_MODEL", CHEAP_MODEL, "clip"),
    "punctuation": ("SPIKEBOT_CHEAP_MODEL", CHEAP_MODEL, "clip"),
    "labels": ("SPIKEBOT_CHEAP_MODEL", CHEAP_MODEL, "background"),
    "compare_labels": ("SPIKEBOT_CHEAP_MODEL", CHEAP_MODEL, "background"),
}
# $ per million tokens (input, output); cache reads 0.1x input, cache writes 1.25x
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
          "claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-opus-4-8": (5.0, 25.0),
          "claude-fable-5-2": (8.0, 40.0)}
DAILY_USD = 0.75          # default daily budget (about £18 a month at most; a normal day is far less)
BACKGROUND_SHARE = 0.5    # labelling and the rule compiler stop at this share of it
UK = ZoneInfo("Europe/London")


class AIError(RuntimeError):
    pass


def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def api_key(base: Path | None = None) -> str | None:
    """ANTHROPIC_API_KEY from .env next to config.json (fresh, like the IG token),
    then the process environment."""
    if base is not None:
        v = _dotenv(Path(base) / ".env").get("ANTHROPIC_API_KEY")
        if v:
            return v
    return os.environ.get("ANTHROPIC_API_KEY") or None


def model_name(base: Path | None = None, task: str = "study") -> str:
    """The model for a task (`TASKS`); its env var in .env next to config wins over the
    environment, which wins over the default."""
    var, default, _tier = TASKS.get(task, TASKS["study"])
    if base is not None:
        v = _dotenv(Path(base) / ".env").get(var)
        if v:
            return v
    return os.environ.get(var) or default


def available(base: Path | None = None) -> bool:
    if not api_key(base):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def ask_json(system: str, content: list[dict] | str, schema: dict, base: Path | None = None,
             max_tokens: int = 8000, client=None, effort: str = "medium", task: str = "study") -> dict:
    """One structured-output request on the task's model. Returns the parsed JSON. Raises AIError
    (also when today's budget for this kind of task is used up: `check_budget`)."""
    model = model_name(base, task)
    check_budget(base, task)
    if client is None:
        try:
            import anthropic
        except ImportError as e:
            raise AIError("the anthropic package is not installed") from e
        key = api_key(base)
        if not key:
            raise AIError("ANTHROPIC_API_KEY is not set")
        client = anthropic.Anthropic(api_key=key, timeout=120.0)
    kw = dict(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": content}],
              output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
    if model.startswith(FALLBACK_MODELS):   # a refusal re-runs on the fallback model; Haiku has none
        kw.update(betas=[FALLBACK_BETA], fallbacks="default")
    try:
        resp = client.beta.messages.create(**kw)
    except Exception as e:  # noqa: BLE001 - network/API errors become a fallback, never a crash
        raise AIError(f"Claude API call failed: {type(e).__name__}: {str(e)[:200]}") from e
    record(base, task, getattr(resp, "model", None) or model, getattr(resp, "usage", None))
    if getattr(resp, "stop_reason", None) == "refusal":
        raise AIError("the model declined this request")
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise AIError("the answer was cut off (max_tokens)")
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    try:
        return json.loads(text)
    except ValueError as e:
        raise AIError("the answer was not valid JSON") from e


# --------------------------------------------------------------------------
# Spend: a ledger per UK day and a daily budget
# --------------------------------------------------------------------------
def usage_path(base: Path | None) -> Path | None:
    """data/ai_usage.json next to config.json (None without a base: tests, one-off scripts)."""
    return Path(base) / "data" / "ai_usage.json" if base is not None else None


def cost_usd(model: str, usage) -> float:
    """What one response cost, from its usage (input, output, cache reads and writes)."""
    if usage is None:
        return 0.0
    pin, pout = PRICES.get(str(model), PRICES["claude-sonnet-5-5"])

    def n(k: str) -> float:
        v = getattr(usage, k, None) if not isinstance(usage, dict) else usage.get(k)
        return float(v) if isinstance(v, (int, float)) else 0.0
    return (n("input_tokens") * pin + n("cache_creation_input_tokens") * pin * 1.25
            + n("cache_read_input_tokens") * pin * 0.1 + n("output_tokens") * pout) / 1e6


def uk_day(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), UK).strftime("%Y-%m-%d")


def _read_usage(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(base: Path | None, task: str, model: str, usage, now: float | None = None) -> float:
    """Add one request to the ledger (locked: the bot and render subprocesses all write). Never raises."""
    path = usage_path(base)
    usd = cost_usd(model, usage)
    if path is None or usage is None:
        return usd
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = _read_usage(path)
            days = data.setdefault("days", {})
            day = days.setdefault(uk_day(now), {"usd": 0.0, "tasks": {}})
            row = day["tasks"].setdefault(task, {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
            row["calls"] += 1
            row["in"] += int(getattr(usage, "input_tokens", 0) or 0)
            row["out"] += int(getattr(usage, "output_tokens", 0) or 0)
            row["usd"] = round(row["usd"] + usd, 5)
            day["usd"] = round(day["usd"] + usd, 5)
            for old in sorted(days)[:-40]:   # 40 days kept
                days.pop(old, None)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
    except OSError as e:
        log.info("could not log AI usage: %s", e)
    return usd


def usage_on(base: Path | None, day: str | None = None) -> dict:
    """{"usd", "tasks": {task: {calls, in, out, usd}}} for one UK day (today by default)."""
    path = usage_path(base)
    if path is None:
        return {"usd": 0.0, "tasks": {}}
    return (_read_usage(path).get("days") or {}).get(day or uk_day(), {"usd": 0.0, "tasks": {}})


def daily_budget(base: Path | None) -> float:
    """Dollars a day: SPIKEBOT_AI_DAILY_USD (.env, then the environment), else `ai.daily_usd` in
    config.json, else DAILY_USD. 0 = no limit."""
    raw = None
    if base is not None:
        raw = _dotenv(Path(base) / ".env").get("SPIKEBOT_AI_DAILY_USD")
    raw = raw or os.environ.get("SPIKEBOT_AI_DAILY_USD")
    if raw is None and base is not None:
        try:
            raw = (json.loads((Path(base) / "config.json").read_text(encoding="utf-8")).get("ai") or {}).get("daily_usd")
        except (OSError, ValueError, AttributeError):
            raw = None
    try:
        return max(0.0, float(raw)) if raw is not None else DAILY_USD
    except (TypeError, ValueError):
        return DAILY_USD


def check_budget(base: Path | None, task: str, now: float | None = None) -> None:
    """Raise AIError when today's spend has reached this task's share of the daily budget."""
    tier = TASKS.get(task, TASKS["study"])[2]
    if base is None or tier == "tim":
        return
    cap = daily_budget(base)
    if cap <= 0:
        return
    limit = cap * (BACKGROUND_SHARE if tier == "background" else 1.0)
    spent = float(usage_on(base, uk_day(now)).get("usd") or 0.0)
    if spent >= limit:
        raise AIError(f"today's AI budget is used up (${spent:.2f} of ${limit:.2f} for {tier} work);"
                      " it resets at midnight UK time")


def usage_line(base: Path | None, day: str | None = None) -> str:
    """One line for #daily-summary: today's spend, the budget and the biggest tasks."""
    u = usage_on(base, day)
    cap = daily_budget(base)
    tasks = sorted((u.get("tasks") or {}).items(), key=lambda kv: -kv[1].get("usd", 0))
    bits = [f"{TASK_NAMES.get(t, t)} {r['calls']}× ${r['usd']:.2f}" for t, r in tasks[:5]]
    head = f"🤖 **Claude API**: ${float(u.get('usd') or 0):.2f} today" + (f" of ${cap:.2f} budget" if cap else "")
    return head + (" · " + " · ".join(bits) if bits else " · no requests")


TASK_NAMES = {"study": "#inspiration", "feedback": "feedback", "rules": "rule compiler", "copy": "titles",
              "voice": "voiceover lines", "impact": "impact checks", "speakers": "speaker checks",
              "punctuation": "caption punctuation",
              "labels": "shot labels", "compare_labels": "GTA 5 vs 6 labels"}


# --------------------------------------------------------------------------
# What the editor can actually change (shared by both prompts)
# --------------------------------------------------------------------------
def settings_menu() -> str:
    lines = [
        "grade: " + ", ".join(st.GRADES) + "  (colour grade)",
        "pace: " + ", ".join(st.PACES) + "  (clip length / cut speed)",
        "font: " + ", ".join(st.FONTS),
        "size: " + ", ".join(st.SIZES) + "  (text size)",
        "position: " + ", ".join(st.POSITIONS) + "  (text position)",
        "colour: " + ", ".join(st.COLOURS) + "  (caption colour scheme: white + highlight)",
        "transition: " + ", ".join(st.TRANSITIONS) + "  (between cuts; zoom is a punch-in, i.e. camera"
        " movement); Tokyo Girl and trend edits also have fade  (a soft crossfade; length fade_s)",
        "motion: none, subtle, full  (camera movement the editor adds: none = every shot completely still,"
        " no pans, push-ins, zooms, drift or animated stills)",
        "framing: box45, box11, stack, fill, fit  (Tokyo Girl and trend edits only: box45 = a big 4:5 picture with"
        " black bars above/below and text in them (default); box11 = a 1:1 picture over a blurred copy; stack = two clips at once,"
        " one above the other, each whole; fill crops the 16:9 trailer to fill the 9:16 screen; fit shows"
        " the whole frame so nothing is cut off, over a blurred copy of itself)",
        "smart_fill (custom): 0 or 1  (Tokyo Girl and trend edits: 1 = a close-up of one person goes full screen and"
        " every crop is centred on the subject; default 1)",
        "media: clips  (Tokyo Girl and trend edits: always real trailer video, never screenshots; there is no other"
        " value)",
        "text_level: none, minimal, full, plain  (none = no captions/hook/text at all; minimal = short "
        "opening hook and captions only around the payoff (Lacy) / only the opening card (Tokyo Girl); "
        "full = every spoken word captioned (Lacy) / every story card (Tokyo Girl); plain = Tokyo Girl and trend edits only:"
        " one short factual line such as 'GTA 6 is almost here...' or the release countdown, never a question)",
        "intro: none, logo, countdown, rockstar_presents  (Tokyo Girl and trend edits only: logo = the official GTA VI"
        " logo builds up over logo_s seconds over a calm trailer shot, then bursts outward (over logo_fade_s"
        " seconds) exactly on the song's drop, never wider than logo_size of the screen; countdown = a plain"
        " 'N days left until GTA 6 releases' line instead; rockstar_presents = the trailer's own 'Rockstar Games"
        " Presents' shot over the ocean and skyscrapers, where the text fades in, plays up to the drop (the plain"
        " black title card is never used); intro_mix = share of logo-intro edits that use rockstar_presents"
        " instead, so not every edit starts with the logo)",
        "ending: " + ", ".join(st.ENDINGS) + "  (Lacy only: end card)",
        "opening: " + ", ".join(st.OPENINGS) + "  (stream clips only: teaser = the clip's biggest moment plays"
        " first for 2.5-4.5 s while an AI voice reads a short hook, then the clip from its start; normally only"
        " the experimental version C gets it)",
        "hook: Lacy tones " + ", ".join(st.HOOKS["lacy"]) + "; Tokyo Girl / trend story kinds "
        + ", ".join(st.HOOKS["tokyo_girl"]) + "  (needs mode lacy, tokyo_girl or trend)",
    ]
    for k, (lo, hi) in st.CUSTOM_KEYS.items():
        lines.append(f"{k}: number {lo} to {hi}")
    lines.append("text_colour, highlight: hex colour like #FFE500")
    lines.append("grain: film grain strength (0 = off, 6-12 subtle); vignette: 1 = on, 0 = off; "
                 "letterbox: 1 = cinema bars (Tokyo Girl only)")
    lines.append("hook_s: seconds the opening hook stays on screen; payoff: where the peak lands (0.2-0.9)")
    lines.append("Tokyo Girl timing: max_s = longest a Tokyo Girl edit may be, logo intro included but not the"
                 " Rockstar Games Presents opening, which is added on top (e.g. 24; trending-"
                 "sound edits ignore it and only stop before their sound ends); hold_beats ="
                 " fewest beats each clip is held (2+ keeps a clip's action intact, fewer cuts); logo_s = seconds"
                 " the logo builds before the drop; logo_size = the logo's largest width as a share of the screen"
                 " (0.6 = 60%); fade_s = crossfade length in seconds")
    lines.append("Tokyo Girl / trend footage choice (0/1 = off/on unless a range is given): unique_clips = never"
                 " the same clip (or the same footage from another trailer file) twice in one edit, default 1;"
                 " crop_safe = prefer shots whose main subject sits inside the 9:16 crop and centre the crop on it,"
                 " default 1; action_first = the most action-packed clips go in the first third, default 0;"
                 " ambient_bias 0-1 = how strongly calm scenery / atmospheric / establishing shots are preferred;"
                 " palette_match 0-1 = clips must share the opening clip's colour palette (hue and tone), 0 = off;"
                 " impact_ending = the last clip is an impactful closing shot (someone walking away from the camera,"
                 " a car driving into the distance), default 0; opener_rotation 0-20 = the opening clip is never one"
                 " that opened the last N edits, default 10; footage_match = only clips that fit the edit (action,"
                 " atmosphere, the story's tags), never filler or title cards, default 1; glitch_check = the"
                 " finished edit is scanned for stray frames, broken fades and cuts off the beat and re-rendered,"
                 " default 1; end_fade_s 0-6 = the last seconds fade slowly to black, default 3")
    lines.append("sharpen 0-1: sharpening / clarity on every kind of edit (the TikTok '4K' look; 0 = off, 0.5 ="
                 " clear, 1 = strong)")
    lines.append("text_box 0-1: every kind: how dark the see-through box behind text that sits over the picture is"
                 " (0 = no box, white text with an outline only; default 0.55; text on a black band never gets one)")
    lines.append("Stream clips (lacy) only: lead_in_s 0-5 = seconds added before the clip's start, then snapped"
                 " back to the start of the sentence so the first words are never cut; require_facecam = skip a"
                 " clip whose top panel doesn't show the streamer's real face (e.g. a 'facecam' that is really"
                 " another part of the game screen), default 1; streamer_only = captions come only from the"
                 " streamer's own voice, never in-game dialogue, NPC lines, radio or game audio, default 1")
    lines.append("variety 0-1 (Tokyo Girl / trend): clips used in the recent edits are avoided and the edit draws"
                 " from the trailers / Extended Look used least lately, for a broad range of footage between edits;"
                 " default 0.7")
    lines.append("Footage is matched to each story's tags; the #inspiration presets Tim approves steer pacing"
                 " and look. Edits are exported silent, but which SOUND an edit is cut to (its beats, drop and"
                 " length) is chosen per edit with the `song` field, never a setting, note or unsupported item."
                 " Which clips a re-render uses is the `footage` field (keep = same clips; naming another version"
                 " such as 'use the clips from B1' or its file name reuses that version's clips).")
    lines.append("Value 'auto' (or null) clears a pinned setting so the editor goes back to learning it.")
    return "\n".join(lines)


SETTING_ROW = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["all", "lacy", "tokyo_girl", "trend"]},
        "setting": {"type": "string"},
        "value": {"type": "string"},
        "scope": {"type": "string", "enum": ["this_edit", "always"]},
    },
    "required": ["mode", "setting", "value", "scope"],
    "additionalProperties": False,
}

FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "settings": {"type": "array", "items": SETTING_ROW},
        "notes": {"type": "array", "items": {"type": "string"}},
        "unsupported": {"type": "array", "items": {"type": "string"}},
        "hook_text": {"type": ["string", "null"]},
        "footage": {"type": "string", "enum": ["keep", "new", "default"]},
        "song": {"type": ["string", "null"]},
    },
    "required": ["summary", "settings", "notes", "unsupported", "hook_text", "footage", "song"],
    "additionalProperties": False,
}

FEEDBACK_SYSTEM = """You turn a YouTube Shorts creator's feedback into concrete settings for an automated
video editor. The channel posts GTA 6 Shorts in two modes:
- lacy: stream clips of streamers playing GTA (Lacy, Adin Ross, Seal; facecam stacked over gameplay, their voice audio).
- tokyo_girl: silent cinematic edits cut from official Rockstar GTA VI trailers and screenshots,
  with text cards; the creator adds the music in the TikTok/Instagram app.
- trend: the same kind of silent trailer edit, cut to a trending sound instead of the Tokyo Girl song
  (same editor and settings as tokyo_girl, but its own rules).

Settings the editor can change (use these names and values exactly):
{menu}

Rules:
- Translate everything you can into settings. Strong opinions about quality ("captions are AI slop",
  "too much text", "looks cheap") usually mean lowering text_level, simpler transitions, a calmer grade.
- scope "always" = it should apply to future edits; "this_edit" = only to the edit being reviewed.
  The creator wants his instructions kept: use "always" unless he clearly says it is only for this
  edit ("just this one", "only here"). Feedback sent with /feedback is always "always".
- mode: which kind of edit a row is for: "all", "lacy", "tokyo_girl" or "trend". Use "all" only when the
  feedback is clearly about every kind; when it names one kind, use that one.
- notes: short lasting instructions that are not a setting (e.g. "payoff in the first 3 seconds"),
  written as instructions to an editor. Prefer settings; use a note only when no setting comes close
  (notes are queued for new editor code and switch themselves into settings once it exists).
- unsupported: things the editor cannot do yet, plainly worded, so the creator knows. Do not pretend.
- hook_text: only if the creator wrote the exact new opening text they want, otherwise null.
- footage (review feedback on one edit): "keep" = re-render with exactly the same clips as the edit
  being reviewed (e.g. "same clips as B1", "keep the footage", or when he only asks to change text,
  colour, song or timing); "new" = he wants different clips; "default" = he said nothing about clips
  (the editor then keeps them). Never turn a footage request into a note, setting or unsupported item.
- song: the sound this edit should be cut to, when he asks to switch it (e.g. "switch the song to
  impulsive"): copy the closest name from "Sounds available", else his exact words. null = unchanged.
  This is a per-edit choice: never a note, setting or unsupported item.
- summary: one plain sentence of what you will change."""


def interpret_feedback(text: str, mode: str, base: Path | None = None, current: dict | None = None,
                       context: str = "slash", taste: list[str] | None = None, client=None,
                       sounds: list[str] | None = None) -> dict:
    """Plain-language feedback -> {summary, settings[], notes[], unsupported[], hook_text}.
    `context` is "slash" (/feedback, future edits) or "review" (the Feedback button on
    one variant: `current` is that variant's style)."""
    parts = [f"Context: {'/feedback command (future edits)' if context == 'slash' else 'feedback on one review variant'}",
             f"Mode the creator is talking about: {mode}"]
    if current:
        parts.append("Current settings of that edit: " + json.dumps(current, sort_keys=True))
    if sounds:
        parts.append("Sounds available: " + ", ".join(sounds[:60]))
    if taste:
        parts.append("Instructions already in force (newest last):\n- " + "\n- ".join(taste[-30:]))
    parts.append("Feedback:\n" + text.strip()[:3000])
    out = ask_json(FEEDBACK_SYSTEM.format(menu=settings_menu()), "\n\n".join(parts), FEEDBACK_SCHEMA,
                   base=base, max_tokens=4000, client=client, effort="medium", task="feedback")
    out.setdefault("footage", "default")
    out.setdefault("song", None)
    for row in out.get("settings", []):
        if str(row.get("value", "")).lower() in ("auto", "null", "none") and row.get("setting") not in (
                "text_level", "intro", "ending", "opening", "transition"):
            row["value"] = "auto"
    return out


# --------------------------------------------------------------------------
# #inspiration: the artistic side of a reference Short
# --------------------------------------------------------------------------
# The on-screen captions of a reference (Tim, 20 Nov: "in inspiration take note of the caption on
# screen as well"): how they look and behave, never what they say.
CAPTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "present": {"type": "boolean"},
        "kind": {"type": "string", "enum": ["speech_subtitles", "hook_only", "story_text", "none"]},
        "position": {"type": "string", "enum": ["top", "upper", "middle", "lower", "bottom", "band_above_picture",
                                                "band_below_picture", "varies"]},
        "size": {"type": "string", "enum": ["small", "medium", "large", "huge"]},
        "case": {"type": "string", "enum": ["sentence", "upper", "lower", "title", "mixed"]},
        "words_per_line": {"type": "integer"},
        "lines": {"type": "integer"},
        "look": {"type": "string", "enum": ["white_outline", "white_shadow", "box_background", "coloured",
                                            "highlighted_word", "plain"]},
        "colours": {"type": "string"},
        "font_feel": {"type": "string"},
        "animation": {"type": "string", "enum": ["none", "pop", "word_by_word", "fade", "slide", "bounce"]},
        "seconds_on_screen": {"type": "number"},
        "hook_text": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["present", "kind", "position", "size", "case", "words_per_line", "lines", "look", "colours",
                 "font_feel", "animation", "seconds_on_screen", "hook_text", "notes"],
}

ART_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "what_makes_it_good": {"type": "array", "items": {"type": "string"}},
        "techniques": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "how": {"type": "string"},
                           "can_copy": {"type": "boolean"}},
            "required": ["name", "how", "can_copy"], "additionalProperties": False}},
        "text_use": {"type": "string"},
        "opening": {"type": "string"},
        "settings": {"type": "array", "items": SETTING_ROW},
        "avoid": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["edit", "stream_clip", "other"]},
        "kind_why": {"type": "string"},
        "captions": CAPTION_SCHEMA,
    },
    "required": ["summary", "what_makes_it_good", "techniques", "text_use", "opening", "settings", "avoid",
                 "kind", "kind_why", "captions"],
    "additionalProperties": False,
}

ART_SYSTEM = """You are a senior short-form video editor studying a reference Short that a creator liked,
so their automated editor can learn the *craft*, not copy the video. You get frames sampled through
the video (with timestamps) and numbers measured from it (cut timing, tempo, grade, text, motion).

Describe the artistic choices that make it good: how it opens and hooks, rhythm and where cuts land
against the music, how text is used (or deliberately not used), colour and contrast mood, camera
motion and zooms, transitions, use of logos/title cards, contrast between calm and intense moments,
how it ends or loops. Be specific and practical ("text appears only once, on the drop, then the
footage carries it") rather than generic praise.

Never transcribe or quote on-screen words, speech, usernames or captions, and never suggest reusing
their footage, audio or wording: describe the *type* of choice only.

captions: look closely at the on-screen text in every frame: spoken-word subtitles, the hook / title text
and any story text. Record how it LOOKS and BEHAVES, never what it says: where it sits (band_above_picture /
band_below_picture when the picture is letterboxed and the text is on the black), size relative to the
frame (small < 4% of the height, medium 4-6%, large 6-9%, huge above), letter case, words per line, lines
on screen at once, the look (white_outline = white letters with a black outline like TikTok's own captions;
box_background = text on a solid box; highlighted_word = the spoken word in another colour), colours, the
font's feel (e.g. "rounded bold sans like TikTok Sans"), how it appears (none = just appears and disappears),
about how many seconds each caption stays on screen, and hook_text = how the opening hook text is styled and
where (or "none"). notes: anything else that makes the captions easy to read (e.g. "never covers the face").
present = false and kind "none" when there is no on-screen text at all.

kind: what sort of video it is, which decides what our editor learns from it:
- "edit": a montage cut together from game/trailer/film footage, usually to music (many cuts, often
  beat-synced, no streamer talking to camera). Teaches the trailer and trending-sound edits.
- "stream_clip": a moment from a livestream or gaming video: one take or a few jump cuts, the streamer's
  own voice, usually a webcam/facecam on screen and captions of what they say. Teaches stream clips.
- "other": anything else (vlog, podcast, news, meme, tutorial, ad, IRL). Kept for reference only.
kind_why: a few words on what gave it away (e.g. "webcam in the top corner, one continuous take").
For a stream_clip, choose settings for mode "lacy" (the stream-clip editor); for an edit, mode "tokyo_girl".

techniques[].can_copy = true only if the editor below could reproduce it with its settings.
settings: the closest settings for our editor (mode "all" unless clearly mode-specific, scope "always").
Editor settings available:
{menu}"""


def _image_block(jpg: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(jpg).decode("ascii")}}


def study_reference(frames: list[tuple[float, bytes]], measured: dict, note: str = "",
                    base: Path | None = None, client=None) -> dict:
    """frames: [(seconds, jpeg bytes)]. Returns ART_SCHEMA output."""
    content: list[dict] = []
    for t, jpg in frames[:10]:
        content.append({"type": "text", "text": f"Frame at {t:.1f}s"})
        content.append(_image_block(jpg))
    content.append({"type": "text", "text": "Measured: " + json.dumps(measured, sort_keys=True, default=str)[:6000]})
    if note.strip():
        content.append({"type": "text", "text": "The creator's note about why they sent it: " + note.strip()[:800]})
    return ask_json(ART_SYSTEM.format(menu=settings_menu()), content, ART_SCHEMA, base=base,
                    max_tokens=6000, client=client, effort="medium", task="study")


# --------------------------------------------------------------------------
# Stream clips: YouTube title + post caption written from what was said
# --------------------------------------------------------------------------
CLIP_COPY_SYSTEM = """You write the YouTube Shorts title, the TikTok/Instagram caption and the on-screen hook for a
short clip of a streamer playing GTA. You get the streamer's name, the game, chat's reaction words and the
transcript of what was said. Your only job is to make a stranger scrolling past STOP and watch. Write like the
best clipper accounts, not like a bot summarising the clip.

What makes people stop (use the transcript for the specific detail; never invent events):
- A curiosity gap: set up a situation and hold back how it ends ("Lacy's security job lasted 40 seconds",
  "Kai trusted the NPC with the getaway car").
- Stakes or tension: something about to go wrong, a risk, a rule being broken, a plan that can't work.
- A contradiction or a surprising specific detail: an odd number, a weird place, an unexpected person,
  a promise he's about to break ("Adin swore he'd drive carefully this time").
- A short line he actually said that's funny or dramatic on its own, as part of the title (in quotes only
  if it is word for word from the transcript, at most 6 quoted words).
Flat and banned: a plain summary of the activity ("Lacy takes on another security job in GTA 6", "Adin plays
GTA 6", "Kai explores Vice City"), "{name} does X in GTA 6" with nothing at stake, filler endings like "in GTA 6"
when they add nothing, and generic hype ("funny moment", "best clip", "you have to see this").
- title: 25-70 characters, must contain the streamer's name exactly as given, plain English, sentence case,
  no hashtags, no emojis, no ALL CAPS words except GTA, never quotes around the whole title.
- caption: 15-80 characters, casual and lowercase is fine, must contain the streamer's name, at most one emoji,
  no hashtags (they are added later), a different angle from the title (not a copy of it).
- hook: the text on screen for the first seconds: 3-8 words, at most 42 characters, must contain the streamer's
  name, sentence case, no emojis, no hashtags. The SETUP that makes someone stay for the moment ("Kai tries to
  rob the jewellery store", "Lacy's first shift as a guard"); never the outcome or punchline, never a call to
  follow anyone.
- Never: insults, mocking, drama, negativity about the streamer, "you won't believe", "insane", "crazy",
  "wait for it", "watch till the end", "POV", "theory", questions to farm comments, story spoilers.
If the transcript is vague, build the hook on chat's reaction and what is clearly going on, still with tension
("Chat saw this coming before {name} did").
Give exactly the number of options asked for, each a clearly different angle (e.g. one curiosity gap, one
stakes, one built on what he said), best first."""

CLIP_COPY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"options": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"title": {"type": "string"}, "caption": {"type": "string"}, "hook": {"type": "string"}},
        "required": ["title", "caption", "hook"]}}},
    "required": ["options"],
}


def pronoun_rule(name: str, pronouns: str = "he/him") -> str:
    """The line every streamer prompt ends with (Tim, 21 Nov: "Lacy is he/him, not she/her")."""
    return (f"\n- Pronouns: {name} is {pronouns}. Whenever you refer to {name} without the name, use "
            f"{pronouns} only (never any other pronouns).")


def write_clip_copy(name: str, game: str, transcript: str, vibe: list[str] | None = None, n: int = 3,
                    base: Path | None = None, client=None, pronouns: str = "he/him") -> list[dict]:
    """[{title, caption, hook}] x n for one clip. Raises AIError (callers fall back to templates)."""
    content = (f"Streamer: {name} ({pronouns})\nGame: {game}\nChat reaction: {', '.join((vibe or [])[:8]) or 'none'}\n"
               f"Options wanted: {n}\nTranscript:\n{transcript.strip()[:3000] or '(no clear speech)'}")
    out = ask_json(CLIP_COPY_SYSTEM.replace("{name}", name) + pronoun_rule(name, pronouns), content, CLIP_COPY_SCHEMA, base,
                   max_tokens=2500, client=client, effort="low", task="copy")
    return [o for o in out.get("options") or [] if o.get("title") and o.get("caption")]


# --------------------------------------------------------------------------
# Stream clips, teaser opening: the line the AI voice says over the big moment
# --------------------------------------------------------------------------
VOICE_HOOK_SYSTEM = """You write the spoken hook for the first 3 seconds of a Short. The clip's biggest moment
plays first as a teaser while an AI voice reads your line, then the clip plays from the start. You get the
streamer's name, the game, chat's reaction words and the transcript of what was said.
- line: 5-14 words, one or two short spoken sentences, must contain the streamer's name exactly as given.
- It makes the viewer need to see how it happened: set up the moment, never give away the punchline or
  the outcome, never describe exactly what they're about to see.
- Talk like a real person narrating ("{name} thought this heist was easy... it wasn't."), plain English,
  no hashtags, no emojis, no ALL CAPS, no quotes from the transcript.
- Only what the transcript supports; never invent events. If it is vague, keep it general.
- Never: insults, mocking, drama, negativity about the streamer, "you won't believe", "insane", "crazy",
  "wait for it", "watch till the end", "POV", story spoilers."""

VOICE_HOOK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"line": {"type": "string"}},
    "required": ["line"],
}


def write_voice_hook(name: str, game: str, transcript: str, vibe: list[str] | None = None,
                     base: Path | None = None, client=None, pronouns: str = "he/him") -> str:
    """One spoken teaser line for a clip. Raises AIError (callers fall back to the title)."""
    content = (f"Streamer: {name} ({pronouns})\nGame: {game}\nChat reaction: {', '.join((vibe or [])[:8]) or 'none'}\n"
               f"Transcript:\n{transcript.strip()[:3000] or '(no clear speech)'}")
    out = ask_json(VOICE_HOOK_SYSTEM.replace("{name}", name) + pronoun_rule(name, pronouns), content, VOICE_HOOK_SCHEMA, base,
                   max_tokens=1200, client=client, effort="low", task="voice")
    return str(out.get("line") or "").strip()


# --------------------------------------------------------------------------
# Stream clips: which transcript lines are the streamer, which are the game
# --------------------------------------------------------------------------
SPEAKER_SYSTEM = """You get the transcript of a GTA live-stream clip as numbered lines with times. The audio mixes the
streamer's own microphone with the game's audio. Return the numbers of the lines that are NOT the streamer
talking live: scripted cutscene dialogue, NPC / pedestrian lines, in-game radio, TV or phone calls, mission
voice lines. The streamer reacting, talking to chat, or to friends on voice chat counts as the streamer.
Judge by wording and context (game dialogue is scripted, in character and about the story; the streamer is
casual and reacts). When unsure, it is the streamer: leave it out of the list."""

SPEAKER_SCHEMA = {"type": "object", "additionalProperties": False,
                  "properties": {"game": {"type": "array", "items": {"type": "integer"}}},
                  "required": ["game"]}


def game_lines(name: str, game: str, lines: list[tuple[float, float, str]], base: Path | None = None,
               client=None) -> list[int]:
    """Indices of transcript lines that are game audio, not the streamer. Raises AIError."""
    text = "\n".join(f"{i}. [{a:.1f}-{b:.1f}s] {t}" for i, (a, b, t) in enumerate(lines))
    out = ask_json(SPEAKER_SYSTEM, f"Streamer: {name}\nGame: {game}\nTranscript:\n{text[:6000]}", SPEAKER_SCHEMA,
                   base, max_tokens=2000, client=client, effort="low", task="speakers")
    return sorted({int(i) for i in out.get("game") or [] if isinstance(i, int) and 0 <= i < len(lines)})


# --------------------------------------------------------------------------
# Stream clips: caption punctuation (speech-to-text puts a full stop at every pause)
# --------------------------------------------------------------------------
PUNCT_SYSTEM = """You get the words of a short live-stream transcript from speech-to-text, one numbered word per
line. Speech-to-text puts a full stop wherever the speaker pauses, even mid-sentence, and capitalises the word
after it. Fix ONLY punctuation and capital letters so the words read as proper sentences or phrases: a full stop,
question mark or exclamation mark only where a sentence or phrase really ends, commas where natural, and a
capital letter only at the start of a sentence, for "I" and for names. Words shouted in capitals stay in capitals.
Never add, remove, merge, split, reorder or re-spell a word. Return exactly one entry per input word, in order."""

PUNCT_SCHEMA = {"type": "object", "additionalProperties": False,
                "properties": {"words": {"type": "array", "items": {"type": "string"}}},
                "required": ["words"]}


def punctuate(words: list[str], base: Path | None = None, client=None) -> list[str]:
    """The same words with sentence punctuation and capitals fixed. Raises AIError."""
    text = "\n".join(f"{i}. {w}" for i, w in enumerate(words))
    out = ask_json(PUNCT_SYSTEM, f"Words:\n{text[:8000]}", PUNCT_SCHEMA, base,
                   max_tokens=4000, client=client, effort="low", task="punctuation")
    return [str(w) for w in out.get("words") or []]


# --------------------------------------------------------------------------
# Stream clips: is this chat spike a real moment? (spikebot/impact.py)
# --------------------------------------------------------------------------
IMPACT_SYSTEM = """You judge whether a moment from a GTA livestream is worth turning into a Short. Chat suddenly
got busy, and you decide whether something actually HAPPENED that viewers would want to watch, or whether chat
was only busy because the streamer turned to chat.
You get the streamer's name, the game, the transcript around the moment with times relative to when chat peaked
(chat reacts a few seconds AFTER what caused it), chat's most common messages, and how loud the streamer got
compared with the rest of the clip (1.0 = normal, 2+ = shouting or laughing hard).
kind:
- funny: a genuinely funny moment or line; shock: something surprising or unbelievable happens;
  skill: an impressive play, escape, chase or stunt; fail: a fail, crash, death or chaotic mistake;
  rage: frustration at the game (never mocking); social: a memorable interaction with another player or NPC;
- chat_prompt: chat only spiked because the streamer asked chat something (a question, poll, "type 1", "spam W",
  "say hi") and nothing happened in the game;
- support_thanks: thanking subs / gifts / donations / a raid, giveaways;
- ad_or_sponsor: an ad break or sponsor read; break_or_idle: away, loading, menus, nothing on screen happening;
- just_talking: ordinary chatting with no payoff; other: none of these.
score 0-10: how much a stranger scrolling Shorts would care. 8-10 big laugh or jaw-drop; 5-7 solid clip;
3-4 mild; 0-2 nothing happens. A question to chat followed by a real reaction to something in the game is a
moment; a question to chat that chat merely answers is not.
why: one short sentence. Judge only from what you are given; never invent events."""

IMPACT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["funny", "shock", "skill", "fail", "rage", "social", "chat_prompt",
                                            "support_thanks", "ad_or_sponsor", "break_or_idle", "just_talking",
                                            "other"]},
        "score": {"type": "integer"},
        "why": {"type": "string"},
    },
    "required": ["kind", "score", "why"],
}


def rate_impact(name: str, game: str, timeline: str, chat: dict, loud: float,
                base: Path | None = None, client=None) -> dict:
    """{"kind", "score", "why"} for one moment. Raises AIError."""
    content = (f"Streamer: {name}\nGame: {game}\nLoudness at the moment: {loud:.1f}x normal\n"
               f"Chat ({chat.get('source')}): most common messages {json.dumps(chat.get('messages') or [])[:600]};"
               f" hype words {json.dumps(chat.get('hype words') or [])}\n"
               f"Transcript (seconds from chat's peak):\n{timeline.strip()[:3500] or '(no clear speech)'}")
    return ask_json(IMPACT_SYSTEM, content, IMPACT_SCHEMA, base, max_tokens=1500, client=client,
                    effort="low", task="impact")
