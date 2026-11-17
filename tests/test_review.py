"""Review queue, publishing rules and the Discord bot flows (with fakes, no network)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from spikebot import discord_bot as DB
from spikebot import lacy as L
from spikebot import review as R
from spikebot import style as S

UK = R.UK
T = datetime(2026, 11, 19, 12, 0, tzinfo=UK).timestamp()   # Thu 12:00 UK


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def render(self, item, specs, n, folder, preview_bytes):
        self.calls.append((n, [s["label"] for s in specs], preview_bytes))
        out = []
        for s in specs:
            (folder / f"r{n}_{s['label']}.mp4").write_bytes(b"video" * 10)
            (folder / f"r{n}_{s['label']}_preview.mp4").write_bytes(b"prev")
            base = s.get("from") or {}
            title = (s.get("texts") or {}).get("hook") or base.get("title") or f"Lacy moment {n}{s['label']}"
            out.append({"label": s["label"], "style": s["style"], "preset": s.get("preset"),
                        "experimental": s.get("experimental", False), "title": title,
                        "description": f"{title}\n\n#Lacy #Shorts #GTA6", "tags": ["Lacy"],
                        "hook": "Lacy hook", "captions": {"instagram": "Lacy ig #GTA6", "tiktok": "Lacy tt #GTA6"},
                        "file": f"r{n}_{s['label']}.mp4", "preview": f"r{n}_{s['label']}_preview.mp4",
                        "duration": 20.0, "cuts": 3})
        return out

    def validate(self, item, v):
        if "lacy" not in v["title"].lower():
            raise ValueError("needs Lacy")

    def finish(self, item, status):
        pass


def new_item(tmp_path, mode="lacy", now=T):
    store = R.Store(tmp_path / "data")
    item = store.create(mode, {"job_id": "20261119-110000"}, now, title="Lacy test")
    item = R.render_round(store, item["id"], {}, tmp_path, {"kind": "initial"}, now=now,
                          adapter=FakeAdapter(), seed=1)
    return store, item


# ---- settings / windows ---------------------------------------------------------------
def test_settings_from_env_and_defaults():
    r = R.settings({"discord_bot_token": "x", "discord_channel_id": "123", "discord_approver_ids": "1, 2",
                    "review": {"windows": {"youtube": ["10:00-12:00"]}}})
    assert r["token"] == "x" and r["channel_id"] == 123 and r["approver_ids"] == [1, 2]
    assert r["windows"]["youtube"] == ["10:00-12:00"] and r["windows"]["instagram"] == ["08:00-22:30"]
    assert r["expire_hours"] == 48 and r["tiktok_channel_id"] == 0
    assert R.auto_platforms(r, "lacy") == ["youtube", "instagram"] and R.auto_platforms(r, "tokyo_girl") == []
    assert R.settings({"review_enabled": "false"})["enabled"] is False


def test_windows_in_uk_time_and_across_midnight():
    noon = T
    assert R.in_window(["07:00-23:00"], noon)
    assert not R.in_window(["13:00-14:00"], noon)
    assert R.in_window(["22:00-13:00"], noon) and R.in_window([], noon)
    nxt = R.next_window(["13:00-14:00"], noon)
    assert datetime.fromtimestamp(nxt, UK).hour == 13


# ---- items, rounds, requests ------------------------------------------------------------
def test_initial_round_is_open_with_expiry_and_profile(tmp_path):
    store, item = new_item(tmp_path)
    assert item["status"] == "open" and item["expires"] == T + 48 * 3600
    r = item["rounds"][0]
    assert [v["label"] for v in r["variants"]] == ["A", "B", "C"] and r["status"] == "ready"
    assert len({v["style"]["grade"] for v in r["variants"]}) == 3
    prof = store.profile()
    assert sum(row["shown"] for row in prof.data["stats"]["grade"].values()) == 3


def test_redo_feedback_and_preset_requests(tmp_path):
    store, item = new_item(tmp_path)
    ad = FakeAdapter()
    with store.edit(item["id"]) as it:
        R.request(it, "redo", round=1)
    with pytest.raises(ValueError):  # one re-render at a time
        with store.edit(item["id"]) as it:
            R.request(it, "redo", round=1)
    item = R.render_round(store, item["id"], {}, tmp_path, now=T + 60, adapter=ad, seed=2)
    assert len(item["rounds"]) == 2 and item["request"]["status"] == "done"
    olds = {v["style"]["grade"] for v in item["rounds"][0]["variants"]}
    assert all(v["style"]["grade"] not in olds for v in item["rounds"][1]["variants"][:1])
    assert item["expires"] == T + 60 + 48 * 3600   # a new round restarts the 48 h

    b = R.variant_of(item, 2, "B")
    with store.edit(item["id"]) as it:
        R.request(it, "feedback", round=2, label="B", notes="bigger text, faster. hook: Lacy cannot believe it")
    item = R.render_round(store, item["id"], {}, tmp_path, now=T + 120, adapter=ad)
    r3 = item["rounds"][2]
    v = r3["variants"][0]
    assert r3["kind"] == "feedback" and v["label"] == "B3" and v["title"] == "Lacy cannot believe it"
    assert v["style"]["size"] == S._step(tuple(S.SIZES), b["style"]["size"], 1)
    assert "Understood: text_bigger, faster, new_hook" in r3["comment"]
    assert store.profile().data["feedback"]["intents"]["faster"] == 1

    from spikebot.presets import Presets
    Presets(store.data_dir / "presets").save({"name": "moody", "copy": ["colour"],
                                              "settings": {"colour": {"grade": "moody", "saturation": 0.8}}})
    with store.edit(item["id"]) as it:
        R.request(it, "preset", round=3, label="B3", name="moody")
    item = R.render_round(store, item["id"], {}, tmp_path, now=T + 180, adapter=ad)
    p = item["rounds"][3]["variants"][0]
    assert p["label"] == "P4" and p["preset"] == "moody" and p["style"]["grade"] == "moody"
    assert p["style"]["custom"]["saturation"] == 0.8 and p["style"]["size"] == v["style"]["size"]


def test_failed_render_is_recorded_not_raised(tmp_path):
    store, item = new_item(tmp_path)

    class Broken(FakeAdapter):
        def render(self, *a):
            raise RuntimeError("ffmpeg exploded")
    with store.edit(item["id"]) as it:
        R.request(it, "redo", round=1)
    item = R.render_round(store, item["id"], {}, tmp_path, adapter=Broken())
    assert item["request"]["status"] == "failed" and "exploded" in item["request"]["error"]
    assert item["status"] == "open" and len(item["rounds"]) == 1


def test_edit_caption_validates(tmp_path):
    store, item = new_item(tmp_path)
    ad = FakeAdapter()
    with store.edit(item["id"]) as it:
        R.edit_caption(it, 1, "A", "Lacy's new title", "Lacy IG", "Lacy TT", ad.validate)
    v = R.variant_of(store.load(item["id"]), 1, "A")
    assert v["title"] == "Lacy's new title" and v["captions"]["tiktok"] == "Lacy TT" and v["edited"]
    with pytest.raises(ValueError):
        with store.edit(item["id"]) as it:
            R.edit_caption(it, 1, "A", "No name here", "x", "y", ad.validate)
    assert R.variant_of(store.load(item["id"]), 1, "A")["title"] == "Lacy's new title"


def test_approve_plans_platforms_and_closes(tmp_path):
    store, item = new_item(tmp_path)
    rc = R.settings({})
    with store.edit(item["id"]) as it:
        R.approve(it, 1, "B", rc, {"youtube"}, now=T)   # Instagram not configured
    item = store.load(item["id"])
    assert item["status"] == "approved" and item["approved"]["label"] == "B"
    assert set(item["publish"]) == {"youtube", "manual"}
    assert item["publish"]["manual"]["platforms"] == ["instagram", "tiktok"]
    with pytest.raises(ValueError):
        with store.edit(item["id"]) as it:
            R.approve(it, 1, "A", rc, set())
    with pytest.raises(ValueError):
        with store.edit(item["id"]) as it:
            R.request(it, "redo", round=1)
    store2, tg = new_item(tmp_path / "tg", mode="tokyo_girl")
    with store2.edit(tg["id"]) as it:
        R.approve(it, 1, "A", rc, {"youtube", "instagram"})
    assert store2.load(tg["id"])["publish"]["manual"]["platforms"] == ["youtube", "instagram", "tiktok"]


def test_expiry_after_48h_frees_files(tmp_path):
    store, item = new_item(tmp_path)
    assert not R.due_to_expire(item, T + 47 * 3600)
    assert R.due_to_expire(item, T + 48 * 3600)
    import spikebot.review as rv
    orig = rv.adapter_for
    rv.adapter_for = lambda mode, cfg, base: FakeAdapter()
    try:
        item = R.finish(store, {}, tmp_path, item["id"], "expired", T + 48 * 3600, "expired")
    finally:
        rv.adapter_for = orig
    assert item["status"] == "expired" and all(r["status"] == "closed" for r in item["rounds"])
    assert not list(store.dir(item["id"]).glob("*.mp4"))


# ---- publishing -------------------------------------------------------------------------
class FakeYT:
    def __init__(self, fail=0):
        self.uploads, self.fail = [], fail

    def upload(self, path, title, desc, tags, privacy):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("quota")
        self.uploads.append((path.name, title, privacy))
        return f"vid{len(self.uploads)}"

    def set_thumbnail(self, vid, path):
        pass


class FakeIG:
    def __init__(self, tmp_path, cap=3):
        from spikebot import instagram as I
        self.icfg = I.settings({"ig_user_id": "1", "instagram": {"max_per_day": cap,
                                                                  "posts_csv": str(tmp_path / "ig.csv")}})
        self.posts = I.PostLog(tmp_path / "ig.csv")
        self.posted = []

        class Tok:
            def get(self_):
                return "EAAtoken"
        self.token = Tok()
        me = self

        class Api:
            def create_reel(self_, url, caption, share):
                me.posted.append(caption)
                return "c1"

            def status(self_, c):
                return "FINISHED", ""

            def publish(self_, c):
                return "m1"

            def permalink(self_, m):
                return "https://www.instagram.com/reel/abc/"

        class Host:
            def put(self_, path, name):
                return "https://storage.example/x", lambda: None
        self.ig_factory = Api
        self.host_factory = Host


def publisher(tmp_path, store, yt=None, **cfg):
    lacy_csv = tmp_path / "subs.csv"
    c = {"lacy": {"submissions_csv": str(lacy_csv), "max_uploads_per_day": 2}, "data_dir": str(store.data_dir),
         "youtube_client_id": "a", "youtube_client_secret": "b", "youtube_refresh_token": "c"} | cfg
    yt = yt or FakeYT()
    return R.Publisher(c, tmp_path, store, youtube_factory=lambda: yt, ig=FakeIG(tmp_path)), yt


def approved(tmp_path, store=None, label="A", now=T):
    if store is None:
        store, item = new_item(tmp_path, now=now)
    else:
        item = store.create("lacy", {"job_id": f"j{int(now)}"}, now)
        item = R.render_round(store, item["id"], {}, tmp_path, {"kind": "initial"}, now=now, adapter=FakeAdapter())
    with store.edit(item["id"]) as it:
        R.approve(it, 1, label, R.settings({}), {"youtube", "instagram"}, now)
    return store, store.load(item["id"])


def test_publish_youtube_and_instagram_for_lacy(tmp_path, monkeypatch):
    store, item = approved(tmp_path)
    jobs = L.Jobs(store.data_dir)
    jobs.save({"id": "20261119-110000", "status": "review"})
    pub, yt = publisher(tmp_path, store)
    monkeypatch.setattr("spikebot.editor.pick_thumbnail", lambda *a, **k: 0x1234)
    assert pub.available() == {"youtube", "instagram"}
    assert sorted(pub.due(T)) == sorted([(item["id"], "youtube"), (item["id"], "instagram")])
    row = pub.publish(item["id"], "youtube", T)
    assert row["status"] == "done" and row["url"] == "https://youtube.com/shorts/vid1"
    assert yt.uploads == [("final.mp4", "Lacy moment 1A", "public")]
    rows = L.Submissions(tmp_path / "subs.csv").rows()
    assert rows[0]["job_id"] == "20261119-110000" and rows[0]["youtube_url"].endswith("vid1")
    assert jobs.load("20261119-110000")["status"] == "uploaded"
    row = pub.publish(item["id"], "instagram", T)
    assert row["status"] == "done" and row["url"] == "https://www.instagram.com/reel/abc/"
    assert L.Submissions(tmp_path / "subs.csv").rows()[0]["instagram_url"] == row["url"]
    item = store.load(item["id"])
    assert not R.publish_finished(item)          # manual (TikTok) not done yet
    with store.edit(item["id"]) as it:
        it["publish"]["manual"]["status"] = "done"
    assert R.publish_finished(store.load(item["id"]))


def test_publish_waits_for_window_and_caps(tmp_path):
    store, item = approved(tmp_path)
    pub, yt = publisher(tmp_path, store)
    night = datetime(2026, 11, 19, 23, 30, tzinfo=UK).timestamp()
    row = pub.publish(item["id"], "youtube", night)
    assert row["status"] == "waiting" and "posting window" in row["reason"] and not yt.uploads
    # Lacy cap 2 a day: two uploads, the third waits
    for k in range(2):
        _, it = approved(tmp_path, store, now=T + k + 1)
        assert pub.publish(it["id"], "youtube", T)["status"] == "done"
    row = pub.publish(item["id"], "youtube", T)
    assert row["status"] == "waiting" and "Lacy daily cap 2" in row["reason"]
    assert pub.publish(item["id"], "youtube", T + 86400)["status"] == "done"   # next UK day


def test_publish_retries_then_fails(tmp_path):
    store, item = approved(tmp_path)
    pub, yt = publisher(tmp_path, store, yt=FakeYT(fail=5))
    assert pub.publish(item["id"], "youtube", T)["status"] == "retry"
    assert pub.publish(item["id"], "youtube", T)["status"] == "retry"
    row = pub.publish(item["id"], "youtube", T)
    assert row["status"] == "failed" and row["attempts"] == 3 and "quota" in row["error"]
    assert (item["id"], "youtube") not in pub.due(T)


def test_lacy_title_rules_still_apply_at_upload(tmp_path):
    store, item = approved(tmp_path)
    with store.edit(item["id"]) as it:
        it["rounds"][0]["variants"][0]["title"] = "This clip is cringe Lacy"
    pub, yt = publisher(tmp_path, store, yt=FakeYT())
    pub.rcfg["max_attempts"] = 1
    row = pub.publish(item["id"], "youtube", T)
    assert row["status"] == "failed" and "title rules" in row["error"] and not yt.uploads


# ---- Discord layouts --------------------------------------------------------------------
def test_round_payload_and_buttons(tmp_path):
    store, item = new_item(tmp_path)
    p = DB.round_payload(item, item["rounds"][0])
    assert "`" + item["id"] + "`" in p["content"] and "Expires <t:" in p["content"]
    assert [e["title"].split(" ")[0] for e in p["embeds"]] == ["A", "B", "C"]
    assert "experimental" in p["embeds"][2]["title"]
    assert [f["name"] for f in p["embeds"][0]["fields"]] == ["YouTube title", "Instagram caption", "TikTok caption"]
    labels = [b["label"] for row in p["rows"] for b in row]
    assert labels == ["Approve A", "Approve B", "Approve C", "Redo (3 new)", "Reject", "Feedback", "Edit caption"]
    for row in p["rows"]:
        for b in row:
            assert len(b["custom_id"]) <= 100
            d = DB.parse_cid(b["custom_id"])
            assert d["item"] == item["id"] and d["round"] == 1
    assert DB.parse_cid("something else") is None
    assert all(b["disabled"] for row in DB.buttons(item, item["rounds"][0], True) for b in row)
    total = sum(len(json.dumps(e)) for e in p["embeds"])
    assert total < 6000


def test_manual_payload_for_lacy_logs_links():
    item = {"id": "lacy-1", "mode": "lacy", "source": {"job_id": "20261119-110000"}}
    v = {"label": "B", "title": "Lacy t", "captions": {"tiktok": "Lacy tt", "instagram": "Lacy ig"}}
    p = DB.manual_payload(item, v, ["tiktok"])
    assert "TikTok caption:\n```Lacy tt```" in p["content"] and "Instagram caption" not in p["content"]
    assert "spikebot.lacy posted 20261119-110000 --tiktok URL" in p["content"]
    p = DB.manual_payload({"id": "tg-1", "mode": "tokyo_girl", "source": {}},
                          v | {"silent": True}, ["youtube", "instagram", "tiktok"], link="https://x")
    assert "Tokyo Girl sound" in p["content"] and "YouTube title" in p["content"] and p["embeds"][0]["url"] == "https://x"


def test_style_overview_shows_pins_and_feedback(tmp_path):
    prof = S.Profile(tmp_path / "p.json")
    prof.pin("font", "montserrat")
    prof.data["feedback"]["intents"] = {"text_bigger": 3}
    text = DB.style_overview(prof, {"fast": {}})
    assert "font **montserrat** 📌" in text and "text_bigger ×3" in text and "fast (0/0)" in text
    assert DB.style_options("hook:tokyo_girl") == ["news", "theory", "meme", "couple"]


# ---- Discord bot flows with fake Discord objects ----------------------------------------
class FakeMsg:
    _n = 0

    def __init__(self, channel, content, **kw):
        FakeMsg._n += 1
        self.id, self.channel, self.content, self.kw = FakeMsg._n, channel, content, kw
        self.jump_url = f"https://discord.com/channels/1/{channel.id}/{self.id}"
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        self.content = kw.get("content", self.content)

    async def delete(self):
        self.deleted = True

    async def create_thread(self, name, auto_archive_duration=None):
        return self.channel.bot_channels.setdefault(999, FakeChannel(999, self.channel.bot_channels))


class FakeChannel:
    def __init__(self, cid, registry):
        self.id, self.sent, self.bot_channels = cid, [], registry
        registry[cid] = self

        class G:
            filesize_limit = 10 * 1024 * 1024
        self.guild = G()

    async def send(self, content=None, **kw):
        m = FakeMsg(self, content, **kw)
        self.sent.append(m)
        return m

    async def fetch_message(self, mid):
        return next(m for m in self.sent if m.id == mid)


class FakeResponse:
    def __init__(self):
        self.done, self.sent, self.modal = False, [], None

    def is_done(self):
        return self.done

    async def defer(self, **kw):
        self.done = True

    async def send_message(self, text, **kw):
        self.done = True
        self.sent.append((text, kw))

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal


class FakeUser:
    def __init__(self, uid):
        self.id, self.display_name = uid, "Tim"


class FakeInteraction:
    def __init__(self, uid=42):
        self.user, self.response = FakeUser(uid), FakeResponse()

        class F:
            sent = []

            async def send(self_, text, **kw):
                self_.sent.append(text)
        self.followup = F()


def bot_for(tmp_path, store):
    cfg = {"data_dir": str(store.data_dir), "discord_bot_token": "t", "discord_channel_id": "100",
           "discord_tiktok_channel_id": "200", "discord_approver_ids": "42",
           "lacy": {"submissions_csv": str(tmp_path / "subs.csv")}}
    (tmp_path / "config.json").write_text("{}")
    b = DB.ReviewBot(cfg, tmp_path / "config.json")
    reg: dict = {}
    FakeChannel(100, reg)
    FakeChannel(200, reg)

    async def channel(cid):
        return reg.get(cid)
    b.channel = channel
    b.render_q = asyncio.Queue()
    b._publisher, _ = publisher(tmp_path, store)
    return b, reg


def test_bot_posts_round_with_previews_and_thread(tmp_path):
    store, item = new_item(tmp_path)
    b, reg = bot_for(tmp_path, store)
    asyncio.run(b.scan(T))
    msg = reg[100].sent[0]
    assert len(msg.kw["files"]) == 3 and len(msg.kw["embeds"]) == 3
    item = store.load(item["id"])
    assert item["thread_id"] == 999 and item["rounds"][0]["message_id"] == msg.id
    asyncio.run(b.scan(T))
    assert len(reg[100].sent) == 1   # posted once


def test_bot_only_lets_approvers_press_buttons(tmp_path):
    store, item = new_item(tmp_path)
    b, reg = bot_for(tmp_path, store)
    inter = FakeInteraction(uid=7)
    asyncio.run(b.on_button(inter, {"action": "approve", "item": item["id"], "round": 1, "label": "A"}))
    assert "Only Tim" in inter.response.sent[0][0]
    assert store.load(item["id"])["status"] == "open"


def test_bot_approve_flow_posts_tiktok_and_links(tmp_path, monkeypatch):
    store, item = new_item(tmp_path)
    b, reg = bot_for(tmp_path, store)
    monkeypatch.setattr("spikebot.editor.pick_thumbnail", lambda *a, **k: 0x1234)
    monkeypatch.setattr(R, "adapter_for", lambda mode, cfg, base: FakeAdapter())

    async def go():
        await b.scan(T)
        await b.on_button(FakeInteraction(), {"action": "approve", "item": item["id"], "round": 1, "label": "B"})
        await b.publish_due(T)
        await b.scan(T)
        await b.scan(T)
    asyncio.run(go())
    item = store.load(item["id"])
    assert item["status"] == "done"
    thread = [m.content for m in reg[999].sent]
    assert any("Approved **B**" in t for t in thread)
    assert any("YouTube Shorts** is live: https://youtube.com/shorts/vid1" in t for t in thread)
    assert any("Instagram Reels** is live" in t for t in thread)
    assert any(t.startswith("🏁 Done") for t in thread)
    tiktok = reg[200].sent[0]
    assert "TikTok caption" in tiktok.content and tiktok.kw["file"].filename == f"{item['id']}.mp4"
    first = reg[100].sent[0]
    assert "Approved B" in first.edits[-1]["content"]
    prof = store.profile()
    assert prof.data["decisions"][-1]["action"] == "approve" and prof.data["decisions"][-1]["chosen"] == "B"


def test_bot_reject_redo_and_feedback_modal(tmp_path, monkeypatch):
    store, item = new_item(tmp_path)
    b, reg = bot_for(tmp_path, store)
    monkeypatch.setattr(R, "adapter_for", lambda mode, cfg, base: FakeAdapter())

    async def go():
        await b.scan(T)
        inter = FakeInteraction()
        await b.on_button(inter, {"action": "redo", "item": item["id"], "round": 1, "label": ""})
        assert store.load(item["id"])["request"]["kind"] == "redo"
        await b.scan(T)
        assert b.render_q.qsize() == 1
        inter = FakeInteraction()
        await b.on_button(inter, {"action": "feedback", "item": item["id"], "round": 1, "label": ""})
        assert inter.response.sent[0][0] == "Which variant?"
        inter = FakeInteraction()
        await b.on_button(inter, {"action": "fbpick", "item": item["id"], "round": 1, "label": "C"})
        assert inter.response.modal is not None and "Feedback on C" in inter.response.modal.title
        inter = FakeInteraction()
        await b.on_button(inter, {"action": "edpick", "item": item["id"], "round": 1, "label": "A"})
        assert inter.response.modal.title_in.default == "Lacy moment 1A"
        inter = FakeInteraction()
        await b.on_button(inter, {"action": "reject", "item": item["id"], "round": 1, "label": ""})
    asyncio.run(go())
    assert store.load(item["id"])["status"] == "rejected"
    assert "Rejected" in reg[100].sent[0].edits[-1]["content"]


def test_bot_expires_old_items(tmp_path, monkeypatch):
    store, item = new_item(tmp_path)
    b, reg = bot_for(tmp_path, store)
    monkeypatch.setattr(R, "adapter_for", lambda mode, cfg, base: FakeAdapter())
    asyncio.run(b.scan(T))
    asyncio.run(b.scan(T + 49 * 3600))
    assert store.load(item["id"])["status"] == "expired"
    assert "Expired" in reg[100].sent[0].edits[-1]["content"]
    assert store.profile().data["decisions"][-1]["action"] == "expire"


def test_cli_approve_and_list(tmp_path, capsys):
    store, item = new_item(tmp_path)
    cfg = {"data_dir": str(store.data_dir)}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    assert R.main(["--config", str(tmp_path / "config.json"), "approve", item["id"], "z"]) == 1
    assert R.main(["--config", str(tmp_path / "config.json"), "approve", item["id"], "b"]) == 0
    assert store.load(item["id"])["approved"]["label"] == "B"
    assert R.main(["--config", str(tmp_path / "config.json"), "list"]) == 0
    assert "approved" in capsys.readouterr().out


# ---- #inspiration in the bot -----------------------------------------------------------------
class FakeAttachment:
    def __init__(self, name):
        self.filename, self.content_type = name, "video/mp4"

    async def save(self, path):
        Path(path).write_bytes(b"video")


class FakeMessage:
    def __init__(self, channel, content, attachments=(), uid=42):
        self.channel, self.content, self.attachments = channel, content, list(attachments)
        self.author = FakeUser(uid)
        self.author.bot = False
        self.replies = []

    async def reply(self, text, **kw):
        self.replies.append(text)

    async def create_thread(self, name, auto_archive_duration=None):
        th = FakeChannel(555, self.channel.bot_channels)
        th.parent_id = self.channel.id
        return th


def test_inspiration_link_fails_then_upload_is_analysed(tmp_path, monkeypatch):
    store = R.Store(tmp_path / "data")
    b, reg = bot_for(tmp_path, store)
    b.rcfg["inspiration_channel_id"] = 300
    insp = FakeChannel(300, reg)
    calls = []

    async def analyse(name, copy, url=None, file=None, note="", **kw):
        calls.append((name, copy, url, file))
        if url:
            return {"ok": False, "error": "login required", "ask_upload": True}
        return {"ok": True, "name": name, "text": f"**Preset `{name}` saved**", "strip": None}
    monkeypatch.setattr(b, "analyse", analyse)

    async def go():
        await b.on_message(FakeMessage(insp, "https://www.instagram.com/reel/abc/ name: night drive copy: colour, pacing"))
        thread = reg[555]
        assert "upload the video file directly" in thread.sent[-1].content
        await b.on_message(FakeMessage(thread, "here you go", [FakeAttachment("clip.mp4")]))
        assert thread.sent[-1].content == "**Preset `night-drive` saved**"
        await b.on_message(FakeMessage(insp, "https://www.tiktok.com/@a/video/1", uid=7))   # not Tim: ignored
        await b.on_message(FakeMessage(insp, "just chatting"))
    asyncio.run(go())
    assert calls[0][:3] == ("night-drive", ["colour", "pacing"], "https://www.instagram.com/reel/abc/")
    assert calls[1][0] == "night-drive" and calls[1][1] == ["colour", "pacing"] and calls[1][3] is not None
    assert len(calls) == 2


def test_inspiration_sound_links_become_songs(tmp_path, monkeypatch):
    from spikebot import music as M
    store = R.Store(tmp_path / "data")
    b, reg = bot_for(tmp_path, store)
    b.rcfg["inspiration_channel_id"] = 300
    insp = FakeChannel(300, reg)
    page = "https://www.tiktok.com/music/Tokyo-Girl-7301234567890123456"
    monkeypatch.setattr(M, "resolve_short", lambda u: page if "vm.tiktok" in u else u)
    songs, analysed = [], []

    async def add_songs(message, files, urls=()):
        songs.append(list(urls))

    async def analyse(name, copy, url=None, file=None, note="", **kw):
        analysed.append(url)
        return {"ok": True, "name": name, "text": "saved", "strip": None}
    monkeypatch.setattr(b, "add_songs", add_songs)
    monkeypatch.setattr(b, "analyse", analyse)

    async def go():
        await b.on_message(FakeMessage(insp, "https://vm.tiktok.com/ZMabc/"))
        await b.on_message(FakeMessage(insp, "sound: https://www.tiktok.com/@a/video/1"))
        await b.on_message(FakeMessage(insp, "https://www.tiktok.com/@a/video/2"))
    asyncio.run(go())
    assert songs == [[page], ["https://www.tiktok.com/@a/video/1"]]
    assert analysed == ["https://www.tiktok.com/@a/video/2"]


def test_inspiration_threads_archive_after_12_quiet_hours(tmp_path):
    store = R.Store(tmp_path / "data")
    b, reg = bot_for(tmp_path, store)
    b.rcfg["inspiration_channel_id"] = 300
    insp = FakeChannel(300, reg)
    now = 1_800_000_000.0

    def sid(ts):
        return int(ts * 1000 - DB.DISCORD_EPOCH_MS) << 22

    class Th:
        def __init__(self, created, last=None, owner=99, archived=False):
            self.id, self.owner_id, self.archived = sid(created), owner, archived
            self.last_message_id = sid(last) if last else None
            self.name = "Inspiration"

        async def edit(self, archived):
            self.archived = archived

    old = Th(now - 13 * 3600)
    busy = Th(now - 20 * 3600, last=now - 3600)      # old, but Tim posted an hour ago
    fresh = Th(now - 2 * 3600)
    theirs = Th(now - 30 * 3600, owner=7)            # someone else's thread: left alone
    insp.threads = [old, busy, fresh, theirs]

    class C:
        user = FakeUser(99)
    b.client = C()
    assert abs(DB.snowflake_ts(old.id) - (now - 13 * 3600)) < 0.01
    asyncio.run(b.archive_inspiration(now))
    assert [t.archived for t in insp.threads] == [True, False, False, False]
    busy.last_message_id = sid(now - 13 * 3600)
    asyncio.run(b.archive_inspiration(now + 60))      # checks at most every 10 min
    assert not busy.archived
    asyncio.run(b.archive_inspiration(now + 601))
    assert busy.archived


def test_trend_schedule_runs_each_slot_once_and_skips_pileups(tmp_path, monkeypatch):
    from datetime import datetime as _dt
    from spikebot import review as RV
    from spikebot import tokyo_girl as TG
    uk = RV.UK

    def at(h, m):
        return _dt(2026, 11, 18, h, m, tzinfo=uk).timestamp()
    times = ["07:20", "15:50", "19:40"]
    assert DB.trend_slots_due(times, {}, at(7, 0)) == (None, [])
    assert DB.trend_slots_due(times, {}, at(7, 25)) == ("07:20", [])
    assert DB.trend_slots_due(times, {"2026-11-18": ["07:20"]}, at(9, 0)) == (None, [])
    assert DB.trend_slots_due(times, {}, at(20, 0)) == ("19:40", ["07:20", "15:50"])   # bot was down all day

    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    runs = []

    async def run_trend(inter, song, n):
        runs.append(song)
        b._trend_running = False
    monkeypatch.setattr(b, "run_trend", run_trend)

    async def go():
        assert await b.trend_tick(at(7, 21)) == "07:20"
        await asyncio.sleep(0)
        assert await b.trend_tick(at(7, 30)) is None             # same slot never twice
        assert await b.trend_tick(at(15, 51)) == "15:50"
        await asyncio.sleep(0)
    asyncio.run(go())
    assert runs == ["auto", "auto"]

    rows = [{"file": "new.mp3"}, {"file": "old.mp3"}]
    assert TG.pick_rotating(rows, {})["file"] == "new.mp3"
    assert TG.pick_rotating(rows, {"new.mp3": 1})["file"] == "old.mp3"



def test_progress_bar_is_posted_edited_and_removed(tmp_path):
    from spikebot import progress as P
    store = R.Store(tmp_path / "data")
    b, reg = bot_for(tmp_path, store)
    review_ch = reg[100]
    job = P.Job.create(store.data_dir, "Trending sound edit · original-sound", now=1000.0)
    job.data["started"] = 1000.0
    with P.tracking(job):
        P.current().update(0.0, "Version A (1/3): rendering")
        with P.current().span(0.0, 1 / 3):
            P.current().update(0.5, detail="shot 7/14")
    job.write(force=True)
    t = P.text(job.data, 1100.0)
    assert "33%" in t and "shot 7/14" in t and "1:40" in t and "█" in t

    async def go():
        await b.progress_tick(1100.0)
        msg = review_ch.sent[-1]
        assert "Trending sound edit" in msg.content
        job.update(1.0, "Version C (3/3): preview")
        job.write(force=True)
        await b.progress_tick(1105.0)
        assert msg.edits and "100%" in msg.content
        job.finish(True)
        await b.progress_tick(1110.0)
        assert getattr(msg, "deleted", False) and not P.load_all(store.data_dir)
        bad = P.Job.create(store.data_dir, "Lacy clip x", now=1200.0)
        bad.data["started"] = 1200.0
        bad.finish(False, "VOD not published")
        await b.progress_tick(1300.0)
        assert "VOD not published" in review_ch.sent[-1].content and not P.load_all(store.data_dir)
    asyncio.run(go())
    # a subprocess finds its job through the environment
    j2 = P.Job.create(store.data_dir, "Studying x", channel="custom", channel_id=555)
    env = DB.progress_env(j2)
    assert env[P.ENV] == str(j2.path)


def test_stats_channels_are_created_once_then_renamed(tmp_path):
    from datetime import datetime as _dt
    from spikebot import review as RV
    now = _dt(2026, 11, 17, 21, 0, tzinfo=RV.UK).timestamp()
    names = DB.stat_names({"live": 5, "watching": 40, "moments_today": 12, "to_review": 3,
                           "approved_today": 2, "started": now - (5 * 3600 + 30 * 60 + 20)}, now)
    assert names["live"] == "🟢 Live streamers: 5/40" and names["uptime"] == "⏱ Bot uptime: 5h 30m"
    assert names["gta6"] == "🎮 GTA 6 in 2 days"
    assert DB.stat_names({}, _dt(2026, 11, 19, 9, 0, tzinfo=RV.UK).timestamp())["gta6"] == "🎮 GTA 6: out now"

    class Ch:
        _n = 100

        def __init__(self, name):
            Ch._n += 1
            self.id, self.name, self.renames = Ch._n, name, 0

        async def edit(self, name, reason=None):
            self.name, self.renames = name, self.renames + 1

    class Guild:
        def __init__(self):
            self.chans, self.categories = {}, []
            self.default_role, self.me = object(), object()

        def get_channel(self, cid):
            return self.chans.get(cid)

        async def create_category(self, name, **kw):
            c = Ch(name)
            self.chans[c.id] = c
            self.categories.append(c)
            return c

        async def create_voice_channel(self, name, category=None, reason=None):
            c = Ch(name)
            self.chans[c.id] = c
            return c

    g = Guild()
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    b.rcfg["guild_id"] = 1

    class Client:
        def get_guild(self, gid):
            return g
    b.client = Client()
    live = {"n": 5}
    b.stats_source = lambda t: {"live": live["n"], "watching": 40, "moments_today": 1}
    asyncio.run(b.stats_tick(now))
    made = [c.name for c in g.chans.values()]
    assert "📊 Stats" in made and "🟢 Live streamers: 5/40" in made and len(made) == 7
    live["n"] = 6
    asyncio.run(b.stats_tick(now + 600))
    assert len(g.chans) == 7                                  # no duplicates
    assert "🟢 Live streamers: 6/40" in [c.name for c in g.chans.values()]


def test_progress_dead_worker_shows_stopped(tmp_path):
    import os
    from spikebot import progress
    job = progress.Job.create(tmp_path, "Trend edit")
    now = job.data["updated"]
    assert progress.state_of(job.data, now) == "running"      # this process is alive
    d = dict(job.data, pid=999999999)                          # no such process
    assert progress.state_of(d, now) == "stalled"
    assert "process ended" in progress.text(d, now)
    assert job.data["pid"] == os.getpid()
