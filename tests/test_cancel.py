"""/cancel: run ids, cooperative cancel (SIGTERM -> Cancelled), forced stop, clean-up of
review items, and the Discord flows (Cancel button, /cancel list / one / all / queued)."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from spikebot import discord_bot as DB
from spikebot import progress as P
from spikebot import review as R

from .test_review import FakeAdapter, FakeInteraction, bot_for, new_item


# ---- progress primitives ------------------------------------------------------------------
def test_cancelled_job_raises_on_update_and_finishes_cancelled(tmp_path):
    job = P.Job.create(tmp_path, "Trend edit")
    rid = P.short(job.data["id"])
    assert len(rid) == 6 and f"run `{rid}`" in P.text(job.data, time.time())
    path, d = P.resolve(tmp_path, rid, time.time())
    assert path == job.path
    assert P.request_cancel(job.path, "Tim") and not P.request_cancel(job.path, "Tim")   # once only
    with pytest.raises(P.Cancelled):
        job.update(0.5, "rendering")
    assert not isinstance(P.Cancelled(), Exception)        # `except Exception` handlers don't swallow it
    job.finish(False, "Error: killed")                     # whoever finishes it, it ends as cancelled
    d = json.loads(job.path.read_text())
    assert d["state"] == "cancelled" and d["detail"] == "cancelled by Tim"
    assert "🛑 Stopped" in P.text(d, time.time())
    with pytest.raises(ValueError, match="already been cancelled"):
        P.resolve(tmp_path, rid, time.time())


def test_resolve_errors(tmp_path):
    with pytest.raises(ValueError, match="no run `zzz`"):
        P.resolve(tmp_path, "zzz", time.time())
    with pytest.raises(ValueError, match="give a run id"):
        P.resolve(tmp_path, "", time.time())
    job = P.Job.create(tmp_path, "Done one")
    job.finish(True)
    with pytest.raises(ValueError, match="already finished"):
        P.resolve(tmp_path, P.short(job.data["id"]), time.time())


def test_never_signals_the_bot_itself(tmp_path):
    job = P.Job.create(tmp_path, "Studying x")          # written by this process: pid is ours
    assert job.data["pid"] == os.getpid()
    assert P.request_cancel(job.path, "Tim")            # only marked; the worker sees it on its next update
    assert not P.force_stop(job.path)                   # and is never killed from here
    assert P.state_of(json.loads(job.path.read_text()), time.time()) == "running"


def test_prune_drops_old_markers(tmp_path):
    job = P.Job.create(tmp_path, "x", now=1000.0)
    P.request_cancel(job.path, "Tim")
    old = time.time() - 2 * 86400
    os.utime(job.path, (old, old))
    os.utime(P.cancel_path(job.path), (old, old))
    P.prune(tmp_path, time.time())
    assert not job.path.exists() and not P.cancel_path(job.path).exists()


# ---- real worker processes -----------------------------------------------------------------
WORKER = textwrap.dedent("""
    import subprocess, sys, time
    from spikebot import progress as P
    mode = sys.argv[1]
    if mode == "ignore":
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)     # stuck in C code, as far as signals go
    else:
        P.arm_cancel()
    job = P.Job.from_env()
    with P.tracking(job):
        try:
            for i in range(2):                            # a batch: the first edit gets cancelled
                P.current().update(0.1 * (i + 1), f"edit {i}")
                child = subprocess.Popen(["sleep", "60"])
                open(sys.argv[2] + f".child{i}", "w").write(str(child.pid))
                try:
                    child.wait()
                except BaseException:
                    child.kill()
                    raise
        except P.Cancelled:
            open(sys.argv[2] + ".cancelled", "w").write("1")
            job.finish(False)
""")


def start_worker(tmp_path, mode):
    job = P.Job.create(tmp_path / "data", "Worker test", channel="custom", channel_id=100)
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, **{P.ENV: str(job.path)}, PYTHONPATH=str(root))
    p = subprocess.Popen([sys.executable, str(script), mode, str(tmp_path / "out")], env=env,
                         start_new_session=True)
    job.adopt(p.pid)
    deadline = time.time() + 20
    while not (tmp_path / "out.child0").exists():
        assert time.time() < deadline and p.poll() is None, "worker did not start"
        time.sleep(0.05)
    return job, p


def test_cancel_stops_a_worker_and_its_ffmpeg(tmp_path):
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    job, p = start_worker(tmp_path, "arm")
    child = int((tmp_path / "out.child0").read_text())
    rid = P.short(job.data["id"])
    t0 = time.time()
    msg = asyncio.run(b.cancel_run(rid, "Tim", grace=10, poll=0.1))
    assert p.wait(10) == 0 and time.time() - t0 < 8
    assert msg.startswith("🛑 Stopped **Worker test**") and rid in msg
    assert (tmp_path / "out.cancelled").exists()
    assert not (tmp_path / "out.child1").exists()          # nothing after the cancelled step ran
    assert not P.alive(child) or open(f"/proc/{child}/stat").read().split()[2] == "Z"
    d = json.loads(job.path.read_text())
    assert d["state"] == "cancelled" and "Tim" in d["detail"]


def test_cancel_force_stops_a_worker_that_ignores_sigterm(tmp_path):
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    job, p = start_worker(tmp_path, "ignore")
    child = int((tmp_path / "out.child0").read_text())
    msg = asyncio.run(b.cancel_run(P.short(job.data["id"]), "Tim", grace=0.6, poll=0.2))
    assert "Force-stopped" in msg
    assert p.wait(10) == -signal.SIGKILL and p.pid in b.force_stopped
    deadline = time.time() + 5
    while P.alive(child) and time.time() < deadline:      # the whole process group went (ffmpeg too)
        try:
            if open(f"/proc/{child}/stat").read().split()[2] == "Z":
                break
        except OSError:
            break
        time.sleep(0.05)
    d = json.loads(job.path.read_text())
    d["cancel"] = True
    assert P.state_of(d, time.time()) == "cancelled"


# ---- review items ------------------------------------------------------------------------
def test_cancel_render_queued_request_and_first_render(tmp_path):
    store, item = new_item(tmp_path)
    with store.edit(item["id"]) as it:
        R.request(it, "redo")
    (store.dir(item["id"]) / "r2_A.mp4").write_bytes(b"half")      # half-written next round
    it = R.cancel_render(store, item["id"])
    assert it["request"]["status"] == "cancelled" and it["status"] == "open"
    assert not (store.dir(item["id"]) / "r2_A.mp4").exists()
    assert (store.dir(item["id"]) / "r1_A.mp4").exists()            # finished rounds stay
    fresh = store.create("lacy", {}, 1000.0, title="Lacy new")
    assert R.cancel_render(store, fresh["id"])["status"] == "cancelled"


def test_render_round_cancelled_midway(tmp_path):
    class Stops(FakeAdapter):
        def render(self, item, specs, n, folder, preview_bytes):
            (folder / f"r{n}_A.mp4").write_bytes(b"half")
            job = P.current()
            P.request_cancel(job.path, "Tim")
            job._checked = 0
            job.update(0.5)                                         # -> Cancelled
            raise AssertionError("not reached")
    store = R.Store(tmp_path / "data")
    item = store.create("tokyo_girl", {}, 1000.0, title="Tokyo Girl edit")
    it = R.render_round(store, item["id"], {}, tmp_path, {"kind": "initial"}, now=1000.0, adapter=Stops())
    assert it["status"] == "cancelled" and not list(store.dir(item["id"]).glob("r1_*"))
    (_, d), = P.load_all(store.data_dir)
    assert d["state"] == "cancelled" and d["meta"] == {"item": item["id"]}


# ---- Discord ---------------------------------------------------------------------------
def test_progress_bar_has_cancel_button_until_it_ends(tmp_path):
    store = R.Store(tmp_path / "data")
    b, reg = bot_for(tmp_path, store)
    job = P.Job.create(store.data_dir, "Trend edit", now=1000.0)
    job.data["started"] = 1000.0
    job.write(force=True)

    async def go():
        await b.progress_tick(1100.0)
        msg = reg[100].sent[-1]
        view = msg.kw["view"]
        assert [c.custom_id for c in view.children] == [DB.cid("cancel", job.data["id"])]
        P.request_cancel(job.path, "Tim")
        job.finish(False)
        await b.progress_tick(1101.0)
        assert msg.edits[-1]["view"] is None and "🛑 Stopped" in msg.content
        await b.progress_tick(1101.0 + 121)                    # shown 2 min, then removed
        assert getattr(msg, "deleted", False) and not P.load_all(store.data_dir)
    asyncio.run(go())


def test_cancel_command_lists_and_cancels_queued(tmp_path):
    store, item = new_item(tmp_path)
    b, reg = bot_for(tmp_path, store)
    inter = FakeInteraction()
    asyncio.run(b.cancel_command(inter, None))
    assert inter.response.sent[-1][0] == "Nothing is running right now."

    with store.edit(item["id"]) as it:
        R.request(it, "redo")
    job = P.Job.create(store.data_dir, "Studying `ref`", icon="🔎")
    rows = b.runs(time.time())
    assert [r[0] for r in rows] == [P.short(job.data["id"]), DB.QUEUED + item["id"]]
    inter = FakeInteraction()
    asyncio.run(b.cancel_command(inter, None))
    text = inter.response.sent[-1][0]
    assert "Studying `ref`" in text and "redo (queued)" in text and inter.response.sent[-1][1]["ephemeral"]

    async def go():
        i2 = FakeInteraction()
        await b.cancel_command(i2, DB.QUEUED + item["id"])
        for _ in range(20):
            await asyncio.sleep(0.01)
        return i2
    i2 = asyncio.run(go())
    assert "Cancelled the queued re-render" in i2.followup.sent[-1]
    assert store.load(item["id"])["request"]["status"] == "cancelled"
    assert len(b.runs(time.time())) == 1


def test_cancel_unknown_id_and_permissions(tmp_path):
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    inter = FakeInteraction()
    asyncio.run(b.cancel_command(inter, "abc123"))
    assert "no run `abc123`" in inter.response.sent[-1][0]
    stranger = FakeInteraction(uid=7)
    job = P.Job.create(store.data_dir, "x")
    asyncio.run(b.on_button(stranger, DB.parse_cid(DB.cid("cancel", job.data["id"]))))
    assert "Only Tim" in stranger.response.sent[-1][0] and not P.cancel_path(job.path).exists()


def test_cancel_all_and_button(tmp_path):
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)
    jobs = [P.Job.create(store.data_dir, f"job {i}") for i in range(2)]

    async def finish_soon():
        await asyncio.sleep(0.05)
        for j in jobs:                    # the workers notice and finish
            j.finish(False)

    async def go():
        i1 = FakeInteraction()
        t = asyncio.create_task(finish_soon())
        orig = b.cancel_run
        b.cancel_run = lambda ref, by: orig(ref, by, grace=2, poll=0.02)
        await b.cancel_command(i1, "all")
        for _ in range(100):
            await asyncio.sleep(0.02)
            if i1.followup.sent:
                break
        await t
        return i1
    i1 = asyncio.run(go())
    assert i1.response.sent[-1][0] == "🛑 Cancelling 2 runs…"
    assert i1.followup.sent[-1].count("🛑 Stopped") == 2
    assert all(json.loads(j.path.read_text())["state"] == "cancelled" for j in jobs)

    j3 = P.Job.create(store.data_dir, "via button")
    b3 = FakeInteraction()

    async def press():
        await b.on_button(b3, DB.parse_cid(DB.cid("cancel", j3.data["id"])))
        await asyncio.sleep(0.05)          # the cancel runs in the background after the reply
    asyncio.run(press())
    assert P.cancel_path(j3.path).exists() and "Cancelling 1 run" in b3.response.sent[-1][0]


# ---- rounded progress bar, time left, 2 s refresh ------------------------------------------
def test_rounded_bar_pieces_and_levels():
    import struct
    import zlib
    pieces = P.bar_pieces()
    assert len(pieces) == 9 and all(n.startswith(P.EMOJI_PREFIX) and len(n) <= 32 for n in pieces)
    png = pieces[P.EMOJI_PREFIX + "l2"]
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (64, 64)
    raw = zlib.decompress(png[png.index(b"IDAT") + 4:-16])
    assert len(raw) == 64 * (1 + 64 * 4)
    em = {n: f"<{n}>" for n in pieces}
    half = P.emoji_bar(0.5, em)
    assert half.count("<") == P.SEGMENTS and half.startswith(f"<{P.EMOJI_PREFIX}l2>")
    assert half.endswith(f"<{P.EMOJI_PREFIX}r0>") and P.emoji_bar(1.0, em).endswith(f"<{P.EMOJI_PREFIX}r2>")
    assert f"<{P.EMOJI_PREFIX}m1>" in P.emoji_bar(0.46, em)            # half pieces: 24 steps
    assert P.emoji_bar(0.5, {}) is None
    assert P.bar(0.5, 16) == "`" + "█" * 8 + "░" * 8 + "`" and "▌" in P.bar(0.53, 16)


def test_time_left_and_message_layout():
    d = {"id": "1-abc123", "title": "Lacy clip", "state": "running", "frac": 0.25, "stage": "Rendering",
         "started": 1000.0, "updated": 1060.0}
    assert P.eta(d, 1060.0) == 180.0
    assert P.eta(dict(d, frac=0.02), 1060.0) is None and P.eta(d, 1004.0) is None
    t = P.text(d, 1060.0, lambda f: "[BAR]")
    assert t.splitlines()[1] == "[BAR] **25%**" and "about 3:00 left" in t and "run `abc123`" in t
    assert "estimating time left" in P.text(dict(d, frac=0.01), 1060.0)
    assert P.text(d, 1060.0, lambda f: None).splitlines()[1].startswith("`")      # text bar fallback


def test_bar_emojis_are_uploaded_once(tmp_path):
    store = R.Store(tmp_path / "data")
    b, _ = bot_for(tmp_path, store)

    class E:
        def __init__(self, name):
            self.name = name

        def __str__(self):
            return f"<:{self.name}:1>"

    class Client:
        made = []

        async def fetch_application_emojis(self):
            return [E(P.EMOJI_PREFIX + "l0"), E("other")]

        async def create_application_emoji(self, name, image):
            Client.made.append(name)
            return E(name)
    b.client = Client()
    asyncio.run(b.ensure_bar_emojis())
    assert len(Client.made) == 8 and P.EMOJI_PREFIX + "l0" not in Client.made
    assert len(b.bar_emojis) == 9 and b.render_bar(0.5).count("<:") == P.SEGMENTS

    class Broken:
        async def fetch_application_emojis(self):
            raise RuntimeError("no access")
    b2, _ = bot_for(tmp_path, store)
    b2.client = Broken()
    asyncio.run(b2.ensure_bar_emojis())                             # never raises: text bar instead
    assert b2.render_bar(0.5) is None


def test_bars_refresh_every_2s_and_slow_down_when_crowded(tmp_path):
    store = R.Store(tmp_path / "data")
    b, reg = bot_for(tmp_path, store)
    job = P.Job.create(store.data_dir, "Trend edit", now=1000.0)
    job.data["started"] = 1000.0

    async def go():
        job.update(0.2)
        job.write(force=True)
        await b.progress_tick(1010.0)
        msg = reg[100].sent[-1]
        job.update(0.3)
        job.write(force=True)
        await b.progress_tick(1011.0)                               # 1 s later: not yet
        assert not msg.edits
        await b.progress_tick(1012.0)                               # 2 s: edited
        assert len(msg.edits) == 1
        others = [P.Job.create(store.data_dir, f"other {i}", now=1000.0) for i in range(3)]
        for o in others:
            o.data["started"] = 1000.0
            o.write(force=True)
        job.update(0.4)
        job.write(force=True)
        await b.progress_tick(1014.0)                               # 4 bars in the channel: every 4 s
        assert len(msg.edits) == 1
        await b.progress_tick(1016.0)
        assert len(msg.edits) == 2
    asyncio.run(go())
