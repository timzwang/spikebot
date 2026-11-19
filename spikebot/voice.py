"""AI voiceover for the teaser opening of stream clips (the experimental version C).

`speak(text, out, cfg)` writes an MP3 of the line, or returns None (the teaser then runs
with the line on screen only). Engines, in order (`clips.voice.engines`):

  edge    Microsoft Edge's neural voices through the `edge-tts` package: free, no key.
  google  Google Cloud Text-to-Speech with the VM's attached service account (metadata
          server token, no key file). Free tier: 1M Neural2 characters a month. Needs the
          Text-to-Speech API enabled on the project; otherwise it fails and is skipped.

Settings (config `clips.voice`, all optional): `engines`, `edge_voice` (default
en-US-AndrewNeural), `edge_rate` ("+8%"), `google_voice` (en-US-Neural2-J).
Never raises; never logs the text's audio or any token.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import urllib.request
from pathlib import Path

log = logging.getLogger("voice")

DEFAULTS = {"engines": ["edge", "google"], "edge_voice": "en-US-AndrewNeural", "edge_rate": "+8%",
            "google_voice": "en-US-Neural2-J", "timeout_s": 40}
METADATA_TOKEN = ("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/"
                  "default/token")
GOOGLE_TTS = "https://texttospeech.googleapis.com/v1/text:synthesize"


class VoiceError(RuntimeError):
    pass


def settings(cfg: dict | None) -> dict:
    v = ((cfg or {}).get("clips") or {}).get("voice") or {}
    return DEFAULTS | {k: val for k, val in v.items() if val is not None}


def spoken(text: str) -> str:
    """The line as a voice should read it: "GTA 6" -> "GTA six", no emojis or hashtags."""
    t = re.sub(r"#\w+", "", text or "")
    t = re.sub(r"[^\w\s.,!?'’\-:]", "", t)
    t = re.sub(r"\bGTA\s*(VI|6)\b", "GTA six", t, flags=re.I)
    t = re.sub(r"\bGTA\s*(V|5)\b", "GTA five", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def edge(text: str, out: Path, s: dict) -> Path:
    try:
        import edge_tts
    except ImportError as e:
        raise VoiceError("edge-tts is not installed") from e

    async def go():
        await edge_tts.Communicate(text, s["edge_voice"], rate=s["edge_rate"]).save(str(out))
    try:
        asyncio.run(asyncio.wait_for(go(), float(s["timeout_s"])))
    except Exception as e:  # noqa: BLE001 - network / service changes
        raise VoiceError(f"edge-tts failed: {type(e).__name__}: {str(e)[:200]}") from e
    return out


def _metadata_token(timeout: float) -> str:
    req = urllib.request.Request(METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=min(timeout, 5)) as r:   # noqa: S310 - fixed internal URL
        return json.loads(r.read())["access_token"]


def google(text: str, out: Path, s: dict) -> Path:
    try:
        token = _metadata_token(float(s["timeout_s"]))
    except Exception as e:  # noqa: BLE001 - not on a GCP VM
        raise VoiceError(f"no Google service account here ({type(e).__name__})") from e
    voice = s["google_voice"]
    body = {"input": {"text": text}, "audioConfig": {"audioEncoding": "MP3", "speakingRate": 1.08},
            "voice": {"languageCode": "-".join(voice.split("-")[:2]), "name": voice}}
    req = urllib.request.Request(GOOGLE_TTS, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=float(s["timeout_s"])) as r:   # noqa: S310
            audio = base64.b64decode(json.loads(r.read())["audioContent"])
    except Exception as e:  # noqa: BLE001 - API disabled (403), quota...
        raise VoiceError(f"Google Text-to-Speech failed: {type(e).__name__}: {str(e)[:160]}") from e
    out.write_bytes(audio)
    return out


ENGINES = {"edge": edge, "google": google}


def speak(text: str, out: Path, cfg: dict | None = None, engines: dict | None = None) -> Path | None:
    """MP3 of `text` at `out`, from the first engine that works; None if none does."""
    s = settings(cfg)
    line = spoken(text)
    if not line or (engines is None and os.environ.get("SPIKEBOT_VOICE", "").lower() == "off"):
        return None
    for name in s["engines"]:
        fn = (engines or ENGINES).get(name)
        if fn is None:
            continue
        try:
            path = fn(line, out, s)
            if path.exists() and path.stat().st_size > 1000:
                return path
            raise VoiceError("empty audio")
        except VoiceError as e:
            log.info("voiceover via %s unavailable: %s", name, e)
            out.unlink(missing_ok=True)
    return None
