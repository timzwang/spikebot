"""Turn Tim's feedback (any wording) into lasting editor rules and per-edit changes.

`learn()` backs the /feedback slash command: every setting, note and "can't do yet" item
is appended to the numbered rules list in data/style_profile.json (/rules), and the newest
enabled setting wins in every future edit (all three review variants). `for_variant()`
backs the Feedback button on a review round: it re-renders that variant with the changes
and saves them to the list too, unless Tim says it's only for this one edit.

With ANTHROPIC_API_KEY set the Claude API reads the feedback (spikebot/ai.py);
without it, or if the call fails, the rule-based parser in style.py is used.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import ai
from . import style as st

log = logging.getLogger("feedback")
MODES = st.STYLE_MODES   # lacy, tokyo_girl, trend
# Feedback on one clip applies to that kind of clip only, unless Tim says it's for everything.
EVERY = re.compile(r"(?i)\b(all|every|both|each)\b.{0,14}\b(edits?|clips?|videos?|modes?|shorts?|kinds?)\b"
                   r"|\beverywhere\b|\bfor everything\b|\ball of them\b")


def _split(rows: list[dict], default_mode: str) -> tuple[list[dict], list[dict], list[str]]:
    """(rows to set, rows to clear, problems)."""
    ok, clear, bad = [], [], []
    for r in rows or []:
        if str(r.get("value", "")).strip().lower() in ("auto", ""):
            mode = r.get("mode") if r.get("mode") in ("all", *MODES) else default_mode
            clear.append({"mode": mode, "modes": st.expand_modes(mode), "setting": str(r.get("setting")),
                          "value": None})
            continue
        good, why = st.apply_settings([r], default_mode)
        bad += why
        for g in good:
            g["scope"] = r.get("scope", "always")
            ok.append(g)
    return ok, clear, bad


KEEP_FOOTAGE = re.compile(r"(?i)\b(same|exact|identical|keep( the)?|original)\s+(clips?|footage|shots?|scenes?|video)\b"
                          r"|\bdon'?t change the (clips?|footage|shots?)\b|\bclips? (as|from) [a-c]\d\b")
NEW_FOOTAGE = re.compile(r"(?i)\b(different|new|other|fresh|change the)\s+(clips?|footage|shots?|scenes?)\b")
SONG_TO = re.compile(r"(?i)\b(?:song|sound|music|track|audio)\b\s*(?:to|as|=|:|with|->)\s*[\"“']?([^\"”'.,;\n]{2,40}?)[\"”']?"
                     r"(?=\s+(?:and|but|with|please|instead)\b|[.,;!\n]|$)")
SONG_NAMED = re.compile(r"(?i)\b(?:use|switch to|change to|swap to)\s+(?:the\s+)?[\"“']?((?:[\w'-]+\s+){0,3}?[\w'-]+)[\"”']?"
                        r"\s+(?:song|sound|track)\b")
SONG_QUOTED = re.compile(r"(?i)\b(?:song|sound|music|track|audio)\b[^\"“\n]{0,20}[\"“]([^\"”\n]{2,60})[\"”]")
SONG = SONG_TO   # used to spot sound requests in "can't do" lines


def footage_of(text: str) -> str:
    """"keep" / "new" / "default" from the wording (rule parser)."""
    if NEW_FOOTAGE.search(text or "") and not KEEP_FOOTAGE.search(text or ""):
        return "new"
    return "keep" if KEEP_FOOTAGE.search(text or "") else "default"


def song_of(text: str) -> str | None:
    m = SONG_QUOTED.search(text or "") or SONG_TO.search(text or "") or SONG_NAMED.search(text or "")
    return m.group(1).strip() if m else None


# A Feedback press on one video is about THAT video. It becomes a saved rule only when Tim says so
# in words (Tim, 18 Nov 2026: "stop making all feedback automatically new rules"). /feedback is the
# way to add rules on purpose.
ALWAYS = re.compile(r"(?i)\b(always|from now on|in (the )?future|going forward|every ?time|next time|"
                    r"make (it|this|that) a rule|save (it|this|that)( as a rule)?|as a rule|by default|"
                    r"(on|for|in) (all|every|future)\b.{0,14}\b(edits?|clips?|videos?|shorts?)|never)\b")
ONLY_THIS = re.compile(r"(?i)\b(just|only) (for )?(this|that|here)\b|\b(this|that) one only\b|\bjust here\b|"
                       r"\bthis (edit|clip|video|one) only\b|\bnot (always|every time|in future)\b")


def rule_based(text: str, mode: str, profile: st.Profile, current: dict | None = None) -> dict:
    """Same shape as ai.interpret_feedback, from the regex parser. With `current` (a
    review variant's style) changes are relative to it; they are kept for future edits
    too unless the wording says it's only for this one ("just this one", "only here")."""
    targets = MODES if mode == "all" else (mode,)
    scope = "this_edit" if current is not None and ONLY_THIS.search(text or "") else "always"
    rows: list[dict] = []
    intents: list[str] = []
    unknown: list[str] = []
    hook = None
    for m in targets:
        changes, texts, intents, unknown = st.parse_feedback(text, m)
        hook = texts.get("hook") or hook
        if current is not None:
            before = json.loads(json.dumps(current))
        else:
            before = profile.preferred(m)
            cp = profile.custom_pins(m)
            if cp:
                before["custom"] = cp
        after = st.apply_changes(before, changes)
        for dim in st.dims(m):
            if after.get(dim) != before.get(dim) and after.get(dim) is not None:
                rows.append({"mode": m if dim in ("hook", "ending", "intro") or mode != "all" else "all",
                             "setting": dim, "value": str(after[dim]), "scope": scope})
        for k, v in (after.get("custom") or {}).items():
            if (before.get("custom") or {}).get(k) != v:
                rows.append({"mode": mode, "setting": k, "value": str(v), "scope": scope})
    seen, uniq = set(), []
    for r in rows:
        key = (r["mode"], r["setting"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    summary = ("Understood: " + ", ".join(intents)) if intents else "Nothing I could turn into a setting"
    footage, song = footage_of(text), song_of(text)
    if current is not None:   # per-edit requests aren't "can't do" items
        unknown = [u for u in unknown if not (KEEP_FOOTAGE.search(u) or NEW_FOOTAGE.search(u) or SONG.search(u))]
    return {"summary": summary, "settings": uniq, "notes": [],
            "unsupported": unknown, "hook_text": hook, "engine": "rules", "intents": intents,
            "footage": footage, "song": song}


def interpret(text: str, mode: str, profile: st.Profile, base: Path | None, context: str = "slash",
              current: dict | None = None, client=None, sounds: list[str] | None = None) -> dict:
    if client is not None or ai.available(base):
        try:
            out = ai.interpret_feedback(text, mode, base=base, current=current, context=context,
                                        taste=profile.taste(None if mode == "all" else mode), client=client,
                                        sounds=sounds)
            out["engine"] = "claude"
            return out
        except ai.AIError as e:
            log.warning("AI feedback failed, using the rule parser: %s", e)
    return rule_based(text, mode, profile, current if context == "review" else None)


def scoped(rows: list[dict], mode: str) -> list[dict]:
    """Rows limited to `mode` (an explicit choice of which edits the feedback is about)."""
    out = []
    for r in rows:
        r = dict(r, mode=mode)
        r.pop("modes", None)
        out.append(r)
    return out


def learn(text: str, mode: str, profile: st.Profile, base: Path | None = None, now: float | None = None,
          client=None) -> tuple[dict, list[str]]:
    """/feedback: store a rule (and pin its settings). `mode`: which edits it's for ("all",
    lacy, tokyo_girl, trend): an explicit choice, so every row gets exactly that.
    Returns (rule, problems)."""
    out = interpret(text, mode, profile, base, "slash", client=client)
    rows = out.get("settings", [])
    if mode != "all":
        rows = scoped(rows, mode)
    ok, clear, bad = _split(rows, mode)
    for r in ok:
        r.pop("scope", None)
    rule = profile.add_rule(text, out.get("summary", ""), mode, ok + clear, out.get("notes") or [],
                            out.get("unsupported") or [], now=now)
    rule["engine"] = out.get("engine")
    return rule, bad


def for_variant(text: str, mode: str, style: dict, profile: st.Profile, base: Path | None = None,
                item_id: str = "", now: float | None = None, client=None, sounds: list[str] | None = None) -> dict:
    """Feedback button: {style (new), texts, intents, comment, rule, footage, song}. Settings apply
    to the re-render. Nothing is saved as a rule unless the wording asks for it (`ALWAYS`). `footage` ("keep" unless
    Tim asks for new clips) and `song` (a sound to switch to) are for this edit only."""
    out = interpret(text, mode, profile, base, "review", current=style, client=client, sounds=sounds)
    footage = out.get("footage") if out.get("footage") in ("keep", "new") else "keep"
    song = (out.get("song") or "").strip() or None
    everywhere = bool(EVERY.search(text or ""))
    rows = out.get("settings", [])
    if not everywhere:   # about this clip: this kind of clip only
        rows = [r if r.get("mode") in MODES and r.get("mode") != mode and _named(text, r["mode"]) else
                dict(r, mode=mode) for r in rows]
    ok, clear, bad = _split(rows, mode)
    new = json.loads(json.dumps(style))
    applied = []
    for r in ok:
        if mode not in (r.get("modes") or st.expand_modes(r["mode"])):
            continue
        try:
            new = st.set_value(new, r["setting"], str(r["value"]), mode)
            applied.append(f"{r['setting']} → {r['value']}")
        except ValueError as e:
            bad.append(str(e))
    keep = bool(ALWAYS.search(text or "")) and not ONLY_THIS.search(text or "")
    lasting = ([dict(r) for r in ok if r.get("scope") != "this_edit"] + clear) if keep else []
    rule = None
    if keep and (lasting or out.get("notes") or out.get("unsupported")):
        for r in lasting:
            r.pop("scope", None)
        rule = profile.add_rule(text, out.get("summary", ""), "all" if everywhere else mode, lasting,
                                out.get("notes") or [], out.get("unsupported") or [], now=now,
                                source=f"review {item_id}")
    texts = {"hook": out["hook_text"]} if out.get("hook_text") else {}
    bits = [out.get("summary") or ("Applied: " + ", ".join(applied) if applied else "No change recognised")]
    if applied:
        bits.append("Changed: " + ", ".join(applied))
    if rule and rule["entries"]:
        bits.append("Saved to /rules as " + ", ".join(f"#{e['n']}" for e in rule["entries"]))
    if applied and not keep:
        bits.append("Only this video (say \"always\" or \"from now on\" to save it as a rule)")
    elif len(applied) > len((rule or {}).get("settings") or []):
        bits.append("(some changes were only for this edit)")
    bits.append("Same clips as before" if footage == "keep" else "New clips")
    if song:
        bits.append(f"Sound: {song}")
    if out.get("unsupported"):
        bits.append("Can't do yet: " + " | ".join(u[:90] for u in out["unsupported"][:3]))
    if bad:
        bits.append("Skipped: " + " | ".join(b[:90] for b in bad[:3]))
    intents = out.get("intents") or [f"{r['setting']}={r['value']}" for r in ok]
    return {"style": new, "texts": texts, "intents": intents, "comment": ". ".join(bits), "rule": rule,
            "engine": out.get("engine"), "footage": footage, "song": song}


NAMED = {"lacy": r"\blacy\b", "tokyo_girl": r"\btokyo\s*girl\b", "trend": r"\btrend(ing)?\b"}


def _named(text: str, mode: str) -> bool:
    """Did Tim name that kind of edit himself (e.g. "and on Lacy clips too")?"""
    return bool(re.search(NAMED.get(mode, "$^"), text or "", re.I))


def format_rule(rule: dict) -> str:
    """What one feedback added to the rules list, one numbered line per entry."""
    lines = [rule.get("summary") or "Saved:"]
    for e in rule.get("entries") or []:
        tag = {"note": " _(note: being turned into settings automatically)_",
               "unsupported": " _(not possible yet: queued for new editor code)_"}.get(e["kind"], "")
        lines.append(f"**#{e['n']}** {st.entry_text(e, where=False)}{tag} → applies to"
                     f" **{st.modes_label(st.entry_modes(e))}**")
    if not rule.get("entries"):
        lines.append("Nothing saved.")
    return "\n".join(lines)
