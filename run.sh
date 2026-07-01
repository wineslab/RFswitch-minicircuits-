#!/usr/bin/env bash
#
# Start the mini-switch web app. Run ./setup.sh once first.
# Path-relative, so it works from anywhere you copy the folder to.
#
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PY="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "No .venv found — run ./setup.sh first." >&2
  exit 1
fi

echo "Starting mini-switch on http://127.0.0.1:5000  (Ctrl-C to stop)"
exec "$PY" webapp/app.py
