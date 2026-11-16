#!/usr/bin/env bash
# One-shot setup for a fresh Ubuntu 24.04 server. Run as root from the folder
# containing this script:   sudo bash setup.sh
set -euo pipefail

APP=/opt/spikebot
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip ffmpeg

echo "==> Creating service user and copying files to $APP"
id -u spikebot >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin spikebot
mkdir -p "$APP"
cp -r "$SRC/spikebot" "$SRC/requirements.txt" "$APP/"
[ -f "$APP/config.json" ] || cp "$SRC/config.example.json" "$APP/config.json"

echo "==> Python environment"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --upgrade pip
"$APP/venv/bin/pip" install -r "$APP/requirements.txt"
chown -R spikebot:spikebot "$APP"

echo "==> Installing background service"
cat > /etc/systemd/system/spikebot.service <<EOF
[Unit]
Description=GTA 6 Clipped chat spike detector
After=network-online.target
Wants=network-online.target

[Service]
User=spikebot
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python -m spikebot.main --config $APP/config.json
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable spikebot

echo
echo "Done. Next:"
echo "  1. Edit the config:      sudo nano $APP/config.json"
echo "  2. Test connections:     cd $APP && sudo -u spikebot venv/bin/python -m spikebot.main --check"
echo "  3. Start it for real:    sudo systemctl start spikebot"
echo "  4. Watch the log:        sudo journalctl -u spikebot -f"
