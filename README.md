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
| `spikebot-update.timer` | Every minute runs `deploy/auto_update.sh` |
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

## Auto-watch (launch day: many streamers at once)

All Twitch chat now runs over a few shared connections (50 channels each), and live/category
checks are one Twitch request per 100 channels, so the bot can follow dozens of streams.

Turn on auto-watch with `DISCOVERY_MODE=true` in `.env` (needs the Twitch keys). Every 5 minutes
it looks up the biggest live **Grand Theft Auto VI** streams on Twitch (English, 3000+ viewers)
and reads their chat too, up to `max_channels` (40) on top of your `channels`. A stream that leaves
the top list or the GTA category is dropped after 30 minutes. #status gets one line when the
list changes; auto-watched streamers never send live/offline alerts.

- Auto-watched streamers have **no programme**: their cards say "check permission before
  posting", and nothing is ever downloaded from them (only a VOD link).
- Every moment is ranked against all moments from all streamers in the last hour
  ("Rank #2 of 14" on the card).
- #moments cap: at most `max_alerts_per_hour` (12) cards per hour from no-programme streamers;
  after that only a moment that beats every one already sent gets through. Programme streamers
  are never capped. Held-back moments are still saved (`data/spikes.db`, `alerted: false`).
- `discovery` block in `config.json`: `categories`, `languages` (`[]` = any), `min_viewers`,
  `max_channels`, `interval_s`, `drop_after_s`, `exclude` (logins never to auto-watch),
  `max_alerts_per_hour`. `--check` shows which streams it would pick right now.

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
4. Renders 1080x1920: **his facecam full-width in the top third for the whole clip**
   (trimmed to shape, never squashed), **gameplay in the bottom two thirds**, the title as
   on-screen text and captions. Captions only appear when they add something: lines that
   are just reactions ("bro", "no way", laughing) are cut, and if nothing meaningful is
   said there are no captions at all. No end card unless you ask for one. **No watermark,
   logo or "6" overlay** in Lacy mode.
   **Sound effects**: a whoosh into visible cuts, a riser into the payoff and a hit on it,
   quietly under his voice. Put royalty-free files (e.g. Pixabay sound effects) in
   `/opt/spikebot/data/sfx/whoosh/`, `.../hit/` and `.../riser/`; no files = no effects.
   Volume via `/feedback` ("quieter sound effects", or `sfx` 0-1; 0 turns them off).
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

## Stream clips for the other channels (Adin Ross, Seal, Kai Cenat...)

The same pipeline as Lacy mode, for **every channel in `config.json` `channels` that has a
`programme`**, plus channels you cleared yourself in `clips.allow` (default `["kaicenat"]`).
On by default; `CLIPS_MODE=false` in `.env` switches it off. Any other `"programme": null`
channel (and auto-watch channels) never gets clips.

- Chat spike -> the category is checked **right then** (Kick channel info / Helix): only GTA V
  or VI (Kick's "GTA V"/"GTA VI" names too). Gambling and anything else: no clip.
- **Highlights without chat** (`spikebot/highlights.py`, `HIGHLIGHTS_MODE`, on by default): every
  10 min the bot reads the newest VOD *audio* of each clip streamer and turns the loudest moments
  (shouting, laughing, crashes) into clip jobs too, max 2 per stream-hour, only while the stream
  was in GTA, never near a chat-spike clip. Job ids end in `-a`.
- A 150 s sample around each moment is cut from the VOD (`sample_before_s` / `sample_after_s` /
  `sample_s`); the editor picks the Short from inside it and **never starts or ends mid-word or
  mid-sentence**. Kick often publishes the VOD only when the stream ends, so a job without one
  waits (`waiting_vod`, checked every 10 min for up to 14 h) instead of failing.
- **The facecam is found automatically** in each clip (the face that stays put), unless you set a
  box for that streamer. To check, with no file needed (grabs a few seconds of the live stream):

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy layout --streamer adinross --out /tmp/check.jpg
```

  It writes the Short's layout plus `/tmp/check_frame.jpg` (whole frame, box + grid). To fix a box:
  `"clips": {"streamers": {"adinross": {"facecam": {"x": 0.75, "y": 0.0, "w": 0.25, "h": 0.3}}}}`.
- Same review as Lacy: 3 variants in #ready-to-post (labelled with the streamer's name).
  **Captions are always on** for stream clips. Titles and post captions are written by Claude
  from what was actually said in the clip (with `ANTHROPIC_API_KEY`), templates otherwise.
- Each streamer has its own daily cap (`max_uploads_per_day`, default 3) and its own
  submissions CSV: `logs/<channel>_submissions.csv`. Job ids are `<channel>-YYYYMMDD-HHMMSS`.

## Nudity check

Every rendered version (clips and edits) is scanned with NudeNet (`spikebot/safety.py`) before
it reaches #ready-to-post: anything with nudity (or a sustained sexual close-up such as strip-club
or twerking framing) is **deleted and never shown**; #announcements says what was blocked. It is
checked again right before posting, and nothing posts if the check can't run. Tokyo Girl / trend
edits never use trailer shots that were flagged. Manual check: `venv/bin/python -m spikebot.safety scan VIDEO`.

## TikTok auto-post

After Approve, stream clips go to YouTube, Instagram **and TikTok** automatically. Silent edits
(Tokyo Girl, trending sounds) go to your **TikTok inbox as drafts**: open TikTok, tap the
notification, add the sound, post (the sound has to be added in the app for the edit to count on
the sound's page). Until TikTok audits your app, public direct posts aren't allowed, so clips also
arrive as inbox drafts; after the audit they post straight to the profile.

Setup, once:
1. developers.tiktok.com > Manage apps > Connect an app. Add **Login Kit** and **Content Posting
   API** (turn on Direct Post), scopes `user.info.basic`, `video.upload`, `video.publish`, and a
   redirect URI on a site you control (e.g. your GitHub Pages URL).
2. `.env`: `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REDIRECT_URI`.
3. Consent (saves `TIKTOK_REFRESH_TOKEN`, never printed), then check:

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.tiktok --env .env auth
```

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.tiktok --env .env check
```

4. For public direct posts, submit the app for TikTok's audit (Content Posting API > Apply).
`TIKTOK_MAX_PER_DAY` (15) caps posts per UK day; `"tiktok": false` in `review.modes.<mode>`
turns TikTok off for that kind.

## #announcements

New rules, what the rule compiler built, videos going live, blocked videos and what a deploy
changed go to the server's channel with "announcements" in its name (or
`DISCORD_ANNOUNCEMENTS_CHANNEL_ID`). #status keeps bot health only.

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
hand. On by default, 4 a day (`TOKYO_GIRL_PER_DAY` in `.env`, or `tokyo_girl.per_day`).
**No on-screen text**: Tokyo Girl and trending-sound edits are always clean (`text_level` none;
no captions, story cards or hook box), whatever a rule or preset says.

**What each edit is**

- 1080x1920, 15-30 s, **no audio track at all**. Add the official "Tokyo Girl" sound in
  TikTok/IG. **Cuts follow the real song**: in #inspiration paste the TikTok sound link
  (on the sound's page: Share > Copy link; `name: tokyo-girl` optional), or write `sound:`
  before a link to any TikTok/Reel/Short that uses it (only its audio is pulled, the video is
  thrown away), or upload an audio file (or put it in `/opt/spikebot/data/music/`). If
  TikTok won't hand over the sound page's file, the bot says so: use the `sound:` way. The bot finds its beats and
  the drop (librosa), starts the edit one bar before the drop (the logo intro fades away
  exactly on it) and every card says **where to start the sound** in the app, e.g. "start
  it at 0:42". Tokyo Girl edits **only** use the Tokyo Girl song: a sound titled
  "Tokyo Girl", or anything posted with `name: tokyo-girl` (`tokyo_girl.song` changes the
  name). Any other sound is saved as a trending sound and never touches these edits.
  A sound saved as "original-sound" can get its real name (the beat map is kept and edits that
  used the old name still find it):
  `cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.music rename original-sound Dragonfly`. No
  Tokyo Girl song yet = an even 120 BPM grid. The song itself is only measured, never put in the file.
- **Rendered with Remotion** (motion design) when it's installed: eased push-ins and
  drifts, spring punch-ins with motion blur, eased flashes, words popping in one by one,
  the logo resolving from blur and scaling away into the edit, film grain, vignette,
  cinema bars. Installed automatically on deploy (`deploy/setup_remotion.sh`: Node 22,
  `npm ci` in `remotion/`, Remotion's headless Chrome; about 500 MB disk). About 2 minutes
  per 15 s edit on the VM. If Remotion isn't there or a render fails, the ffmpeg renderer
  is used, so an edit is never lost. Force one with `tokyo_girl.engine`: `auto` (default),
  `remotion` or `ffmpeg`. Tokyo Girl / trend edits render at 30 fps (the trailers' own rate; 60 doubled
  every frame and stuttered), CRF 18. Lacy clips: 60 fps, or the VOD's
  own rate when it's lower. A Remotion render that prints nothing for 5 min, or runs over
  40 min, is killed and falls back to ffmpeg. `tokyo_girl.remotion_concurrency` sets frames
  rendered in parallel (default: half the CPUs, max 4).
  `remotion_cache_mb` sets Remotion's decoded-frame cache; `remotion_tmp` (e.g. `/dev/shm/spikebot-remotion`)
  renders in a RAM disk (media is copied there per render). Several renders at once (e.g.
  multiple `/trend`) split the frame slots and cache evenly instead of each taking all of it. Remotion is free for individuals and teams of up to 3.
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

**Footage** lives in `/opt/spikebot/data/rockstar_media/`, a shared library of official
Rockstar media (not only for Tokyo Girl). Old configs that say `data/tokyo_girl/media` use
the new folder automatically unless the old one already has files. Once a week it tries to download the official media: images from
`rockstargames.com/VI/media` and the trailers from the Rockstar Games YouTube channel
(video only, 1080p max; anything not uploaded by Rockstar is refused). If the media folder
has no usable shots, `make` and the daily run fetch first; if there is still nothing, `make`
exits with an error and the daily run makes no review items (rather than failed ones). Google Cloud IPs
are sometimes blocked by YouTube, so if `scan` shows few shots, download the screenshots
and trailers yourself from rockstargames.com/VI/media and copy them in:

```bash
gcloud compute scp ~/Downloads/*.jpg YOUR_VM:/tmp/ && \
  gcloud compute ssh YOUR_VM -- 'sudo mv /tmp/*.jpg /opt/spikebot/data/rockstar_media/ && sudo chown -R spikebot: /opt/spikebot/data/rockstar_media'
```

Tags come from file names (`jason`, `lucia`, `vice`, `beach`, `car`, `chase`, ...). To tag
parts of a trailer, skip a file or cut out a part, create
`/opt/spikebot/data/rockstar_media/tags.json`:

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
| **Feedback** | Pick a variant, type notes in your own words, it is re-rendered and posted in the thread for approval. The re-render changes only what you asked for: Tokyo Girl / trend edits keep **exactly the same clips** in the same order (the card says "🎞️ Same clips as B1") unless you ask for new ones, and "switch the song to impulsive" cuts that edit to any sound in the music library (later re-renders keep it). With `ANTHROPIC_API_KEY` set, Claude reads the notes (anything: "too much text", "feels cheap", "darker and slower", a new hook in quotes); anything you say should apply *from now on* is also saved as a rule (see `/feedback`). Without a key the rule parser understands: bigger/smaller text, text higher/lower/middle, faster/slower, more/less colour or contrast, warmer/cooler, a colour, a font, no flashes / add zoom / add flash, no captions / less text / full captions, logo intro, end with a CTA / a question, and `hook: new wording`. The reply says what changed and what it can't do yet. |
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

**`/trend [song] [per_song]`**: trailer edits cut to your **trending sounds** (every sound
saved from #inspiration except the Tokyo Girl song; `song:` picks one, `per_song` 1-3). Same
official Rockstar footage, styles and 3 versions as the Tokyo Girl edits, but labelled
"Trending sound", with plain GTA captions and no Tokyo Girl credit. Silent: each card says
which sound to add in the app, where to start it, and links the sound. Each edit is
cut to fit inside the sound (a short sound gives a shorter edit, keeping the hook and the
question). On the server: `python -m spikebot.tokyo_girl trend [--song NAME] [-n N]`.
Approved ones are also copied to `exports/trend/`.

**Automatic:** the bot also makes one trending-sound edit at **07:20, 15:50 and 19:40 UK**
every day (`review.trend.times`; `review.trend.enabled: false` turns it off), rotating through
your sounds (least used first, newest wins ties). If the bot was down, only the latest
missed slot runs. These never count towards the Tokyo Girl daily edits.

**Live progress:** every long job shows a message with a rounded progress bar (the bot uploads
its pieces once as its own emojis; a text bar if that fails), the time so far and about how long
is left, updated every 2 s (less often when several bars share a channel, to stay inside Discord's
edit limit): renders (Lacy clips, Tokyo Girl, trending sounds, redos) in #ready-to-post,
#inspiration analysis in its thread, sound downloads under your message. It disappears when
the job finishes (the review card or reply replaces it), stays with the reason if it fails,
and shows for 2 minutes when a Lacy clip is skipped (BRB, not English...).

**Cancelling (`/cancel`):** every progress bar shows a run id (e.g. ``run `a1b2c3` ``) and a
**Cancel** button. `/cancel` on its own lists what's running, including queued re-renders
(`q-<clip id>`); `/cancel run:a1b2c3` stops one (autocomplete lists them), `/cancel run:all`
stops everything. Only approvers can cancel. A cancel stops just that run: in a batch (Tokyo
Girl daily, several trend edits) the next edit still goes ahead. The clip is marked
`cancelled` (no failure alert), the half-rendered files are deleted and finished rounds stay
reviewable. A worker that doesn't stop within 30 s (stuck in Whisper, say) is killed together
with its ffmpeg/Chrome. Queued Lacy jobs (still waiting for the VOD) can't be cancelled until
they start rendering.

**Stats channels:** a **📊 Stats** category at the top of the server with locked voice
channels whose names show live numbers: live streamers (of those watched), moments today,
waiting for review, approved today, bot uptime and the GTA 6 countdown. The bot creates them
itself and renames them every 10 minutes (Discord only allows 2 renames per channel per 10
minutes, so they can't tick every second). It needs **Manage Channels**; without it #status
says so (it retries every minute until it works). `/stats` refreshes them now. Settings:
`review.stats` (`enabled`, `category`, `every_s`).

**`/feedback text:<anything> [mode]`**: tell the editor what you think in plain words, e.g.
*"the captions are AI slop, only caption when he says something that matters"* or *"open the
trailer edits on the GTA 6 logo, add film grain, cut harder on the beat"*. Each setting, note
and "can't do yet" item is added to one **numbered rules list**, and every future edit (all
three variants) follows it. For each setting the **newest entry that's on wins**, whether it
was for all edits or one mode. The reply lists the new numbers with **Turn off** buttons.

The **Feedback** button on a clip does the same (the re-render shows the new numbers with
buttons), unless you say it's only for that one, e.g. *"darker, just this one"*.

**What a rule applies to:** every rule has an **Applies to** menu under it with ticks for
**Lacy clips**, **Tokyo Girl edits** and **Trend edits** (trailer edits cut to a trending sound:
the same editor as Tokyo Girl, but their own rules). Only the kinds where the setting exists are
offered (an end card is Lacy only; framing, logo intro... are Tokyo Girl and Trend). Rules saved
before this applied Tokyo Girl rules to Trend edits too, so they start with both ticked. The
reply to `/feedback` (pick *Trend edits*, *Lacy*, *Tokyo Girl* or all) and the message under a
Feedback re-render list each saved rule with what it applies to and the same ticks. The Feedback
button on a clip applies to that kind of clip only, unless you say "all edits" or name another kind.

**`/rules`** shows the whole list by category (🎨 colour & look, ✂️ pace & cuts, 🎬 footage &
camera, 🔤 text, 🪝 hook/intro/ending, 🔊 sound, 📝 notes, 🚫 not possible yet). Each rule has a plain
name and a line saying what it does on screen (e.g. **Transition: cut**, "a straight cut, no effect").
Rules saved more than once (same setting and value, or the same note) show as one line (`#3 · #9`);
its button switches all of them. `/rules search:cut` (words, or a number like `#12`) shows only the
matches, and paging/buttons keep the search. Off ones are ~~crossed out~~, and _replaced by
#N_ marks a setting a newer entry overrides. Every rule has its own **Turn off / Turn on**
button beside it (long lists have ◀ Prev / Next ▶ pages, about 11 rules each: Discord's
limit). For several at once: `/rules disable:3, 5-7` / `/rules enable:4`. Turning an entry
off brings back the one before it. If Discord ever refuses the list, it comes as `rules.txt`. **Notes** and **not possible yet** items are compiled automatically (the *rule compiler*,
`spikebot/rules.py`): at start, right after each `/feedback` and every few minutes, Claude reads each
one against what the editor can do now. If settings achieve it they are added as new rules and the
note is switched off ("now #N"); if the editor already does it, "built in now"; if it needs new code
it stays on, marked _needs new editor code_, goes into `data/rules/needs_code.json` and, with
`GITHUB_TOKEN` in `.env` (a fine-grained token with Issues read/write on this repo), is filed as a
GitHub issue labelled `rule-request`. Filing it starts the **Build rule request** GitHub Action
(`.github/workflows/rule-request.yml`) at once: Claude Code builds the rule as a real setting, runs
the tests and pushes to `main`, which deploys in 1-2 minutes, and the bot then switches the rule on by
itself. The Action needs one repository secret: `CLAUDE_CODE_OAUTH_TOKEN` (run `claude setup-token`,
uses your Claude plan) or `ANTHROPIC_API_KEY` (pay per use). It only runs for issues your own account
opened. Re-run one by hand: Actions > Build rule request > Run workflow > issue number. Each verdict
is stamped with a hash of the settings menu (`ai.settings_menu()`) and `style.BUILT_IN`, so when new
code adds a setting every open note is read again and switches itself on. #status gets one line per
rule handled.
`/style setting value` also adds an entry. Version C (experimental) never tries an option
you've rejected more often than you've picked it.

Needs `ANTHROPIC_API_KEY` in `/opt/spikebot/.env` (console.anthropic.com > API keys). Feedback
runs on `claude-sonnet-5-5` (override with `SPIKEBOT_FEEDBACK_MODEL`), #inspiration analysis on
`claude-opus-5-5` (`SPIKEBOT_AI_MODEL`). Each `/feedback` costs under 1p and
each #inspiration analysis about 5-8p. Without a key the rule parser is used (fewer phrasings).

**On-screen text** (`text_level`, varied across A/B/C and learned like the others):
`none` = no captions, hook or end card at all; `minimal` = a 2.5 s opening hook and captions
only for the lines around the payoff (Lacy) / only the opening card (Tokyo Girl); `full` =
every word (Lacy) / every story card (Tokyo Girl); `plain` (Tokyo Girl) = one short factual
line such as "2 days until GTA 6", never a question or a story card. **Logo intro**
(`intro: logo`, Tokyo Girl): the official GTA VI logo builds up over `logo_s` seconds (default
one bar) over the calmest trailer clip, then on the song's drop grows quickly while fading to
transparent, never wider than `logo_size` of the screen (e.g. 0.6). The sound note on each card
says where to start the song so the drop lands there. Put Rockstar's official transparent logo
PNG at `/opt/spikebot/data/brand/gta6_logo.png`; without the file the intro is skipped.
`intro: countdown` shows a plain "N days until GTA 6" line instead.

**Movement, framing and footage** (all settings, so `/feedback` and `/rules` control them):
`motion` `none` (every shot completely still: no pans, push-ins, punch-in zooms or animated
stills; a zoom transition becomes a cut) / `subtle` / `full`, for every mode. Tokyo Girl only:
`framing` `fit` (the whole 16:9 trailer frame, nothing cut off, over a blurred copy of itself) or
`fill` (cropped to fill the screen); `media` `clips` (trailer video only, never screenshots) or
`mixed`; `transition` `fade` (a soft crossfade, `fade_s` seconds); `hold_beats` (no clip shorter
than that many beats, and each shows the middle of its clip so the action stays intact);
`max_s` (the longest a Tokyo Girl edit may be, logo intro included; trending-sound edits
ignore it and are only limited by their sound: an edit never runs past the end of the sound
from its start point). Footage is picked by each story's
tags; the logo intro uses the calmest clip (measured once per trailer).

Notes saved before these settings existed were turned into them automatically: in `/rules`
those notes show as off with "now #N" (the setting that replaced them). **Film look** (exact values, via `/feedback`,
`/style`-like pins or presets): `grain` (0-30), `vignette` (0/1), `letterbox` (0/1, cinema bars,
Tokyo Girl only).

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
   channel ids. The bot comes online and `/style`, `/feedback`, `/rules` and `/preset` appear
   when you type `/`.

Without a token, clips still render and wait in `data/review/`; nothing posts. Fallback
commands on the server (only while the bot is off, so nothing posts twice):

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.review list
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.review approve lacy-20261119-061502 B
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.review publish
```

Re-render an edit you already approved (e.g. after a renderer fix), as long as nothing went
out automatically to YouTube/Instagram. This one is safe with the bot running: it re-renders the
approved version unchanged (same clips, text, sound and style) and posts it in #ready-to-post
to approve again.

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.review reopen ITEM_ID   # ids from `review list`
```

Before rendering, every trailer clip is checked for scene cuts the library scan missed (dark
shots) and moved off them, so no other scene flashes up between two clips.

## #inspiration: copy the style of Shorts you like

Post a TikTok / Instagram / YouTube link, or upload the video, in #inspiration. Optional notes
in the same message: `copy: pacing, colour` (parts: pacing, colour, text, transitions, hook,
structure, music-sync, look; default all), `name: night-drive`, and anything about what you
like in it ("love that there's no text until the drop"). The bot opens a thread, measures
the style and replies with a breakdown and a thumbnail strip, and saves the preset to
`data/presets/<name>.json`. If a link can't be downloaded (Instagram often needs a login) it
asks you to upload the file in the thread instead.

Threads tidy themselves away: after 12 hours with no new message the bot archives them, so
they leave the channel list and sit under the channel's **Threads** button (posting in one
reopens it). Change it with `review.inspiration_archive_hours` in `config.json` (0 = off).
The bot needs the **Manage Threads** permission if Discord refuses (it logs a warning).

It measures **style only** and never reuses their footage, audio or words: the video is
deleted straight after the analysis and the preset holds numbers (average shot length, cuts on
the beat, grade, text size/position/colour, hook *type* such as POV/question, hook length,
where the payoff lands, ending type, zoom/shake/flash).

With `ANTHROPIC_API_KEY` set it also does an **artistic read**: Claude looks at ~8 small frames
plus the measured numbers (and your note) and writes a second reply, **Why it works**: how it
opens, the rhythm, how text is used or deliberately left out, mood, the techniques it uses
(marked ✅ when our editor can do them, otherwise "not in our editor yet"), and what to avoid.
The closest text level / logo intro / grain / vignette / cinema bars are saved as the preset's
`look` part. It describes the *type* of choice only, never their words or footage, and the
frames are never stored.

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
