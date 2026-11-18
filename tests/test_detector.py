"""Simulated-chat tests for the spike detector. Run: python -m pytest -q"""
import random

from spikebot.detector import SpikeDetector, normalise
from spikebot.sources import _parse_privmsg

T0 = 1_800_000_000.0


def feed(det, start, seconds, rate, users=200, texts=("hello", "gg", "what is he doing")):
    """Poisson-ish chat at `rate` msgs/sec; returns spikes emitted."""
    rng = random.Random(int(start))
    spikes = []
    for s in range(seconds):
        t = start + s
        for _ in range(rng.choices([0, 1, 2, 3], weights=[1, 1, 1, 1])[0] * 0 + _poisson(rng, rate)):
            det.add_message(t + rng.random(), f"u{rng.randrange(users)}", rng.choice(texts))
        sp = det.tick(t + 1)
        if sp:
            spikes.append(sp)
    return spikes


def _poisson(rng, lam):
    # Knuth; fine for small lambdas
    import math
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def test_steady_chat_no_false_alarms():
    det = SpikeDetector("kick", "x")
    assert feed(det, T0, 1800, 3.0) == []


def test_burst_detected_with_sensible_window():
    det = SpikeDetector("kick", "x")
    assert feed(det, T0, 600, 2.0) == []
    burst_start = T0 + 600
    hype = ("KEKW", "NO WAY", "clip it", "💀💀💀", "[emote:1:OMEGALUL]", "LMAOOO")
    spikes = feed(det, burst_start, 25, 15.0, users=2000, texts=hype)
    spikes += feed(det, burst_start + 25, 120, 2.0)
    assert len(spikes) == 1
    sp = spikes[0]
    assert abs(sp.onset - burst_start) < 10
    assert sp.clip_start < burst_start - 15          # starts before the moment
    assert sp.clip_end > burst_start + 10
    assert sp.clip_end - sp.clip_start <= 75
    assert sp.hype_share > 0.6 and sp.clip_mentions > 0
    assert sp.score > 3


def test_single_user_spam_scores_low():
    det = SpikeDetector("kick", "x")
    feed(det, T0, 600, 2.0)
    spam = feed(det, T0 + 600, 25, 15.0, users=1, texts=("buy my stuff",))
    spam += feed(det, T0 + 625, 120, 2.0)
    real_det = SpikeDetector("kick", "y")
    feed(real_det, T0, 600, 2.0)
    real = feed(real_det, T0 + 600, 25, 15.0, users=2000, texts=("KEKW", "NO WAY"))
    real += feed(real_det, T0 + 625, 120, 2.0)
    assert spam and real
    assert spam[0].score < real[0].score / 3


def test_cooldown_merges_back_to_back_bursts():
    det = SpikeDetector("kick", "x")
    feed(det, T0, 600, 2.0)
    spikes = []
    t = T0 + 600
    for _ in range(3):  # three bursts 30s apart
        spikes += feed(det, t, 12, 15.0, users=2000, texts=("KEKW",))
        spikes += feed(det, t + 12, 18, 2.0)
        t += 30
    assert 1 <= len(spikes) <= 2


def test_busy_channel_needs_relative_jump():
    det = SpikeDetector("twitch", "big")
    feed(det, T0, 900, 40.0, users=50000)
    # +30% is normal fluctuation for a big chat, not a moment
    assert feed(det, T0 + 900, 30, 52.0, users=50000) == []
    spikes = feed(det, T0 + 930, 20, 140.0, users=50000, texts=("W", "NO WAY"))
    spikes += feed(det, T0 + 950, 120, 40.0, users=50000)
    assert len(spikes) == 1


def test_kick_key_parsing():
    from spikebot.sources import _find_pusher_creds
    assert _find_pusher_creds({"app_key": "34bf7a0ff419a2a775b9", "cluster": "us2"}) == ("34bf7a0ff419a2a775b9", "us2")
    nested = {"data": {"provider": "pusher", "pusher": {"key": "34bf7a0ff419a2a775b9", "cluster": "us2"}}}
    assert _find_pusher_creds(nested) == ("34bf7a0ff419a2a775b9", "us2")
    assert _find_pusher_creds({"data": {"url": "wss://x", "token": "abc"}}) is None
    assert _find_pusher_creds({"app_key": "bad key!", "cluster": "us2"}) is None


def test_helpers():
    assert normalise("[emote:2506823:KEKW] lol") == "KEKW  lol"
    line = "@badge-info=;color=#FF0000 :bob!bob@bob.tmi.twitch.tv PRIVMSG #kaicenat :NO WAY :)"
    assert _parse_privmsg(line) == ("bob", "NO WAY :)")
    assert _parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome") is None


def test_kick_key_found_in_kicks_javascript():
    from spikebot import sources as S
    js = 'x=1;const p=new Pusher("32cbd69e4b950bf97679",{cluster:"us2"});q=new Pusher("abcdef0123456789abcd", {wsHost:"ws", cluster:"us2", forceTLS:!0})'
    assert S.pusher_from_js(js) == ("abcdef0123456789abcd", "us2")   # the retired key is skipped
    assert S.pusher_from_js('new Pusher("abcdef0123456789abcd", {wsHost:"ws", cluster:"us2"})') == ("abcdef0123456789abcd", "us2")
    assert S.pusher_from_js('NEXT_PUBLIC_PUSHER_APP_KEY:"0123456789abcdef0123",NEXT_PUBLIC_PUSHER_APP_CLUSTER:"eu1"') == \
        ("0123456789abcdef0123", "eu1")
    assert S.pusher_from_js("<html>nothing here</html>") is None
