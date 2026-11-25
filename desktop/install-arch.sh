#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v pacman >/dev/null 2>&1; then
  echo "This helper is for Arch Linux and Arch-based distributions." >&2
  exit 1
fi

sudo pacman -Syu --needed \
  webkit2gtk-4.1 base-devel curl wget file openssl \
  appmenu-gtk-module libappindicator-gtk3 librsvg xdotool \
  rustup nodejs npm python-pipx

if ! rustup show active-toolchain >/dev/null 2>&1; then
  rustup default stable
fi

if ! command -v daemon >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/daemon" ]]; then
  pipx install "$ROOT/dist/daemon_agent-1.0.0-py3-none-any.whl"
  pipx ensurepath
  echo "Daemon engine installed. Open a new terminal, run 'daemon auth add', then rerun this script." 
  exit 0
fi

cd "$ROOT/desktop"
npm install
npm run tauri dev
