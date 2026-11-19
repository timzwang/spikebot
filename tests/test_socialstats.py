import json

from spikebot import socialstats as S


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeYTHttp:
    """YouTube Data API: one channel with 3 uploads over two playlist pages."""

    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.rsplit("/", 1)[-1]
        self.calls.append((path, dict(params or {})))
        p = params or {}
        assert p.get("key") == "k"
        if path == "videos" and p.get("part") == "snippet":
            return Resp(200, {"items": [{"snippet": {"channelId": "UC1"}}]})
        if path == "channels":
            assert p.get("id") == "UC1" or p.get("forHandle") == "@gta"
            return Resp(200, {"items": [{"id": "UC1", "statistics": {"viewCount": "120000", "subscriberCount": "800",
                                                                        "hiddenSubscriberCount": False},
                                         "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if path == "playlistItems":
            if p.get("pageToken"):
                return Resp(200, {"items": [{"contentDetails": {"videoId": "v3"}}]})
            return Resp(200, {"items": [{"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}],
                              "nextPageToken": "n"})
        if path == "videos":
            assert p["id"] == "v1,v2,v3"
            return Resp(200, {"items": [{"statistics": {"likeCount": "10"}}, {"statistics": {"likeCount": "5"}},
                                        {"statistics": {}}]})
        return Resp(404, {"error": {"errors": [{"reason": "notFound"}]}})


class FakeIG:
    user_id = "17841"

    def __init__(self, insights=True):
        self.insights, self.calls = insights, 0

    def _call(self, method, path, params=None, **kw):
        self.calls += 1
        if path == self.user_id:
            return {"followers_count": 250}
        if path.endswith("/media"):
            if params.get("after"):
                return {"data": [{"id": "m3", "like_count": 1}]}
            return {"data": [{"id": "m1", "like_count": 40}, {"id": "m2", "like_count": 2}],
                    "paging": {"cursors": {"after": "c"}, "next": "https://x"}}
        if path.endswith("/insights"):
            if not self.insights:
                raise RuntimeError("HTTP 400 10 (#10) Application does not have permission")
            return {"data": [{"name": "views", "values": [{"value": {"m1": 900, "m2": 50, "m3": 7}[path[:2]]}]}]}
        raise AssertionError(path)


def test_youtube_totals_from_an_uploaded_video(tmp_path):
    data = tmp_path / "data"
    item = data / "review" / "items" / "x"
    item.mkdir(parents=True)
    (item / "item.json").write_text(json.dumps({"publish": {"youtube": {"video_id": "abc"}}}))
    assert S.uploaded_video_ids(tmp_path, data) == ["abc"]
    yt = S.YouTubeStats("k", http=FakeYTHttp())
    st = S.refresh({}, tmp_path, data, now=1000.0, yt=yt, ig=None)
    assert st["platforms"]["youtube"] == {"views": 120000, "followers": 800, "likes": 15, "at": 1000.0}
    assert st["youtube_channel"] == "UC1"                       # remembered, looked up once
    assert st["totals"]["views"] == {"total": 120000, "platforms": ["YT"]}
    assert '"k"' not in json.dumps(S.load(data))                # no API key saved


def test_instagram_views_only_once_every_post_is_counted():
    ig, cache = FakeIG(), {}
    out = S.instagram_totals(ig, cache, now=0.0, budget=2)
    assert out["followers"] == 250 and out["likes"] == 43 and "views" not in out   # 1 post still unread
    out = S.instagram_totals(ig, cache, now=60.0, budget=2)
    assert out["views"] == 957
    out = S.instagram_totals(FakeIG(insights=False), {}, now=0.0)
    assert "views" not in out and out["likes"] == 43


def test_a_failing_platform_keeps_its_last_numbers(tmp_path):
    data = tmp_path / "data"
    st = S.refresh({}, tmp_path, data, now=0.0, yt=None, ig=FakeIG())
    assert st["totals"]["followers"] == {"total": 250, "platforms": ["IG"]}

    class Broken:
        user_id = "1"

        def _call(self, *a, **k):
            raise RuntimeError("HTTP 500")
    st = S.refresh({}, tmp_path, data, now=600.0, yt=None, ig=Broken())
    assert st["totals"]["followers"]["total"] == 250 and "instagram" in st["errors"]
    st = S.refresh({}, tmp_path, data, now=S.STALE_S + 1, yt=None, ig=Broken())
    assert "followers" not in st["totals"]                      # too old to show


def test_combine_sums_platforms():
    plats = {"youtube": {"views": 100, "likes": 5, "followers": 3, "at": 0},
             "instagram": {"likes": 7, "followers": 4, "at": 0}}
    t = S.combine(plats, 10)
    assert t["views"] == {"total": 100, "platforms": ["YT"]}
    assert t["likes"] == {"total": 12, "platforms": ["YT", "IG"]}
