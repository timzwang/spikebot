"""Cutting a clip out of a VOD that is still being recorded (YourRAGE, 21 Nov), and streamer pronouns."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from spikebot import editor as E
from spikebot import lacy as L

LIVE = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:EVENT
#EXTINF:10.000,
0.ts
#EXTINF:10.000,
1-muted.ts
#EXTINF:10.000,
2.ts
#EXTINF:4.500,
3.ts
"""


def test_segments_and_cut_playlist():
    segs, init, ended = E.hls_segments(LIVE, "https://cdn.example/abc/1080p60/index-muted-X.m3u8")
    assert not ended and [round(s[0], 1) for s in segs] == [0, 10, 20, 30]
    assert segs[1][2] == "https://cdn.example/abc/1080p60/1-muted.ts"
    text, skip = E.cut_playlist(segs, init, 12.0, 10.0)
    assert "0.ts" not in text and "1-muted.ts" in text and "2.ts" in text and "3.ts" not in text
    assert text.rstrip().endswith("#EXT-X-ENDLIST") and skip == pytest.approx(2.0)
    with pytest.raises(E.VodNotReady):
        E.cut_playlist(segs, init, 100.0, 10.0)


def test_live_vod_not_reaching_the_clip_waits(tmp_path, monkeypatch):
    def fake_run(cmd, timeout=600, cwd=None):
        assert "yt_dlp" in cmd
        return subprocess.CompletedProcess(cmd, 0, "https://cdn.example/a/index.m3u8\n", "")
    monkeypatch.setattr(E, "run", fake_run)
    with pytest.raises(E.VodNotReady) as e:
        E.fetch_segment("https://www.twitch.tv/videos/1", 30.0, 20.0, tmp_path / "s.mp4", get_text=lambda u: LIVE)
    assert "so far" in str(e.value)


def test_empty_download_waits_and_retries_instead_of_failing(tmp_path, monkeypatch):
    def fake_run(cmd, timeout=600, cwd=None):
        if "yt_dlp" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "https://cdn.example/a/clip.mp4\n", "")
        Path(cmd[-1]).write_bytes(b"x" * 100)      # ffmpeg wrote next to nothing
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(E, "run", fake_run)
    out = tmp_path / "s.mp4"
    with pytest.raises(E.VodNotReady) as e:
        E.fetch_segment("https://www.twitch.tv/videos/1", 30.0, 20.0, out)
    assert "came back empty" in str(e.value) and not out.exists()

    def slow(cmd, timeout=600, cwd=None):
        if "yt_dlp" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "https://cdn.example/a/clip.mp4\n", "")
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(E, "run", slow)
    with pytest.raises(E.VodNotReady):
        E.fetch_segment("https://www.twitch.tv/videos/1", 30.0, 20.0, out)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_cuts_the_right_range_out_of_a_live_playlist(tmp_path, monkeypatch):
    hls = tmp_path / "vod"
    hls.mkdir()
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=40",
                    "-f", "lavfi", "-i", "sine=d=40", "-c:v", "libx264", "-g", "30", "-c:a", "aac", "-f", "hls",
                    "-hls_time", "4", "-hls_list_size", "0", str(hls / "index.m3u8")], check=True)
    live = (hls / "index.m3u8").read_text().replace("#EXT-X-ENDLIST", "")   # still recording
    (hls / "index.m3u8").write_text(live)
    real_run = E.run

    def fake_run(cmd, timeout=600, cwd=None):
        if "yt_dlp" in cmd:
            return subprocess.CompletedProcess(cmd, 0, str(hls / "index.m3u8") + "\n", "")
        return real_run(cmd, timeout, cwd)
    monkeypatch.setattr(E, "run", fake_run)
    out = E.fetch_segment("x", 21.0, 8.0, tmp_path / "seg.mp4", get_text=lambda u: Path(u).read_text(),
                          get_bytes=lambda u: Path(u).read_bytes())
    info = E.probe(out)
    assert info["duration"] == pytest.approx(8.0, abs=1.0) and info["audio"]
    assert not (tmp_path / ".seg_parts").exists()        # the downloaded parts are cleaned up


def test_pronouns():
    assert L.pronouns_ok("Lacy thought he had it", "he/him")
    assert not L.pronouns_ok("Lacy can't believe her luck", "he/him")
    assert not L.pronouns_ok("She did NOT see this coming", "he/him")
    assert L.pronouns_ok("Lauren can't believe her luck", "she/her")
    from spikebot import ai, campaigns
    assert "he/him" in ai.pronoun_rule("Lacy") and campaigns.pronouns_for("lacy", {}) == "he/him"
    assert campaigns.pronouns_for("laurenhamdenn", {}) == "she/her"


def test_render_job_waits_instead_of_failing(tmp_path):
    from spikebot import review as R
    from tests.test_lacy import job_for
    jobs, lcfg = L.Jobs(tmp_path), L.settings({})

    def not_yet(url, start, dur, out):
        raise E.VodNotReady("the VOD reaches 40 min so far; this clip ends at 52 min")
    job = L.render_job(job_for(tmp_path, jobs, 1), lcfg, jobs, set(), fetch=not_yet,
                       transcribe=lambda p, m: ([], "en", 1.0), review_store=R.Store(tmp_path),
                       cfg={"data_dir": str(tmp_path)}, base=tmp_path)
    assert job["status"] == "waiting_vod" and "so far" in job["reason"]
    old = {"status": "failed", "reason": "segment download came back empty (VOD not ready yet?)", "created": 0}
    assert L.retry_after_fix(old | {"created": __import__("time").time()})   # this week's failures retry once
