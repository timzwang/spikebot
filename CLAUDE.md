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
- Never commit or print secrets (Discord webhook, Discord bot token, Twitch secret, Google
  OAuth JSON, `gcs-key.json`, Instagram access token, Anthropic API key).
- Nothing is published to YouTube/Instagram/TikTok without Tim's Approve in Discord.
- #inspiration downloads other creators' Shorts **only to measure style**: delete the file
  right after analysis, never reuse their footage, audio or wording.

## Task tracker

GitHub Issues on `timzwang/spikebot` is the task list (labels `pre-launch`,
`launch-week`, `automate`, `deadlines`, `owner:tim`, `owner:claude-code`; one milestone
per phase).

- At the start of a job, check the open issues: `gh issue list`.
- Reference the issue in every commit message (e.g. `Build editor captions (#15)`).
- Close it with `Closes #N` in the commit only when the work is done and tested.

## Still to build

- **Editor:** vertical 1080x1920, word-level captions, programme watermark/hashtags (Lacy mode
  already has a Lacy-only version in `editor.py`/`lacy.py`; captions follow `text_level`:
  `minimal` = 2.5 s hook + captions only around the payoff, `none` = no text),
  optional AI voiceover hook.
- **Stream-clip editor** for the other programme channels: plug it into the review queue
  with a `ReviewAdapter` (see `review.adapter_for`), like Lacy and Tokyo Girl.
- (Done: review queue + Discord approval bot, uploader with windows/caps, #inspiration presets.)

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

It posts to four Discord webhooks, which must stay separate:

| Config field | Channel | What goes there |
|---|---|---|
| `discord_webhook_url` | #moments | Clip moments only (`candidate` spikes) |
| `status_webhook_url` | #status | Bot health: start/restart, streamer live/offline (with category), chat down >5 min and reconnected, Kick 4001 key error, disk <20% free |
| `summary_webhook_url` | #daily-summary | 22:00 UK daily summary: moments per streamer + top 3 by score (last 24h) |
| `videos_webhook_url` | #ready-to-post | Legacy only (review off / `lacy deliver`): finished file attached, or a GCS link. Old name `tokyo_girl_webhook_url` still read |

An empty field disables that channel; the message is still logged.

**The Discord approval bot** (discord.py, `DISCORD_BOT_TOKEN`) owns #ready-to-post,
#tiktok-queue and #inspiration (channel ids `DISCORD_*_CHANNEL_ID`, `DISCORD_GUILD_ID`,
`DISCORD_APPROVER_IDS`). **Nothing is published without Tim pressing Approve.**

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
  - Twitch: anonymous IRC for chat via `TwitchChatPool` (shared sockets, `per_conn` 50 channels
    each, `JoinLimiter` keeps JOINs under the anonymous limit, channels added/removed live;
    `run_twitch_chat` is the old one-socket reader, unused by `Bot`); `TwitchHelix.stream_states`
    batches 100 logins per request, `game_ids` / `top_streams` feed auto-watch; Helix (`TwitchHelix`) for live/category only when
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
- `spikebot/watchlist.py` (pure): **auto-watch** (`discovery` block, `DISCOVERY_MODE`,
  `DISCOVERY_MAX_CHANNELS`, off by default). `plan()` / `seen()` pick the biggest eligible live
  GTA VI streams (category, viewers, language, `exclude`, `max_channels`, dropped after
  `drop_after_s` out of the top). `Bot.discovery_loop()` -> `discovery_tick()` ->
  `add_discovered` / `drop_discovered` (meta `programme: None, discovered: True`; config channels
  are never dropped; no live/offline #status alerts for them). `AlertGate` ranks each candidate
  against all candidates in the last hour (`rank`, `rank_of`, `alerted` stored on the record, rank
  shown on the #moments card) and caps no-programme alerts at `max_alerts_per_hour`; programme
  channels always alert. Tested in `tests/test_watchlist.py`.
- `spikebot/lacy.py`: **Lacy mode** (off unless `LACY_MODE=true` / `lacy.enabled`). For
  `twitch/lacy` only, `Bot.handle()` re-checks the category with Helix Get Streams at the
  spike (`fresh_state`) and, for exactly Grand Theft Auto V/VI, `LacyMode.enqueue()` writes
  `data/lacy/jobs/<id>/job.json`. `LacyMode.worker()` waits `vod_delay_s`, looks up the VOD
  and runs `python -m spikebot.lacy render ID` as a subprocess (`render_job`: hard guards
  for channel/programme/category, cut segment, BRB/away/camera-off skip, English-only
  Whisper check, then a review item with 3 variants via `lacy.ReviewAdapter`; the segment and
  `words.json` are kept for re-renders until the item closes). Job status `review`. Approval
  and upload happen through the review queue (auto-upload removed). The old single-render
  path (`review_store=None`) and `lacy approve ID` / `upload_job` remain for old `ready` jobs.
  Titles come from `TITLES` and must pass `title_ok` (contains Lacy, no `BANNED` words,
  English). Never add a watermark/logo in Lacy mode (`build_ass(watermark=None)`).
- `spikebot/editor.py`: ffmpeg/yt-dlp helpers: `fetch_segment` (only the clip range of a
  VOD), `layout` (facecam stacked over gameplay for the whole clip), `away_reason`,
  `build_ass(style=)` (word captions, font/size/position/colours, hook length, end card;
  `caption_margin` keeps clear of the hook box and the bottom 320 px), `keep_segments` /
  `remap_words` (window around the peak at the payoff point + jump cuts, peak always kept),
  `render_variant` (trim/concat, zoom punch or white flash per cut, grade, captions),
  `preview` (size-budgeted), `render`, `review_copy`, `pick_thumbnail` (dHash vs.
  `data/lacy/thumbnails.json`), `transcribe` (faster-whisper, lazy import).
- `spikebot/youtube.py`: YouTube Data API upload (resumable), `set_thumbnail`, and the
  one-time `python -m spikebot.youtube auth --env .env` that saves `YOUTUBE_REFRESH_TOKEN`.
- `tests/test_lacy.py`: Lacy config/env, GTA filter, titles, CSV + daily cap, layout,
  BRB detection, captions without watermark, end-to-end render on a synthetic stream
  (needs ffmpeg), and the category check at spike time inside `Bot`.
- `spikebot/tokyo_girl.py`: **Tokyo Girl mode** (on by default; `TOKYO_GIRL_MODE`,
  `TOKYO_GIRL_PER_DAY`, `tokyo_girl` config block). Standalone CLI, not part of `Bot`: run daily
  by `spikebot-tokyo-girl.timer` (06:47 UK, `daily` tops up to `per_day`). Makes silent
  (`-an`, no audio stream) 1080x1920 15-30 s edits from official Rockstar GTA VI trailers and
  screenshots only (`fetch`: `ALLOWED_HOSTS`, yt-dlp limited to uploader Rockstar; plus a drop
  folder `data/rockstar_media/`). `Library` caches per-file analysis (scene cuts, YAVG,
  letterbox crop) in `library.json`; `build_shots` trims trailer head/tail, dark shots and
  `tags.json` excludes. `plan_clip` puts text cards on whole bars and cuts on beats
  (`bpm`, default 120), picking least-used footage by the story's tags. `STORIES` hold the
  on-screen text (last card is always a question; date windows relative to launch).
  Output `exports/tokyo_girl/*.mp4` + `.txt` (IG/TikTok captions from `caption_ig` /
  `caption_tiktok`). No channel logo/watermark; the optional `intro: logo` only fades in Rockstar's official GTA VI logo (`logo_file`, default `data/brand/gta6_logo.png`, skipped if missing). Text cards follow `text_level` (`cards_for_level`). State in `data/tokyo_girl/state.json`.
  With review on (default) `daily` makes `per_day` review items (`make_review`, 3 variants:
  own story/hook kind, `PACE_PATTERNS`, grade, text style, transition `flash`/`zoom`/`cut`);
  approved edits are copied to `exports/tokyo_girl/`. With review off the old `make` +
  `deliver` (webhook) path runs.
- `spikebot/delivery.py`: #ready-to-post for every mode (`MODES` labels/colours). Never
  re-encodes: files <= `videos.discord_max_mb` are attached; bigger ones (or a Discord 413)
  go to the private bucket `VIDEOS_GCS_BUCKET` via `GCSUploader` (google-cloud-storage, lazy
  import, objects `videos/<mode>/<name>`). Credentials from `gcs_client()`: `gcs-key.json` next to
  config if it exists, else the VM's attached service account `spikebot-uploads` (the project's
  org policy blocks key creation); keyless signing goes through the IAM API via `sign_kwargs()`
  (needs `roles/iam.serviceAccountTokenCreator` on itself and the VM's cloud-platform scope) and Discord gets
  a 7-day V4 signed link. Objects older than `keep_days` are deleted (dedicated bucket).
  `settings()` also reads the old Tokyo Girl names. Lacy: `upload_job(send=...)` posts the
  Short after the YouTube upload and only then deletes it; `lacy deliver ID` retries.
  Lacy Shorts go on all 3 platforms: YouTube via API, TikTok/Instagram by hand from
  #ready-to-post, then `lacy posted ID --tiktok URL --instagram URL` (`crosspost_url_ok`)
  fills `tiktok_url`/`instagram_url` in the submissions CSV (`Submissions._upgrade` adds the
  columns to old files); `lacy list` flags `missing_platforms`.
  Tested in `tests/test_delivery.py`.
- `spikebot/instagram.py`: **Instagram Reels auto-post** (Instagram API with Instagram Login,
  graph.instagram.com) for Lacy Shorts, called from `lacy.upload_job(social=Crossposter)` after
  the YouTube upload. First copies the Short to `exports/tiktok/lacy_<id>.mp4` + caption `.txt`
  (manual TikTok upload). Then, if `IG_USER_ID` + `IG_ACCESS_TOKEN` are set: uploads it to the
  `VIDEOS_GCS_BUCKET` bucket under `instagram/` with a 60 min V4 signed link (`GCSHost`), REELS
  container > poll `status_code` until FINISHED > `media_publish`, deletes the object in a
  `finally`. Caption hook from `HOOKS` (never the YouTube title, `lacy.BANNED` applies) +
  GTA hashtags. Cap `max_per_day` (3, `IG_MAX_REELS_PER_DAY`) per UK day from
  `logs/instagram_posts.csv`; over cap = `instagram_status: waiting`, failures retried up to
  `max_attempts`; `Bot.instagram_loop()` runs hourly (`retry_pending` + `maybe_refresh`).
  Token: `Token` reads `.env` before the process env (systemd's copy goes stale). Host by
  prefix (`host_for`): `IGAA` = Instagram Login on graph.instagram.com, refreshed every 50 days
  via `refresh_access_token`, saved with `youtube.set_env_value` (atomic); `EAA` = Unified login
  (Facebook Login for Business, the only option for new Meta apps) on graph.facebook.com, no
  refresh, `check_token` daily; `IG_USER_ID` comes from `/me/assigned_instagram_accounts`
  (needs `business_management` in the login configuration).
  `data/instagram/state.json` holds only a token fingerprint and times. The token goes in the
  `Authorization` header and is scrubbed from errors. CLI: `python -m spikebot.instagram
  check | refresh | retry [JOB]`. Tested in `tests/test_instagram.py`.
- `tests/test_tokyo_girl.py`: stories, beat grid, shot picking, tags, captions, daily quota,
  host guard, and an end-to-end silent render (needs ffmpeg).
- `spikebot/style.py` (pure): variant dimensions (grade, pace, font, size, position, colour,
  transition, hook tone per mode, `text_level` none/minimal/full, Lacy ending, Tokyo Girl `intro`
  none/logo) + exact `custom` values (incl. film look `grain`/`vignette`/`letterbox`, `look_filter`);
  pins `dim` (all modes) or `dim:mode`; `custom_pins`; `rules` (from /feedback, `add_rule` /
  `remove_rule` restores earlier pins, `taste()` feeds the AI); `resolve()` -> render
  numbers, `grade_filter()`; `Profile` = `data/style_profile.json` (shown/approved/passed/rejected
  per option, pins from `/style`, decisions, feedback notes + intent counts, preset stats);
  `pick_variants()`: A best-scoring, B from a most-approved preset in rotation (else weighted
  random), C experimental (least shown); unpinned dims differ across A/B/C; `parse_feedback()`
  (rule based, no API) + `apply_changes()`; `hook_style()` / `ending_style()` classifiers.
- `spikebot/presets.py`: `data/presets/<name>.json` (parts: pacing, colour, text, transitions,
  hook, structure, music-sync; `PART_KEYS` says which settings each part may set), CRUD,
  `combine()`, `to_style()`, `parse_copy()` / `parse_name()` for #inspiration notes.
- `spikebot/review.py`: review items `data/review/items/<id>/item.json` (+ `r<N>_<L>.mp4`,
  previews, `final.mp4`), `Store.edit()` = flock'd read-modify-write (bot and render
  subprocesses both write). Status rendering -> open -> approved -> done | rejected | expired
  (48 h after the latest round). Requests redo / feedback / preset are rendered by
  `python -m spikebot.review render ID` (`render_round` -> mode `ReviewAdapter`). `approve()` plans
  platforms from `review.modes.<mode>.auto` (Lacy: youtube+instagram; Tokyo Girl: none, it's
  silent) filtered by `Publisher.available()`; the rest are manual (#tiktok-queue).
  `Publisher.publish()` respects windows (UK), `youtube_max_per_day`, the Lacy CSV cap, the IG
  cap; retries 3x; Lacy also gets thumbnail, submissions CSV row and job status. Never posts a
  platform twice. CLI `list | render | approve ITEM LABEL | reject | publish` (fallback only).
- `spikebot/ai.py`: Claude API (optional; `ANTHROPIC_API_KEY` from `.env` next to config, then env;
  model `claude-opus-5-5`, `SPIKEBOT_AI_MODEL`; lazy `anthropic` import). `ask_json` = one
  structured-output request with server-side refusal fallback; `interpret_feedback` (feedback ->
  settings rows with scope this_edit/always, notes, unsupported, hook_text) and `study_reference`
  (frames + measured numbers -> what makes it good, techniques, text use, look settings).
  `settings_menu()` lists every setting the editor really has: keep it in sync with style.py.
- `spikebot/feedback.py`: `learn()` (/feedback -> `Profile.add_rule`, pins settings per mode,
  stores notes) and `for_variant()` (review Feedback button: re-render changes, lasting ones become
  a rule). Falls back to `style.parse_feedback` without a key or on API failure.
- `spikebot/discord_bot.py`: `ReviewBot` (started by `Bot.run` when the token is set; reconnects,
  never crashes the bot). Pure layouts `round_payload` / `buttons` / `manual_payload` /
  `style_overview`; custom ids `rv|action|item|round|label` handled in `on_interaction` (survive
  restarts). Loops: `scan` (post ready rounds + thread, queue requests, report failures, expire,
  #tiktok-queue, finish), `render_worker` (one subprocess at a time), `publish_loop` (60 s, under
  `publish_lock`). Feedback/Edit caption use modals. `/style [setting] [value]`, `/feedback text [mode]`, `/rules [remove]`, `/preset apply |
  list | show | rename | edit | delete | combine`. #inspiration `on_message` (approvers only).
- `spikebot/inspiration.py`: style analysis in a subprocess (`python -m spikebot.inspiration
  analyse --url|--file --name --copy --json`). yt-dlp only for tiktok/instagram/youtube hosts,
  <= 4 min; the reference file is **always deleted** and only numbers are kept (hook *type*,
  never their words or footage). PySceneDetect cuts (ffmpeg fallback), tempo + on-beat share from
  an onset envelope (numpy), signalstats colour -> grade, optional easyocr text (`requirements-ocr.txt`),
  OpenCV zoom/shake, flash/fade at cuts, structure. Download failure -> bot asks for an upload.
- `tests/test_style.py`, `tests/test_review.py` (store, publisher with fakes, bot flows with fake
  Discord objects, #inspiration flow), `tests/test_review_modes.py` (real ffmpeg: Lacy and Tokyo Girl
  variants, feedback re-render, Lacy rules), `tests/test_inspiration.py`.
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
  (templates in `deploy/`), and `spikebot-tokyo-girl.timer`.
- `deploy/auto_update.sh`: run by the timer every 5 min. `git pull --ff-only` as the
  clone's owner; if HEAD differs from `/opt/spikebot/.deployed_commit` it stages the
  code, syntax-checks it, swaps it in, `pip install`s if `requirements.txt` changed and
  restarts the bot. It also installs/enables `spikebot-tokyo-girl.timer` if missing.
  Never touches `config.json`, `.env`, `data/` or `exports/`.
- `.env` / `.env.example`: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TOKYO_GIRL_MODE`, `TOKYO_GIRL_PER_DAY`, `LACY_MODE`,
  `LACY_MAX_UPLOADS_PER_DAY`, `LACY_AUTO_UPLOAD` (ignored), `DISCORD_BOT_TOKEN`,
  `DISCORD_REVIEW_CHANNEL_ID`, `DISCORD_TIKTOK_CHANNEL_ID`, `DISCORD_INSPIRATION_CHANNEL_ID`,
  `DISCORD_GUILD_ID`, `DISCORD_APPROVER_IDS`, `REVIEW_ENABLED`, `YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN`,
  `IG_USER_ID`, `IG_ACCESS_TOKEN` (read by `instagram.Token`, not `apply_env`),
  `IG_MAX_REELS_PER_DAY`, `INSTAGRAM_AUTO_POST`. `main.apply_env()`
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
- The live `config.json` and `.env` contain real secrets (four Discord webhooks, Twitch secret, Instagram token; `gcs-key.json` next to them if used). Don't print
  them or paste them into commits, logs or external requests.

### Conventions

- Python 3.11+ syntax (`X | None`), `from __future__ import annotations`, asyncio throughout.
- Dependencies are kept minimal (`websockets`, `curl_cffi`; `yt-dlp` and `faster-whisper`
  only for Lacy mode, imported in the render subprocess; `discord.py` for the approval bot;
  `scenedetect`/OpenCV only in the inspiration subprocess; easyocr optional via
  `requirements-ocr.txt`); add new ones to `requirements.txt`.
- Keep `detector.py` free of I/O so it stays testable with simulated timestamps.
- Network failures should log and retry rather than crash the service (systemd restarts
  it anyway). Status/summary code must never take the bot down.
