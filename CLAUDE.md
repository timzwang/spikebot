# CLAUDE.md

## Project

Automated GTA 6 Shorts channel (gta-6-clipped-yt). Tim runs everything. The goal is
revenue on a budget of about £20/month. Give all times in UK time.

## Hard rules

- Main income is clipping programmes: Adin Ross, Seal (via Content Rewards) and Lacy
  (via Reach), plus the campaigns joined on 20 Nov 2026 in `spikebot/campaigns.py` (Kai Cenat,
  YourRAGE, Clix, Dasgasd3m, Bobby Vanriper, KreekCraft, BrettLiftsXP; Lauren Hamden off: needs a
  dedicated account). Each campaign's rules live in its `CAMPAIGNS` entry. Channels with
  `programme: null` need Tim to check permission before anything is posted.
- Reach and Clipping Nation ban AI-generated content: `ai_allowed` stays false unless a campaign
  page says AI is fine. Calls to action go in captions, never on screen.
- Never write code that downloads video from streamers who haven't allowed clipping (or that
  Tim hasn't cleared in `clips.allow`).
- **Nudity is never posted**: every rendered video goes through `spikebot/safety.py` before review
  and again before any platform; blocked = deleted. Don't add a path around it.
- GTA gameplay only. Skip gambling/casino segments.
- Rockstar policy: no isolated cutscenes, endings, story spoilers or leaked footage.
- Every Short must add original value (captions, hook, context). Never raw re-uploads,
  so it stays within YouTube's reused content rules.
- Never commit or print secrets (Discord webhook, Discord bot token, Twitch secret, Google
  OAuth JSON, `gcs-key.json`, Instagram access token, Anthropic API key, GitHub token).
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

- **Editor:** stream clips 1080x1920 with a facecam / 1080x1350 (4:5) without; edits 4:5; word-level captions, programme watermark/hashtags (Lacy mode
  already has a Lacy-only version in `editor.py`/`lacy.py`; captions follow `text_level`:
  `minimal` = 2.5 s hook + captions only around the payoff, `none` = no text).
- (Done: AI voiceover teaser on stream clips' version C, review queue + Discord approval bot, uploader with windows/caps, #inspiration presets,
  stream-clip editor for every programme channel via `lacy.profiles`.)

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
| `summary_webhook_url` | #daily-summary | 22:00 UK daily summary: moments per streamer + top 3 by score (last 24h) + server CPU (average, peak, time maxed out, upgrade verdict) |
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
  sent on start if it's still before midnight UK. CPU: `status_loop` adds a load-per-core sample every 5 min
  (`add_cpu_sample`, `status_state.cpu`, 26 h kept); `cpu_summary` -> `messages.cpu_line` in the summary
  (maxed out = >= `CPU_BUSY` 0.9; >= 2 h = `upgrade` verdict).
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
- `spikebot/campaigns.py`: **campaign registry** `CAMPAIGNS` (channel -> platform, name, programme, urls,
  pay, `platforms`, `hashtags`, `cta`, `ai_allowed`, `layout` auto|full, `rules`, `enabled`/`off_reason`),
  `registry(cfg)` (+ `cfg["campaigns"]` overrides; `SPIKEBOT_CAMPAIGNS=off` in tests/conftest.py = config
  only), `merge_channels` (Bot init + `lacy.profiles`: every campaign is watched/clipped without touching
  config.json), `finish_caption` / `finish_copy` (required tags + CTA line in captions/description, never in
  the video), `copy_problems` (Edit caption check), budget watch `parse_contentrewards` (status / budgetCents /
  budgetSpentCents / budgetProgressBps in the page's data) / `parse_reach` (hero-status-badge) -> `verdict` ->
  `update` (data/campaigns/state.json; Tim's end/resume wins) <- `Bot.campaign_loop` every 30 min ->
  `main.stop_campaign` (waiting jobs cancelled, open items closed). `ended(cfg, ch)` is checked by
  `profiles`, `LacyMode.enqueue/_process` and `render_job`. `/campaigns [end] [resume]`. Tested in
  `tests/test_campaigns.py`.
- `spikebot/picker.py` (pure): **best-of-day** selection: `decide(job, jobs, cap, now, live)` -> go / wait
  (status `standby`, re-checked every `RECHECK_S`) / drop; `bar` = the streamer's usual top-`cap` score from
  the last 7 days; no history = `HOLD_S` wait; stream over / after 23:00 UK = fill best-first. Called in
  `LacyMode._process` (job `selected`); cap `max_renders_per_day` 10. `Bot` sets `LacyMode.is_live`.
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
- **Stream clips for the other programme channels** (Adin Ross, Seal; `clips` block, `CLIPS_MODE`, on
  by default) run through the Lacy pipeline. `lacy.profiles(cfg)` = (platform, channel) -> full settings
  + `name`/`platform`/`hashtag`/`programme` (Lacy from the `lacy` block; every other `channels` entry with
  a programme from `clips` + `clips.streamers.<channel>` overrides; never a `programme: null` channel).
  `profile_for(cfg, job)` (old jobs = twitch/lacy), `who(job)` = (streamer, platform); jobs carry
  `platform`/`streamer`, ids `<channel>-YYYYMMDD-HHMMSS`; titles/hooks/captions/hashtags use the name
  (`TITLES`/`instagram.HOOKS` hold `{name}`); per-streamer cap + `logs/<channel>_submissions.csv`
  (`Publisher.clip_profile`); Kick: fresh category from Kick channel info, VOD via `kick_stream_vod`
  (yt-dlp `kick:vod`), missing VOD = `waiting_vod` (every `VOD_RETRY_S` for `VOD_WAIT_S`). Review item
  mode stays `lacy` (style mode too); `discord_bot.mode_label` shows the streamer. A failed fresh
  lookup still alerts #moments but makes no clip. YouTube tags/descriptions/hashtags target launch-week
  searches ("gta 6 gameplay", "gta 6 clips", "<streamer> gta 6"). Tested in `tests/test_clips.py`.
- **Feedback button = this video only** unless the wording asks for a rule (`feedback.ALWAYS`:
  always / from now on / every / never...). /feedback is how rules are added on purpose.
- `spikebot/safety.py`: NudeNet (onnx, CPU) on 2 fps frames: `judge` (exposed >= 0.45 once, >= 0.30
  twice, or a big covered-buttocks/breast box in 3+ frames = blocked), `screen_variants` (in
  `review._render_round`, deletes blocked files; all blocked = item failed "🔞 blocked"),
  `Publisher.safety_check` (re-scan unless `safety: ok`; unavailable = not posted), Tokyo Girl
  `nudity_scan` -> library entry `nsfw` times -> `build_shots` skips those shots. Also
  `find_facecam` / `facecam_from_faces` (the face that stays put = the webcam) for clip profiles
  with `facecam_auto` (no box in config); the job stores `facecam`.
- **Rule-request settings (#40-#84)**: `style.CUSTOM_KEYS` + `TG_CUSTOM` / `LACY_CUSTOM` (stream clips only) /
  `CUSTOM_DEFAULTS`, read by `resolve`: `logo_fade_s`, `end_fade_s` (3), `intro_mix`, `unique_clips` (1),
  `crop_safe` (1), `action_first`, `ambient_bias`, `palette_match`, `impact_ending`, `opener_rotation` (10),
  `footage_match` (1), `glitch_check` (1), `sharpen` (all kinds, `style.sharpen_filter` in `look_filter`;
  Remotion gets it as an ffmpeg post-pass, `remotion.post_filter`), `lead_in_s`, `require_facecam` (1); intro
  `rockstar_presents`. Tokyo Girl picking: `tokyo_girl.Prefs` / `prefs_for(r, shots, openers, fade)` ->
  `choose_shot(..., prefs, anchor)` (`narrow` = soft filters; `footage_groups` = same footage across files via
  `shot_info.dup_groups`), `action_order`, `impact_end`, opener history `state["openers"]`. Crossfades:
  `cut_fades` (per cut, <= half of either clip, whole frames) + `clean_plan` (per-slot `tails`, `Slot.hold` =
  clean seconds, then the last frame is held: tpad in ffmpeg, `<Freeze>` in Remotion) = `prepare` (also
  `crop_safe_focus`: no face known -> `Shot.focus = (subject_x, 0.5)` for the `CROPPED` framings); `end_fade`. `ReviewAdapter.pick_intro` / `presents` / `glitch_pass` (`edit_cuts`,
  `glitches`, `fix_glitches`); clip reuse `spec["footage_from"]` (`review.named_version`, `feedback.footage_ref`
  / `find_version`: "B1" or `B_<item id>`), `trim_to`, `plan_from_footage`. `spikebot/shot_info.py`: one
  72x40 2 fps decode per file (`entry["looks"]`: YUV, 12-strip detail, 64-bit hash) -> `shot_stats` (`subject_x` only
  moves off-centre when the centre crop would cut the subject off). `spikebot/shot_labels.py`: Claude vision
  labels per shot (`entry["labels"]`, `VERSION`, retry after 6 h; `UNSAFE` shots never used; `presents_segments`;
  `STORY_TAGS`); `Library.add_extra` / `needs_extra` / `presents_shots`; `Library.label_ask` = test hook.
  Lacy: `editor.keep_segments(lead_in=)`, `lacy.facecam_required(review_store)`, `safety.real_facecam`
  (conftest stubs it to None), `streamer_only` -> `ReviewAdapter.streamer_words` (`phrase_lines` +
  `ai.game_lines`, cache `speakers.json`; captions, clip copy and the teaser line use it). On-screen hook =
  `ai.write_clip_copy` `hook` (3-8 words, the setup) when `screen_hook_ok`, else the title (variant `screen_hook`). Tokyo Girl
  `variety` (0.7): `recent_load` from `state["recent_edits"]` -> `Prefs.recent` / `src_load` in `_choose_pref`.
  Tested in `tests/test_rule_requests.py`.
- `spikebot/highlights.py` + `Bot.highlights_loop`: clip streamers' VOD **audio only**, a 30 min
  chunk per 10 min tick (`plan_scan`, state data/highlights/state.json, category history `cats`),
  `envelope` / `peaks` (robust z vs the last 3 min), `pick` (away from chat spikes, 2 per hour) ->
  jobs `<id>-a` with `source: audio`. `HIGHLIGHTS_MODE`.
- `spikebot/tiktok.py`: Content Posting API (`TikTok.post`: creator_info -> direct init, or inbox
  init when the app isn't audited / the edit is silent; FILE_UPLOAD chunks; status poll). Refresh
  token rotates and is saved to .env. `Publisher._tiktok_post`, `tiktok_cfg` (reads .env fresh),
  `auto_platforms` always adds tiktok unless `"tiktok": false`. CLI `auth | check | post`.
- #announcements: `ReviewBot.announce` (channel id or the one named "announcements", else
  #status): new rules (`announce_new_rules`), compiler notices, live links, blocked videos,
  deploy request `announce {text}`.
- Review cards (`round_payload`) are minimal: one header line (streamer/category/VOD time/id), then ONE
  embed whose inline fields are A | B | C side by side (`variant_cell`: length, song + start or clip title,
  🎙️/🎞️ teaser; `VARIANT_DOT`). No style details.
- **Voiceover teaser** (only when the profile's `ai_allowed`: no current campaign allows it; stream clips, style dim `opening` normal|teaser, lacy only; `pick_variants` gives
  "teaser" to the experimental C unless pinned/preset): `ReviewAdapter.voice_line` (Claude
  `ai.write_voice_hook`, cached in the job's voice.json, `voice_line_ok`, else the title) -> `teaser()`
  (`voice.speak` -> mp3, `editor.teaser_length`, `teaser_window` on `loudness` = loudest moment in the 6 s
  before chat's peak) -> `build_ass(lead=, lead_text=)` + `render_variant(teaser=, voice=)` (teaser first,
  clip audio ducked `TEASER_DUCK` under the voice, white flash into the clip; sfx cues shifted). Window
  shrinks so teaser + clip <= `max_clip_s`. Variant `teaser` {line, voice, s}; Feedback re-renders keep it.
  `spikebot/voice.py`: engines edge (edge-tts, free) then google (Cloud TTS via the VM's metadata token),
  config `clips.voice`; `SPIKEBOT_VOICE=off` (tests/conftest.py) = never a real service. Tested in
  `tests/test_teaser.py`.
- **Impact filter** (`spikebot/impact.py`, config `impact`): 1) `detector.chat_kind` (answer / command /
  event) -> Spike `answer_share`/`command_share`/`event_share`; `chat_verdict` in `Bot.handle` -> status
  `chat_prompt` (saved with `filtered_why`, no alert, no clip; before low_score). 2) in `lacy._render_job_body`
  after transcription, before the review item: `judge` = `speech_verdict` (`ASKED_RE` / `THANKS_RE` in the 30 s
  before the peak, no `REACTION_RE` around it and `reaction_level` < `loud_ratio`) then `ai_verdict`
  (`ai.rate_impact`, Sonnet low effort: kind + 0-10 score; `NON_MOMENTS` or < `min_score` = skip; no key / API
  failure = through) -> job `impact`, skip reason "low impact: ...". `Bot.filtered_since` ->
  `messages.summary(filtered=)` 🧹 line. Tested in `tests/test_impact.py`.
- Kick key: `kick_pusher_credentials` tries the realtime descriptor, then `pusher_from_js` on
  kick.com's scripts; cached 6 h, failures 30 min.
- **Text rules** (`style.LOCKED` / `enforce`, applied to every rendered spec in `review.specs_for`; saved
  rules and `dims` are unchanged):
  stream clips always captioned (text_level full|minimal, the adapter turns none into full), Tokyo Girl
  and trend edits never have on-screen text (text_level none).
- Whisper never decodes through PyAV: `editor.decode_audio` (ffmpeg -> 16 kHz float32) is handed to
  faster-whisper (PyAV 19 broke `av.open(metadata_errors=)`). `lacy.retry_after_fix` re-runs renders
  that crashed on that once.
- `spikebot/editor.py`: ffmpeg/yt-dlp helpers: `fetch_segment` (1080p60 when the stream had it: `-S res:1080,fps`; only the clip range of a
  VOD), `layout` (facecam stacked over gameplay for the whole clip), `away_reason`,
  `build_ass(style=)` (word captions, font/size/position/colours, hook length, end card;
  `caption_margin` keeps clear of the hook box and the bottom 320 px), `keep_segments` /
  `remap_words` (window around the peak at the payoff point + jump cuts, peak always kept),
  `render_variant` (trim/concat, zoom punch or white flash per cut, grade, captions, SFX mix),
  layout = facecam exactly the top third (`CAM_SHARE`), gameplay two thirds; `layout(..., None)` = `full_layout`
  (no facecam found / box shows no face / campaign `layout: full`: whole frame in 1080x1350 `FULL_H`, `Layout.cam`
  None, `picture` = band limits, job `no_facecam`, `lacy.clip_facecam`); captions TikTok style (`CAPTION_FONT`
  TikTok Sans bundled in `spikebot/fonts` via `ass_filter` fontsdir, static, `caption_times` hold
  `CAPTION_HOLD_S` 1.5 s, hook +1.5 s, `sentence_case`, no `cta` end card); no SFX (`sfx_cues=None`); output `out_fps` = source rate capped at 60 (#39, unknown = 30), final encodes `CRF` 18 (also Tokyo Girl/Remotion); captions only when
  `worth_captioning` (>= 3 meaningful words, `FILLER`), filler-only caption groups cut,
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
  (`-an`, no audio stream) 1080x1350 (4:5, `W, H`) 30 fps 15-30 s edits from official Rockstar GTA VI trailers and
  screenshots only (`fetch`: `ALLOWED_HOSTS`, yt-dlp limited to uploader Rockstar; plus a drop
  folder `data/rockstar_media/`). `Library` caches per-file analysis (scene cuts, YAVG,
  letterbox crop) in `library.json`; `build_shots` trims trailer head/tail, dark shots and
  `tags.json` excludes; `clean_plan` (in `render_edit` / `render_plan`) moves clips off cuts the scan
  missed (`window_cuts`, threshold 0.12, crossfade tail included). `plan_clip` puts text cards on whole bars and cuts on beats
  (`bpm`, default 120), picking least-used footage by the story's tags. `STORIES` hold the
  on-screen text (last card is always a question; date windows relative to launch).
  Output `exports/tokyo_girl/*.mp4` + `.txt` (IG/TikTok captions from `caption_ig` /
  `caption_tiktok`). No channel logo/watermark; the optional `intro: logo` only fades in Rockstar's official GTA VI logo (`logo_file`, default `data/brand/gta6_logo.png`, skipped if missing). Text cards follow `text_level` (`cards_for_level`). State in `data/tokyo_girl/state.json`.
  **Trending-sound edits** (`make_trend`, CLI `trend`, Discord `/trend` -> `ReviewBot.run_trend`):
  same renderer/style (item mode `tokyo_girl`, `source.kind: "trend"`, `source.song` = file),
  one item per sound in `music.Library.trending()` (never the Tokyo Girl song); `music.window`
  keeps the edit inside the sound, cards trimmed to fit, `trend_captions` (no Tokyo Girl
  credit), label via `discord_bot.mode_label`, exports to `exports/trend/`, not counted in per_day.
  With review on (default) `daily` makes `per_day` review items (`make_review`, 3 variants:
  own story/hook kind, `PACE_PATTERNS`, grade, text style, transition `flash`/`zoom`/`cut`);
  approved edits are copied to `exports/tokyo_girl/`. With review off the old `make` +
  `deliver` (webhook) path runs. Rules from style: `media: clips` drops images; `max_s` caps the plan
  (logo included; `edit_cap`: Tokyo Girl only, trend edits ignore it); with a song the plan is re-planned /
  `trim_plan`ned so it never runs past the sound's end from `song_start`; `plan_clip(hold_beats=, intact=, intro_s=)` (`merge_holds`, middle of each clip,
  `with_intro_shot` = one `calm_shot` under the logo: `Shot.motion` from `measure_motion`, cached in
  library.json, added once to old entries); the logo builds for `logo_s` (song window starts that many
  beats before the drop, `music.window(build_beats=)`) and bursts at the drop (`intro_filter`,
  `LOGO_GROW`, `logo_width`); `slot_filter(motion, framing, smart, fps)` = a -filter_complex graph
  ([0:v], [1:v] for stack -> [v]): `slot_layout` (smart fill: `Shot.single` close-up -> full screen; stack
  without `shot2` -> box45), `cover_chain` (crop centred on `Shot.focus`), `BOXES` box45 = the whole 4:5 frame /
  box11 1080x1080, all on black (`BLACK_BARS`, `blur_bg` is plain black, Remotion has no blur layer), stack = two 1080x608
  (`stack_tops`), `fit_filter` = whole frame on black; `slot_layout` turns a cropping framing into `fit` when `cuts_off`
  (vision `TEXT_TAGS` or not `crop_ok`; `shot_info.CROP_ASPECT` 4:5); text cards TikTok style, static, held 1.5 s; "high" text with a boxed/stacked picture sits in the band (`BAND_MARGIN`; Remotion text.y 0.035).
  Faces: `nudity_scan` stores `entry["faces"]` (`safety.flag_times(faces=)` / `frame_faces`, same NudeNet
  pass, once more for old entries), `subject_of` -> `Shot.focus`/`single`. `edit_fps`: 60 only when every
  video clip's source is >= 50 fps (`entry["fps"]`, probed once), else `FPS` 30 (no doubled frames, #39);
  `render_slot`/`join_crossfade`/`render_plan` and Remotion props take it. Stack: `Slot.shot2`/`offset2`
  (`with_seconds`, kept by `footage_of`/`refit` as key2/offset2). Variety: `choose_shot` skips clips within
  `NEAR_S` (30 s) of one already in the edit (`near_used`); `KNOWN_RANGES` tags parts of official videos
  (Extended Look 7:44 = animals; story `wildlife`); `searches` (default the Extended Look) = yt-dlp
  `ytsearch` on Rockstar's channel with a title filter (`title_regex`, no backslashes); `daily` fetches as
  soon as a source/search is new (`state.fetched`); downloads prefer 60 fps (`-S res:1080,fps`); `transition: fade` renders slots with an overlap and joins them with
  `join_crossfade` (xfade on each beat); `plain_line` / `countdown_line`; the dark "Ask" box only for a
  card ending in "?". Remotion gets the same via props `motion`, `framing`, `fade`, `logo.size/out`.
- `spikebot/compare.py`: **GTA 5 vs GTA 6** comparison edits (`/compare [count]` -> `ReviewBot.run_compare`
  -> `tokyo_girl compare -n`; `daily_compare` tops up `compare_per_day`, default 1, counts in
  `state.compare_days`). Review items mode `tokyo_girl`, `source.kind: "compare"` (`ReviewAdapter.render` ->
  `render_variants`, ffmpeg only, label `COMPARE_LABEL`, exports `exports/compare/`). GTA 5 library =
  `<media_dir>/.gta5` (hidden: GTA 6 edits never see it; `gta5_cfg`, `GTA5_SEARCHES` = Rockstar's 2013
  trailers/gameplay, never the PS5 remaster; fetched with the GTA 6 media, `GTA5_FETCH`), shots `prefixed`
  `.gta5/...`. `label_shots`: one frame per shot -> Claude (Sonnet, effort low) theme from `THEMES` +
  quality 0-3, cached in data/compare/labels.json per shot key. `pick_pairs` (same theme, biggest-difference
  themes first, best/least-used shots; `keep` = a re-render's pairs), `plan_compare` (hook over a GTA 6 hero
  shot, then pairs; layout stack = `Slot.shot2`, sequence = GTA 5 40% then GTA 6 60% in 4:5; corner labels
  via `render_plan(labels=)` / `build_text_ass(labels=)`, theme captions as cards, question last). Versions
  A/B stack, C sequence (`LAYOUTS`); variant keeps `pairs`, `layout`, `hook_card`, `question`.
- `spikebot/music.py`: song beat maps (librosa, lazy): `analyse` (bpm, beats filled back to 0,
  drop = biggest loudness rise), `Library` (`data/music/`, cache `library.json`; Tokyo Girl uses only `tokyo_girl.song`
  (default `tokyo-girl`, `TOKYO_GIRL_SONG`), other songs are trending sounds and never replace it),
  `start_for` (one bar before the drop), `grid` (beat times from the start), `note` (where to
  start the sound), `Library.rename` / CLI `music rename OLD NEW` (old names kept as `aliases`, matched by `current`). Audio uploaded in #inspiration -> `python -m spikebot.music add`; TikTok sound
  links (`/music/`, vm.tiktok short links resolved by `resolve_short`) or `sound:` + a video link
  -> `add --url` (`song_links`, `fetch`: sound page `playUrl` via curl_cffi, else yt-dlp `-x` audio only). Tokyo Girl
  `plan_clip(beats=)` cuts on it; edits stay silent.
- `spikebot/sfx.py`: whoosh/riser/hit helpers, **unused since 20 Nov** (Tim: no sound effects on any edit
  or clip; the `sfx` setting does nothing). Don't wire it back in.
- `spikebot/remotion.py` + `remotion/` (React, remotion 4.0.529 pinned): Tokyo Girl renderer
  (`TrailerEdit`: eased Ken Burns, spring punch + blur, flashes, kinetic word text, logo reveal,
  grain/vignette/letterbox). `props_for` builds the props; media hard-linked into a temp public dir
  under data/.remotion; `--muted`. `tokyo_girl.engine` auto|remotion|ffmpeg; any failure falls back
  to ffmpeg `render_plan`. `REMOTION_BROWSER` / `remotion_browser` = a local Chrome. Watchdog kills a render after 5 min with no output or 40 min total (then ffmpeg); `remotion_concurrency` (default half the CPUs, max 4), `remotion_cache_mb` (offthread video cache), `remotion_tmp` (work dir, e.g. a RAM disk). Parallel renders register in `<work>/active/` (`render_slot`) and split concurrency and cache evenly. Deployed by
  `deploy/setup_remotion.sh` (Node 22 in /opt/spikebot/node if needed, npm ci on lockfile change,
  `remotion browser ensure`). Tests force ffmpeg unless marked `remotion` (tests/conftest.py).
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
  **rules list** `data["entries"]` (numbered, `kind` setting/note/unsupported, `mode`, `on`; categories
  `CATEGORIES` / `category_of` / `entry_text`): `add_rule` appends a feedback's entries, `pin()` (/style)
  appends one, `set_enabled(nums, on)`; `pinned(mode)` / `custom_pins(mode)` are derived: the newest
  enabled entry for "all" or that mode wins (switching one off falls back). v1 profiles (`pinned`,
  `custom_pins`, `rules`) are replayed into entries by `migrate()` on load; `parse_numbers("3, 5-7")`;
  each entry has `modes` (subset of `STYLE_MODES` = lacy, tokyo_girl, trend; `entry_modes()`: old entries
  without it keep "tokyo_girl" = Tokyo Girl + trend), `set_modes`, `valid_modes(setting, value)`,
  `expand_modes`, `modes_label`; `apply_settings` keeps per row only the kinds where the setting exists.
  `trend` is its own style mode (dims = Tokyo Girl's, `TG_LIKE`); `review.style_mode(item)` = "trend"
  for `source.kind: trend`, used for pick_variants / feedback / stats.
  `taste()` = enabled entries for the AI; `upgrade_notes()` (once per profile, flag `upgrades`:
  `NOTES_UPGRADE`) turns old notes matching `NOTE_RULES` into setting entries and switches them and
  `NOW_POSSIBLE` "not possible" items off (`upgraded` = "now #N" shown in /rules); new dims `motion`
  (all modes; none = no pans/push-ins/punch zooms, zoom -> cut in `resolve`), Tokyo Girl `framing`
  box45 (default)|box11|stack|fill|fit (A/B/C explore only `FRAMING_EXPLORE`; A = the default unless pinned or a
  preset sets it; `framing_upgrade` / `FRAMING_UPGRADE` switched old fill/fit rules off once; new profiles
  start with every upgrade flag) + custom `smart_fill` (default 1), `media` clips|mixed, transition `fade`, text_level `plain`, intro `countdown` (`TG_ONLY`,
  `TG_TRANSITIONS`, `TG_TEXT_LEVELS`); Tokyo Girl custom `max_s`, `hold_beats`, `logo_s`, `logo_size`,
  `fade_s` (`TG_CUSTOM`); `resolve()` -> render
  numbers, `grade_filter()`; `Profile` = `data/style_profile.json` (shown/approved/passed/rejected
  per option, pins from `/style`, decisions, feedback notes + intent counts, preset stats);
  `pick_variants()`: A best-scoring, B from a most-approved preset in rotation (else weighted
  random), C experimental (least shown, never an option rejected more than approved); unpinned dims differ across A/B/C; `parse_feedback()`
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
  platform twice. CLI `list | render | approve ITEM LABEL | reject | publish` (fallback only); `reopen ITEM` undoes an
  approval with nothing auto-published and queues an `again` request (same version, current renderer).
- `spikebot/ai.py`: Claude API (optional; `ANTHROPIC_API_KEY` from `.env` next to config, then env;
  model `claude-opus-5-5` (`SPIKEBOT_AI_MODEL`) for #inspiration, `claude-sonnet-5-5`
  (`SPIKEBOT_FEEDBACK_MODEL`) for feedback via `model_name(base, task)`; lazy `anthropic` import). `ask_json` = one
  structured-output request with server-side refusal fallback; `interpret_feedback` (feedback ->
  settings rows with scope this_edit/always, notes, unsupported, hook_text) and `study_reference`
  (frames + measured numbers -> what makes it good, techniques, text use, look settings).
  `settings_menu()` lists every setting the editor really has: keep it in sync with style.py.
- `spikebot/rules.py`: **rule compiler**. `ReviewBot.rules_loop` (start, after /feedback via `rules_wake`,
  every 2 min, AI backoff) -> `compile_rules`: `decide` (one Claude request for the enabled note/unsupported
  entries not yet `checked` against `capabilities_version()` = hash of `ai.settings_menu()` + `style.BUILT_IN`)
  -> `apply` under `profile_lock` on a fresh profile: verdict settings (new entries, limited to the note's
  kinds, note off "now #N"), built_in (off), needs_code (stays on, `status`/`needs`, `data/rules/needs_code.json`,
  GitHub issue label `rule-request` when `GITHUB_TOKEN` is in .env, retried hourly; the issue starts
  `.github/workflows/rule-request.yml` = Claude Code builds, tests and pushes to main), not_editor. #status
  gets `compile_notice`. **When you build a new editor capability, expose it in `settings_menu` (or add a
  `BUILT_IN` line) so saved notes upgrade themselves.**
- `spikebot/deploy_requests.json`: one-off actions run once by `ReviewBot.run_deploy_requests` after the
  restart that deploys them (whitelist: `trend`, `feedback`, `repost`, `daily` {per_day} = Tokyo Girl
  top-up now; `until` = expiry; done ids in `data/deploy_requests.json`).
  Use it when Tim asks for e.g. "/trend after the restart".
- `spikebot/feedback.py`: `learn()` (/feedback -> `Profile.add_rule`: entries in the rules list) and
  `for_variant()` (review Feedback button: re-render changes; saved as entries too unless the wording
  is `ONLY_THIS` ("just this one") or the AI says scope this_edit; the round stores `entries` so its
  card gets Turn off/on buttons). Falls back to `style.parse_feedback` without a key or on API failure.
  Per-edit only (never rules): `footage` keep|new (default keep; `footage_of`) and `song` (`song_of`; the AI
  gets the library's `sounds`) -> spec `keep_footage` / `song`; Tokyo Girl `ReviewAdapter` saves each variant's
  `footage` (`footage_of(plan)`), re-renders `refit` the new plan onto it slot by slot, `find_sound` matches the
  name, the variant's own sound carries over to later rounds, `footage_note` goes on the card. Variants
  without a saved clip list are read back from their video: `spikebot/footage_match.py` (`Index` = 2 fps
  gradient hashes + 4x4 brightness layouts of every library file per framing, cached in
  data/tokyo_girl/frame_hashes.json; `frame_matches` per edit frame, `runs` by library shot, `recover`);
  `refit(skip=1)` when a logo intro is new; extra slots get new clips, never repeats.
  `deploy_requests.json` also takes `feedback` {item, round, label, notes} = a Feedback re-render.
- `spikebot/discord_bot.py`: `ReviewBot` (started by `Bot.run` when the token is set; reconnects,
  never crashes the bot). **#ready-to-post board** (`review.board`, default on): ONE message
  (`board_payload` / `board_items` / `board_line`, state data/review/board.json) lists every open item;
  the drop-down `rv|board|-|0|` expands one in place (previews attached only when the expanded item
  changes, `refresh_board`), `rv|bclose|...` collapses; `scan` -> `board_post` marks ready rounds
  `posted` + `board: True` instead of sending; `say()` without a thread = `board_note` on the line;
  `close_round_messages` / `refresh_round` refresh the board for board rounds. Pure layouts `round_payload` / `buttons` / `manual_payload` /
  `style_overview`; custom ids `rv|action|item|round|label` handled in `on_interaction` (survive
  restarts). Loops: `scan` (post ready rounds + thread, queue requests, report failures, expire,
  #tiktok-queue, finish), `render_worker` (one subprocess at a time), `publish_loop` (60 s, under
  `publish_lock`). Feedback/Edit caption use modals. `/style [setting] [value]`, `/feedback text [mode]`, `/rules [search] [disable] [enable]` (Components V2; `rule_groups` merges entries with the same
  `style.rule_key` into one line (button/menu label = dot-joined numbers, acts on all), `rule_title` = plain
  name + explanation from `style.SETTING_INFO` / `VALUE_INFO` (`describe_setting`), search where = `s:<words>` `LayoutView`
  via `ReviewBot.layout`: `rules_pages` / `rules_page` = category headings + a `Section` per rule with its
  Turn off/on button `rv|rulep|-|<page>|<n>`, pages `rv|rulesp|-|<page>|` within 40 components / 4000
  chars; answered without deferring; Discord refusal -> `rules.txt`; old `rules_off`/`rules_on` menus still
  toggle; each rule also has an "Applies to" menu `rv|rulem|<where>|<page>|<n>` (`rule_block`, `PER_RULE` = 5
  components); `rules_subset` (where = dot-joined numbers) = the /feedback reply and the message posted
  in the thread under a feedback round (`round.entries`); old `rv|rule|...` buttons (`entry_buttons` /
  `flip_rows`) still toggle; /feedback mode adds `trend`; `feedback.for_variant` keeps rows to the clip's
  own kind unless `EVERY` ("all edits") or the kind is named (`_named`); `report_error` + `tree.error` show the real error instead of "interaction failed"), `/cancel [run]`, `/preset apply |
  list | show | rename | edit | delete | combine`. #inspiration `on_message` (approvers only).
  `trend_loop` (review.trend.times UK, `trend_slots_due`, state data/trend/schedule.json) runs
  `tokyo_girl trend --song auto` (`pick_rotating`, counts in data/trend/used.json).
  `progress_loop` / `progress_tick`: one message per job file in data/progress/ (edited, deleted
  when done, kept on failure; ids in `_messages.json`). `stats_loop` / `stats_tick`: 📊 Stats
  category of locked voice channels (`stat_names`, ids in data/discord_stats.json), renamed
  as their numbers change (see Stats below); `stats_source` = `main.Bot.stats_snapshot`.
  Stats: `stats_loop` checks every 60 s; `renames_due` / `note_rename` keep each channel to Discord's 2 renames
  per 10 min (normally one per `RENAME_GAP_S` ~5 min; `/stats` = `force`, limit only), history in
  data/discord_stats.json `renamed`. `stat_names` adds views/likes/followers (`compact`, platforms in brackets)
  and `launch_text` (`GTA6_LAUNCH` = 00:00 GMT 19 Nov 2026, `review.stats.gta6_launch`). `main.Bot.live_count`:
  lookup in the last `LIVE_FRESH_S` wins (`state_at`; discovered streams live on add and from each top list),
  else chat in the last `CHAT_LIVE_S` = live and the channel is `unchecked`; failed lookups `_poll_warn`.
  `social_loop` -> `spikebot/socialstats.py` (`refresh` every `social_every_s`: YouTube Data API with
  `YOUTUBE_API_KEY` or the OAuth token if it has youtube.readonly, channel from `youtube_channel` or an uploaded
  video, else the upload login's own channel (`my_channel_id`), else `/stats youtube_channel:@handle`
  (`set_youtube_channel`); the key is read fresh from .env (`env_value`); YouTube also gets its own stats channel
  `youtube` (views · subs · Shorts, `stat_names`) and `/stats` replies with `status_text`; Instagram
  followers/likes + per-post `views` insights in turns; TikTok not counted; last good numbers kept `STALE_S`;
  data/social_stats.json). Tested in `tests/test_socialstats.py`.
  `archive_inspiration` (from `scan_loop`, every 10 min): the bot's #inspiration threads with no
  message for `review.inspiration_archive_hours` (12) are archived (`stale_threads`, snowflake times).
- `spikebot/help.py` (pure): **/help** [category] [search] (ephemeral): `COMMANDS` (name, `cat`, what, how, tags)
  under `CATEGORIES`, `help_text` / `select` / `chunks` (<= 2000 chars per message). Lists slash commands,
  review buttons and #inspiration actions. **Add a line for every new command**: `tests/test_help.py`
  checks every command registered in `ReviewBot.register_commands` is listed.
- `spikebot/progress.py`: live progress for long jobs. `Job.create` writes data/progress/<id>.json;
  `current()` (contextvar, or `SPIKEBOT_PROGRESS` env from the bot for subprocesses), `update`,
  nested `span(lo, hi)`, `finish(ok, text, state)`; `review.render_round` makes one unless a
  job is active (Lacy `render_job` has its own; `/trend` = `Bot.run_trend` makes one bar for the whole batch
  before the subprocess starts: library check (`Library.scan(on_file=)`, the nudity check of new files can
  take minutes), sounds, then each edit in a span; no sounds = skipped); `text(d, now, render_bar)` / `bar()` (text fallback, 1/8 cells) / `eta()` (time left) for the message (`NN% | ⏱ m:ss spent | m:ss remaining` beside the bar);
  rounded bar = `bar_pieces()` (9 PNG pieces, pure-Python encoder) uploaded once as application emojis
  by `ReviewBot.ensure_bar_emojis` (`EMOJI_PREFIX`), drawn by `emoji_bar`; `progress_loop` every 2 s,
  each bar edited at most every 2 s x ceil(bars in channel / 2); each write stores the worker `pid`, and `state_of` shows a job whose process died as stopped at once. Never raises,
  except `Cancelled` (a BaseException) for **/cancel**: run id = `short(id)` (shown on the bar);
  `request_cancel` writes `<id>.cancel` + SIGTERMs the worker (never the bot's own pid/pgid);
  worker CLIs call `arm_cancel()` so SIGTERM -> `Cancelled` (plain SIGTERM still exits), `update()`
  checks the marker too; `finish()` of a cancelled job always ends `cancelled`. Job boundaries
  catch it: `review.render_round` -> `review.cancel_render` (request/item `cancelled`, partial
  `r<N>_*` deleted, batch continues), Lacy `render_job` -> status `cancelled`, inspiration/music
  -> `{"cancelled": true}`. `ReviewBot.cancel_run` waits `HARD_KILL_S` (30 s), then cleans up by
  `meta` and `force_stop`s the process group (bot subprocesses use `start_new_session=True`;
  `Job.adopt(pid)` for jobs the bot creates; `force_stopped` pids aren't reported as crashes).
  `/cancel [run|all|q-<item>]` + the bar's Cancel button (`rv|cancel|<job id>`). Tested in
  `tests/test_cancel.py`.
- #inspiration **video kind** (`inspiration.KINDS` edit | stream_clip | other): `classify` = Tim's `type:`
  (`kind_from_note`, bot passes `--kind`) > Claude (`ART_SCHEMA.kind`) > `guess_kind` (webcam via
  `safety.find_facecam`, cut rate, beat sync). Preset gets `kind`, `kind_why`, `modes` (`KIND_MODES`: edit ->
  tokyo_girl+trend, stream_clip -> lacy, other -> none) and `copy` limited to `KIND_PARTS`; stream clips' pace
  = `clip_pace(length)`. `presets.for_mode` filters what `review._specs_for` rotates into B (no `modes` =
  old preset, everywhere); `/preset apply` still applies any preset.
- `spikebot/inspiration.py`: style analysis in a subprocess (`python -m spikebot.inspiration
  analyse --url|--file --name --copy --json`). yt-dlp only for tiktok/instagram/youtube hosts,
  <= 4 min; the reference file is **always deleted** and only numbers are kept (hook *type*,
  never their words or footage). PySceneDetect cuts (ffmpeg fallback), tempo + on-beat share from
  an onset envelope (numpy), signalstats colour -> grade, optional easyocr text (`requirements-ocr.txt`),
  OpenCV zoom/shake, flash/fade at cuts, structure, **framing** (`framing`: 8 frames, `sharp_rows` edge share
  per row, `bands` (median rows; a gap is closed unless no frame ever had an edge there = a stacked divider),
  `framing_from` -> fill / box45 / box11 / fit / stack; preset part `framing` in `presets.PART_KEYS`, Tokyo
  Girl/trend only). On-screen captions: `ai.CAPTION_SCHEMA` (ART_SCHEMA `captions`: position, size, case,
  words/line, look, animation, seconds on screen, hook text style) -> preset `captions` + `caption_settings`
  into the text part; `caption_line` in the reply. Download failure -> bot asks for an upload.
- `tests/test_style.py`, `tests/test_review.py` (store, publisher with fakes, bot flows with fake
  Discord objects, #inspiration flow), `tests/test_layout_captions.py` (4:5 layouts, static captions, the
  board), `tests/test_review_modes.py` (real ffmpeg: Lacy and Tokyo Girl
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
- `deploy/auto_update.sh`: run by the timer every minute. `git pull --ff-only` as the
  clone's owner; if HEAD differs from `/opt/spikebot/.deployed_commit` it stages the
  code, syntax-checks it, swaps it in, `pip install`s if `requirements.txt` changed and
  restarts the bot. It also installs/enables `spikebot-tokyo-girl.timer` if missing.
  Never touches `config.json`, `.env`, `data/` or `exports/`.
- `.env` / `.env.example`: `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TOKYO_GIRL_MODE`, `TOKYO_GIRL_PER_DAY`, `LACY_MODE`,
  `LACY_MAX_UPLOADS_PER_DAY`, `LACY_AUTO_UPLOAD` (ignored), `DISCORD_BOT_TOKEN`,
  `DISCORD_REVIEW_CHANNEL_ID`, `DISCORD_TIKTOK_CHANNEL_ID`, `DISCORD_INSPIRATION_CHANNEL_ID`,
  `DISCORD_GUILD_ID`, `DISCORD_APPROVER_IDS`, `REVIEW_ENABLED`, `YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN`, `YOUTUBE_API_KEY`,
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
  anything pushed to `main` goes live within ~1-2 minutes via `spikebot-update.timer`
  (log: `journalctl -u spikebot-update`). So only push to `main` when it's tested.
- **Tim's standing instruction: deploy straight away.** Once a change's tests pass, commit
  and push it to `main` (fast-forward) in the same job, without asking. Don't leave work on
  a side branch waiting for a "merge it".
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
