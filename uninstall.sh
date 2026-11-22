#!/usr/bin/env bash
# Remove Live Captions: its menu entry, models, settings and Python environment.
# The program folder itself is left for you to delete.
set -euo pipefail

project="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
config="${XDG_CONFIG_HOME:-$HOME/.config}"

pkill -x -f "[^ ]*python[0-9.]* -m live_captions.*" 2>/dev/null || true
rm -f "$data/applications/live-captions.desktop"
rm -rf "$data/live-captions"
rm -rf "$config/live-captions"
rm -rf "$project/.venv"

echo "Uninstalled. You can now delete $project"
