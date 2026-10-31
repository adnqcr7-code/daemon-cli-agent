#!/usr/bin/env sh
set -eu

echo "Installing Daemon..."

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR/.."

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "Python 3.11+ was not found. Install it from https://www.python.org/downloads/" >&2
    exit 1
fi

if [ ! -d .venv ]; then
    "$PYTHON" -m venv .venv
fi

VENV_PYTHON=".venv/bin/python"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -e '.[keyring,crypto,repl]'

echo
echo "Daemon is installed."
echo "Run it with:"
echo "  ./.venv/bin/daemon auth add"
echo "  ./.venv/bin/daemon"
echo
echo "If Ollama is installed, use:"
echo "  ./.venv/bin/daemon auth add --provider ollama"
echo "  ./.venv/bin/daemon models list"
