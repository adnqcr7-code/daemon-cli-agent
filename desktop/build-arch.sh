#!/usr/bin/env bash
set -euo pipefail
DESKTOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DESKTOP_DIR/.." && pwd)"
sudo pacman -S --needed --noconfirm webkit2gtk-4.1 base-devel curl wget file openssl appmenu-gtk-module libappindicator-gtk3 librsvg xdotool rustup nodejs npm python-pipx git
rustup default stable
if [[ -f "$ROOT/dist/daemon_agent-1.0.0-py3-none-any.whl" ]]; then
  pipx install --force "$ROOT/dist/daemon_agent-1.0.0-py3-none-any.whl"
fi
cd "$DESKTOP_DIR"
npm install
npm run build:linux
printf '\nBuild complete. Look in src-tauri/target/release/bundle/appimage/\n'
