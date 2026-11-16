#!/usr/bin/env bash
# Kept for old instructions: the installer now lives in deploy/install.sh.
exec bash "$(cd "$(dirname "$0")" && pwd)/deploy/install.sh" "$@"
