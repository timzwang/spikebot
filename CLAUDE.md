# CLAUDE.md

Spike bot for the GTA 6 Clipped YouTube channel. It reads live public chat on Kick and
Twitch, learns each channel's normal chat rate, and flags moments when chat suddenly
surges (laughing, "NO WAY", "clip it"). Each moment is saved with a suggested VOD clip
window and, if it qualifies, posted to a Discord webhook. It never downloads video.

`README.md` is the user-facing guide (install, config fields, useful commands). Keep
it in sync when behaviour or config changes.

## Layout

- `spikebot/detector.py`: pure detection logic, no networking. `SpikeDetector.add_message()`
  takes each chat line; `tick(now)` runs once a second and returns a `Spike` when one
  ends. The baseline is an exponentially weighted mean/variance (half-life
  `baseline_halflife_s`). A spike opens when rate >= max(`min_rate`, mean × `ratio`)
  **and** z >= `z`, after `warmup_s`, outside `cooldown_s`. The score combines
  log2(peak/baseline), √peak rate, hype-emote share, "clip" mentions and the
  unique-user ratio, so single-user spam scores low.
- `spikebot/sources.py`: chat and stream-state sources.
  - Kick: an unofficial Pusher websocket. The key/cluster are scraped fresh on every
    connect (`kick_pusher_credentials`); config values are only a fallback. Channel info
    comes from `kick.com/api/v2/channels/<slug>` via `curl_cffi` (gets past Cloudflare).
    `chatroom_id` can be set in config if the lookup is blocked.
  - Twitch: anonymous IRC for chat; Helix (`TwitchHelix`) for live/category only when
    `twitch_client_id`/`secret` are set, otherwise Twitch moments are never
    category-filtered.
- `spikebot/main.py`: `Bot` wires it together. `ticker()` drives the detectors,
  `poll_state()` refreshes live/category/start time every 120s, `handle()` sets the
  status (`candidate` / `low_score` / `filtered_category`) and saves *every* spike
  through `Store` (`data/spikes.jsonl` + `data/spikes.db` SQLite). `alert()` posts only
  `candidate` spikes to Discord as plain `content` text (no embeds, max 1900 chars).
  Times are shown in UK time (`Europe/London`); stream offsets are `HH:MM:SS` from the
  stream start.
- `tests/test_detector.py`: simulated-chat tests for the detector plus the Kick key parser.
- `setup.sh`: one-shot root installer for Ubuntu. Creates the `spikebot` system user,
  copies the code to `/opt/spikebot`, builds a venv, and installs `spikebot.service`.
- `config.example.json`: config template. A real `config.json` holds the Discord webhook
  and Twitch secret, so it is gitignored. Never commit it.

## Commands

```bash
python -m spikebot.main --config config.json           # run forever
python -m spikebot.main --config config.json --check   # 60s connection test; also posts a test message to Discord
python -m pytest -q                                    # tests (pytest isn't in requirements.txt; install it first)
```

## Deployment (this server)

- Deployed copy: `/opt/spikebot` (owned by `spikebot`), with its own venv and
  `config.json`. It is **not** a git checkout. Changes in this repo go live only once
  they are copied over (`setup.sh` does that; it keeps an existing `config.json`), then
  `sudo systemctl restart spikebot`.
- Service: `systemctl status spikebot`, logs with `journalctl -u spikebot -f`.
- Live data: `/opt/spikebot/data/`. Don't write test spikes into it.
- The live config contains real secrets (Discord webhook, Twitch secret). Don't print
  them or paste them into commits, logs or external requests.

## Conventions

- Python 3.11+ syntax (`X | None`), `from __future__ import annotations`, asyncio throughout.
- Dependencies are kept minimal (`websockets`, `curl_cffi`); add new ones to `requirements.txt`.
- Keep `detector.py` free of I/O so it stays testable with simulated timestamps.
- Network failures should log and retry rather than crash the service (systemd restarts
  it anyway).
- Channels can be posted only with a clipping programme. `programme: null` means no
  permission, and the alert warns about that.
