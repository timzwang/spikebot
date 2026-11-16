# CLAUDE.md

## Project

Automated GTA 6 Shorts channel (gta-6-clipped-yt). Tim runs everything. The goal is
revenue on a budget of about £20/month. Give all times in UK time.

## Hard rules

- Main income is clipping programmes: Adin Ross, Seal (via Content Rewards) and Lacy
  (via Reach). Channels with `programme: null` need Tim to check permission before
  anything is posted.
- Never write code that downloads video from streamers who haven't allowed clipping.
- GTA gameplay only. Skip gambling/casino segments.
- Rockstar policy: no isolated cutscenes, endings, story spoilers or leaked footage.
- Every Short must add original value (captions, hook, context). Never raw re-uploads,
  so it stays within YouTube's reused content rules.
- Never commit or print secrets (Discord webhook, Twitch secret, Google OAuth JSON).

## Task tracker

GitHub Issues on `timzwang/spikebot` is the task list (labels `pre-launch`,
`launch-week`, `automate`, `deadlines`, `owner:tim`, `owner:claude-code`; one milestone
per phase).

- At the start of a job, check the open issues: `gh issue list`.
- Reference the issue in every commit message (e.g. `Build editor captions (#15)`).
- Close it with `Closes #N` in the commit only when the work is done and tested.

## Still to build

- **Editor:** vertical 1080x1920, word-level captions, programme watermark/hashtags,
  optional AI voiceover hook.
- **Review queue:** Tim approves each clip.
- **Uploader:** YouTube, plus the same edit to TikTok and Reels.

## Deadlines

- GTA 6 launches Thu 19 Nov 2026; big streams from about 05:00 UK.
- YouTube Partner Program Shorts threshold doubles to 20M views/90 days from 1 Feb 2027.
- Google Cloud $300 trial ends about 14 Feb 2027: downsize the VM or move before then.

## Known issues

- The Kick key auto-lookup returns HTTP 400, so the fallback key from config is used.
  If the logs show `4001` errors, the key has rotated (see README for how to find the
  new one).

## The spike bot (this repo)

Spike bot for the GTA 6 Clipped YouTube channel. It reads live public chat on Kick and
Twitch, learns each channel's normal chat rate, and flags moments when chat suddenly
surges (laughing, "NO WAY", "clip it"). Each moment is saved with a suggested VOD clip
window and, if it qualifies, posted to a Discord webhook. It never downloads video.

It posts to three Discord webhooks, which must stay separate:

| Config field | Channel | What goes there |
|---|---|---|
| `discord_webhook_url` | #moments | Clip moments only (`candidate` spikes) |
| `status_webhook_url` | #status | Bot health: start/restart, streamer live/offline (with category), chat down >5 min and reconnected, Kick 4001 key error, disk <20% free |
| `summary_webhook_url` | #daily-summary | 22:00 UK daily summary: moments per streamer + top 3 by score (last 24h) |

An empty field disables that channel; the message is still logged.

`README.md` is the user-facing guide (install, config fields, useful commands). Keep
it in sync when behaviour or config changes.

### Layout

- `spikebot/detector.py`: pure detection logic, no networking. `SpikeDetector.add_message()`
  takes each chat line; `tick(now)` runs once a second and returns a `Spike` when one
  ends. The baseline is an exponentially weighted mean/variance (half-life
  `baseline_halflife_s`). A spike opens when rate >= max(`min_rate`, mean × `ratio`)
  **and** z >= `z`, after `warmup_s`, outside `cooldown_s`. The score combines
  log2(peak/baseline), √peak rate, hype-emote share, "clip" mentions and the
  unique-user ratio, so single-user spam scores low.
- `spikebot/sources.py`: chat and stream-state sources.
  - Kick: an unofficial Pusher websocket. The key/cluster are scraped fresh on every
    connect (`kick_pusher_credentials`), falling back to the config values (currently always; see Known issues). Channel info
    comes from `kick.com/api/v2/channels/<slug>` via `curl_cffi` (gets past Cloudflare).
    `chatroom_id` can be set in config if the lookup is blocked.
  - Twitch: anonymous IRC for chat; Helix (`TwitchHelix`) for live/category only when
    `twitch_client_id`/`secret` are set, otherwise Twitch moments are never
    category-filtered **and Twitch live/offline status alerts don't fire**.
  - Both readers take an `on_status(event, detail)` callback: `connected`,
    `disconnected`, or `key_error` (Kick 4001, from a `pusher:error` event or a 4001
    websocket close code).
- `spikebot/status.py`: pure logic for #status and #daily-summary, with explicit
  timestamps so it is testable. `Notifier` drops a repeat of the same alert key
  (e.g. `live:kick/adinross`, `disk`, `start`) within 30 min. `StatusState` persists
  that history, `last_seen` and `last_summary_date` to `data/status_state.json`, so a
  crash loop can't spam and a restart is reported as "restarted, last seen X".
  `ConnectionWatch` counts a channel as down from its first disconnect (or from startup
  if it never connected) and alerts after 5 min, then again on reconnect.
  `LiveWatch` turns polls into live/offline events (offline at startup is silent).
  `next_summary_time` handles GMT/BST; a summary missed while the bot was down is
  sent on start if it's still before midnight UK.
- `spikebot/main.py`: `Bot` wires it together. `ticker()` drives the detectors,
  `poll_state()` refreshes live/category/start time every 120s, `handle()` sets the
  status (`candidate` / `low_score` / `filtered_category`) and saves *every* spike
  through `Store` (`data/spikes.jsonl` + `data/spikes.db` SQLite). `alert()` posts only
  `candidate` spikes to Discord as plain `content` text (no embeds, max 1900 chars).
  Times are shown in UK time (`Europe/London`); stream offsets are `HH:MM:SS` from the
  stream start. `status_loop()` (every 30s or on an event) sends #status alerts and
  checks disk every 5 min; `summary_loop()` posts the daily summary. All Discord posts
  go through `post_discord()`.
- `tests/test_detector.py`: simulated-chat tests for the detector plus the Kick key parser.
- `tests/test_status.py`: alert de-duplication, disconnect/reconnect timing, live/offline,
  4001 detection, summary timing and content, plus an end-to-end `Bot` test with a fake
  Discord (it also checks nothing leaks into #moments).
- `setup.sh`: one-shot root installer for Ubuntu. Creates the `spikebot` system user,
  copies the code to `/opt/spikebot`, builds a venv, and installs `spikebot.service`.
- `config.example.json`: config template. A real `config.json` holds the Discord webhook
  and Twitch secret, so it is gitignored. Never commit it.

### Commands

```bash
python -m spikebot.main --config config.json           # run forever
python -m spikebot.main --config config.json --check   # 60s connection test: message counts, disk, summary preview,
                                                       # and a test message to each configured webhook
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q                          # tests (.venv is gitignored)
```

### Deployment (this server)

- Deployed copy: `/opt/spikebot` (owned by `spikebot`), with its own venv and
  `config.json`. It is **not** a git checkout. Changes in this repo go live only once
  they are copied over (`setup.sh` does that; it keeps an existing `config.json`), then
  `sudo systemctl restart spikebot`. For a code-only update:
  `sudo cp spikebot/*.py /opt/spikebot/spikebot/ && sudo chown -R spikebot:spikebot /opt/spikebot/spikebot`.
  Restarting sends a "restarted" alert to #status (at most one per 30 min).
- Service: `systemctl status spikebot`, logs with `journalctl -u spikebot -f`.
- Live data: `/opt/spikebot/data/`. Don't write test spikes into it.
- The live config contains real secrets (three Discord webhooks, Twitch secret). Don't print
  them or paste them into commits, logs or external requests.

### Conventions

- Python 3.11+ syntax (`X | None`), `from __future__ import annotations`, asyncio throughout.
- Dependencies are kept minimal (`websockets`, `curl_cffi`); add new ones to `requirements.txt`.
- Keep `detector.py` free of I/O so it stays testable with simulated timestamps.
- Network failures should log and retry rather than crash the service (systemd restarts
  it anyway). Status/summary code must never take the bot down.
