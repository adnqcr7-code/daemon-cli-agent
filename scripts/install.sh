#!/usr/bin/env sh
set -eu

echo "Installing Daemon..."

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "Python 3.11+ was not found. Install it from https://www.python.org/downloads/" >&2
    exit 1
fi

INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/daemon"
VENV_PYTHON="$INSTALL_ROOT/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    mkdir -p "$INSTALL_ROOT"
    "$PYTHON" -m venv "$INSTALL_ROOT"
fi

"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install '.[keyring,crypto,repl]'

mkdir -p "$HOME/.local/bin"
ln -sf "$INSTALL_ROOT/bin/daemon" "$HOME/.local/bin/daemon"

echo
echo "Daemon is installed."
echo "Installed for your user at: $INSTALL_ROOT"
echo "Run it from any project folder with:"
echo "  $HOME/.local/bin/daemon launch"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "Add \$HOME/.local/bin to your PATH to run 'daemon' without its full path." ;;
esac
echo
echo "To enter an Ollama Cloud API key securely:"
echo "  daemon auth add --provider ollama-cloud"
