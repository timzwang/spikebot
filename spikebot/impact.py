"""Is a chat spike a real moment? Filters spikes that only happened because the streamer asked chat
something (a poll, "type 1", "say hi"), or chat was busy with subs, raids, giveaways or bot
commands, so no time is spent downloading or editing them (Tim, 19 Nov).

Three checks, cheapest first:

1. `chat_verdict` (at the spike, free, in main.Bot.handle): most of the surge was answers
   ("1", "2", "yes", "b"), commands ("!enter") or sub/raid/greeting messages, with little hype
   and no "clip it" -> status `chat_prompt`: saved, but no #moments alert and no clip.
2. `speech_verdict` (after the segment is transcribed, before any rendering): the streamer
   asked chat something / thanked subs just before the peak, and there is no reaction after it
   (no reaction words, no loud moment) -> the clip job is skipped.
3. `ai_verdict` (with ANTHROPIC_API_KEY): Claude rates the moment 0-10 from the transcript around
   the peak, chat's messages and how loud the reaction was; a non-moment kind or a score under
   `min_score` -> skipped. Any API failure lets the clip through (checks 1-2 still apply).

Settings: config `impact` block (`enabled`, `chat_prompt_share`, `min_score`, `ai`).
Everything here except `ai_verdict` is pure.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("impact")

DEFAULTS = {"enabled": True, "chat_prompt_share": 0.45, "max_hype_share": 0.3, "max_clip_mentions": 2,
            "min_score": 4, "ai": True, "loud_ratio": 2.2}

# Phrases where the streamer turns to chat rather than something happening in the game.
ASKED_RE = re.compile(
    r"\b(chat,? (what|should|who|which|is|are|do|does|did|can|would|how|type|vote|say|spam|tell|let|drop|"
    r"give|put|help|guess|pick|choose|rate)|type (it )?(in|one|two|1|2|yes|no|a|b|w|l)\b|in (the )?chat|"
    r"vote|poll|prediction|one or two|1 or 2|yes or no|a or b|let me know|what do you (guys )?think|"
    r"should i\b|which one|who wants|spam (the |a |some )?\w+|drop (a|an|some|your) \w+|say hi|"
    r"put (a|an|some|your) \w+ in)", re.IGNORECASE)
THANKS_RE = re.compile(
    r"\b(thanks?( you)? (so much )?for (the )?(\d+ )?(gift(ed)?|subs?|sub|prime|resub|raid|bits|dono|"
    r"donation|follow|tip|cheer)|appreciate (the|you) \w*\s?(subs?|gift|raid|dono)|welcome in|"
    r"welcome raiders|giveaway|hype train|use code|sponsor|ad break|running (an |some )?ads?)", re.IGNORECASE)
# What a real reaction sounds like in a transcript.
REACTION_RE = re.compile(
    r"\b(ha(ha)+|he(he)+|lol|lmao|oh my (god|gosh)|omg|no way|no no|what the|what\?|bro+|yo+|"
    r"let'?s go+|holy|nah+|oh (shit|no|my)|wait what|are you (serious|kidding)|i can'?t|dude|wow|"
    r"jesus|stop|run|go go)\b", re.IGNORECASE)
NON_MOMENTS = ("chat_prompt", "support_thanks", "ad_or_sponsor", "break_or_idle", "just_talking")


def settings(cfg: dict | None) -> dict:
    return DEFAULTS | {k: v for k, v in ((cfg or {}).get("impact") or {}).items() if v is not None}


# ---- 1. chat at the spike --------------------------------------------------------------
def chat_verdict(rec: dict, cfg: dict | None = None) -> str | None:
    """Why this spike isn't a moment (chat was busy answering / entering / welcoming), or None.
    `rec` is a Spike as a dict (old records without the shares always pass)."""
    s = settings(cfg)
    if not s["enabled"]:
        return None
    shares = {k: float(rec.get(f"{k}_share") or 0) for k in ("answer", "command", "event")}
    busy = sum(shares.values())
    if (busy < float(s["chat_prompt_share"]) or float(rec.get("hype_share") or 0) >= float(s["max_hype_share"])
            or int(rec.get("clip_mentions") or 0) > int(s["max_clip_mentions"])):
        return None
    what = max(shares, key=shares.get)
    examples = [t for t in (rec.get("sample") or []) if len(t) <= 20][:3]
    eg = f" (e.g. {', '.join(repr(t) for t in examples)})" if examples else ""
    return {"answer": "chat was answering the streamer (a question or poll)",
            "command": "chat was typing bot commands / giveaway entries",
            "event": "chat was busy with subs, a raid or greetings"}[what] + f": {busy:.0%} of messages{eg}"


# ---- 2. what the streamer said, and whether anything happened ----------------------------
def text_between(words: list, a: float, b: float) -> str:
    return " ".join(w.text.strip() for w in words if a <= w.start <= b)


def reaction_level(levels: list[float], peak: float, hop_s: float = 0.1) -> float:
    """Loudest half-second in the 8 s before / 2 s after chat's peak, over the clip's median
    level (1 = no louder than usual). Pure."""
    if not levels:
        return 0.0
    srt = sorted(levels)
    median = max(srt[len(srt) // 2], 1e-4)
    lo, hi = max(0, int((peak - 8) / hop_s)), min(len(levels), int((peak + 2) / hop_s) + 1)
    if hi <= lo:
        return 0.0
    k = 5
    best = max(sum(levels[max(lo, i - k // 2):min(hi, i + k // 2 + 1)]) / k for i in range(lo, hi))
    return round(best / median, 2)


def speech_verdict(words: list, peak: float | None, levels: list[float] | None = None,
                   cfg: dict | None = None) -> dict:
    """{"skip": bool, "why": str, "asked": phrase|None, "reaction": bool, "loud": ratio}. Skips only
    when the streamer turned to chat (or thanked subs / read an ad) just before the peak AND there
    is no reaction: no reaction words around the peak and nothing loud. Pure."""
    s = settings(cfg)
    peak = peak if peak is not None else (words[-1].end * 0.55 if words else 0.0)
    before = text_between(words, peak - 30, peak + 2)
    around = text_between(words, peak - 8, peak + 4)
    asked = ASKED_RE.search(before) or THANKS_RE.search(before)
    reacted = bool(REACTION_RE.search(around))
    loud = reaction_level(levels or [], peak)
    out = {"skip": False, "why": "", "asked": asked.group(0) if asked else None, "reaction": reacted, "loud": loud}
    if not s["enabled"] or not asked or reacted or loud >= float(s["loud_ratio"]):
        return out
    kind = "thanked supporters / read an ad" if THANKS_RE.search(before) and not ASKED_RE.search(before) \
        else "asked chat something"
    out.update(skip=True, why=f"the streamer {kind} (“{asked.group(0)}”) and nothing happened after it")
    return out


# ---- 3. Claude's rating --------------------------------------------------------------------
def timeline(words: list, peak: float, before: float = 30, after: float = 10) -> str:
    """Transcript lines stamped relative to chat's peak ("[-12.4s] chat type one if...")."""
    lines, cur, t0 = [], [], None
    for w in words:
        if not (peak - before <= w.start <= peak + after):
            continue
        if cur and (w.start - prev_end > 0.7 or len(cur) >= 14):
            lines.append(f"[{t0 - peak:+.1f}s] " + " ".join(cur))
            cur = []
        if not cur:
            t0 = w.start
        cur.append(w.text.strip())
        prev_end = w.end
    if cur:
        lines.append(f"[{t0 - peak:+.1f}s] " + " ".join(cur))
    return "\n".join(lines)


def ai_verdict(job: dict, words: list, peak: float | None, loud: float, cfg: dict | None = None,
               base: Path | None = None, client=None) -> dict | None:
    """{"score", "kind", "why", "skip"} from Claude, or None (no key, turned off, or the API failed)."""
    s = settings(cfg)
    if not s["enabled"] or not s["ai"]:
        return None
    from . import ai
    if client is None and not ai.available(base):
        return None
    peak = peak if peak is not None else (words[-1].end * 0.55 if words else 0.0)
    chat = {"messages": job.get("sample") or [], "hype words": job.get("vibe") or [],
            "source": "chat spike" if job.get("sample") else "loud moment in the audio (no chat data)"}
    try:
        r = ai.rate_impact(job.get("streamer") or "Lacy", job.get("category") or "GTA", timeline(words, peak),
                           chat, loud, base=base, client=client)
    except ai.AIError as e:
        log.info("clip %s: impact rating unavailable (%s); letting it through", job.get("id"), e)
        return None
    score = max(0, min(10, int(r.get("score") or 0)))
    kind = r.get("kind") or "other"
    skip = kind in NON_MOMENTS or score < int(s["min_score"])
    return {"score": score, "kind": kind, "why": str(r.get("why") or "")[:200], "skip": skip}


def judge(job: dict, words: list, levels: list[float], cfg: dict | None = None, base: Path | None = None,
          client=None) -> dict:
    """Checks 2 and 3 for a clip job. {"skip": bool, "why": str, ...details}."""
    peak = (job["peak_time"] - job["clip_start"]) if job.get("peak_time") else None
    sp = speech_verdict(words, peak, levels, cfg)
    if sp["skip"]:
        return sp | {"by": "speech"}
    rating = ai_verdict(job, words, peak, sp["loud"], cfg, base, client)
    if rating is None:
        return sp | {"by": "speech"}
    why = (f"Claude rated it {rating['score']}/10 ({rating['kind'].replace('_', ' ')}): {rating['why']}"
           if rating["skip"] else "")
    return sp | rating | {"why": why, "by": "ai"}
