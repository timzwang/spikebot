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
7. Posts a **review card with a small preview video** to #moments. Watch it, check for
   story spoilers/cutscenes (Rockstar policy), then on the server:

```bash
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy approve 20261119-061502
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy reject 20261119-061502
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy list
```

`approve` uploads to YouTube (refused once `LACY_MAX_UPLOADS_PER_DAY` uploads have
happened that UK day), sets the thumbnail and appends a row to
`/opt/spikebot/logs/lacy_submissions.csv` (`uploaded_uk, youtube_url, title, ...`) ready to
paste into Content Rewards. With `LACY_AUTO_UPLOAD=true` approval is skipped (still capped).

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
Clips live in `data/lacy/jobs/<id>/`; video files are deleted after upload or reject.
The Whisper model (~150 MB) downloads on the first render.

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

**Getting the edits on your phone (Discord #tokyo-girl)**

Each edit is posted to its own Discord channel with both captions in copyable code blocks
(long-press to copy). The video is **never re-encoded or compressed**:

- Up to `discord_max_mb` (10 MB on a server without boosts; set 50 or 100 if you boost to
  level 2 or 3) the original file is attached.
- Bigger files are uploaded, unchanged, to your own private Google Cloud Storage bucket, and
  Discord gets a **Download the original** link that works for 7 days. Uploads older than 8
  days are deleted from the bucket automatically. Cost: well under £1 a month.
- With no bucket set, big files get a card with the file name instead.
- If Discord or the upload fails, the next run (or `deliver`) sends it again.

Setup:

1. Discord: create a `#tokyo-girl` channel, then Edit Channel > Integrations > Webhooks > New
   Webhook > Copy URL. Put it in `/opt/spikebot/config.json` as `"tokyo_girl_webhook_url"`.
2. Bucket (once). Open **Cloud Shell** (the `>_` button at the top of console.cloud.google.com)
   and paste:

```bash
PROJECT=$(gcloud config get-value project); B=$PROJECT-tokyo-girl; SA=spikebot-uploads@$PROJECT.iam.gserviceaccount.com
gcloud storage buckets create gs://$B --location=europe-west2 --uniform-bucket-level-access --public-access-prevention
gcloud iam service-accounts create spikebot-uploads --display-name="spikebot Tokyo Girl uploads"
gcloud storage buckets add-iam-policy-binding gs://$B --member="serviceAccount:$SA" --role=roles/storage.objectAdmin
gcloud iam service-accounts keys create gcs-key.json --iam-account=$SA && echo "Bucket name: $B"
```

   The key can only touch that one bucket. Copy it to the server, then delete the Cloud
   Shell copy (use your VM's name and zone):

```bash
gcloud compute scp gcs-key.json YOUR_VM:/tmp/gcs-key.json --zone YOUR_ZONE && rm gcs-key.json
```

   On the server:

```bash
sudo install -o spikebot -g spikebot -m 600 /tmp/gcs-key.json /opt/spikebot/gcs-key.json && sudo rm /tmp/gcs-key.json
```

3. Add `TOKYO_GIRL_GCS_BUCKET=<the bucket name it printed>` to `/opt/spikebot/.env`.
4. Test: `--check` sends a test message to #tokyo-girl, and
   `sudo -u spikebot venv/bin/python -m spikebot.tokyo_girl deliver <file.mp4>` re-sends one edit.

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
| `deliver` | Send any edits that didn't reach Discord (or `deliver FILE.mp4` to resend one) |
| `daily` | What the timer runs |

Or copy today's edits straight to a laptop:

```bash
gcloud compute scp --recurse "YOUR_VM:/opt/spikebot/exports/tokyo_girl/$(date +%F)_*" ~/Downloads/
```

Before posting: watch it once (check nothing looks like a story spoiler), add the Tokyo
Girl sound in the app, paste the matching caption line from the `.txt`.

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
