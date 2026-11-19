"""Impact filter (Tim, 19 Nov): chat spikes that only happened because the streamer asked chat
something (or subs / raids / giveaways) are dropped before anything is downloaded or edited."""
from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import asdict, replace

import pytest

from spikebot import editor
from spikebot import impact as I
from spikebot import lacy as L
from spikebot import messages as M
from spikebot import review as R
from spikebot.detector import SpikeDetector, chat_kind
from tests.test_detector import T0, feed
from tests.test_feedback import FakeClaude
from tests.test_lacy import T, fake_fetch, job_for, lacy_spike, make_bot, make_stream

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
W = editor.Word


def say(text: str, start: float, step: float = 0.35) -> list:
    return [W(round(start + i * step, 2), round(start + i * step + 0.3, 2), w) for i, w in enumerate(text.split())]


# ---- 1. chat at the spike ------------------------------------------------------------------
def test_chat_kinds():
    assert [chat_kind(t) for t in ("1", "2", "111", "yes", "b", "!enter", "gifted 5 subs", "hi")] == \
        ["answer", "answer", "answer", "answer", "answer", "command", "event", "event"]
    assert [chat_kind(t) for t in ("L", "W", "what is he doing", "LMAO 1")] == [None] * 4
    assert chat_kind("1", hype=True) is None      # anything with a hype word is a reaction


def spike_from(texts):
    det = SpikeDetector("twitch", "lacy")
    feed(det, T0, 600, 2.0)                                   # learn normal chat
    spikes = feed(det, T0 + 600, 40, 25.0, users=3000, texts=texts)
    spikes += feed(det, T0 + 640, 200, 2.0)
    assert spikes
    return spikes[0]


def test_a_poll_spike_is_not_a_moment_but_a_hype_spike_is():
    poll = spike_from(("1", "2", "1", "11", "yes", "2"))
    assert poll.answer_share >= 0.6
    why = I.chat_verdict(asdict(poll))
    assert why and why.startswith("chat was answering the streamer")
    hype = spike_from(("KEKW", "LMAO", "NO WAY", "1", "OMEGALUL"))
    assert I.chat_verdict(asdict(hype)) is None
    entries = spike_from(("!enter", "!join", "!enter"))
    assert "giveaway" in I.chat_verdict(asdict(entries))


def test_chat_verdict_guards():
    rec = {"answer_share": 0.5, "command_share": 0.1, "event_share": 0.0, "hype_share": 0.1,
           "clip_mentions": 0, "sample": ["1", "2", "a very long message that is not an example"]}
    assert "'1', '2'" in I.chat_verdict(rec)
    assert I.chat_verdict(rec | {"hype_share": 0.4}) is None          # lots of hype: something happened
    assert I.chat_verdict(rec | {"clip_mentions": 5}) is None         # chat says "clip it"
    assert I.chat_verdict(rec | {"answer_share": 0.2}) is None
    assert I.chat_verdict({"hype_share": 0.0}) is None                # old records always pass
    assert I.chat_verdict(rec, {"impact": {"enabled": False}}) is None


def test_bot_drops_chat_prompt_spikes_before_alert_or_clip(tmp_path, monkeypatch):
    bot, posts, _ = make_bot(tmp_path, monkeypatch, "Grand Theft Auto VI")
    poll = replace(lacy_spike(), hype_share=0.05, clip_mentions=0, answer_share=0.7, sample=["1", "2"])
    asyncio.run(bot.handle(("twitch", "lacy"), poll))
    status, data = bot.store.db.execute("select status, data from spikes").fetchone()
    assert status == "chat_prompt" and "answering the streamer" in json.loads(data)["filtered_why"]
    assert bot.lacy.jobs.all() == [] and not posts                   # no clip, no #moments alert
    asyncio.run(bot.handle(("twitch", "lacy"), lacy_spike()))         # a real moment still goes through
    assert len(bot.lacy.jobs.all()) == 1
    assert bot.filtered_since(0) == {"chat": 1, "clips": 0}
    text = bot.summary_message(T + 86400)["embeds"][0]["description"]
    assert "🧹 **Filtered out**: 1 chat surge caused only by chat answering" in text


def test_summary_line_only_when_something_was_filtered():
    assert "Filtered" not in M.summary([], [("kick", "x")], T)["embeds"][0]["description"]
    d = M.summary([], [("kick", "x")], T, filtered={"chat": 0, "clips": 3})["embeds"][0]["description"]
    assert "3 clips judged low impact before editing" in d


# ---- 2. what the streamer said -----------------------------------------------------------------
QUESTION = say("okay chat type one if you think he makes it and two if not", 2.0)


def test_asking_chat_with_no_reaction_is_skipped():
    words = QUESTION + say("alright lets see what you guys said", 16.0)
    v = I.speech_verdict(words, peak=20.0, levels=[0.1] * 300)
    assert v["skip"] and "asked chat something" in v["why"] and v["asked"]
    # ...but if something happened after it (he laughs / shouts), it's a moment
    assert not I.speech_verdict(QUESTION + say("oh my god no way", 18.0), 20.0, [0.1] * 300)["skip"]
    loud = [0.1] * 300
    loud[150:165] = [0.6] * 15
    assert not I.speech_verdict(words, 20.0, loud)["skip"]
    # nothing said to chat: never skipped by this check
    assert not I.speech_verdict(say("we are driving to the airport now", 2.0), 20.0, [0.1] * 300)["skip"]


def test_thanking_subs_or_ads_is_skipped():
    words = say("thank you so much for the gifted subs appreciate it", 10.0)
    v = I.speech_verdict(words, 18.0, [0.1] * 300)
    assert v["skip"] and "thanked supporters" in v["why"]


def test_reaction_level_and_timeline():
    levels = [0.1] * 300
    levels[140:150] = [0.5] * 10
    assert I.reaction_level(levels, 20.0) >= 4 and I.reaction_level([], 20.0) == 0.0
    tl = I.timeline(QUESTION + say("ha ha", 22.0), 20.0)
    assert tl.splitlines()[0].startswith("[-18.0s] okay chat type one") and "[+2.0s] ha ha" in tl


# ---- 3. Claude's rating ------------------------------------------------------------------------
JOB = {"id": "j1", "streamer": "Lacy", "category": "Grand Theft Auto VI", "sample": ["1", "2"], "vibe": [],
       "peak_time": 1020.0, "clip_start": 1000.0}


def test_claude_rates_the_moment():
    words = say("lets do the heist", 5.0) + say("oh my god he crashed into the bank", 17.0)
    c = FakeClaude({"kind": "fail", "score": 7, "why": "crashes the getaway car into the bank"})
    v = I.judge(JOB, words, [0.1] * 300, base=None, client=c)
    assert not v["skip"] and v["score"] == 7 and v["by"] == "ai"
    sent = json.dumps(c.requests[0]["messages"])
    assert "[+-" not in sent and "oh my god he crashed" in sent and "Loudness" in sent
    low = I.judge(JOB, words, [0.1] * 300, client=FakeClaude({"kind": "just_talking", "score": 6, "why": "chat"}))
    assert low["skip"] and low["why"].startswith("Claude rated it 6/10 (just talking)")
    weak = I.judge(JOB, words, [0.1] * 300, client=FakeClaude({"kind": "funny", "score": 2, "why": "meh"}))
    assert weak["skip"]
    ok = I.judge(JOB, words, [0.1] * 300, {"impact": {"min_score": 2}},
                 client=FakeClaude({"kind": "funny", "score": 2, "why": "meh"}))
    assert not ok["skip"]


def test_no_key_or_api_failure_lets_it_through(tmp_path):
    words = say("lets do the heist", 5.0)
    assert I.ai_verdict(JOB, words, 20.0, 1.0, base=tmp_path) is None        # no key
    v = I.judge(JOB, words, [0.1] * 300, base=tmp_path,
                client=FakeClaude({"kind": "funny", "score": 9, "why": "x"}, stop="refusal"))
    assert not v["skip"] and v["by"] == "speech"
    # the free speech check runs first: a clear non-moment never costs an API call
    c = FakeClaude()
    assert I.judge(JOB, QUESTION, [0.1] * 300, client=c)["skip"] and c.requests == []


# ---- end to end: skipped before any rendering ------------------------------------------------------
@needs_ffmpeg
def test_clip_job_where_streamer_only_asked_chat_is_not_edited(tmp_path):
    src = tmp_path / "stream.mp4"
    make_stream(src, seconds=12)
    jobs, lcfg = L.Jobs(tmp_path), L.settings({})
    store = R.Store(tmp_path)
    job = job_for(tmp_path, jobs, 1)
    peak = job["peak_time"] - job["clip_start"]
    words = say("okay chat type one if you want the car and two for the bike", max(0.2, peak - 6))
    job = L.render_job(job, lcfg, jobs, set(), fetch=fake_fetch(src), transcribe=lambda p, m: (words, "en", 0.97),
                       now=T, review_store=store, cfg={"data_dir": str(tmp_path)}, base=tmp_path)
    assert job["status"] == "skipped" and job["reason"].startswith("low impact: the streamer asked chat")
    assert job["impact"]["asked"] and "review_item" not in job
    assert not (jobs.dir(job["id"]) / "segment.mp4").exists() and not store.all()
