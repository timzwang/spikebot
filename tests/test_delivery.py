"""#ready-to-post: shared settings, and Lacy Shorts sent after the YouTube upload.
Run: python -m pytest -q"""
from pathlib import Path

import pytest

from spikebot import delivery as D
from spikebot import lacy as L
from tests.test_lacy import FakeYT, T, ready_job
from tests.test_tokyo_girl import FakeDiscord, FakeGCS


def test_settings_new_names_win_over_old():
    from spikebot.main import apply_env
    cfg = apply_env({"videos_webhook_url": "https://d/new", "tokyo_girl_webhook_url": "https://d/old",
                     "videos": {"discord_max_mb": 100, "keep_days": 3}, "tokyo_girl": {"discord_max_mb": 50}},
                    [], {"VIDEOS_GCS_BUCKET": "new-b", "TOKYO_GIRL_GCS_BUCKET": "old-b"})
    d = D.settings(cfg, Path("/opt/spikebot"))
    assert d["webhook_url"] == "https://d/new" and d["gcs_bucket"] == "new-b"
    assert d["discord_max_mb"] == 100 and d["keep_days"] == 3 and d["link_days"] == 7
    assert D.settings({})["webhook_url"] == "" and D.settings({})["gcs_bucket"] == ""


def test_message_labels_the_mode_and_never_pings():
    p = D.message("lacy", "lacy_1.mp4", 5_000_000, ["45s"], [("Title", "Lacy @everyone")])
    assert p["content"].startswith("**Lacy** · 45s · 5.0 MB") and "```Lacy @everyone```" in p["content"]
    assert p["allowed_mentions"] == {"parse": []} and "embeds" not in p


def lacy_setup(tmp_path, size, **dkw):
    jobs, subs = L.Jobs(tmp_path), L.Submissions(tmp_path / "s.csv")
    job = ready_job(jobs, 1)
    job.update(category="Grand Theft Auto VI", duration=41.5)
    jobs.save(job)
    (jobs.dir(job["id"]) / "short.mp4").write_bytes(b"\x01" * size)
    dcfg = dict(D.settings({}, tmp_path), webhook_url="https://d/x", **dkw)
    return jobs, subs, job, dcfg


def test_lacy_short_is_sent_after_upload_then_deleted(tmp_path):
    jobs, subs, job, dcfg = lacy_setup(tmp_path, 3_000_000)
    fake = FakeDiscord()
    job = L.upload_job(job, L.settings({}), jobs, subs, FakeYT(), now=T,
                       send=lambda p, j: L.send_video(dcfg, p, j, post=fake))
    assert job["status"] == "uploaded" and job["delivered"] == "attached"
    (call,) = fake.calls
    assert call["file"] == b"\x01" * 3_000_000 and call["filename"] == f"lacy_{job['id']}.mp4"
    c = call["payload"]["content"]
    assert c.startswith("**Lacy** · 41.5s · YouTube ✅ <https://youtube.com/shorts/vid1>"
                        " · now post on **TikTok + Instagram**")
    cap = f"```{job['title']} #Lacy #GTA6 #GTA```"
    assert f"TikTok caption:\n{cap}" in c and f"Instagram Reels caption:\n{cap}" in c
    assert f"spikebot.lacy posted {job['id']} --tiktok TIKTOK_URL --instagram INSTAGRAM_URL" in c
    assert not (jobs.dir(job["id"]) / "short.mp4").exists()


def test_lacy_big_short_gets_a_link(tmp_path):
    FakeGCS.made.clear()
    jobs, subs, job, dcfg = lacy_setup(tmp_path, 11_000_000, gcs_bucket="b")
    fake = FakeDiscord()
    job = L.upload_job(job, L.settings({}), jobs, subs, FakeYT(), now=T,
                       send=lambda p, j: L.send_video(dcfg, p, j, post=fake, uploader_factory=FakeGCS))
    assert job["delivered"] == "link" and FakeGCS.made[0].object_name == f"videos/lacy/lacy_{job['id']}.mp4"


def test_lacy_delivery_failure_keeps_file_and_upload(tmp_path):
    jobs, subs, job, dcfg = lacy_setup(tmp_path, 1_000_000)
    job = L.upload_job(job, L.settings({}), jobs, subs, FakeYT(), now=T,
                       send=lambda p, j: L.send_video(dcfg, p, j, post=FakeDiscord([502])))
    saved = jobs.load(job["id"])
    assert saved["status"] == "uploaded" and saved["youtube_url"] and saved["delivered"] is None
    assert "lacy deliver" in saved["reason"] and (jobs.dir(job["id"]) / "short.mp4").exists()
    assert len(subs.rows()) == 1                                    # CSV logged once, upload not undone
    assert L.deliver_job(saved, jobs, lambda p, j: L.send_video(dcfg, p, j, post=FakeDiscord())) == "attached"
    assert not (jobs.dir(job["id"]) / "short.mp4").exists()
    with pytest.raises(ValueError, match="gone"):
        L.deliver_job(saved, jobs, lambda p, j: "attached")


def test_lacy_without_webhook_behaves_as_before(tmp_path):
    jobs, subs, job, _ = lacy_setup(tmp_path, 1000)
    job = L.upload_job(job, L.settings({}), jobs, subs, FakeYT(), now=T)
    assert "delivered" not in job and not (jobs.dir(job["id"]) / "short.mp4").exists()


# ---- all three platforms: TikTok / Instagram links logged next to YouTube -----------
def test_crosspost_url_check():
    assert L.crosspost_url_ok("tiktok", "https://www.tiktok.com/@gta6.clipped/video/7300000000000000000")
    assert L.crosspost_url_ok("instagram", "https://www.instagram.com/reel/C1abcDEF/")
    for p, u in (("tiktok", "https://www.instagram.com/reel/x/"), ("tiktok", "http://tiktok.com/@a/video/1"),
                 ("instagram", "https://evilinstagram.com/reel/x"), ("tiktok", "https://www.tiktok.com/"),
                 ("instagram", "")):
        assert not L.crosspost_url_ok(p, u), u


def test_old_csv_gets_new_columns_and_links(tmp_path):
    f = tmp_path / "s.csv"
    f.write_text("uploaded_uk,youtube_url,title,video_id,job_id,source_vod\n"
                 "2026-11-19 06:00:01 GMT,https://youtube.com/shorts/a,T1,a,J1,v\n")
    subs = L.Submissions(f)
    subs.append({"uploaded_uk": "2026-11-19 07:00:00 GMT", "youtube_url": "https://youtube.com/shorts/b",
                 "title": "T2", "video_id": "b", "job_id": "J2"})
    assert f.read_text().splitlines()[0] == ",".join(L.CSV_FIELDS)
    assert subs.set_links("J1", {"tiktok": "https://www.tiktok.com/@x/video/1"})
    assert not subs.set_links("nope", {"tiktok": "https://www.tiktok.com/@x/video/2"})
    rows = subs.rows()
    assert [r["job_id"] for r in rows] == ["J1", "J2"] and rows[0]["youtube_url"] == "https://youtube.com/shorts/a"
    assert rows[0]["tiktok_url"] == "https://www.tiktok.com/@x/video/1" and rows[0]["instagram_url"] == ""
    assert subs.uploads_on("2026-11-19") == 2


def test_posted_command_logs_links(tmp_path, capsys):
    import json
    jobs, subs = L.Jobs(tmp_path / "data"), L.Submissions(tmp_path / "subs.csv")
    job = L.upload_job(ready_job(jobs, 1), L.settings({}), jobs, subs, FakeYT(), now=T)
    assert L.missing_platforms(job) == ["TikTok", "Instagram"]
    cfgf = tmp_path / "config.json"
    cfgf.write_text(json.dumps({"data_dir": str(tmp_path / "data"),
                                "lacy": {"submissions_csv": str(tmp_path / "subs.csv")}}))
    run = lambda *a: L.main(["--config", str(cfgf), *a])
    assert run("posted", job["id"], "--tiktok", "https://www.instagram.com/reel/x/") == 1
    assert run("posted", job["id"], "--tiktok", "https://www.tiktok.com/@gta6.clipped/video/1") == 0
    assert "Still to post: Instagram" in capsys.readouterr().out
    assert run("posted", job["id"], "--instagram", "https://www.instagram.com/reel/Cabc/") == 0
    assert "On all 3 platforms" in capsys.readouterr().out
    row = subs.rows()[0]
    assert row["youtube_url"] and row["tiktok_url"].endswith("/video/1") and row["instagram_url"].endswith("/Cabc/")
    assert L.missing_platforms(jobs.load(job["id"])) == []
    assert run("list") == 0 and "NOT YET ON" not in capsys.readouterr().out
