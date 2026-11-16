# GTA 6 Clipped: chat spike detector

Listens to live chat on Kick and Twitch, learns each channel's normal chat speed,
and flags moments when chat suddenly explodes (laughing, "NO WAY", "clip it").
Each moment is logged with a suggested clip window and, optionally, sent to a
Discord channel on your phone.

The chat bot only reads public chat and never downloads video. The one exception is
**Lacy mode** (below), which cuts short segments from Lacy's own Twitch VODs because
his clipping programme allows it.

## What it records for each moment

| Field | Meaning |
|---|---|
| `onset_uk` | When chat started surging, UK time |
| `stream_offset_start` / `_end` | Where to cut in the VOD, e.g. `02:14:05` to `02:14:50` |
| `score` | How strong the moment was. Roughly: 3+ worth a look, 6+ strong, 10+ big |
| `peak_rate` vs `baseline` | Chat messages per second at the peak vs normal |
| `clip_mentions` | How many people typed "clip" |
| `sample` | The most repeated chat messages, which tell you what happened |
| `vibe` | Top hype words/emotes in the spike, spelling variants merged (LOOOL → LOL) |
| `vod_url` | Link to the VOD at the clip start (`?t=`), or empty if the VOD wasn't published yet |
| `status` | `candidate`, `low_score`, or `filtered_category` (streamer was not playing GTA) |
| `programme` | The clipping programme for that streamer, or empty if none |

Results go to `data/spikes.jsonl` (one line per moment) and `data/spikes.db` (SQLite).

## Install on the server

The server needs a **git clone** (not an `scp` copy) so it can auto-update. Clone it as
your normal login user (not root); the updater pulls with that user's GitHub access
(an HTTPS token saved by git, or a read-only deploy key for SSH).

```bash
ssh YOUR_USER@YOUR_SERVER_IP
git clone https://github.com/timzwang/spikebot.git ~/spikebot
cd ~/spikebot && sudo bash deploy/install.sh  # one command: packages, venv, service, auto-update
nano /opt/spikebot/.env                 # TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
nano /opt/spikebot/config.json          # add channels + Discord webhooks
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.main --check
systemctl start spikebot
journalctl -u spikebot -f               # live log, Ctrl+C to exit
```

`install.sh` is safe to re-run and never overwrites an existing `config.json` or `.env`.
(`setup.sh` still works; it just calls `deploy/install.sh`.)

### What gets installed

| Unit | Does |
|---|---|
| `spikebot.service` | Runs the bot in the background from `/opt/spikebot`, starts on boot, restarts 10s after a crash |
| `spikebot-update.timer` | Every 5 minutes runs `deploy/auto_update.sh` |
| `spikebot-tokyo-girl.timer` | 06:47 UK daily: makes the Tokyo Girl edits (see below) |

`deploy/auto_update.sh` does `git pull --ff-only` in the clone. **Only if there are new
commits** it copies `spikebot/` to `/opt/spikebot`, updates the venv if `requirements.txt`
changed, and restarts the bot. A commit with a Python syntax error is refused and the bot
stays on the old code. `config.json`, `.env` and `data/` are never touched.

```bash
systemctl list-timers spikebot-update.timer   # next/last check
journalctl -u spikebot-update -n 50           # what the updater did
systemctl start spikebot-update               # check for updates now
sudo bash ~/spikebot/deploy/auto_update.sh --force   # redeploy even with no new commits
```

## Twitch keys (.env)

`TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` are read from `/opt/spikebot/.env`
(template: `.env.example`, free keys at dev.twitch.tv/console). They override the
`twitch_client_*` fields in `config.json`. `.env` is gitignored: never commit it.

## config.json

- `channels`: `platform` is `kick` or `twitch`; `name` is the channel slug from the URL
  (kick.com/**adinross**, twitch.tv/**kaicenat**); `programme` is a note for yourself,
  `null` means no clipping permission. Optional `display` is the name shown in Discord
  (e.g. `"Adin Ross"`); it defaults to `name`.
- `require_category_contains`: moments are only alerted if the stream category contains
  this text. Kick category is read automatically; Twitch needs the Twitch keys
  (`.env`, see above), otherwise Twitch moments are never filtered.
- `min_score_to_alert`: raise to 5 if Discord gets too noisy, lower to 2 if too quiet.
- `discord_webhook_url`: Discord server → channel settings → Integrations → Webhooks → New → Copy URL.
  This is #moments: clip moments only. Each alert is a coloured card (grey under score 3,
  orange 3 to 5, red above 5) with a "Watch moment" link straight to the VOD at the clip start,
  or the live stream if the VOD isn't published yet. Twitch VOD links need the Twitch keys (`.env`).
  Times in Discord show in each viewer's own time zone; the log shows UK time.
- `status_webhook_url`: a second webhook (e.g. #status) for bot health. Alerts: bot started/restarted,
  a streamer went live or offline (with category), a channel's chat disconnected for 5+ minutes
  and when it reconnects, Kick error 4001 (chat key rotated), disk under 20% free. The same alert
  is never repeated within 30 minutes. Twitch live/offline needs the Twitch keys (`.env`).
- `summary_webhook_url`: a third webhook (#daily-summary). At 22:00 UK time every day: moments
  per streamer and the top 3 by score for the last 24 hours.
- `detector`: tuning. `ratio` and `z` control sensitivity (higher = fewer, bigger moments);
  `pre_roll_s` / `post_roll_s` control how much footage either side of the reaction.

After editing `config.json` or `.env`: `systemctl restart spikebot`.

## Lacy mode (Lacy clipping campaign)

Turns Lacy's GTA chat spikes into finished Shorts for the Lacy campaign (Reach / Content
Rewards). Off by default. It only ever touches `twitch.tv/lacy`.

**What it does**

1. When chat spikes on Lacy's stream, it asks Twitch (Helix Get Streams) for his category
   **right then**. Only `Grand Theft Auto V` and `Grand Theft Auto VI` count (exact match,
   `lacy.allowed_categories`). Anything else, or Twitch keys missing, means no clip.
2. About 5 minutes later (`vod_delay_s`, so the VOD has caught up) it cuts that moment
   (max 59s) from his Twitch VOD. It does not download the whole VOD.
3. Skips the clip if it shows a BRB/away screen (screen or facecam frozen), his camera is
   off (facecam black), the speech isn't English, or there's almost no speech.
4. Renders 1080x1920: **his facecam full-width on top for the whole clip**, gameplay
   below, the title as on-screen text and word-by-word captions. **No watermark, logo or
   "6" overlay** in Lacy mode.
5. Title always contains "Lacy" and is neutral or funny (it never uses negative,
   mocking or drama words; see `TITLES` / `BANNED` in `spikebot/lacy.py`). Description
   includes `#Lacy #Shorts`.
6. Thumbnail is a frame from that clip (near the chat peak). It's compared with every
   earlier thumbnail and a different frame is picked if it looks too similar.
7. Renders **3 variants (A/B/C)** with different colour grade, pacing (clip length and jump
   cuts), text style and hook/title, and posts them in #ready-to-post for approval (see
   "Approval in Discord" below). Check for story spoilers/cutscenes (Rockstar policy) before
   approving. **Nothing is uploaded until you press Approve** (`LACY_AUTO_UPLOAD` is ignored).

On approval the bot uploads to YouTube (inside the posting window, max
`LACY_MAX_UPLOADS_PER_DAY` a UK day; extra approvals queue for the next slot), sets the
thumbnail, posts the Instagram Reel (if set up), appends a row to
`/opt/spikebot/logs/lacy_submissions.csv` for Content Rewards, and puts the TikTok file +
caption in #tiktok-queue with the `lacy posted` command. Live links are replied in the clip's
thread. After posting on TikTok, log the link so the CSV has all three platforms:

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy posted 20261119-061502 \
  --tiktok https://www.tiktok.com/@gta6.clipped/video/...
```

The CSV has `tiktok_url` and `instagram_url` columns (older files are upgraded in place).
`lacy list` shows `NOT YET ON: TikTok` for any Short still missing a platform.

**Setup**

1. `.env`: `LACY_MODE=true`, `LACY_MAX_UPLOADS_PER_DAY=3`, Twitch keys (required), and the
   YouTube keys below. `.env` values override the `lacy` block in `config.json`.
2. YouTube keys: in Google Cloud Console enable **YouTube Data API v3**, create an OAuth
   client of type **Desktop app**, put its id/secret in `.env` as `YOUTUBE_CLIENT_ID` /
   `YOUTUBE_CLIENT_SECRET`, then get the refresh token (saved into `.env`, not printed):

```bash
ssh -L 8765:127.0.0.1:8765 YOUR_USER@YOUR_SERVER_IP
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.youtube auth --env .env
```

   Open the link it prints on your laptop and sign in as the channel's Google account.
3. Facecam position: `lacy.facecam` is the box around his camera as fractions of the
   frame (`x`, `y` = top-left, `w`, `h` = size). The default is the top-left quarter. Check
   it against a real clip (any Lacy VOD segment saved as an mp4) and adjust:

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy layout clip.mp4 --out /tmp/check.jpg
```

4. `systemctl restart spikebot`, then `--check` shows "Lacy mode ON" and whether ffmpeg,
   yt-dlp, faster-whisper and the YouTube keys are present.

Notes: YouTube only shows custom thumbnails once the channel is phone-verified; until then
the upload still works and the review card says the thumbnail wasn't set. A Google Cloud
project that hasn't passed YouTube's API audit can only upload **private** videos (#25).
Clips live in `data/lacy/jobs/<id>/`; the cut segment is kept while the clip is in review
(Redo/Feedback re-render from it) and deleted once it is posted, rejected or expired.
The Whisper model (~150 MB) downloads on the first render.

## Instagram Reels (auto-post) and the TikTok folder

After you approve a clip in Discord (see "Approval in Discord"), for Lacy Shorts and any mode
set to auto-post Instagram:

1. **TikTok:** the file is copied unchanged to `exports/tiktok/lacy_<id>.mp4` with
   `lacy_<id>.txt` (caption) beside it. Upload by hand, then log it with `lacy posted ID --tiktok URL`.
   Files older than 14 days are deleted (`instagram.tiktok_keep_days`).
2. **Instagram:** posted as a Reel through the Instagram API (graph.instagram.com):
   - the video goes to your private bucket (`VIDEOS_GCS_BUCKET`) and Instagram gets a
     signed link that works for 60 minutes; the copy is **deleted right after publishing**
     (or failing);
   - container (`media_type=REELS`) > wait until `FINISHED` > `media_publish`;
   - the caption uses a **different hook from the YouTube title** (`HOOKS` in
     `spikebot/instagram.py`, same banned-word rules as titles) plus GTA hashtags
     (`#GTA6 #GTAVI #GrandTheftAuto6 #GTA6Clips #Lacy ...`; GTA V clips get `#GTA5` ones);
   - **max 3 Reels a UK day** (`IG_MAX_REELS_PER_DAY`) inside the Instagram posting window.
     Over the cap, the Reel waits and the bot posts it when the cap and window allow. A failed post is retried hourly, up
     to 3 tries, with an alert in #status;
   - each attempt is logged to `logs/instagram_posts.csv`
     (`posted_uk, status, job_id, mode, instagram_url, media_id, hook, error`) and the Reel
     link is added to `instagram_url` in `logs/lacy_submissions.csv` for Content Rewards.
3. **Token:** Instagram Login (`IGAA...`) tokens last 60 days. The bot refreshes it every 50 days
   (`refresh_access_token`) and writes the new one back into `.env` (it always reads the
   token from `.env`, so no restart is needed). If a refresh fails, #status gets an alert
   and it retries every 6 hours. Unified login
   (`EAA...`) tokens don't expire by default and are checked once a day instead.

**Setup (once)**

1. The Instagram account must be a **Professional** account (Creator or Business). Switch in
   the Instagram app: Settings > Account type and tools.
2. At developers.facebook.com create an app with the **Instagram API** use case. Under App
   roles > Roles add the Instagram account as an **Instagram Tester** and accept the invite in
   the Instagram app (Settings > Website permissions > Apps and websites > Tester invites). The
   app can stay in Development mode because it only posts to your own account.
3. Get a token. New apps only get **API setup with Unified login** (Facebook Login for
   Business). Its tokens start with `EAA` and work on graph.facebook.com; the bot picks the host
   from the token. In Use cases > Instagram API > Customize > API setup with Unified login:
   - the login configuration must include `instagram_basic`, `instagram_content_publish` and
     `business_management`. The Unified login editor doesn't offer `business_management`: add
     the **Facebook Login for Business** product, then edit the same configuration under
     Facebook Login for Business > Configurations;
   - **2. Generate access tokens** > add the Instagram account > generate the token;
   - `python -m spikebot.instagram check` then lists the Instagram accounts the token can use,
     with the id to put in `IG_USER_ID`.
   These tokens don't expire by default, so the bot checks the token daily instead of
   refreshing it and alerts #status if it stops working. (Older apps that still have **API
   setup with Instagram login** give `IGAA...` tokens: graph.instagram.com, refreshed every 50
   days as described above.)
4. In `/opt/spikebot/.env`: `IG_USER_ID=...`, `IG_ACCESS_TOKEN=...` (and `VIDEOS_GCS_BUCKET`,
   see "Finished videos on your phone"). Then check and restart:

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.instagram check
sudo systemctl restart spikebot
```

Other commands:

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.instagram retry            # post waiting Reels now
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.instagram retry 20261119-061502
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.instagram refresh          # refresh the token now
```

Set `INSTAGRAM_AUTO_POST=false` to pause auto-posting (TikTok export carries on). Reels
use the same file as YouTube: 1080x1920 H.264/AAC, under 60 s, no watermark.

## Tokyo Girl mode (TikTok / Instagram campaign edits)

Makes short, **silent** GTA 6 edits for the Tokyo Girl sound campaign, which you post by
hand. On by default, 2 a day (`TOKYO_GIRL_PER_DAY` in `.env`, or `tokyo_girl.per_day`).

**What each edit is**

- 1080x1920, 15-30 s, **no audio track at all**. Add the official "Tokyo Girl" sound in
  TikTok/IG. Cuts sit on a 120 BPM grid (every 0.5 s) and the length is whole bars, so if
  you start the sound on a beat the cuts land on the music.
- Footage: only official Rockstar GTA VI trailers and screenshots. No stream clips, no
  voices or dialogue. Vice City views, cars, Jason & Lucia.
- Big on-screen text telling a short story (news, theory, meme or Jason & Lucia joke),
  always ending in a question to get comments. Stories that mention the launch date only
  run before 19 Nov 2026; "it's out" ones only after. List them with `stories`.
- No logo, watermark or channel name. Rockstar's logo intro and end cards are cut off
  (`trailer_head_s` / `trailer_tail_s`) and very dark shots (title cards) are skipped.
- Saved as `/opt/spikebot/exports/tokyo_girl/<date>_<time>-N_<kind>_<story>.mp4` plus a
  `.txt` with both captions:

```
IG: <hook> Song: Tokyo Girl by @PassiveBand #GTA6
TikTok: <hook> Song: Tokyo Girl by @Passive #GTA6
```

The `<hook>` is the story's hook plus its question, e.g. "Leonida looks insane. Which spot
are you visiting first?".

**On your phone**: each edit arrives in #ready-to-post as **3 variants** (different story/hook,
cut speed, grade, text style) for approval (see "Approval in Discord"). Approved edits go to
#tiktok-queue with both captions and are copied to `exports/tokyo_girl/`. They stay silent:
add the official sound in the app.

**When**: `spikebot-tokyo-girl.timer` runs at 06:47 UK and tops up to `per_day` edits for
that UK day (running it again the same day does nothing). The auto-update installs the
timer by itself; `install.sh` does too.

**Footage**: once a week it tries to download the official media: images from
`rockstargames.com/VI/media` and the trailers from the Rockstar Games YouTube channel
(video only, 1080p max; anything not uploaded by Rockstar is refused). Google Cloud IPs
are sometimes blocked by YouTube, so if `scan` shows few shots, download the screenshots
and trailers yourself from rockstargames.com/VI/media and copy them in:

```bash
gcloud compute scp ~/Downloads/*.jpg YOUR_VM:/tmp/ && \
  gcloud compute ssh YOUR_VM -- 'sudo mv /tmp/*.jpg /opt/spikebot/data/tokyo_girl/media/ && sudo chown -R spikebot: /opt/spikebot/data/tokyo_girl'
```

Tags come from file names (`jason`, `lucia`, `vice`, `beach`, `car`, `chase`, ...). To tag
parts of a trailer, skip a file or cut out a part, create
`/opt/spikebot/data/tokyo_girl/media/tags.json`:

```json
{
  "Grand_Theft_Auto_VI_Trailer_1_trailer_QdBZY2fkU-0.mp4": {
    "ranges": [{"start": 20, "end": 31, "tags": ["cars"]}, {"start": 60, "end": 75, "tags": ["couple"]}],
    "exclude": [[88, 95]]
  },
  "some_screenshot.jpg": {"skip": true}
}
```

**Commands** (run in `/opt/spikebot` as `sudo -u spikebot venv/bin/python -m spikebot.tokyo_girl ...`):

| Command | Does |
|---|---|
| `fetch` | Download official media now, then `scan` |
| `scan` | Analyse new files (shot cuts, letterbox) and print shots per tag |
| `make -n 3` | Make 3 edits now (on top of the daily ones) |
| `make --story lucia-boss` | Make one edit with a specific story |
| `stories` | All stories; LIVE = usable today |
| `deliver` | Send any edits that didn't reach #ready-to-post (or `deliver FILE.mp4` to resend one) |
| `daily` | What the timer runs |

Or copy today's edits straight to a laptop:

```bash
gcloud compute scp --recurse "YOUR_VM:/opt/spikebot/exports/tokyo_girl/$(date +%F)_*" ~/Downloads/
```

Before posting: watch it once (check nothing looks like a story spoiler), add the Tokyo
Girl sound in the app, paste the matching caption line from the `.txt`.

## Approval in Discord (#ready-to-post bot)

Every clip from every mode (Lacy, Tokyo Girl, and the stream-clip editor later) waits for
your approval. **Nothing is posted anywhere until you press Approve.**

**What you see in #ready-to-post**: one message per clip with **3 preview videos A/B/C**.
Each card shows the YouTube title, Instagram and TikTok captions (long-press to copy) and its
style: colour grade, pace, text (font/size/position/colour), transition and hook type.
A is the look you approve most, B is built on one of your most-approved #inspiration
presets (rotating), C is **🧪 experimental** (settings it has tried least) so you keep
seeing new ideas. A thread opens under each clip for everything that follows.

| Button | What happens |
|---|---|
| **Approve A/B/C** | That version is posted: YouTube Shorts + Instagram Reels automatically (posting windows and daily caps; queued if needed), TikTok file + caption to #tiktok-queue. Live links are replied in the thread. |
| **Redo** | 3 new variants (avoiding the looks you just saw). |
| **Reject** | Dropped; files deleted. |
| **Feedback** | Pick a variant, type notes, it is re-rendered and posted in the thread for approval. Understood: bigger/smaller text, text higher/lower/middle, faster/slower, more/less colour, more/less contrast, warmer/cooler, a colour (yellow, green, blue, red, pink), a font (montserrat, roboto, narrow, dejavu), no flashes / add zoom / add flash, end with a CTA / a question, and `hook: new wording` (or the new hook in quotes). Anything else is logged and the reply says what wasn't understood. |
| **Edit caption** | Change the YouTube title and IG/TikTok captions before approving (Lacy rules still checked). The burned-in hook only changes via Feedback `hook: ...`. |

Unapproved clips **expire 48 hours** after their latest round (buttons grey out, files deleted).

Posting rules (`config.json` `"review"` block): windows `07:00-23:00` YouTube and
`08:00-22:30` Instagram (UK time), `youtube_max_per_day` 6 (all modes; Lacy also keeps its own
cap), Instagram cap `IG_MAX_REELS_PER_DAY`. Failed uploads retry 3 times, then #status is told.
Tokyo Girl edits are **silent**, so all three platforms go to #tiktok-queue by default (add the
official Tokyo Girl sound in each app). To auto-post them anyway set
`"review": {"modes": {"tokyo_girl": {"auto": ["youtube", "instagram"]}}}`.

**Learning**: every approve / reject / redo / feedback / caption edit goes to
`data/style_profile.json`. `/style` shows what variant A uses now per mode, the approval
counts, recurring feedback and presets ranked by approvals. Pin a setting with
`/style setting:font value:montserrat` (all variants then use it); `value:auto` unpins.

**Set up the bot (once, ~10 minutes)**

1. https://discord.com/developers/applications > **New Application**, name it e.g.
   `GTA6 Clipped Review`, accept the terms, **Create**.
2. **Bot** tab: **Reset Token** > **Copy** and paste it into `/opt/spikebot/.env` as
   `DISCORD_BOT_TOKEN=...` (never in chat or git). On the same page turn **on** "Message
   Content Intent" (needed to read links in #inspiration) and turn **off** "Public Bot".
3. **OAuth2** tab > **OAuth2 URL Generator**: tick scopes `bot` and `applications.commands`,
   then bot permissions View Channels, Send Messages, Send Messages in Threads, Create Public
   Threads, Embed Links, Attach Files, Read Message History, Use Application Commands. Open the
   generated URL, pick your server, **Authorize**. (Same thing as a link:
   `https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&scope=bot+applications.commands&permissions=311385246720`,
   with the Application ID from the General Information tab.)
4. In Discord create channels `#ready-to-post` (already there), `#tiktok-queue` and
   `#inspiration`. If they are private, add the bot to each (channel > Edit > Permissions).
5. User Settings > Advanced > **Developer Mode** on. Then right-click to **Copy ID** of each
   channel, the server, and yourself, and add to `/opt/spikebot/.env`:

```bash
DISCORD_BOT_TOKEN=...
DISCORD_REVIEW_CHANNEL_ID=...       # #ready-to-post
DISCORD_TIKTOK_CHANNEL_ID=...       # #tiktok-queue
DISCORD_INSPIRATION_CHANNEL_ID=...  # #inspiration
DISCORD_GUILD_ID=...                # the server (slash commands appear instantly)
DISCORD_APPROVER_IDS=...            # your user id (only you can press buttons)
```

6. `sudo systemctl restart spikebot`. `--check` shows "Discord approval bot: token set" and the
   channel ids. The bot comes online and `/style` and `/preset` appear when you type `/`.

Without a token, clips still render and wait in `data/review/`; nothing posts. Fallback
commands on the server (only while the bot is off, so nothing posts twice):

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.review list
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.review approve lacy-20261119-061502 B
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.review publish
```

## #inspiration: copy the style of Shorts you like

Post a TikTok / Instagram / YouTube link, or upload the video, in #inspiration. Optional notes
in the same message: `copy: pacing, colour` (parts: pacing, colour, text, transitions, hook,
structure, music-sync; default all) and `name: night-drive`. The bot opens a thread, measures
the style and replies with a breakdown and a thumbnail strip, and saves the preset to
`data/presets/<name>.json`. If a link can't be downloaded (Instagram often needs a login) it
asks you to upload the file in the thread instead.

It measures **style only** and never reuses their footage, audio or words: the video is
deleted straight after the analysis and the preset holds numbers (average shot length, cuts on
the beat, grade, text size/position/colour, hook *type* such as POV/question, hook length,
where the payoff lands, ending type, zoom/shake/flash).

- `/preset list` · `/preset show name` · `/preset rename name new_name` · `/preset delete name`
- `/preset edit name setting value`: e.g. `pace fast`, `saturation 1.3`, `text_px 96`,
  `position middle`, `ending cta`, `copy pacing,colour`
- `/preset combine new_name pacing:A colour:B text:C ...`: parts from different presets
- `/preset apply name [variant]` inside a clip's thread re-renders that clip with the preset
  and posts it for approval
- Variant B of every new clip uses your most-approved presets in rotation; approvals and
  rejections per preset are tracked in `style_profile.json` (`/style` shows the ranking).

Text measurement uses OCR (easyocr), which needs PyTorch (~1 GB disk), so it is optional.
Without it everything else is measured and the breakdown says text was skipped. To add it:

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/pip install -r /home/YOU/spikebot/requirements-ocr.txt
```

## Big files and the Google Cloud Storage bucket

The approval bot attaches files up to the server's Discord limit (10 MB unboosted). Bigger
TikTok-queue files (and previews, if three don't fit) go to your private bucket and Discord
gets a 7-day download link. The old webhook delivery below is only used when review is
switched off (`REVIEW_ENABLED=false`) or for `lacy deliver` on old clips:

- **Tokyo Girl**: each edit, with the IG and TikTok captions.
- **Lacy**: each Short once it's on YouTube, with the captions still to post by hand.

Text to copy is in code blocks (long-press to copy). The video is **never re-encoded or
compressed**; you get the exact file the server made:

- Up to `videos.discord_max_mb` (10 MB on a server without boosts; set 50 or 100 if you boost
  to level 2 or 3) the original file is attached.
- Bigger files are uploaded, unchanged, to your own private Google Cloud Storage bucket, and
  Discord gets a **Download the original** link that works for 7 days. Uploads older than 8
  days are deleted from the bucket automatically. Cost: well under £1 a month.
- With no bucket set, big files get a card with their path on the server instead.
- If Discord or the upload fails, nothing is lost: Tokyo Girl retries on the next run (or
  `python -m spikebot.tokyo_girl deliver`), and a Lacy Short is kept on disk until
  `python -m spikebot.lacy deliver ID` gets it through. The YouTube upload is never undone.

Setup:

1. Discord: create a `#ready-to-post` channel, then Edit Channel > Integrations > Webhooks >
   New Webhook > Copy URL. Put it in `/opt/spikebot/config.json` as `"videos_webhook_url"`.
   (The older name `tokyo_girl_webhook_url` still works.)
2. Bucket (once). Open **Cloud Shell** (the `>_` button at the top of console.cloud.google.com)
   and paste:

```bash
PROJECT=$(gcloud config get-value project); B=$PROJECT-videos; SA=spikebot-uploads@$PROJECT.iam.gserviceaccount.com
gcloud services enable iamcredentials.googleapis.com
gcloud storage buckets create gs://$B --location=europe-west2 --uniform-bucket-level-access --public-access-prevention
gcloud iam service-accounts create spikebot-uploads --display-name="spikebot video uploads"
gcloud storage buckets add-iam-policy-binding gs://$B --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
gcloud iam service-accounts add-iam-policy-binding $SA --member="serviceAccount:$SA" --role=roles/iam.serviceAccountTokenCreator
echo "Bucket name: $B"
```

   No key file: the VM itself runs as `spikebot-uploads`, which can only touch that one
   bucket (and the bucket must hold nothing else: old files in it are deleted). Attach it
   to the VM. This needs the VM stopped for a minute (the bot restarts on boot; an
   ephemeral external IP may change). `gcloud compute instances list` shows the name and zone:

```bash
VM=YOUR_VM; ZONE=YOUR_ZONE
gcloud compute instances stop $VM --zone $ZONE
gcloud compute instances set-service-account $VM --zone $ZONE --service-account=$SA --scopes=cloud-platform
gcloud compute instances start $VM --zone $ZONE
```

   (A `gcs-key.json` service-account key next to `config.json` is still used if present,
   for servers outside Google Cloud. New Google Cloud organisations block key creation.)
3. Add `VIDEOS_GCS_BUCKET=<the bucket name it printed>` to `/opt/spikebot/.env`.
4. Test: `sudo -u spikebot venv/bin/python -m spikebot.instagram check` uploads a tiny
   file, opens its signed link and deletes it. `--check` sends a test message to #ready-to-post, and
   `sudo -u spikebot venv/bin/python -m spikebot.tokyo_girl deliver <file.mp4>` re-sends one edit.

## Useful commands

```bash
systemctl status spikebot                      # running?
journalctl -u spikebot --since "1 hour ago"    # recent log
tail -n 5 /opt/spikebot/data/spikes.jsonl      # latest moments
sqlite3 /opt/spikebot/data/spikes.db \
  "select onset_uk, channel, score, stream_offset_start from spikes where status='candidate' order by score desc limit 20"
```

## If Kick chat shows 0 messages in `--check`

Kick chat uses an unofficial connection that Kick can change without notice.
The bot asks Kick for its current chat key on every connect, so a key rotation
normally fixes itself. Leave `kick_pusher_key` and `kick_pusher_cluster` empty.
1. If the log shows `pusher error 4001`, open the streamer's Kick page in Chrome,
   press F12, go to Network, filter `pusher`. Put the key after `/app/` in
   `kick_pusher_key` and the `ws-XXX` part in `kick_pusher_cluster`.
2. If the log says the channel lookup failed (Cloudflare), open
   `https://kick.com/api/v2/channels/SLUG` in your browser, find `"chatroom":{"id":12345`,
   and add `"chatroom_id": 12345` to that channel in the config.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Simulated-chat tests (no false alarms in steady chat, bursts caught with the right timing,
spam scored low, big chats need a real jump) plus tests for status alerts and the daily summary.
