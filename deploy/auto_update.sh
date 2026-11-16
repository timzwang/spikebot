#!/usr/bin/env bash
# Pull the latest code into this git checkout and, only if there is something
# new, copy it to the live install and restart the bot.
#
# Runs as root every 5 minutes from spikebot-update.timer. Run it by hand with:
#   sudo bash deploy/auto_update.sh            # normal
#   sudo bash deploy/auto_update.sh --force    # redeploy even with no new commits
#
# The git pull runs as the owner of the checkout, so it uses their GitHub
# credentials (deploy key / credential helper). config.json, .env and data/ in the
# live install are never touched.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="${SPIKEBOT_APP:-/opt/spikebot}"
SERVICE="${SPIKEBOT_SERVICE:-spikebot}"
STAMP="$APP/.deployed_commit"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { echo "[auto_update] $*"; }

# Never run two updates at once (slow pull + next timer tick).
exec 9>/run/spikebot-update.lock
flock -n 9 || { log "another update is running, skipping"; exit 0; }

OWNER="$(stat -c %U "$REPO")"
as_owner() {
    if [ "$OWNER" = "$(id -un)" ]; then "$@"; else runuser -u "$OWNER" -- "$@"; fi
}
git_() { as_owner git -C "$REPO" "$@"; }

BRANCH="$(git_ rev-parse --abbrev-ref HEAD)"
git_ fetch --quiet origin "$BRANCH"

LOCAL="$(git_ rev-parse HEAD)"
REMOTE="$(git_ rev-parse "origin/$BRANCH")"
if [ "$LOCAL" != "$REMOTE" ]; then
    log "new commits on $BRANCH: ${LOCAL:0:7} -> ${REMOTE:0:7}"
    # --ff-only: never create merge commits on the server. If someone edited the
    # checkout by hand this fails loudly instead of deploying a mess.
    git_ pull --ff-only --quiet origin "$BRANCH"
fi

HEAD="$(git_ rev-parse HEAD)"
DEPLOYED="$(cat "$STAMP" 2>/dev/null || true)"
# Compare against what is actually live (not just what was pulled), so a deploy
# that failed half-way is retried on the next run.
if [ "$HEAD" = "$DEPLOYED" ] && [ "$FORCE" = 0 ]; then
    exit 0
fi

log "deploying ${HEAD:0:7} ($(git_ log -1 --format=%s HEAD)) to $APP"

REQ_CHANGED=0
if ! cmp -s "$REPO/requirements.txt" "$APP/requirements.txt"; then REQ_CHANGED=1; fi

# Stage the new code next to the live copy and syntax-check it there, so a
# broken commit never replaces working code (the bot keeps running the old one).
STAGE="$APP/.spikebot.new"
rm -rf "$STAGE"
cp -r "$REPO/spikebot" "$STAGE"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
if ! "$APP/venv/bin/python" -m compileall -q "$STAGE" >/dev/null; then
    rm -rf "$STAGE"
    log "ERROR: ${HEAD:0:7} has a syntax error; NOT deploying, bot left on ${DEPLOYED:0:7}"
    exit 1
fi

if [ "$REQ_CHANGED" = 1 ]; then
    log "requirements.txt changed, updating venv"
    "$APP/venv/bin/pip" install --quiet -r "$REPO/requirements.txt"
fi
install -m 644 "$REPO/requirements.txt" "$APP/requirements.txt"

rm -rf "$APP/.spikebot.old"
[ -d "$APP/spikebot" ] && mv "$APP/spikebot" "$APP/.spikebot.old"
mv "$STAGE" "$APP/spikebot"
rm -rf "$APP/.spikebot.old"

chown -R spikebot:spikebot "$APP/spikebot" "$APP/requirements.txt"
[ -d "$APP/venv" ] && chown -R spikebot:spikebot "$APP/venv"

# Install/refresh the Tokyo Girl daily timer (added after the first install.sh run).
# Never fatal: a problem here must not block the bot's own deploy.
if [ -f "$REPO/deploy/spikebot-tokyo-girl.timer" ]; then
    {
        mkdir -p "$APP/exports" && chown spikebot:spikebot "$APP/exports"
        sed "s#@APP@#$APP#g" "$REPO/deploy/spikebot-tokyo-girl.service" > /etc/systemd/system/spikebot-tokyo-girl.service
        install -m 644 "$REPO/deploy/spikebot-tokyo-girl.timer" /etc/systemd/system/spikebot-tokyo-girl.timer
        systemctl daemon-reload
        systemctl is-enabled --quiet spikebot-tokyo-girl.timer \
            || { systemctl enable --now spikebot-tokyo-girl.timer && log "enabled spikebot-tokyo-girl.timer"; }
    } || log "WARNING: could not install the Tokyo Girl timer"
fi

systemctl restart "$SERVICE"
echo "$HEAD" > "$STAMP"
log "restarted $SERVICE on ${HEAD:0:7}"
