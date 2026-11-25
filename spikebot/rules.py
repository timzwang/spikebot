"""Rule compiler: every saved rule ends up in the edit, without anyone hand-coding it.

Settings entries in the rules list (/rules) already drive every render. Notes and "not
possible yet" items used to sit there unused until someone wrote a regex for them. This
module reads each enabled note / "not possible" entry against *what the editor can do
right now* (`ai.settings_menu()` + `style.BUILT_IN`) and decides, per entry:

- settings:   it maps onto real settings -> they are added as new rules (newest wins) and the
              note is switched off with "now #N";
- built_in:   the editor already does it by itself -> switched off, "built in now";
- needs_code: the editor needs new code -> it stays on, marked, written to
              data/rules/needs_code.json and (with GITHUB_TOKEN in .env) filed as a GitHub
              issue labelled `rule-request`, which the scheduled Claude Code job builds;
- not_editor: it's about something other than the edit (posting times, titles...) -> kept as a note.

Each verdict is stamped with `capabilities_version()` (a hash of the settings menu and
BUILT_IN). When new code adds a setting, the hash changes and every remaining note is read
again, so a rule that needed code turns itself on once that code is deployed.

Runs in the approval bot (`ReviewBot.rules_loop`), never in a render. Needs ANTHROPIC_API_KEY;
without it nothing is decided (and nothing is lost: it retries when a key is there).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import ai
from . import style as st

log = logging.getLogger("rules")

REPO = "timzwang/spikebot"
LABELS = ["rule-request", "owner:claude-code", "automate"]
BATCH = 25
VERDICTS = ("settings", "built_in", "needs_code", "not_editor")


def capabilities_version() -> str:
    text = ai.settings_menu() + "\n" + "\n".join(st.BUILT_IN)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


RECHECK_S = 24 * 3600   # a "needs code" note is read again after a deploy at most once a day


def pending(entries: list[dict], version: str, now: float | None = None) -> list[dict]:
    """Enabled notes / "not possible" items not yet read against this version of the editor.
    Credits (Tim, 24 Nov): a note already judged "not about the editor" is never asked again, and
    one that needs new code is asked again only when the editor changed AND a day has passed, so a
    run of deploys doesn't re-send every note each time."""
    now = now or time.time()
    out = []
    for e in entries:
        if not e.get("on", True) or e.get("kind") not in ("note", "unsupported") or e.get("checked") == version:
            continue
        if e.get("checked") and e.get("status") == "not_editor":
            continue
        if e.get("checked") and now - float(e.get("checked_at") or 0) < RECHECK_S:
            continue
        out.append(e)
    return out


RESULT = {
    "type": "object",
    "properties": {
        "n": {"type": "integer"},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "settings": {"type": "array", "items": ai.SETTING_ROW},
        "reason": {"type": "string"},
        "needs": {"type": "string"},
    },
    "required": ["n", "verdict", "settings", "reason", "needs"],
    "additionalProperties": False,
}
SCHEMA = {"type": "object", "properties": {"results": {"type": "array", "items": RESULT}},
          "required": ["results"], "additionalProperties": False}

SYSTEM = """You maintain the rules of an automated GTA 6 Shorts editor. The creator gave instructions that
were saved as notes because, when he gave them, no setting matched. Read each one against what the
editor can do NOW and give a verdict.

Edit kinds: lacy (Lacy's stream clips: facecam over gameplay, his voice), tokyo_girl (silent edits of
official GTA VI trailers, text cards; music added later in the app), trend (the same silent trailer edit
cut to a trending sound; same settings as tokyo_girl).

Settings the editor has (names and values exactly as written):
{menu}

What the editor already does by itself:
- {built_in}

Verdicts:
- "settings": the instruction is fully or mostly achieved by settings. List every row needed (scope
  "always", mode = the kinds it is about; only kinds the note applies to). Put anything still missing
  in "needs".
- "built_in": the editor already does it (see the list above), no setting needed.
- "needs_code": no setting comes close; the editor needs new code. "needs" = a short, concrete spec for
  a developer (what to build, which edit kinds, sensible default, and the setting name you would add).
- "not_editor": not about how an edit looks or is cut (e.g. posting schedule, captions for the app
  post, account strategy).
Be faithful to what he asked: never pick a setting that only loosely relates. "reason": one short
sentence
for the creator."""


def ask(entries: list[dict], effective: dict[str, dict], base: Path | None, client=None) -> list[dict]:
    lines = []
    for e in entries:
        lines.append(json.dumps({"n": e["n"], "kind": e["kind"], "applies_to": st.entry_modes(e),
                                 "text": e.get("text", ""), "he_said": (e.get("said") or "")[:400]},
                                ensure_ascii=False))
    content = ("Settings in force now, per edit kind: " + json.dumps(effective, sort_keys=True)
               + "\n\nSaved instructions (one JSON per line):\n" + "\n".join(lines))
    system = SYSTEM.format(menu=ai.settings_menu(), built_in="\n- ".join(st.BUILT_IN))
    out = ai.ask_json(system, content, SCHEMA, base=base, max_tokens=8000, client=client,
                      effort="medium", task="rules")
    return out.get("results") or []


def decide(profile: st.Profile, base: Path | None, client=None, limit: int = BATCH) -> tuple[str, list[dict]]:
    """Ask the model about the pending entries of `profile` (read only). Returns (version, results).
    Raises ai.AIError."""
    version = capabilities_version()
    todo = pending(profile.entries(), version)[:limit]
    if not todo:
        return version, []
    effective = {m: profile._effective(m) for m in st.STYLE_MODES}
    results = ask(todo, effective, base, client=client)
    # an entry the answer skipped is still marked as read (no verdict): otherwise it was asked
    # again every 2 minutes, forever
    got = {int(r.get("n", -1)) for r in results}
    results += [{"n": e["n"], "verdict": "none", "settings": [], "reason": "", "needs": ""}
                for e in todo if e["n"] not in got]
    return version, results


def apply(profile: st.Profile, version: str, results: list[dict], now: float | None = None) -> list[dict]:
    """Write verdicts into the profile (call under the profile lock, on a freshly loaded profile).
    Returns one change record per entry handled: {n, verdict, added: [n...], needs, reason, text}."""
    now = now or time.time()
    changes = []
    for r in results:
        e = profile.entry(int(r.get("n", -1)))
        if e is None or not e.get("on", True) or e.get("kind") not in ("note", "unsupported"):
            continue
        if r.get("verdict") == "none":   # skipped by the model: read, nothing changes
            e["checked"], e["checked_at"] = version, now
            continue
        verdict = r.get("verdict") if r.get("verdict") in VERDICTS else "needs_code"
        mine = st.entry_modes(e)
        rows = [dict(row, modes=[m for m in st.expand_modes(row.get("mode")) if m in mine] or mine)
                for row in (r.get("settings") or [])] if verdict == "settings" else []
        good, bad = st.apply_settings(rows, e.get("mode", "all"))
        added = []
        for row in good:
            new = profile.add_entry("setting", row["modes"], row["setting"], row["value"],
                                    said=e.get("text", ""), source=f"from rule #{e['n']}", now=now)
            added.append(new["n"])
        needs = (r.get("needs") or "").strip()
        if verdict == "settings" and not added:
            verdict, needs = "needs_code", needs or "; ".join(bad) or (r.get("reason") or "")
        e["checked"], e["checked_at"] = version, now
        e["reason"] = (r.get("reason") or "")[:300]
        if added and not needs:
            e["on"], e["upgraded"], e["status"] = False, "now " + ", ".join(f"#{n}" for n in added), "done"
        elif verdict == "built_in":
            e["on"], e["upgraded"], e["status"] = False, "built in now", "done"
        elif verdict == "not_editor":
            e["status"] = "not_editor"
        else:
            e["status"] = "needs_code"
            e["needs"] = needs[:1000] or e.get("text", "")
            if added:
                e["partly"] = "partly done by " + ", ".join(f"#{n}" for n in added)
        changes.append({"n": e["n"], "verdict": verdict, "added": added, "needs": e.get("needs", ""),
                        "reason": e["reason"], "text": e.get("text", "")})
    return changes


def needs_code(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("on", True) and e.get("status") == "needs_code"]


def export(entries: list[dict], data_dir: Path) -> Path:
    """data/rules/needs_code.json: what the editor still can't do, for the developer."""
    path = Path(data_dir) / "rules" / "needs_code.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"n": e["n"], "applies_to": st.entry_modes(e), "rule": e.get("text", ""), "spec": e.get("needs", ""),
             "said": e.get("said", ""), "issue": e.get("issue")} for e in needs_code(entries)]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------
# GitHub issues (optional): GITHUB_TOKEN in .env next to config.json
# --------------------------------------------------------------------------
def github_token(base: Path | None) -> str | None:
    import os
    if base is not None:
        v = ai._dotenv(Path(base) / ".env").get("GITHUB_TOKEN")
        if v:
            return v
    return os.environ.get("GITHUB_TOKEN") or None


def issue_body(e: dict) -> str:
    return (f"Saved editor rule **#{e['n']}** (applies to: {st.modes_label(st.entry_modes(e))}) needs new editor code.\n\n"
            f"**Rule:** {e.get('text', '')}\n\n"
            f"**Tim's words:** {(e.get('said') or '')[:800]}\n\n"
            f"**Spec (from the rule compiler):** {e.get('needs', '')}\n\n"
            "Build it as a real setting (style.py dims/CUSTOM_KEYS, `resolve`, the renderer, and "
            "`ai.settings_menu`) with tests. Once deployed the bot re-reads this rule against the new "
            "settings menu and turns it on by itself.\n\nFiled automatically by the spike bot's rule compiler.")


def file_issue(e: dict, token: str, repo: str = REPO, opener=None) -> int:
    """Create the issue; returns its number. Raises OSError / ValueError."""
    payload = {"title": f"Editor rule #{e['n']}: {(e.get('text') or '')[:90]}", "body": issue_body(e),
               "labels": LABELS}
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues",
                                 data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json",
                                          "User-Agent": "spikebot-rules"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=30) as resp:
            return int(json.loads(resp.read().decode("utf-8"))["number"])
    except urllib.error.HTTPError as err:   # never echo the token: only the status
        raise OSError(f"GitHub answered HTTP {err.code}") from None
