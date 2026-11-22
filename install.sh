#!/usr/bin/env bash
# Set up Live Captions for the current user. Safe to run again; an interrupted
# model download continues from where it stopped.
set -euo pipefail

project="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$project"

if command -v uv >/dev/null; then
    [ -d .venv ] || uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -e .
else
    if ! python3 -c "import ensurepip" 2>/dev/null; then
        echo "Python's venv module is missing. Install it, then run this again:" >&2
        echo "    sudo apt install python3-venv" >&2
        exit 1
    fi
    [ -d .venv ] || python3 -m venv .venv
    .venv/bin/pip install -e .
fi

.venv/bin/python -m live_captions --download-only

applications="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$applications"
cat >"$applications/live-captions.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Live Captions
Name[ar]=التسميات التوضيحية المباشرة
Comment=Always-on-top live English captions
Exec="$project/.venv/bin/python" -m live_captions
Icon=media-view-subtitles-symbolic
Terminal=false
Categories=Utility;Accessibility;
EOF

echo "Installed. Open \"Live Captions\" from the applications menu."
