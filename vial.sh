#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
MAIN="$SCRIPT_DIR/src/main/python/main.py"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Ensure pip is available (some distros don't bootstrap it automatically)
if [ ! -f "$VENV_DIR/bin/pip" ]; then
    echo "Bootstrapping pip..."
    "$VENV_DIR/bin/python" -m ensurepip --upgrade
fi

if [ ! -f "$VENV_DIR/.deps-installed" ]; then
    echo "Installing dependencies..."
    "$VENV_DIR/bin/pip" install --upgrade pip -q
    "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS" -q
    "$VENV_DIR/bin/pip" install bleak -q
    touch "$VENV_DIR/.deps-installed"
fi

exec "$VENV_DIR/bin/python" "$MAIN"
