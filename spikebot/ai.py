"""Claude API helpers: read Tim's feedback in plain words, and study what makes a
reference Short look good.

Optional. Without ANTHROPIC_API_KEY (environment, or .env next to config.json) every
caller falls back to the rule-based parser (style.parse_feedback) and the measured
numbers only. The `anthropic` package is imported lazily, so the chat bot never
needs it.

Models: /feedback runs on Sonnet 5.5 (SPIKEBOT_FEEDBACK_MODEL), #inspiration on Opus 5.5
(SPIKEBOT_AI_MODEL). Cost: one /feedback is a few thousand tokens (under 1p); one #inspiration
analysis with 8 small frames is roughly 10-15k tokens (about 5-8p).

Privacy/policy: only frames and numbers are sent, never the reference file itself,
and the model is told not to copy on-screen words, speech or footage. Nothing it
returns is ever burned into our edits as their wording.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from . import style as st

log = logging.getLogger("ai")

MODEL = "claude-opus-5-5"            # #inspiration study (vision, harder judgement)
FEEDBACK_MODEL = "claude-sonnet-5-5"  # /feedback and the review Feedback button
FALLBACK_BETA = "server-side-fallback-2026-07-01"


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
    """`task="feedback"` -> SPIKEBOT_FEEDBACK_MODEL (default Sonnet 5.5); anything else ->
    SPIKEBOT_AI_MODEL (default Opus 5.5). .env next to config wins over the environment."""
    var, default = ("SPIKEBOT_FEEDBACK_MODEL", FEEDBACK_MODEL) if task == "feedback" \
        else ("SPIKEBOT_AI_MODEL", MODEL)
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
    """One structured-output request. Returns the parsed JSON. Raises AIError."""
    if client is None:
        try:
            import anthropic
        except ImportError as e:
            raise AIError("the anthropic package is not installed") from e
        key = api_key(base)
        if not key:
            raise AIError("ANTHROPIC_API_KEY is not set")
        client = anthropic.Anthropic(api_key=key, timeout=120.0)
    try:
        resp = client.beta.messages.create(
            model=model_name(base, task), max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=[FALLBACK_BETA], fallbacks="default")
    except Exception as e:  # noqa: BLE001 - network/API errors become a fallback, never a crash
        raise AIError(f"Claude API call failed: {type(e).__name__}: {str(e)[:200]}") from e
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
        "framing: box45, box11, stack, fill, fit  (Tokyo Girl and trend edits only: box45 = a big 4:5 picture over a"
        " blurred copy with text in the bands above/below (default); box11 = the same at 1:1; stack = two clips at once,"
        " one above the other, each whole; fill crops the 16:9 trailer to fill the 9:16 screen; fit shows"
        " the whole frame so nothing is cut off, over a blurred copy of itself)",
        "smart_fill (custom): 0 or 1  (Tokyo Girl and trend edits: 1 = a close-up of one person goes full screen and"
        " every crop is centred on the subject; default 1)",
        "media: clips, mixed  (Tokyo Girl and trend edits only: clips = real trailer video only, never screenshots)",
        "text_level: none, minimal, full, plain  (none = no captions/hook/text at all; minimal = short "
        "opening hook and captions only around the payoff (Lacy) / only the opening card (Tokyo Girl); "
        "full = every spoken word captioned (Lacy) / every story card (Tokyo Girl); plain = Tokyo Girl and trend edits only:"
        " one short factual line such as 'GTA 6 is almost here...' or the release countdown, never a question)",
        "intro: none, logo, countdown  (Tokyo Girl and trend edits only: logo = the official GTA VI logo builds up over logo_s"
        " seconds over a calm trailer shot, then bursts outward and fades exactly on the song's drop, never"
        " wider than logo_size of the screen; countdown = a plain 'N days until GTA 6' line instead)",
        "ending: " + ", ".join(st.ENDINGS) + "  (Lacy only: end card)",
        "hook: Lacy tones " + ", ".join(st.HOOKS["lacy"]) + "; Tokyo Girl / trend story kinds "
        + ", ".join(st.HOOKS["tokyo_girl"]) + "  (needs mode lacy, tokyo_girl or trend)",
    ]
    for k, (lo, hi) in st.CUSTOM_KEYS.items():
        lines.append(f"{k}: number {lo} to {hi}")
    lines.append("text_colour, highlight: hex colour like #FFE500")
    lines.append("grain: film grain strength (0 = off, 6-12 subtle); vignette: 1 = on, 0 = off; "
                 "letterbox: 1 = cinema bars (Tokyo Girl only)")
    lines.append("hook_s: seconds the opening hook stays on screen; payoff: where the peak lands (0.2-0.9)")
    lines.append("Tokyo Girl timing: max_s = longest a Tokyo Girl edit may be, intro included (e.g. 24; trending-"
                 "sound edits ignore it and only stop before their sound ends); hold_beats ="
                 " fewest beats each clip is held (2+ keeps a clip's action intact, fewer cuts); logo_s = seconds"
                 " the logo builds before the drop; logo_size = the logo's largest width as a share of the screen"
                 " (0.6 = 60%); fade_s = crossfade length in seconds")
    lines.append("Footage is matched to each story's tags; the #inspiration presets Tim approves steer pacing"
                 " and look. Edits are exported silent, but which SOUND an edit is cut to (its beats, drop and"
                 " length) is chosen per edit with the `song` field, never a setting, note or unsupported item."
                 " Which clips a re-render uses is the `footage` field.")
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
                "text_level", "intro", "ending", "transition"):
            row["value"] = "auto"
    return out


# --------------------------------------------------------------------------
# #inspiration: the artistic side of a reference Short
# --------------------------------------------------------------------------
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
    },
    "required": ["summary", "what_makes_it_good", "techniques", "text_use", "opening", "settings", "avoid"],
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
                    max_tokens=6000, client=client, effort="high")


# --------------------------------------------------------------------------
# Stream clips: YouTube title + post caption written from what was said
# --------------------------------------------------------------------------
CLIP_COPY_SYSTEM = """You write the YouTube Shorts title and the TikTok/Instagram caption for a short clip of a
streamer playing GTA. You get the streamer's name, the game, chat's reaction words and the transcript of
what was said in the clip. Write like a real clipper account that people actually follow, not like a bot:
- Describe what actually happens or is said in THIS clip, specifically. Use the transcript; never invent
  events that aren't in it. If the transcript is vague, keep it simple ("{name} loses it at this GTA 6 moment").
- title: 25-70 characters, must contain the streamer's name exactly as given, plain English, sentence case,
  no hashtags, no emojis, no ALL CAPS words except GTA, no quotes around the whole thing.
- caption: 15-80 characters, casual and lowercase is fine, must contain the streamer's name, at most one emoji,
  no hashtags (they are added later), must not repeat the title word for word.
- Never: insults, mocking, drama, negativity about the streamer, "you won't believe", "insane", "crazy",
  "wait for it", "watch till the end", "POV", "theory", questions to farm comments, story spoilers.
Give exactly the number of options asked for, each clearly different in angle."""

CLIP_COPY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"options": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"title": {"type": "string"}, "caption": {"type": "string"}},
        "required": ["title", "caption"]}}},
    "required": ["options"],
}


def write_clip_copy(name: str, game: str, transcript: str, vibe: list[str] | None = None, n: int = 3,
                    base: Path | None = None, client=None) -> list[dict]:
    """[{title, caption}] x n for one clip. Raises AIError (callers fall back to templates)."""
    content = (f"Streamer: {name}\nGame: {game}\nChat reaction: {', '.join((vibe or [])[:8]) or 'none'}\n"
               f"Options wanted: {n}\nTranscript:\n{transcript.strip()[:3000] or '(no clear speech)'}")
    out = ask_json(CLIP_COPY_SYSTEM.replace("{name}", name), content, CLIP_COPY_SCHEMA, base,
                   max_tokens=1500, client=client, effort="low", task="feedback")
    return [o for o in out.get("options") or [] if o.get("title") and o.get("caption")]
