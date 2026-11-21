"""Clipping campaigns Tim has joined: who to clip, each campaign's own rules, and whether it
still has budget.

`CAMPAIGNS` is the registry (rules copied from each campaign page on 20 Nov 2026). It is
merged into the config at load time (`merge_channels`, `lacy.profiles`), so a new campaign
only needs an entry here: the live config.json on the server doesn't have to change. A
config.json `channels` entry or `clips.streamers.<channel>` block still overrides anything
here, and `campaigns.<channel>: {"enabled": false}` switches one off.

Budget watch (`check` / `Bot.campaign_loop`): every `CHECK_EVERY_S` the campaign pages are
read. A Content Rewards campaign whose status is no longer `active`, or whose budget is
spent, or a Reach page whose badge no longer says Live, is marked ended in
data/campaigns/state.json. From then on that streamer gets no new clips (`ended`, used by
`lacy.profiles` and the render guard), queued jobs are cancelled and open review items are
closed. Tim can also end/resume one by hand with /campaigns.

Pure apart from `fetch_page` and the state file helpers.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger("campaigns")

CHECK_EVERY_S = 30 * 60
SPENT_BPS = 9900          # 99% of the budget paid out = treat as gone (the last cents never clear)

# Every rule a campaign page states that the bot can follow itself goes in a field; the
# rest goes in `rules` (shown in /campaigns and on review cards so Tim sees it before approving).
#   platforms   where the campaign pays (others are skipped for that streamer)
#   hashtags    must be in every caption / description
#   mention     the caption must name the streamer (always true here: titles/captions carry the name)
#   cta         where to watch them live: goes in the caption, never on screen (Tim, 20 Nov)
#   ai_allowed  AI-generated content allowed (the AI voiceover teaser); False = never
#   layout      "auto" (facecam on top when there is one) or "full" (the whole stream frame, e.g.
#               a required overlay must stay in shot)
#   source      what the clips must come from
CAMPAIGNS: dict[str, dict] = {
    # ---- the original three -------------------------------------------------------------
    "lacy": {
        "platform": "twitch", "name": "Lacy", "programme": "Lacy (Reach pool)", "network": "Reach",
        "urls": [], "pay": "pool", "platforms": ["youtube", "tiktok", "instagram"],
        "hashtags": ["#Lacy"], "cta": "Watch Lacy live on Twitch: twitch.tv/lacy",
        "ai_allowed": False, "logos": False, "layout": "auto",
        "rules": ["Reach: AI-generated content gets you removed from every Reach campaign",
                  "No logos"],
    },
    "adinross": {
        "platform": "kick", "name": "Adin Ross", "programme": "Adin Ross clipping programme",
        "network": "Adin Ross", "urls": [], "pay": "programme", "platforms": ["youtube", "tiktok", "instagram"],
        "hashtags": ["#AdinRoss"], "cta": "Watch Adin Ross live on Kick: kick.com/adinross",
        "ai_allowed": False, "logos": False, "layout": "auto", "rules": [],
    },
    "sealstats": {
        "platform": "kick", "name": "Seal", "programme": "Seal (Content Rewards)", "network": "Content Rewards",
        "urls": [], "pay": "programme", "platforms": ["youtube", "tiktok", "instagram"],
        "hashtags": ["#Seal"], "cta": "Watch Seal live on Kick: kick.com/sealstats",
        "ai_allowed": False, "logos": False, "layout": "auto", "rules": [],
    },
    # ---- joined 20 Nov 2026 ---------------------------------------------------------------
    "kaicenat": {
        "platform": "twitch", "name": "Kai Cenat", "programme": "Kai Cenat GTA 6 pool (Content Rewards)",
        "network": "Content Rewards",
        "urls": ["https://contentrewards.com/c/campaigns/1d2bcb00-c904-415f-aa82-f95b96baad86"],
        "pay": "$10K pool split by views (min 750K total views)",
        "platforms": ["youtube", "tiktok", "instagram"], "hashtags": ["#KaiCenat"],
        "cta": "Watch Kai Cenat live on Twitch: twitch.tv/kaicenat",
        "ai_allowed": False, "logos": False, "layout": "auto",
        "source": "Kai Cenat's GTA 6 marathon (Twitch / YouTube)",
        "rules": ["Content must come from Kai Cenat's GTA 6 marathon on Twitch and YouTube",
                  "Kai Cenat playing GTA 6 must be the main focus",
                  "Captions must use #KaiCenat"],
    },
    "yourragegaming": {
        "platform": "twitch", "name": "YourRAGE", "programme": "YourRAGE GTA 6 pool (Reach)", "network": "Reach",
        "urls": ["https://contentrewards.com/c/campaigns/be9b123e-ea47-4305-b6c5-af81f503b247",
                 "https://reachclipping.com/yourRAGE"],
        "pay": "$5K pool split by views (min 500K total views)",
        "platforms": ["youtube", "tiktok", "instagram"], "hashtags": ["#yourrage"],
        "cta": "Watch YourRAGE live on Twitch: twitch.tv/yourragegaming",
        "ai_allowed": False, "logos": False, "layout": "auto",
        "source": "YourRAGE's GTA 6 marathon stream",
        "rules": ["Rage must be the primary focus (not just in the intro)",
                  "Caption/text must mention Rage's name; captions must use #yourrage",
                  "No logos, no AI-generated content, never portray him negatively, no hate farming",
                  "Audience >= 50% USA/CA/AU/UK/NZ; submit links within 30 min of posting"],
    },
    "clix": {
        "platform": "twitch", "name": "Clix", "programme": "Clix GTA VI marathon pool (Reach)", "network": "Reach",
        "urls": ["https://contentrewards.com/c/campaigns/985dc674-c75e-44fc-ac87-1b1506324389",
                 "https://reachclipping.com/ClixGTA"],
        "pay": "$5K pool split by views (min 400K total views)",
        "platforms": ["youtube", "tiktok", "instagram"], "hashtags": ["#Clix"],
        "cta": "Watch Clix live on Twitch: twitch.tv/clix",
        "ai_allowed": False, "logos": False, "layout": "auto",
        "source": "Clix's GTA marathon (from 18 Nov 2026)",
        "rules": ["Clix must be the primary focus", "Caption/text must mention Clix; captions must use #Clix",
                  "No logos, no AI-generated videos, never portray him negatively",
                  "Submit links within 30 min of posting"],
    },
    "laurenhamdenn": {
        "platform": "twitch", "name": "Lauren Hamden", "programme": "Lauren Hamden GTA 6 CPM (Reach)",
        "network": "Reach",
        "urls": ["https://contentrewards.com/c/campaigns/dd617572-f0f7-4754-8df5-4c0deb81763a",
                 "https://reachclipping.com/Sisters"],
        "pay": "$1.50 per 1K views (TikTok/Instagram, $15-$250 per video, min 10K views)",
        "platforms": ["tiktok", "instagram"], "hashtags": ["#LaurenHamden", "@LaurenHamden"],
        "cta": "Watch Lauren Hamden live on Twitch: twitch.tv/laurenhamdenn",
        "ai_allowed": False, "logos": False, "layout": "auto",
        "source": "Lauren Hamden's GTA 6 livestream",
        # The campaign only pays accounts dedicated to her (and with her link in the bio):
        # gta6.clipped would be rejected, so it stays off until Tim has a dedicated account.
        "enabled": False,
        "off_reason": "needs a dedicated Lauren Hamden account with https://lozzahamden.click/ in the bio",
        "rules": ["TikTok and Instagram Reels only", "Accounts MUST be dedicated Lauren Hamden accounts",
                  "Bio must contain https://lozzahamden.click/", "Captions: #LaurenHamden and tag @LaurenHamden",
                  "No logos, no AI-generated videos"],
    },
    "dasgasd3m": {
        "platform": "kick", "name": "Dasgasd3m", "programme": "Dasgasd3m x RUCKUS GTA 6 CPM (Content Rewards)",
        "network": "Content Rewards",
        "urls": ["https://contentrewards.com/c/campaigns/051002dd-5c46-43d0-a446-43f2957ba3a9"],
        "pay": "$2 per 1K views (TikTok/YouTube, $10-$150 per video)",
        "platforms": ["youtube", "tiktok"], "hashtags": ["#Dasgasd3m"],
        "cta": "Watch Dasgasd3m live on Kick: kick.com/dasgasd3m",
        "ai_allowed": False, "logos": False, "layout": "auto",
        "source": "Dom's GTA 6 streams",
        "rules": ["Funny and entertaining GTA 6 moments, TikTok and YouTube Shorts only",
                  "GTA 6-specific account required", "1 creator requirement is only visible after joining:"
                  " check it in Content Rewards"],
    },
    "bobby_vanriper": {
        "platform": "twitch", "name": "Bobby Vanriper", "programme": "Bobby Vanriper GTA VI CPM (Clipping Nation)",
        "network": "Content Rewards",
        "urls": ["https://contentrewards.com/c/campaigns/f583b069-ced7-49e5-9ed3-2d3a13d6df0c"],
        "pay": "$1.50 per 1K views ($2.25-$150 per video)",
        "platforms": ["youtube", "tiktok", "instagram"],
        "hashtags": ["#bobbyvanriper", "#gta6", "#gtavi", "#twitchclips"],
        "cta": "Follow Bobby Vanriper on Twitch: twitch.tv/bobby_vanriper",
        "ai_allowed": False, "logos": False, "layout": "auto",
        "source": "Bobby's GTA VI streams and videos only",
        "rules": ["GTA VI only: hook in the first 2 s, one clear moment, CTA to watch him live",
                  "#bobbyvanriper and twitch.tv/bobby_vanriper in the caption; English only",
                  "Captions readable without sound; likes must be visible on every post",
                  "No AI-generated content or thumbnails; never portray Bobby negatively",
                  "At 1,500 views: open a Submit Demographics ticket in the Clipping Nation Discord",
                  "Keep posts up 30 days; no near-identical clip more than 5 times"],
    },
    "kreekcraft": {
        "platform": "twitch", "name": "KreekCraft", "programme": "KreekCraft GTA 6 CPM (ClipLaunch)",
        "network": "Content Rewards",
        "urls": ["https://contentrewards.com/c/campaigns/2eaa14fb-6927-4136-897e-b096e0d907d6"],
        "pay": "$1 per 1K views ($3-$300 per video)",
        "platforms": ["youtube", "tiktok", "instagram"], "hashtags": ["#KreekCraft"],
        "cta": "Watch KreekCraft live on Twitch: twitch.tv/kreekcraft",
        "ai_allowed": False, "logos": False,
        # "Every clip must include KreekCraft's overlay": keep the whole stream frame
        "layout": "full",
        "source": "KreekCraft's GTA 6 livestreams",
        "rules": ["Every clip must include KreekCraft's stream overlay (whole frame kept)",
                  "Drive viewers back to his livestream (CTA in the caption)"],
    },
    "brettliftsxp": {
        "platform": "twitch", "name": "BrettLiftsXP", "programme": "BrettLiftsXP GTA VI CPM (Funnel Clips)",
        "network": "Content Rewards",
        "urls": ["https://contentrewards.com/c/campaigns/954cf1f2-1a69-4f0f-b46c-18a9dfddb219"],
        "pay": "$2 per 1K views on TikTok, $0.50 on Instagram/YouTube (max $100 per video)",
        "platforms": ["youtube", "tiktok", "instagram"], "hashtags": ["#BrettLiftsXP"],
        "cta": "Watch BrettLiftsXP live on Twitch: twitch.tv/brettliftsxp",
        "ai_allowed": False, "logos": False, "layout": "auto",
        "source": "Brett's recent streams only (old streams are rejected)",
        "rules": ["Recent streams only", "1 creator requirement + a requirement doc are only visible after"
                  " joining: check them in Content Rewards"],
    },
}

PLATFORM_URL = {"twitch": "https://www.twitch.tv/{}", "kick": "https://kick.com/{}"}
# How captions and titles refer to each streamer (Tim, 21 Nov: Lacy and the others are he/him).
DEFAULT_PRONOUNS = "he/him"
PRONOUNS = {"laurenhamdenn": "she/her"}


def pronouns_for(channel: str | None, cfg: dict | None = None) -> str:
    c = (registry(cfg).get(str(channel or "lacy").lower()) or {}) if cfg is not None else {}
    return c.get("pronouns") or PRONOUNS.get(str(channel or "").lower()) or DEFAULT_PRONOUNS


def registry(cfg: dict | None = None) -> dict[str, dict]:
    """CAMPAIGNS with config overrides: `campaigns.<channel>` (any field, e.g. enabled)."""
    # SPIKEBOT_CAMPAIGNS=off (tests/conftest.py): only campaigns given in config
    out = {} if os.environ.get("SPIKEBOT_CAMPAIGNS") == "off" else json.loads(json.dumps(CAMPAIGNS))
    for k, v in ((cfg or {}).get("campaigns") or {}).items():
        k = str(k).lower()
        if isinstance(v, dict):
            out[k] = (out.get(k) or {"platform": "twitch", "name": k, "programme": k, "urls": [],
                                     "platforms": ["youtube", "tiktok", "instagram"], "hashtags": [],
                                     "ai_allowed": False, "logos": False, "layout": "auto", "rules": []}) | v
    for k, c in out.items():
        c["channel"] = k
        c.setdefault("enabled", True)
    return out


def merge_channels(cfg: dict) -> dict:
    """Make sure every enabled campaign's channel is watched and carries its programme. Channels
    already in config keep their own settings (a `programme` set there wins)."""
    chans = cfg.setdefault("channels", [])
    have = {(c.get("platform"), str(c.get("name", "")).lower()): c for c in chans}
    for ch, c in registry(cfg).items():
        if not c["enabled"]:
            continue
        key = (c["platform"], ch)
        if key in have:
            if not have[key].get("programme"):
                have[key]["programme"] = c["programme"]
        else:
            chans.append({"platform": c["platform"], "name": ch, "programme": c["programme"],
                          "display": c["name"]})
    return cfg


def for_channel(cfg: dict, channel: str) -> dict | None:
    return registry(cfg).get(str(channel).lower())


# --------------------------------------------------------------------------
# Captions: required hashtags + "where to watch" line, never on screen
# --------------------------------------------------------------------------
def required_tags(c: dict | None) -> list[str]:
    return [t for t in (c or {}).get("hashtags") or [] if t.strip()]


def _missing(text: str, tags: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in tags if t.lower() not in low]


def finish_caption(text: str, c: dict | None, cta: bool = True, limit: int = 2200) -> str:
    """The caption with the campaign's required hashtags/mentions and its CTA (where to watch the
    streamer live) added when missing. The CTA goes before the hashtags."""
    if not c:
        return text
    text = (text or "").rstrip()
    line = (c.get("cta") or "").strip()
    if cta and line and line.lower() not in text.lower():
        body, sep, tail = text.rpartition("\n\n")
        if sep and tail.lstrip().startswith("#"):
            text = f"{body}\n\n{line}\n\n{tail}"
        else:
            text = f"{text}\n\n{line}" if "\n" in text else f"{text} {line}"
    miss = _missing(text, required_tags(c))
    if miss:
        text = f"{text} {' '.join(miss)}"
    return text[:limit]


def finish_copy(captions: dict, description: str, c: dict | None) -> tuple[dict, str]:
    """Every platform caption + the YouTube description, campaign rules applied."""
    if not c:
        return captions, description
    caps = {k: finish_caption(v, c) for k, v in (captions or {}).items()}
    return caps, finish_caption(description, c, limit=4900)


def copy_problems(captions: dict, c: dict | None) -> list[str]:
    """Why an edited caption breaks the campaign's caption rules ([] = fine)."""
    out = []
    for k, text in (captions or {}).items():
        miss = _missing(text or "", required_tags(c))
        if miss:
            out.append(f"the {k} caption must include {' '.join(miss)} ({c.get('name')} campaign rule)")
    return out


# --------------------------------------------------------------------------
# Budget / status
# --------------------------------------------------------------------------
ENDED_STATUS = {"ended", "completed", "complete", "paused", "archived", "closed", "inactive", "cancelled",
                "canceled", "expired", "finished", "draft"}


def parse_contentrewards(html: str) -> dict:
    """{status, budget, spent, bps} from a Content Rewards campaign page (its embedded data)."""
    s = html.replace('\\"', '"')
    out: dict = {}
    m = re.search(r'"status":"(\w+)","(?:statusChangedAt|statusReason|updatedAt)"', s)
    if m:
        out["status"] = m.group(1).lower()
    for key, name in (("budgetCents", "budget"), ("budgetSpentCents", "spent"), ("budgetProgressBps", "bps")):
        vals = [int(x) for x in re.findall(rf'"{key}":(\d+)', s)]
        if vals:
            out[name] = max(vals)
    return out


def parse_reach(html: str) -> dict:
    m = re.search(r'hero-status-badge[^>]*>\s*([^<]+?)\s*<', html)
    return {"badge": m.group(1).strip()} if m else {}


def verdict(page: dict, pay: str = "") -> tuple[bool, str]:
    """(ended, why) from parsed page data. Unknown data = not ended."""
    st = page.get("status")
    if st and st in ENDED_STATUS:
        return True, f"campaign {st}"
    budget, spent, bps = page.get("budget"), page.get("spent"), page.get("bps")
    if budget and spent is not None and spent >= budget:
        return True, f"budget spent (${spent / 100:,.0f} of ${budget / 100:,.0f})"
    if bps is not None and bps >= SPENT_BPS:
        return True, f"budget {bps / 100:.0f}% spent"
    badge = page.get("badge")
    if badge and badge.lower() not in ("live", "active", "open"):
        return True, f"Reach page says {badge}"
    return False, ""


def fetch_page(url: str, timeout: float = 30) -> str:
    """The page HTML (curl_cffi, like the Kick lookups: gets past Cloudflare)."""
    from curl_cffi import requests as creq
    r = creq.get(url, impersonate="chrome", timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.text


def check_one(c: dict, fetch=fetch_page) -> dict:
    """Read every page of a campaign: {"ended": bool, "why", "budget", "spent", "checked"}."""
    info: dict = {"ended": False, "why": "", "checked": time.time()}
    errors = []
    for url in c.get("urls") or []:
        try:
            html = fetch(url)
        except Exception as e:  # noqa: BLE001 - a failed read never ends a campaign
            errors.append(f"{url}: {e}")
            continue
        page = parse_contentrewards(html) if "contentrewards.com" in url else parse_reach(html)
        for k in ("budget", "spent", "status", "badge"):
            if page.get(k) is not None:
                info[k] = page[k]
        ended, why = verdict(page)
        if ended:
            info.update(ended=True, why=why, url=url)
            break
    if errors and len(errors) == len(c.get("urls") or []):
        info["error"] = "; ".join(errors)[:300]
    return info


class State:
    """data/campaigns/state.json: {channel: {ended, why, by, since, budget, spent, checked}}."""

    def __init__(self, data_dir: Path | str):
        self.path = Path(data_dir) / "campaigns" / "state.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, st: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def set(self, channel: str, ended: bool, why: str, by: str = "auto", **extra) -> dict:
        st = self.load()
        row = st.get(channel, {})
        if ended and not row.get("ended"):
            row["since"] = time.time()
        row.update(extra, ended=ended, why=why, by=by)
        st[channel] = row
        self.save(st)
        return row


def data_dir_of(cfg: dict, base: Path | None = None) -> Path:
    d = Path(cfg.get("data_dir", "data"))
    return d if d.is_absolute() or base is None else base / d


def ended(cfg: dict, channel: str, base: Path | None = None) -> str | None:
    """Why this streamer's campaign is over (no more clips), or None while it runs."""
    c = for_channel(cfg, channel)
    if c is not None and not c.get("enabled", True):
        return c.get("off_reason") or "campaign switched off"
    row = State(data_dir_of(cfg, base)).load().get(str(channel).lower()) or {}
    return (row.get("why") or "campaign ended") if row.get("ended") else None


def update(cfg: dict, base: Path | None = None, fetch=fetch_page, now: float | None = None) -> list[dict]:
    """Check every enabled campaign with pages. Returns the ones that just ended:
    [{"channel", "name", "why"}]. A campaign Tim resumed by hand isn't auto-ended again
    unless its page changes to a different reason."""
    state = State(data_dir_of(cfg, base))
    just: list[dict] = []
    for ch, c in registry(cfg).items():
        if not c["enabled"] or not c.get("urls"):
            continue
        info = check_one(c, fetch)
        row = state.load().get(ch) or {}
        extra = {k: info[k] for k in ("budget", "spent", "status", "badge", "checked", "error") if k in info}
        if info["ended"]:
            resumed = row.get("by") == "tim" and not row.get("ended") and row.get("resumed_why") == info["why"]
            if row.get("ended") or resumed:   # already known, or Tim overrode exactly this reason
                state.set(ch, bool(row.get("ended")), row.get("why", ""), row.get("by", "auto"), **extra)
                continue
            state.set(ch, True, info["why"], "auto", **extra)
            just.append({"channel": ch, "name": c["name"], "why": info["why"]})
        elif row.get("ended") and row.get("by") == "tim":   # Tim ended it by hand: stays ended
            state.set(ch, True, row.get("why", ""), "tim", **extra)
        else:   # running (an auto-end whose page is live again, e.g. a budget top-up, resumes)
            state.set(ch, False, "", row.get("by", "auto"), **extra)
    return just


def end_by_hand(cfg: dict, channel: str, base: Path | None = None, why: str = "ended by Tim") -> dict:
    return State(data_dir_of(cfg, base)).set(channel.lower(), True, why, "tim")


def resume(cfg: dict, channel: str, base: Path | None = None) -> dict:
    st = State(data_dir_of(cfg, base))
    row = st.load().get(channel.lower()) or {}
    return st.set(channel.lower(), False, "", "tim", resumed_why=row.get("why", ""))


def find(cfg: dict, text: str) -> str | None:
    """Channel for a name typed in Discord ("kai", "Kai Cenat", "kaicenat")."""
    t = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    if not t:
        return None
    reg = registry(cfg)
    for ch, c in reg.items():
        if t in (re.sub(r"[^a-z0-9]", "", ch), re.sub(r"[^a-z0-9]", "", c["name"].lower())):
            return ch
    hits = [ch for ch, c in reg.items() if t in re.sub(r"[^a-z0-9]", "", c["name"].lower() + ch)]
    return hits[0] if len(hits) == 1 else None


def overview(cfg: dict, base: Path | None = None) -> str:
    """/campaigns text: one block per campaign (status, pay, budget, rules)."""
    st = State(data_dir_of(cfg, base)).load()
    lines = []
    for ch, c in registry(cfg).items():
        row = st.get(ch) or {}
        why = ended(cfg, ch, base)
        flag = f"⛔ {why}" if why else "✅ running"
        budget = ""
        if row.get("budget"):
            budget = f" · budget ${row.get('spent', 0) / 100:,.0f} / ${row['budget'] / 100:,.0f} spent"
        url = PLATFORM_URL.get(c["platform"], "{}").format(ch).removeprefix("https://www.").removeprefix("https://")
        lines.append(f"**{c['name']}** ({url}) · {c.get('network', '')} · {c.get('pay', '')}{budget}\n"
                     f"{flag} · posts to {', '.join(c.get('platforms') or [])} · tags {' '.join(required_tags(c))}")
        for r in c.get("rules") or []:
            lines.append(f"  • {r}")
    return "\n".join(lines)
