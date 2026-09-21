#!/usr/bin/env bash
# Adds "Voice Studio" to the GNOME app grid and Activities search, which is
# how to bring its window back after closing it (closing only hides it).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$DEST"
sed "s|@REPO@|$REPO|g" "$REPO/voice-studio.desktop.in" > "$DEST/voice-studio.desktop"
update-desktop-database "$DEST" >/dev/null 2>&1 || true
echo "Installed $DEST/voice-studio.desktop"
