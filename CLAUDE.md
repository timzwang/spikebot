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

- **Editor:** vertical 1080x1920, word-level captions, programme watermark/hashtags (Lacy mode
  already has a Lacy-only version in `editor.py`/`lacy.py`),
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
window and, if it qualifies, posted to a Discord webhook. The chat bot never downloads
video; only Lacy mode (below) cuts clip-length segments from Lacy's VODs, because his
programme allows clipping.

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
  `candidate` spikes to Discord. For candidates `vod_link()` looks up the VOD
  (`kick_stream_vod` / `TwitchHelix.stream_vod`, links only) and stores `vod_url` with a
  `?t=` offset at the clip start. Log times are UK time (`Europe/London`); stream offsets
  are `HH:MM:SS` from the stream start. Channels may set `display` for the Discord name.
  `status_loop()` (every 30s or on an event) sends #status alerts and checks disk every
  5 min; `summary_loop()` posts the daily summary. All Discord posts
  go through `post_discord()`, which takes a payload dict (or wraps plain text).
- `spikebot/lacy.py`: **Lacy mode** (off unless `LACY_MODE=true` / `lacy.enabled`). For
  `twitch/lacy` only, `Bot.handle()` re-checks the category with Helix Get Streams at the
  spike (`fresh_state`) and, for exactly Grand Theft Auto V/VI, `LacyMode.enqueue()` writes
  `data/lacy/jobs/<id>/job.json`. `LacyMode.worker()` waits `vod_delay_s`, looks up the VOD
  and runs `python -m spikebot.lacy render ID` as a subprocess (`render_job`: hard guards
  for channel/programme/category, cut segment, BRB/away/camera-off skip, English-only
  Whisper check, render, preview, unique thumbnail). A review card + preview goes to
  #moments; `python -m spikebot.lacy approve ID` runs `upload_job` (daily cap from the CSV,
  title/hashtag checks, YouTube upload, thumbnail, append to `logs/lacy_submissions.csv`).
  Titles come from `TITLES` and must pass `title_ok` (contains Lacy, no `BANNED` words,
  English). Never add a watermark/logo in Lacy mode (`build_ass(watermark=None)`).
- `spikebot/editor.py`: ffmpeg/yt-dlp helpers used only by Lacy mode: `fetch_segment` (only
  the clip range of a VOD), `layout` (facecam stacked over gameplay for the whole clip),
  `away_reason`, `build_ass` (word captions), `render`, `review_copy`, `pick_thumbnail`
  (dHash vs. `data/lacy/thumbnails.json`), `transcribe` (faster-whisper, lazy import).
- `spikebot/youtube.py`: YouTube Data API upload (resumable), `set_thumbnail`, and the
  one-time `python -m spikebot.youtube auth --env .env` that saves `YOUTUBE_REFRESH_TOKEN`.
- `tests/test_lacy.py`: Lacy config/env, GTA filter, titles, CSV + daily cap, layout,
  BRB detection, captions without watermark, end-to-end render on a synthetic stream
  (needs ffmpeg), and the category check at spike time inside `Bot`.
- `spikebot/messages.py`: pure Discord layouts (embeds) for #moments (`moment`), #status
  (`status`, colour by alert kind) and #daily-summary (`summary`). Times use Discord
  `<t:unix:style>` markup; `plain()` renders a payload as one log line in UK time. Chat
  text is markdown-escaped and `allowed_mentions` is empty so chat can't ping anyone.
- `tests/test_detector.py`: simulated-chat tests for the detector plus the Kick key parser.
- `tests/test_messages.py`: message layouts, vibe words, VOD offsets, and a `Bot.handle()`
  test that checks the VOD link in the #moments card.
- `tests/test_status.py`: alert de-duplication, disconnect/reconnect timing, live/offline,
  4001 detection, summary timing and content, plus an end-to-end `Bot` test with a fake
  Discord (it also checks nothing leaks into #moments).
- `deploy/install.sh`: one-command root installer for Ubuntu, run from a git clone
  (`setup.sh` is a shim to it). Creates the `spikebot` user, copies the code to
  `/opt/spikebot`, builds the venv, creates `config.json`/`.env` from the examples if
  missing, and installs `spikebot.service` plus `spikebot-update.timer`
  (templates in `deploy/`).
- `deploy/auto_update.sh`: run by the timer every 5 min. `git pull --ff-only` as the
  clone's owner; if HEAD differs from `/opt/spikebot/.deployed_commit` it stages the
  code, syntax-checks it, swaps it in, `pip install`s if `requirements.txt` changed and
  restarts the bot. Never touches `config.json`, `.env` or `data/`.
- `.env` / `.env.example`: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `LACY_MODE`,
  `LACY_MAX_UPLOADS_PER_DAY`, `LACY_AUTO_UPLOAD`, `YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN`. `main.apply_env()`
  overlays them on the config (environment > `.env` next to config > `.env` in cwd >
  `config.json`). Tested in `tests/test_env.py`.
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

- Deployed copy: `/opt/spikebot` (owned by `spikebot`), with its own venv, `config.json`
  and `.env`. It is **not** a git checkout. The server's git clone is auto-deployed:
  anything pushed to `main` goes live within ~5 minutes via `spikebot-update.timer`
  (log: `journalctl -u spikebot-update`). So only push to `main` when it's tested.
  Restarting sends a "restarted" alert to #status (at most one per 30 min).
- Service: `systemctl status spikebot`, logs with `journalctl -u spikebot -f`.
- Live data: `/opt/spikebot/data/`. Don't write test spikes into it.
- The live `config.json` and `.env` contain real secrets (three Discord webhooks, Twitch secret). Don't print
  them or paste them into commits, logs or external requests.

### Conventions

- Python 3.11+ syntax (`X | None`), `from __future__ import annotations`, asyncio throughout.
- Dependencies are kept minimal (`websockets`, `curl_cffi`; `yt-dlp` and `faster-whisper`
  only for Lacy mode, imported in the render subprocess); add new ones to `requirements.txt`.
- Keep `detector.py` free of I/O so it stays testable with simulated timestamps.
- Network failures should log and retry rather than crash the service (systemd restarts
  it anyway). Status/summary code must never take the bot down.
