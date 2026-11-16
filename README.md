# GTA 6 Clipped: chat spike detector

Listens to live chat on Kick and Twitch, learns each channel's normal chat speed,
and flags moments when chat suddenly explodes (laughing, "NO WAY", "clip it").
Each moment is logged with a suggested clip window and, optionally, sent to a
Discord channel on your phone.

It only reads public chat. It does not download or record any video.

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

```bash
# from your own computer: copy the folder up (replace the IP)
scp -r spikebot root@YOUR_SERVER_IP:/root/

# then on the server
ssh root@YOUR_SERVER_IP
cd /root/spikebot && bash setup.sh
nano /opt/spikebot/config.json          # add channels + Discord webhook
cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.main --check
systemctl start spikebot
journalctl -u spikebot -f               # live log, Ctrl+C to exit
```

## config.json

- `channels`: `platform` is `kick` or `twitch`; `name` is the channel slug from the URL
  (kick.com/**adinross**, twitch.tv/**kaicenat**); `programme` is a note for yourself,
  `null` means no clipping permission. Optional `display` is the name shown in Discord
  (e.g. `"Adin Ross"`); it defaults to `name`.
- `require_category_contains`: moments are only alerted if the stream category contains
  this text. Kick category is read automatically; Twitch needs `twitch_client_id`/`secret`
  (free at dev.twitch.tv/console), otherwise Twitch moments are never filtered.
- `min_score_to_alert`: raise to 5 if Discord gets too noisy, lower to 2 if too quiet.
- `discord_webhook_url`: Discord server → channel settings → Integrations → Webhooks → New → Copy URL.
  This is #moments: clip moments only. Each alert is a coloured card (grey under score 3,
  orange 3 to 5, red above 5) with a "Watch moment" link straight to the VOD at the clip start,
  or the live stream if the VOD isn't published yet. Twitch VOD links need `twitch_client_id`/`secret`.
  Times in Discord show in each viewer's own time zone; the log shows UK time.
- `status_webhook_url`: a second webhook (e.g. #status) for bot health. Alerts: bot started/restarted,
  a streamer went live or offline (with category), a channel's chat disconnected for 5+ minutes
  and when it reconnects, Kick error 4001 (chat key rotated), disk under 20% free. The same alert
  is never repeated within 30 minutes. Twitch live/offline needs `twitch_client_id`/`secret`.
- `summary_webhook_url`: a third webhook (#daily-summary). At 22:00 UK time every day: moments
  per streamer and the top 3 by score for the last 24 hours.
- `detector`: tuning. `ratio` and `z` control sensitivity (higher = fewer, bigger moments);
  `pre_roll_s` / `post_roll_s` control how much footage either side of the reaction.

After editing the config: `systemctl restart spikebot`.

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
