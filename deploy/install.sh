#!/usr/bin/env bash
# One-command server setup for spikebot (Ubuntu/Debian). Run as root from a git
# clone of the repo:
#
#   cd /root/spikebot && sudo bash deploy/install.sh
#
# It is safe to re-run. It:
#   1. installs system packages and creates the `spikebot` user
#   2. copies the code to /opt/spikebot and builds its venv
#   3. creates config.json and .env from the examples if they don't exist
#   4. installs spikebot.service (runs in background, restarts on crash)
#   5. installs spikebot-update.timer (git pull + redeploy every 5 minutes)
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "Run as root: sudo bash $0"; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="${SPIKEBOT_APP:-/opt/spikebot}"
UNITS=/etc/systemd/system
OWNER="$(stat -c %U "$REPO")"
git_() { runuser -u "$OWNER" -- git -C "$REPO" "$@"; }

git_ rev-parse --git-dir >/dev/null 2>&1 \
    || { echo "$REPO is not a git checkout; clone the repo and run this from it."; exit 1; }

echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip ffmpeg git

echo "==> Creating service user and $APP"
id -u spikebot >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin spikebot
mkdir -p "$APP/data"
rm -rf "$APP/spikebot" && cp -r "$REPO/spikebot" "$APP/spikebot"
find "$APP/spikebot" -name __pycache__ -prune -exec rm -rf {} +
install -m 644 "$REPO/requirements.txt" "$APP/requirements.txt"
[ -f "$APP/config.json" ] || cp "$REPO/config.example.json" "$APP/config.json"
[ -f "$APP/.env" ] || cp "$REPO/.env.example" "$APP/.env"
chmod 600 "$APP/config.json" "$APP/.env"

echo "==> Python environment"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"
chown -R spikebot:spikebot "$APP"

echo "==> Installing systemd units"
sed "s#@APP@#$APP#g" "$REPO/deploy/spikebot.service" > "$UNITS/spikebot.service"
sed "s#@REPO@#$REPO#g" "$REPO/deploy/spikebot-update.service" > "$UNITS/spikebot-update.service"
install -m 644 "$REPO/deploy/spikebot-update.timer" "$UNITS/spikebot-update.timer"
chmod +x "$REPO/deploy/auto_update.sh" "$REPO/deploy/install.sh"
systemctl daemon-reload
systemctl enable spikebot
systemctl enable --now spikebot-update.timer

# Mark the current commit as deployed, so the timer only restarts on new commits.
git_ rev-parse HEAD > "$APP/.deployed_commit"

echo "==> Checking the timer can reach GitHub"
if git_ fetch --quiet origin 2>/dev/null; then
    echo "    OK: git fetch works as $OWNER"
else
    echo "    WARNING: 'git fetch' failed as $OWNER. The repo is private, so give the"
    echo "    server read access (e.g. a GitHub deploy key) or auto-update won't work."
fi

if systemctl is-active --quiet spikebot; then
    systemctl restart spikebot
    echo "==> Restarted the running bot on the new code"
fi

cat <<MSG

Done. Next:
  1. Twitch keys:          sudo nano $APP/.env
  2. Discord webhooks etc: sudo nano $APP/config.json
  3. Test connections:     cd $APP && sudo -u spikebot venv/bin/python -m spikebot.main --check
  4. Start the bot:        sudo systemctl start spikebot
  5. Watch the log:        sudo journalctl -u spikebot -f

Auto-update: every 5 min the server pulls $REPO and restarts the bot only if
there are new commits.
  Next run / last run:     systemctl list-timers spikebot-update.timer
  Update log:              sudo journalctl -u spikebot-update -n 50
  Update now:              sudo systemctl start spikebot-update
MSG
