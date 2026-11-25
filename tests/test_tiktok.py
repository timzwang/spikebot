"""TikTok Content Posting API client (fake HTTP) and the Publisher wiring."""
import json
from pathlib import Path

import pytest

from spikebot import tiktok as TT


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body or {}

    def json(self):
        return self._b


class FakeHTTP:
    def __init__(self, privacy=("PUBLIC_TO_EVERYONE", "SELF_ONLY"), statuses=("PROCESSING_UPLOAD", "PUBLISH_COMPLETE"),
                 unaudited=False):
        self.calls, self.puts, self.unaudited = [], [], unaudited
        self.privacy, self.statuses = list(privacy), list(statuses)

    def post(self, url, data=None, headers=None, timeout=None):
        body = json.loads(data) if isinstance(data, str) else data
        self.calls.append((url.replace(TT.API, ""), body))
        if url == TT.TOKEN_URL:
            return Resp(200, {"access_token": "AT", "expires_in": 86400, "refresh_token": "RT2"})
        if url.endswith("/creator_info/query/"):
            return Resp(200, {"data": {"privacy_level_options": self.privacy, "creator_username": "gta6clipped"},
                              "error": {"code": "ok"}})
        if url.endswith("/publish/video/init/") and self.unaudited:
            return Resp(403, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts",
                                        "message": "Please review our integration guidelines"}})
        if url.endswith("/video/init/"):
            return Resp(200, {"data": {"publish_id": "p1", "upload_url": "https://upload.tiktok/x"}, "error": {"code": "ok"}})
        if url.endswith("/status/fetch/"):
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            d = {"status": st}
            if st == "PUBLISH_COMPLETE":
                d["publicaly_available_post_id"] = [7311]
            return Resp(200, {"data": d, "error": {"code": "ok"}})
        raise AssertionError(url)

    def put(self, url, data=None, headers=None, timeout=None):
        self.puts.append((len(data), headers["Content-Range"]))
        return Resp(201)


CFG = {"client_key": "k", "client_secret": "s", "refresh_token": "RT1", "mode": "direct",
       "privacy": "PUBLIC_TO_EVERYONE", "max_per_day": 15}


def test_chunk_rules():
    assert TT.chunks(3_000_000) == (3_000_000, 1)
    assert TT.chunks(64 * 1024 * 1024) == (64 * 1024 * 1024, 1)
    size, n = TT.chunks(100 * 1024 * 1024)
    assert size == TT.CHUNK and n == 10


def test_direct_post_uploads_and_returns_the_link(tmp_path):
    from spikebot.youtube import set_env_value
    env = tmp_path / ".env"
    set_env_value(env, "TIKTOK_REFRESH_TOKEN", "RT1")
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x" * 1000)
    http = FakeHTTP()
    res = TT.TikTok(dict(CFG), env, http=http, sleep=lambda s: None).post(v, "adin ross landed it #gta6")
    assert res == {"status": "PUBLISH_COMPLETE", "publish_id": "p1", "url": "https://www.tiktok.com/video/7311",
                   "how": "direct"}
    init = next(b for u, b in http.calls if u == "/v2/post/publish/video/init/")
    assert init["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE" and init["post_info"]["title"].startswith("adin")
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": 1000, "chunk_size": 1000, "total_chunk_count": 1}
    assert http.puts == [(1000, "bytes 0-999/1000")]
    assert "TIKTOK_REFRESH_TOKEN=RT2" in env.read_text()          # rotated token kept


def test_unaudited_app_and_silent_edits_go_to_the_inbox(tmp_path):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x" * 10)
    http = FakeHTTP(privacy=("SELF_ONLY",), statuses=("SEND_TO_USER_INBOX",))
    res = TT.TikTok(dict(CFG), None, http=http, sleep=lambda s: None).post(v, "x")
    assert res["how"] == "inbox" and "inbox" in res["url"]
    assert any(u == "/v2/post/publish/inbox/video/init/" for u, _ in http.calls)
    http2 = FakeHTTP(statuses=("SEND_TO_USER_INBOX",))
    res = TT.TikTok(dict(CFG), None, http=http2, sleep=lambda s: None).post(v, "x", inbox=True)
    assert res["how"] == "inbox" and not any(u.endswith("creator_info/query/") for u, _ in http2.calls)


def test_failed_processing_raises(tmp_path):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")

    class Bad(FakeHTTP):
        def post(self, url, data=None, headers=None, timeout=None):
            if url.endswith("/status/fetch/"):
                return Resp(200, {"data": {"status": "FAILED", "fail_reason": "duration_check_failed"},
                                  "error": {"code": "ok"}})
            return super().post(url, data, headers, timeout)
    with pytest.raises(TT.TikTokError, match="duration_check_failed"):
        TT.TikTok(dict(CFG), None, http=Bad(), sleep=lambda s: None).post(v, "x")


def test_publisher_posts_approved_clip_to_tiktok(tmp_path):
    from spikebot import review as R
    from tests.test_review import approved, publisher, T
    store, item = approved(tmp_path)
    pub, _ = publisher(tmp_path, store)
    posted = []

    class FakeTT:
        def post(self, path, caption, inbox=False):
            posted.append((path.name, caption, inbox))
            return {"status": "PUBLISH_COMPLETE", "publish_id": "p", "url": "https://www.tiktok.com/video/1", "how": "direct"}
    pub.tiktok_factory = lambda: FakeTT()
    pub.tiktok_cfg = lambda: dict(CFG)
    with store.edit(item["id"]) as it:
        it["publish"]["tiktok"] = {"status": "queued", "attempts": 0}
    assert (item["id"], "tiktok") in pub.due(T)
    row = pub.publish(item["id"], "tiktok", T)
    assert row["status"] == "done" and row["url"] == "https://www.tiktok.com/video/1"
    assert posted and posted[0][2] is False and "#GTA6" in posted[0][1]


def test_inbox_mode_asks_only_for_upload_scopes(tmp_path, capsys):
    seen = []

    def ask(prompt):
        url = [l for l in capsys.readouterr().out.splitlines() if l.startswith("https://www.tiktok.com/")][0]
        seen.append(url)
        raise SystemExit("stop")
    cfg = dict(CFG, redirect_uri="https://gta6-clipped.vercel.app/tiktok/callback.html", mode="inbox")
    with pytest.raises(SystemExit):
        TT.auth(tmp_path / ".env", cfg, ask=ask)
    assert "video.upload" in seen[0] and "video.publish" not in seen[0]


def test_auth_accepts_a_link_from_an_earlier_run(tmp_path, capsys):
    cfg = dict(CFG, redirect_uri="https://gta6-clipped.vercel.app/tiktok/callback.html")
    links = []

    def first(prompt):
        links.append([l for l in capsys.readouterr().out.splitlines() if l.startswith("https://www.tiktok.com/")][0])
        raise SystemExit("closed")
    with pytest.raises(SystemExit):
        TT.auth(tmp_path / ".env", cfg, ask=first)
    import urllib.parse as up
    old_state = up.parse_qs(up.urlparse(links[0]).query)["state"][0]
    back = f"{cfg['redirect_uri']}?code=abc&scopes=user.info.basic&state={old_state}"
    TT.auth(tmp_path / ".env", cfg, ask=lambda p: back, http=FakeHTTP())   # second run, first link: fine
    assert "TIKTOK_REFRESH_TOKEN=RT2" in (tmp_path / ".env").read_text()
    with pytest.raises(SystemExit, match="30 minutes"):
        TT.auth(tmp_path / ".env", cfg, ask=lambda p: f"{cfg['redirect_uri']}?code=abc&state=forged", http=FakeHTTP())


def test_unaudited_app_listing_public_falls_back_to_the_inbox(tmp_path):
    # live 25 Nov: creator_info listed PUBLIC_TO_EVERYONE but Direct Post gave 403 unaudited_client...
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x" * 10)
    http = FakeHTTP(statuses=("SEND_TO_USER_INBOX",), unaudited=True)
    res = TT.TikTok(dict(CFG), None, http=http, sleep=lambda s: None).post(v, "kai cenat #KaiCenat")
    assert res["how"] == "inbox" and "inbox" in res["url"]
    urls = [u for u, _ in http.calls]
    assert "/v2/post/publish/video/init/" in urls and "/v2/post/publish/inbox/video/init/" in urls
    assert http.puts == [(10, "bytes 0-9/10")]


def test_other_direct_post_errors_still_raise(tmp_path):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x" * 10)

    class Broken(FakeHTTP):
        def post(self, url, data=None, headers=None, timeout=None):
            if url.endswith("/publish/video/init/"):
                self.calls.append((url.replace(TT.API, ""), None))
                return Resp(400, {"error": {"code": "spam_risk_too_many_posts", "message": "slow down"}})
            return super().post(url, data, headers, timeout)

    with pytest.raises(TT.TikTokError, match="spam_risk"):
        TT.TikTok(dict(CFG), None, http=Broken(), sleep=lambda s: None).post(v, "x")
