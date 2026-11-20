"""Campaign registry, caption rules, budget watch (spikebot/campaigns.py) and the best-of-day
picker (spikebot/picker.py)."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from spikebot import campaigns as C
from spikebot import lacy as L
from spikebot import picker as P

UK = ZoneInfo("Europe/London")


def cfg_in(tmp_path: Path, **extra) -> dict:
    return {"data_dir": str(tmp_path / "data"), "channels": [], "min_score_to_alert": 0} | extra


# ---- registry -----------------------------------------------------------------
def test_every_joined_campaign_is_clipped(tmp_path):
    cfg = cfg_in(tmp_path)
    keys = set(L.profiles(cfg))
    for ch in ("kaicenat", "yourragegaming", "clix", "dasgasd3m", "bobby_vanriper", "kreekcraft", "brettliftsxp",
               "adinross", "sealstats"):
        assert any(k[1] == ch for k in keys), ch
    assert ("kick", "dasgasd3m") in keys and ("twitch", "kaicenat") in keys
    # Lauren Hamden pays only dedicated accounts: off until Tim has one
    assert not any(k[1] == "laurenhamdenn" for k in keys)
    assert "dedicated" in C.ended(cfg, "laurenhamdenn")


def test_merge_channels_adds_watch_entries_but_keeps_config(tmp_path):
    cfg = cfg_in(tmp_path, channels=[{"platform": "twitch", "name": "kaicenat", "programme": None}])
    C.merge_channels(cfg)
    kai = [c for c in cfg["channels"] if c["name"] == "kaicenat"]
    assert len(kai) == 1 and "Kai Cenat" in kai[0]["programme"]
    assert any(c["name"] == "clix" and c["platform"] == "twitch" for c in cfg["channels"])
    C.merge_channels(cfg)   # idempotent
    assert sum(c["name"] == "clix" for c in cfg["channels"]) == 1


def test_config_can_switch_a_campaign_off(tmp_path):
    cfg = cfg_in(tmp_path, campaigns={"clix": {"enabled": False}})
    assert not any(k[1] == "clix" for k in L.profiles(cfg))


def test_profiles_carry_campaign_rules(tmp_path):
    p = L.profiles(cfg_in(tmp_path))
    kai = p[("twitch", "kaicenat")]
    assert kai["name"] == "Kai Cenat" and kai["ai_allowed"] is False
    assert kai["campaign"]["hashtags"] == ["#KaiCenat"]
    assert p[("kick", "dasgasd3m")]["platforms"] == ["youtube", "tiktok"]
    assert p[("twitch", "kreekcraft")]["layout"] == "full"   # his overlay must stay in shot


# ---- captions -----------------------------------------------------------------
def test_caption_gets_required_tags_and_cta_once():
    c = C.CAMPAIGNS["kaicenat"]
    out = C.finish_caption("kai loses it here 😭 #GTA6 #fyp", c)
    assert "#KaiCenat" in out and "twitch.tv/kaicenat" in out
    assert C.finish_caption(out, c) == out
    ig = C.finish_caption("hook\n\nKai playing GTA 6. Follow for daily clips.\n\n#GTA6 #Reels", c)
    assert ig.index("twitch.tv/kaicenat") < ig.index("#GTA6")   # the CTA goes before the hashtags
    caps, desc = C.finish_copy({"tiktok": "a", "instagram": "b"}, "Title\n\n#Shorts", C.CAMPAIGNS["bobby_vanriper"])
    assert all("#bobbyvanriper" in v and "twitch.tv/bobby_vanriper" in v for v in caps.values())
    assert "#bobbyvanriper" in desc


def test_edited_caption_must_keep_campaign_tags():
    bad = C.copy_problems({"tiktok": "clix wins"}, C.CAMPAIGNS["clix"])
    assert bad and "#Clix" in bad[0]
    assert C.copy_problems({"tiktok": "clix wins #clix"}, C.CAMPAIGNS["clix"]) == []


# ---- budget / status ------------------------------------------------------------
CR_LIVE = ('x \\"status\\":\\"active\\",\\"statusChangedAt\\":\\"2026\\" \\"budgetCents\\":500000,'
           '\\"budgetSpentCents\\":50836,\\"budgetProgressBps\\":1016')
CR_SPENT = '"status":"active","updatedAt":"x" "budgetCents":100000,"budgetSpentCents":100000,"budgetProgressBps":10000'
CR_ENDED = '"status":"ended","statusChangedAt":"x" "budgetCents":1000000,"budgetSpentCents":0'
REACH_LIVE = '<div class="hero-status-badge">Live</div>'
REACH_ENDED = '<div class="hero-status-badge">Ended</div>'


def test_parse_pages():
    assert C.parse_contentrewards(CR_LIVE) == {"status": "active", "budget": 500000, "spent": 50836, "bps": 1016}
    assert C.verdict(C.parse_contentrewards(CR_LIVE)) == (False, "")
    assert C.verdict(C.parse_contentrewards(CR_SPENT))[0]
    assert C.verdict(C.parse_contentrewards(CR_ENDED)) == (True, "campaign ended")
    assert C.verdict(C.parse_reach(REACH_LIVE)) == (False, "")
    assert C.verdict(C.parse_reach(REACH_ENDED))[0]


def test_update_ends_a_campaign_and_profiles_drop_it(tmp_path):
    cfg = cfg_in(tmp_path)
    pages = {u: CR_LIVE for c in C.CAMPAIGNS.values() for u in c["urls"]}
    assert C.update(cfg, fetch=lambda u: pages[u]) == []
    clix_cr = C.CAMPAIGNS["clix"]["urls"][0]
    pages[clix_cr] = CR_ENDED
    ended = C.update(cfg, fetch=lambda u: pages[u])
    assert [e["channel"] for e in ended] == ["clix"]
    assert C.ended(cfg, "clix") == "campaign ended"
    assert not any(k[1] == "clix" for k in L.profiles(cfg))
    assert any(k[1] == "clix" for k in L.profiles(cfg, enabled_only=False))   # old items still resolve
    assert C.update(cfg, fetch=lambda u: pages[u]) == []   # told once
    # Tim resumes it by hand: the same page reason doesn't end it again
    C.resume(cfg, "clix")
    assert C.update(cfg, fetch=lambda u: pages[u]) == [] and C.ended(cfg, "clix") is None
    # an unreadable page never ends a campaign
    assert C.update(cfg, fetch=lambda u: (_ for _ in ()).throw(RuntimeError("HTTP 503"))) == []
    assert C.ended(cfg, "kaicenat") is None


def test_end_by_hand_and_find(tmp_path):
    cfg = cfg_in(tmp_path)
    assert C.find(cfg, "Kai Cenat") == "kaicenat" and C.find(cfg, "bobby") == "bobby_vanriper"
    C.end_by_hand(cfg, "kaicenat")
    assert C.ended(cfg, "kaicenat") == "ended by Tim"
    pages = {u: CR_LIVE for c in C.CAMPAIGNS.values() for u in c["urls"]}
    C.update(cfg, fetch=lambda u: pages[u])
    assert C.ended(cfg, "kaicenat") == "ended by Tim"   # a live page doesn't undo Tim's decision
    assert "Kai Cenat" in C.overview(cfg)


def test_stop_campaign_cancels_waiting_jobs_and_open_items(tmp_path):
    from spikebot import review
    from spikebot.main import stop_campaign
    cfg = cfg_in(tmp_path)
    data = Path(cfg["data_dir"])
    jobs = L.Jobs(data)
    for i, st in enumerate(("queued", "standby", "review", "uploaded")):
        jobs.save({"id": f"clix-2026112{i}-000000", "channel": "clix", "platform": "twitch", "status": st})
    store = review.Store(data)
    item = store.create("lacy", {"job_id": "clix-20261122-000000", "channel": "clix", "platform": "twitch"},
                        time.time(), title="t")
    with store.edit(item["id"]) as it:
        it["status"] = "open"
    out = stop_campaign(cfg, data, "clix", "budget spent")
    assert out.startswith("2 waiting clip(s) cancelled, 1 open review item(s) closed")
    assert {j["status"] for j in jobs.all()} == {"cancelled", "review", "uploaded"}
    assert store.load(item["id"])["status"] == "rejected"


def test_render_guard_refuses_ended_campaign(tmp_path):
    cfg = cfg_in(tmp_path)
    C.end_by_hand(cfg, "clix")
    prof = L.profiles(cfg, enabled_only=False)[("twitch", "clix")]
    jobs = L.Jobs(Path(cfg["data_dir"]))
    job = {"id": "clix-20261120-120000", "channel": "clix", "platform": "twitch", "programme": "x",
           "category": "Grand Theft Auto VI", "vod_url": "u", "vod_started_at": "2026-11-20T10:00:00Z",
           "clip_start": 0, "clip_end": 30}
    out = L.render_job(job, prof, jobs, set(), cfg=cfg, base=tmp_path)
    assert out["status"] == "cancelled" and "campaign over" in out["reason"]


def test_approve_plans_only_the_campaigns_platforms():
    from spikebot import review
    item = {"id": "x", "status": "open", "mode": "lacy", "rounds": [{"n": 1, "status": "posted",
            "variants": [{"label": "A"}]}], "source": {"platforms": ["youtube", "tiktok"]}, "log": []}
    rcfg = {"modes": {"lacy": {"auto": ["youtube", "instagram"]}}}
    review.approve(item, 1, "A", rcfg, {"youtube", "instagram", "tiktok"})
    assert set(item["publish"]) == {"youtube", "tiktok", "manual"}
    assert item["publish"]["manual"]["platforms"] == []


# ---- best-of-day picker -----------------------------------------------------------
def ts(day: str, hh: int, mm: int = 0) -> float:
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00").replace(tzinfo=UK).timestamp()


def job(i: str, score: float, onset: float, status: str = "queued", selected: bool = False, ch: str = "kaicenat"):
    return {"id": i, "channel": ch, "platform": "twitch", "score": score, "onset": onset, "ready_at": onset + 300,
            "status": status, **({"selected": onset} if selected else {})}


def test_bar_is_a_typical_days_top_n():
    hist = [job(f"h{k}", float(k), ts("2026-11-19", 12) + k) for k in range(30)]   # 30 moments, scores 0-29
    assert P.bar(hist, 10, "2026-11-20") == 20.0
    assert P.bar(hist, 10, "2026-11-19") is None              # only today: no history yet
    assert P.bar(hist[:5], 10, "2026-11-20") == 0.0           # fewer moments than slots: all qualify


def test_strong_moment_goes_now_weak_one_waits_then_fills_after_stream():
    hist = [job(f"h{k}", float(k), ts("2026-11-19", 12) + k) for k in range(30)]
    now = ts("2026-11-20", 15)
    strong, weak = job("s", 25, now - 600), job("w", 5, now - 600)
    assert P.decide(strong, hist + [strong, weak], 10, now, live=True)[0] == "go"
    assert P.decide(weak, hist + [strong, weak], 10, now, live=True)[0] == "wait"
    assert P.decide(weak, hist + [strong, weak], 10, now, live=False)[0] == "go"   # stream over: slots left


def test_full_day_drops_and_better_waiting_moments_win():
    now = ts("2026-11-20", 15)
    made = [job(f"m{k}", 50, now - 4000 + k, status="review", selected=True) for k in range(10)]
    late = job("late", 99, now - 600)
    assert P.decide(late, made + [late], 10, now)[0] == "drop"
    # 2 slots left, 3 waiting: the weakest of them waits, then is dropped once the stream is over
    some = made[:8]
    a, b, c = job("a", 30, now - 2000), job("b", 20, now - 2000), job("c", 10, now - 2000)
    allj = some + [a, b, c]
    assert P.decide(c, allj, 10, now, live=True)[0] == "wait"
    assert P.decide(c, allj, 10, now, live=False)[0] == "drop"
    assert P.decide(a, allj, 10, now, live=False)[0] == "go"


def test_no_history_hold_window():
    now = ts("2026-11-20", 15)
    j1 = job("j1", 10, now - 300 - P.HOLD_S - 1)
    assert P.decide(j1, [j1], 10, now, live=True)[0] == "go"            # waited the hold, top of the queue
    j2 = job("j2", 10, now - 300 - 60)
    assert P.decide(j2, [j2], 10, now, live=True)[0] == "wait"          # still inside the hold
    assert P.decide(j2, [j2], 10, ts("2026-11-20", 23, 10), live=True)[0] == "go"   # late evening: fill
    # failed / skipped jobs give their slot back
    used = [job(f"f{k}", 50, now - 5000 + k, status="skipped", selected=True) for k in range(10)]
    assert P.decide(j1, used + [j1], 10, now, live=True)[0] == "go"


def test_lacy_mode_waits_then_selects(tmp_path, monkeypatch):
    import asyncio
    cfg = cfg_in(tmp_path, channels=[{"platform": "twitch", "name": "kaicenat", "programme": "x"}])
    mode = L.LacyMode(cfg, Path(cfg["data_dir"]))
    mode.is_live = lambda key: True
    now = time.time()
    j = job("kaicenat-20261120-150000", 10, now - 400)
    j["ready_at"] = now - 100
    mode.jobs.save(j)
    later = []

    async def fake_later(job_id, delay):
        later.append(delay)
    monkeypatch.setattr(mode, "_later", fake_later)

    async def go():
        await mode._process(j["id"], None, None)
        await asyncio.sleep(0)
    asyncio.run(go())
    saved = mode.jobs.load(j["id"])
    assert saved["status"] == "standby" and later == [P.RECHECK_S]
    assert json.dumps(saved)   # still a plain job file
