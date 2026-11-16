"""Finished videos to Discord #ready-to-post, for every mode (Tokyo Girl, Lacy, and
the editor later). The file is never re-encoded or altered:

  - up to `discord_max_mb` it is attached to the message as-is;
  - bigger (or if Discord answers 413) it is uploaded unchanged to a private Google
    Cloud Storage bucket and the message carries a signed download link (max 7 days);
  - with no bucket configured, a card says where the file is on the server.

Each message is labelled with the mode and carries copy-ready text blocks (captions,
title...) in code blocks, so they can be long-pressed and copied on a phone.

Config (top level of config.json):
  "videos_webhook_url": "...",           # #ready-to-post (old name: tokyo_girl_webhook_url)
  "videos": {"discord_max_mb": 10, "gcs_bucket": "", "gcs_key_file": "gcs-key.json",
             "link_days": 7, "keep_days": 8}
  .env: VIDEOS_GCS_BUCKET (old name: TOKYO_GIRL_GCS_BUCKET)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import timedelta
from pathlib import Path

log = logging.getLogger("delivery")

DEFAULTS = {
    "discord_max_mb": 10,          # 10 unboosted, 50 at boost level 2, 100 at level 3
    "gcs_bucket": "",              # empty = no upload; big files get a card instead
    "gcs_key_file": "gcs-key.json",  # service account key next to config.json (secret)
    "link_days": 7,                # 7 is the maximum for a V4 signed URL
    "keep_days": 8,                # older objects are deleted from the bucket
}
# Settings first shipped inside the "tokyo_girl" block; still honoured there.
LEGACY = {"discord_max_mb": "discord_max_mb", "gcs_bucket": "gcs_bucket",
          "gcs_key_file": "gcs_key_file", "gcs_link_days": "link_days", "gcs_keep_days": "keep_days"}
MODES = {  # label, embed colour
    "tokyo_girl": ("Tokyo Girl", 0xFF4FA3),
    "lacy": ("Lacy", 0x9146FF),
}


def settings(cfg: dict, base: Path | None = None) -> dict:
    out = dict(DEFAULTS)
    for old, new in LEGACY.items():
        if (cfg.get("tokyo_girl") or {}).get(old) not in (None, ""):
            out[new] = cfg["tokyo_girl"][old]
    out.update({k: v for k, v in (cfg.get("videos") or {}).items() if k in DEFAULTS})
    for key in ("videos_gcs_bucket", "tokyo_girl_gcs_bucket"):
        if cfg.get(key) not in (None, ""):
            out["gcs_bucket"] = cfg[key]
            break
    out["gcs_bucket"] = str(out["gcs_bucket"] or "").strip().removeprefix("gs://").strip("/")
    out["webhook_url"] = cfg.get("videos_webhook_url") or cfg.get("tokyo_girl_webhook_url") or ""
    if not 1 <= int(out["link_days"]) <= 7:
        raise ValueError("videos.link_days must be 1-7 (signed URL limit)")
    if base is not None and not Path(out["gcs_key_file"]).is_absolute():
        out["gcs_key_file"] = str(base / out["gcs_key_file"])
    return out


def limit_bytes(dcfg: dict) -> int:
    """Largest file we try to attach (Discord counts MiB; leave room for the form)."""
    return int(dcfg["discord_max_mb"] * 1024 * 1024) - 64 * 1024


def mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


def message(mode: str, name: str, size: int, bits: list[str], blocks: list[tuple[str, str]],
            link: str | None = None, expires: float | None = None, too_big: bool = False,
            server_path: str | None = None) -> dict:
    label, colour = MODES.get(mode, (mode, 0x5865F2))
    content = " · ".join([f"**{label}**", *bits, mb(size)])
    for k, text in blocks:
        content += f"\n{k}:\n```{text}```"
    payload: dict = {"content": content[:1990], "allowed_mentions": {"parse": []}}
    if link:
        payload["embeds"] = [{"title": f"Download the original ({mb(size)})", "url": link,
                              "description": f"{name}\nLink works until <t:{int(expires or 0)}:f>.",
                              "color": colour}]
    elif too_big:
        payload["embeds"] = [{"title": f"Too big for Discord ({mb(size)}) and no bucket set",
                              "description": f"Copy it from the server:\n`{server_path or name}`",
                              "color": colour}]
    return payload


def post_webhook(url: str, payload: dict, path: Path | None = None, filename: str | None = None) -> int:
    """POST to a Discord webhook (multipart when a file is attached). HTTP status."""
    from curl_cffi import requests as creq
    if path is None:
        return creq.post(url, json=payload, timeout=30).status_code
    boundary = "spikebot" + os.urandom(8).hex()
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\";"
            f" filename=\"{filename or path.name}\"\r\nContent-Type: video/mp4\r\n\r\n").encode()
    body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    r = creq.post(url, data=body, timeout=300,
                  headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    if r.status_code >= 300:
        log.warning("discord upload: HTTP %s %s", r.status_code, r.text[:200])
    return r.status_code


class GCSUploader:
    """Private bucket + V4 signed GET links, with a service-account key file.
    google-cloud-storage is imported here only, never by the chat bot."""

    def __init__(self, bucket: str, key_file: str):
        from google.cloud import storage  # lazy
        self.client = storage.Client.from_service_account_json(key_file)
        self.bucket = self.client.bucket(bucket)

    def upload(self, path: Path, object_name: str, link_days: int) -> str:
        blob = self.bucket.blob(object_name)
        blob.upload_from_filename(str(path), content_type="video/mp4", timeout=900)
        name = Path(object_name).name
        return blob.generate_signed_url(version="v4", method="GET", expiration=timedelta(days=link_days),
                                        response_disposition=f'attachment; filename="{name}"')

    def cleanup(self, keep_days: int, now: float) -> int:
        """The bucket only holds our deliveries: drop anything older than keep_days."""
        n = 0
        for b in self.client.list_blobs(self.bucket):
            if b.time_created and b.time_created.timestamp() < now - keep_days * 86400:
                b.delete()
                n += 1
        return n


def send(dcfg: dict, path: Path, mode: str, bits: list[str], blocks: list[tuple[str, str]],
         name: str | None = None, now: float | None = None, post=post_webhook,
         uploader_factory=GCSUploader) -> str:
    """Send one finished video. Returns attached / link / card / off. Raises if nothing
    got through, so the caller can keep the file and retry."""
    now = now or time.time()
    url = dcfg.get("webhook_url")
    if not url:
        return "off"
    name = name or path.name
    size = path.stat().st_size
    if size <= limit_bytes(dcfg):
        code = post(url, message(mode, name, size, bits, blocks), path, name)
        if code < 300:
            return "attached"
        if code != 413:
            raise RuntimeError(f"Discord upload failed: HTTP {code}")
        log.info("%s: Discord says too large (%s), using a link instead", name, mb(size))
    if dcfg["gcs_bucket"]:
        up = uploader_factory(dcfg["gcs_bucket"], dcfg["gcs_key_file"])
        link = up.upload(path, f"videos/{mode}/{name}", int(dcfg["link_days"]))
        try:
            up.cleanup(int(dcfg["keep_days"]), now)
        except Exception as e:  # housekeeping only
            log.warning("bucket cleanup failed: %s", e)
        payload = message(mode, name, size, bits, blocks, link, now + int(dcfg["link_days"]) * 86400)
        how = "link"
    else:
        payload = message(mode, name, size, bits, blocks, too_big=True, server_path=str(path.resolve()))
        how = "card"
    code = post(url, payload, None, None)
    if code >= 300:
        raise RuntimeError(f"Discord post failed: HTTP {code}")
    return how
