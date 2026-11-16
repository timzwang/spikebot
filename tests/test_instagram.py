"""Instagram Reels auto-posting, token refresh, TikTok export and the Lacy hook-up.
Run: python -m pytest -q"""
import asyncio
import csv
from pathlib import Path

import pytest

from spikebot import instagram as I
from spikebot import lacy as L
from tests.test_lacy import FakeYT, T, ready_job
from tests.test_tokyo_girl import FakeDiscord

TOKEN = "IGAAtok_secret_123"


class Resp:
    def __init__(self, code, j):
        self.status_code, self._j, self.text = code, j, str(j)

    def json(self):
        return self._j


class FakeHTTP:
    """graph.instagram.com stand-in. statuses: container status_code sequence."""

    def __init__(self, statuses=("IN_PROGRESS", "FINISHED"), fail_create=False, refresh_ok=True):
        self.calls, self.statuses, self.fail_create, self.refresh_ok = [], list(statuses), fail_create, refresh_ok

    def post(self, url, timeout=None, headers=None, json=None, **kw):
        self.calls.append(("post", url, headers, json))
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        if url.endswith("/media"):
            if self.fail_create:
                return Resp(400, {"error": {"message": f"bad video_url (token {TOKEN})", "code": 9004}})
            return Resp(200, {"id": "c1"})
        if url.endswith("/media_publish"):
            assert json == {"creation_id": "c1"}
            return Resp(200, {"id": "m1"})
        raise AssertionError(url)

    def get(self, url, timeout=None, headers=None, params=None, **kw):
        self.calls.append(("get", url, headers, params))
        if url.endswith("/refresh_access_token"):
            assert params == {"grant_type": "ig_refresh_token", "access_token": TOKEN}
            if not self.refresh_ok:
                return Resp(400, {"error": {"message": "Session has expired"}})
            return Resp(200, {"access_token": "IGAAnew_456", "token_type": "bearer", "expires_in": 5183944})
        if url.endswith("/c1"):
            return Resp(200, {"status_code": self.statuses.pop(0), "status": "x"})
        if url.endswith("/m1"):
            return Resp(200, {"permalink": "https://www.instagram.com/reel/ABC123/"})
        raise AssertionError(url)


class FakeHost:
    def __init__(self):
        self.put_paths, self.deleted = [], 0

    def put(self, path, name):
        assert path.exists()
        self.put_paths.append(path)
        return "https://storage.googleapis.com/b/instagram/x.mp4?X-Goog-Signature=abc", self.delete

    def delete(self):
        self.deleted += 1


def env(tmp_path, token=TOKEN):
    p = tmp_path / ".env"
    p.write_text(f"# keep me\nTWITCH_CLIENT_ID=abc\nIG_USER_ID=1784\nIG_ACCESS_TOKEN={token}\n")
    return p


def xposter(tmp_path, http=None, host=None, **icfg):
    cfg = {"ig_user_id": "1784", "instagram": icfg}
    ic = I.settings(cfg, tmp_path)
    tok = I.Token(env(tmp_path))
    http = http or FakeHTTP()
    host = host or FakeHost()
    xp = I.Crossposter(ic, tok, I.PostLog(ic["posts_csv"]), lambda: I.Instagram("1784", tok, http=http),
                       lambda: host, sleep=lambda s: None)
    return xp, http, host


# ---- settings / token ------------------------------------------------------------
def test_env_keys_and_settings(tmp_path):
    from spikebot.main import apply_env
    cfg = apply_env({}, [], {"IG_USER_ID": "1784", "IG_MAX_REELS_PER_DAY": "5", "INSTAGRAM_AUTO_POST": "false"})
    ic = I.settings(cfg, tmp_path)
    assert ic["user_id"] == "1784" and ic["max_per_day"] == 5 and ic["enabled"] is False
    assert ic["posts_csv"] == str(tmp_path / "logs/instagram_posts.csv")
    assert ic["tiktok_dir"] == str(tmp_path / "exports/tiktok")
    assert I.settings({})["max_per_day"] == 3 and I.settings({})["enabled"] is True


def test_token_file_beats_stale_process_env(tmp_path):
    tok = I.Token(env(tmp_path, "from_file"), {"IG_ACCESS_TOKEN": "old_from_systemd"})
    assert tok.get() == "from_file"
    assert I.Token(tmp_path / "missing.env", {"IG_ACCESS_TOKEN": "x"}).get() == "x"


# ---- publish flow ------------------------------------------------------------------
def test_post_reel_container_poll_publish_and_delete(tmp_path):
    xp, http, host = xposter(tmp_path)
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"v")
    res = I.post_reel(xp.ig_factory(), host, clip, "cap #GTA6", xp.icfg, sleep=lambda s: None)
    assert res == {"container_id": "c1", "media_id": "m1", "permalink": "https://www.instagram.com/reel/ABC123/"}
    create = http.calls[0]
    assert create[1] == "https://graph.instagram.com/v23.0/1784/media"
    assert create[3] == {"media_type": "REELS", "caption": "cap #GTA6", "share_to_feed": True,
                         "video_url": "https://storage.googleapis.com/b/instagram/x.mp4?X-Goog-Signature=abc"}
    assert [c[0] for c in http.calls] == ["post", "get", "get", "post", "get"]  # 2 polls
    assert host.deleted == 1
    for c in http.calls:  # token only ever in the header, never in a URL
        assert TOKEN not in c[1]


@pytest.mark.parametrize("statuses,match", [(("ERROR",), "couldn't process"), (("IN_PROGRESS",) * 50, "after")])
def test_post_reel_errors_still_delete_public_copy(tmp_path, statuses, match):
    xp, http, host = xposter(tmp_path, http=FakeHTTP(statuses=statuses), poll_timeout_s=30, poll_s=10)
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"v")
    t = iter(range(0, 1000, 10))
    with pytest.raises(I.InstagramError, match=match):
        I.post_reel(xp.ig_factory(), host, clip, "c", xp.icfg, sleep=lambda s: None, clock=lambda: next(t))
    assert host.deleted == 1


def test_api_error_never_shows_the_token(tmp_path):
    xp, http, host = xposter(tmp_path, http=FakeHTTP(fail_create=True))
    with pytest.raises(I.InstagramError) as e:
        xp.ig_factory().create_reel("https://x", "c")
    assert TOKEN not in str(e.value) and "9004" in str(e.value) and "***" in str(e.value)


# ---- token refresh -----------------------------------------------------------------
def test_refresh_every_50_days_and_saved_to_env(tmp_path):
    ic = I.settings({"ig_user_id": "1784"}, tmp_path)
    tok, state, http = I.Token(env(tmp_path)), I.State(tmp_path / "data"), FakeHTTP()
    refresh = lambda t: I.refresh_token(t, http)  # noqa: E731
    assert I.maybe_refresh(ic, tok, state, T, refresh) == "new_token"   # first sight = just issued
    assert I.maybe_refresh(ic, tok, state, T + 49 * 86400, refresh) == "not_due"
    assert I.maybe_refresh(ic, tok, state, T + 50 * 86400, refresh) == "refreshed"
    text = (tmp_path / ".env").read_text()
    assert "IG_ACCESS_TOKEN=IGAAnew_456" in text and TOKEN not in text
    assert "TWITCH_CLIENT_ID=abc" in text and "# keep me" in text            # nothing else lost
    assert oct((tmp_path / ".env").stat().st_mode)[-3:] == "600"
    st = state.load()
    assert st["refreshed_at"] == T + 50 * 86400 and "IGAA" not in str(st)   # no secret in state
    assert I.maybe_refresh(ic, tok, state, T + 51 * 86400, refresh) == "not_due"  # clock restarted


def test_refresh_failure_waits_6h_before_retrying(tmp_path):
    ic = I.settings({}, tmp_path)
    tok, state = I.Token(env(tmp_path)), I.State(tmp_path / "data")
    refresh = lambda t: I.refresh_token(t, FakeHTTP(refresh_ok=False))  # noqa: E731
    I.maybe_refresh(ic, tok, state, T, refresh)
    res = I.maybe_refresh(ic, tok, state, T + 50 * 86400, refresh)
    assert res.startswith("failed") and "expired" in res and TOKEN not in res
    assert I.maybe_refresh(ic, tok, state, T + 50 * 86400 + 3600, refresh) == "not_due"
    assert I.maybe_refresh(ic, tok, state, T + 50 * 86400 + 7 * 3600, refresh).startswith("failed")


# ---- captions ----------------------------------------------------------------------
def test_hook_differs_from_youtube_title_and_has_gta6_tags():
    job = {"id": "20261119-060001", "title": "Lacy was NOT ready for this 😂", "vibe": ["KEKW"],
           "category": "Grand Theft Auto VI", "channel": "lacy"}
    for _ in range(20):
        hook = I.pick_hook(job, set())
        assert hook != job["title"] and "Lacy" in hook and not L.BANNED.search(hook)
    caps = I.captions(job, I.pick_hook(job, set()))
    assert "#GTA6" in caps["instagram"] and "#GTAVI" in caps["instagram"] and "#Lacy" in caps["instagram"]
    assert "#GTA6" in caps["tiktok"] and caps["tiktok"] != caps["instagram"]
    assert "#GTA5" in I.captions(dict(job, category="Grand Theft Auto V"), "Lacy x")["instagram"]
    every = {h for hs in I.HOOKS.values() for h in hs}
    assert all(I.hook_ok(h, "") for h in every)
    assert I.pick_hook(job, every - {"Lacy's laugh at the end 😭"}) == "Lacy's laugh at the end 😭"


# ---- Lacy: TikTok export + Reel after the YouTube upload -----------------------------
def test_lacy_upload_exports_tiktok_and_posts_reel(tmp_path):
    jobs, subs = L.Jobs(tmp_path / "data"), L.Submissions(tmp_path / "s.csv")
    xp, http, host = xposter(tmp_path)
    job = ready_job(jobs, 1)
    fake = FakeDiscord()
    dcfg = {"webhook_url": "https://d/x", "discord_max_mb": 10, "gcs_bucket": ""}
    job = L.upload_job(job, L.settings({}), jobs, subs, FakeYT(), now=T, social=xp,
                       send=lambda p, j: L.send_video(dcfg, p, j, post=fake))
    saved = jobs.load(job["id"])
    assert saved["instagram_status"] == "published"
    assert saved["instagram_url"] == "https://www.instagram.com/reel/ABC123/"
    mp4 = tmp_path / "exports/tiktok" / f"lacy_{job['id']}.mp4"
    assert mp4.read_bytes() == b"x" and saved["tiktok_export"] == str(mp4)
    assert mp4.with_suffix(".txt").read_text().strip() == saved["tiktok_caption"]
    assert host.put_paths == [mp4]                       # posted from the export copy
    assert subs.rows()[0]["instagram_url"] == saved["instagram_url"]   # Content Rewards CSV
    (row,) = list(csv.DictReader(open(tmp_path / "logs/instagram_posts.csv")))
    assert row["status"] == "published" and row["job_id"] == job["id"] and row["posted_uk"].startswith("2026-11-19")
    c = fake.calls[0]["payload"]["content"]
    assert "Instagram ✅ <https://www.instagram.com/reel/ABC123/>" in c and "now post on **TikTok**" in c
    assert "Instagram Reels caption" not in c and "--tiktok TIKTOK_URL" in c and "--instagram" not in c
    assert not (jobs.dir(job["id"]) / "short.mp4").exists()


def test_daily_cap_then_bot_retry_next_day(tmp_path):
    jobs, subs = L.Jobs(tmp_path / "data"), L.Submissions(tmp_path / "s.csv")
    xp, http, host = xposter(tmp_path, max_per_day=1)
    http.statuses = ["FINISHED", "FINISHED"]
    j1 = L.upload_job(ready_job(jobs, 1), L.settings({}), jobs, subs, FakeYT(), now=T, social=xp)
    j2 = L.upload_job(ready_job(jobs, 2), L.settings({}), jobs, subs, FakeYT(), now=T + 60, social=xp)
    assert j1["instagram_status"] == "published" and j2["instagram_status"] == "waiting"
    assert I.retry_pending(xp, jobs, subs, now=T + 120) == [(j2["id"], "waiting")]
    assert I.retry_pending(xp, jobs, subs, now=T + 86400) == [(j2["id"], "published")]
    assert jobs.load(j2["id"])["instagram_url"] and subs.rows()[1]["instagram_url"]
    assert I.retry_pending(xp, jobs, subs, now=T + 86400 + 60) == []


def test_failure_keeps_youtube_and_retries_up_to_max(tmp_path):
    jobs, subs = L.Jobs(tmp_path / "data"), L.Submissions(tmp_path / "s.csv")
    xp, http, host = xposter(tmp_path, http=FakeHTTP(fail_create=True), max_attempts=2)
    job = L.upload_job(ready_job(jobs, 1), L.settings({}), jobs, subs, FakeYT(), now=T, social=xp)
    assert job["status"] == "uploaded" and job["instagram_status"] == "failed"
    assert I.retry_pending(xp, jobs, subs, now=T + 3600) == [(job["id"], "failed")]
    assert I.retry_pending(xp, jobs, subs, now=T + 7200) == []          # gave up after 2
    rows = list(csv.DictReader(open(tmp_path / "logs/instagram_posts.csv")))
    assert [r["status"] for r in rows] == ["failed", "failed"] and TOKEN not in str(rows)


def test_without_instagram_keys_only_tiktok_export(tmp_path):
    jobs, subs = L.Jobs(tmp_path / "data"), L.Submissions(tmp_path / "s.csv")
    xp = I.crossposter({}, tmp_path, {})
    job = L.upload_job(ready_job(jobs, 1), L.settings({}), jobs, subs, FakeYT(), now=T, social=xp)
    assert job["instagram_status"] == "off" and Path(job["tiktok_export"]).exists()
    assert I.retry_pending(xp, jobs, subs, now=T + 3600) == []


def test_tiktok_export_prunes_old_files(tmp_path):
    import os
    folder = tmp_path / "tt"
    folder.mkdir()
    old = folder / "lacy_old.mp4"
    old.write_bytes(b"o")
    os.utime(old, (T - 20 * 86400, T - 20 * 86400))
    src = tmp_path / "s.mp4"
    src.write_bytes(b"v")
    out = I.export_tiktok(src, "lacy_new", "cap", folder, 14, now=T)
    assert out.exists() and not old.exists()


def test_bot_tick_alerts_on_failed_refresh(tmp_path):
    from spikebot.main import Bot
    (tmp_path / "config.json").write_text("{}")
    env(tmp_path)
    cfg = {"channels": [], "data_dir": str(tmp_path / "data"), "ig_user_id": "1784"}
    bot = Bot(cfg, str(tmp_path / "config.json"))
    assert bot.ig is not None
    sent = []

    async def fake_send(key, text, kind=None):
        sent.append((key, text))
        return True
    bot.notifier.send = fake_send
    bot.ig.token = I.Token(tmp_path / ".env")
    state = I.State(bot.data_dir)
    I.maybe_refresh(bot.ig.icfg, bot.ig.token, state, T)  # remember the token
    orig = I.refresh_token
    I.refresh_token = lambda t, http=None: orig(t, FakeHTTP(refresh_ok=False))
    try:
        st = state.load()
        st["refreshed_at"] = T - 51 * 86400
        state.save(st)
        asyncio.run(bot.instagram_tick(state, T))
    finally:
        I.refresh_token = orig
    assert sent and sent[0][0] == "instagram_token" and TOKEN not in sent[0][1]
