#!/usr/bin/env bash
# Install / refresh the Remotion renderer for Tokyo Girl edits (motion design).
# Called by install.sh and auto_update.sh as root. Best effort: if anything fails the
# bot keeps using the ffmpeg renderer, so this must never block a deploy.
#   - Node.js 22 in $APP/node if the system has no Node >= 18
#   - the libraries headless Chrome needs (apt, each one optional)
#   - $REPO/remotion copied to $APP/remotion (without node_modules), `npm ci` when the
#     lockfile changed, and Remotion's own headless Chrome
set -u
REPO="${1:?repo}"
APP="${2:?app}"
NODE_VERSION="v22.12.0"
say() { echo "[remotion] $*"; }

# ---- Node ---------------------------------------------------------------------------
node_major() { "$1" -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'; }
NODE=""
if command -v node >/dev/null && [ "$(node_major node)" -ge 18 ] 2>/dev/null; then
    NODE="$(command -v node)"
elif [ -x "$APP/node/bin/node" ]; then
    NODE="$APP/node/bin/node"
else
    case "$(uname -m)" in x86_64) ARCH=x64 ;; aarch64) ARCH=arm64 ;; *) say "unsupported CPU $(uname -m)"; exit 0 ;; esac
    say "installing Node $NODE_VERSION"
    TMP="$(mktemp -d)"
    if curl -fsSL "https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-$ARCH.tar.xz" -o "$TMP/node.tar.xz" \
        && tar -xJf "$TMP/node.tar.xz" -C "$TMP"; then
        rm -rf "$APP/node" && mv "$TMP/node-$NODE_VERSION-linux-$ARCH" "$APP/node"
        NODE="$APP/node/bin/node"
    else
        say "could not download Node (skipped, ffmpeg renderer stays in use)"
    fi
    rm -rf "$TMP"
fi
[ -n "$NODE" ] || exit 0
NODE_BIN="$(dirname "$NODE")"

# ---- Chrome's system libraries (names differ between Ubuntu releases) ------------------
for pkg in libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libgbm1 libxrandr2 libxkbcommon0 \
           libxfixes3 libxcomposite1 libxdamage1 libpango-1.0-0 libcairo2 libcups2 libasound2t64 libasound2; do
    dpkg -s "$pkg" >/dev/null 2>&1 && continue
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$pkg" >/dev/null 2>&1 || true
done

# ---- the project ----------------------------------------------------------------------
[ -d "$REPO/remotion" ] || exit 0
mkdir -p "$APP/remotion"
OLD_LOCK="$(cat "$APP/remotion/package-lock.json" 2>/dev/null | md5sum)"
( cd "$REPO/remotion" && tar --exclude=node_modules --exclude=out -cf - . ) | ( cd "$APP/remotion" && tar -xf - )
chown -R spikebot:spikebot "$APP/remotion"
[ -d "$APP/node" ] && chown -R spikebot:spikebot "$APP/node"
NEW_LOCK="$(md5sum < "$APP/remotion/package-lock.json")"
as_bot() { sudo -u spikebot env HOME="$APP" PATH="$NODE_BIN:/usr/bin:/bin" "$@"; }
if [ ! -x "$APP/remotion/node_modules/.bin/remotion" ] || [ "$OLD_LOCK" != "$NEW_LOCK" ]; then
    say "npm ci"
    ( cd "$APP/remotion" && as_bot "$NODE_BIN/npm" ci --omit=dev --no-audit --no-fund --loglevel=error ) \
        || { say "npm ci failed (ffmpeg renderer stays in use)"; exit 0; }
fi
( cd "$APP/remotion" && as_bot "$APP/remotion/node_modules/.bin/remotion" browser ensure >/dev/null 2>&1 ) \
    || say "could not fetch Remotion's headless Chrome yet (retried next deploy)"
say "ready"
exit 0
