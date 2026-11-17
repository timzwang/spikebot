"""Turn Tim's feedback (any wording) into lasting editor rules and per-edit changes.

`learn()` backs the /feedback slash command: everything becomes a rule stored in
data/style_profile.json, and its settings are pinned so every future edit (all three
review variants) uses them. `for_variant()` backs the Feedback button on a review
round: it re-renders that variant with the changes, and anything Tim said should
apply from now on is also saved as a rule.

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
MODES = ("lacy", "tokyo_girl")


def _split(rows: list[dict], default_mode: str) -> tuple[list[dict], list[dict], list[str]]:
    """(rows to set, rows to clear, problems)."""
    ok, clear, bad = [], [], []
    for r in rows or []:
        if str(r.get("value", "")).strip().lower() in ("auto", ""):
            mode = r.get("mode") if r.get("mode") in ("all", *MODES) else default_mode
            clear.append({"mode": mode, "setting": str(r.get("setting")), "value": None})
            continue
        good, why = st.apply_settings([r], default_mode)
        bad += why
        for g in good:
            g["scope"] = r.get("scope", "always")
            ok.append(g)
    return ok, clear, bad


LASTING = re.compile(r"(?i)\b(always|from now on|every (edit|clip|video|short)|in future|going forward|never)\b")


def rule_based(text: str, mode: str, profile: st.Profile, current: dict | None = None) -> dict:
    """Same shape as ai.interpret_feedback, from the regex parser. With `current` (a
    review variant's style) changes are relative to it and only for that edit, unless
    the wording says "always" / "from now on"."""
    targets = MODES if mode == "all" else (mode,)
    scope = "always" if current is None or LASTING.search(text or "") else "this_edit"
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
    return {"summary": summary, "settings": uniq, "notes": [],
            "unsupported": unknown, "hook_text": hook, "engine": "rules", "intents": intents}


def interpret(text: str, mode: str, profile: st.Profile, base: Path | None, context: str = "slash",
              current: dict | None = None, client=None) -> dict:
    if client is not None or ai.available(base):
        try:
            out = ai.interpret_feedback(text, mode, base=base, current=current, context=context,
                                        taste=profile.taste(None if mode == "all" else mode), client=client)
            out["engine"] = "claude"
            return out
        except ai.AIError as e:
            log.warning("AI feedback failed, using the rule parser: %s", e)
    return rule_based(text, mode, profile, current if context == "review" else None)


def learn(text: str, mode: str, profile: st.Profile, base: Path | None = None, now: float | None = None,
          client=None) -> tuple[dict, list[str]]:
    """/feedback: store a rule (and pin its settings). Returns (rule, problems)."""
    out = interpret(text, mode, profile, base, "slash", client=client)
    ok, clear, bad = _split(out.get("settings", []), mode)
    for r in ok:
        r.pop("scope", None)
    rule = profile.add_rule(text, out.get("summary", ""), mode, ok + clear, out.get("notes") or [],
                            out.get("unsupported") or [], now=now)
    rule["engine"] = out.get("engine")
    return rule, bad


def for_variant(text: str, mode: str, style: dict, profile: st.Profile, base: Path | None = None,
                item_id: str = "", now: float | None = None, client=None) -> dict:
    """Feedback button: {style (new), texts, intents, comment, rule}. Settings apply to the
    re-render; the ones meant for future edits also become a rule."""
    out = interpret(text, mode, profile, base, "review", current=style, client=client)
    ok, clear, bad = _split(out.get("settings", []), mode)
    new = json.loads(json.dumps(style))
    applied = []
    for r in ok:
        if r["mode"] not in ("all", mode):
            continue
        try:
            new = st.set_value(new, r["setting"], str(r["value"]), mode)
            applied.append(f"{r['setting']} → {r['value']}")
        except ValueError as e:
            bad.append(str(e))
    lasting = [dict(r) for r in ok if r.get("scope") == "always"] + clear
    rule = None
    if lasting or out.get("notes"):
        for r in lasting:
            r.pop("scope", None)
        rule = profile.add_rule(text, out.get("summary", ""), mode, lasting, out.get("notes") or [],
                                out.get("unsupported") or [], now=now, source=f"review {item_id}")
    texts = {"hook": out["hook_text"]} if out.get("hook_text") else {}
    bits = [out.get("summary") or ("Applied: " + ", ".join(applied) if applied else "No change recognised")]
    if applied:
        bits.append("Changed: " + ", ".join(applied))
    if rule and rule["settings"]:
        bits.append(f"Saved for future edits as rule #{rule['id']}")
    if out.get("unsupported"):
        bits.append("Can't do yet: " + " | ".join(u[:90] for u in out["unsupported"][:3]))
    if bad:
        bits.append("Skipped: " + " | ".join(b[:90] for b in bad[:3]))
    intents = out.get("intents") or [f"{r['setting']}={r['value']}" for r in ok]
    return {"style": new, "texts": texts, "intents": intents, "comment": ". ".join(bits), "rule": rule,
            "engine": out.get("engine")}


def format_rule(rule: dict) -> str:
    """One Discord block for a stored rule."""
    lines = [f"**#{rule['id']}** ({'every mode' if rule['mode'] == 'all' else rule['mode']}) {rule.get('summary') or ''}"]
    if rule.get("settings"):
        lines.append("Settings: " + ", ".join(
            f"{s['setting']}={'auto' if s['value'] is None else s['value']}"
            + ("" if s["mode"] == "all" else f" ({s['mode']})") for s in rule["settings"]))
    for n in (rule.get("notes") or [])[:4]:
        lines.append(f"• {n}")
    if rule.get("unsupported"):
        lines.append("Not possible yet: " + " | ".join(u[:120] for u in rule["unsupported"][:3]))
    return "\n".join(lines)
