#!/usr/bin/env bash
# Single command to start yt-dlp-extended: sets up the venv on first run,
# then serves the app on localhost.
set -euo pipefail

cd "$(dirname "$0")"

HOST="${YTX_HOST:-127.0.0.1}"
PORT="${YTX_PORT:-8723}"
VENV=".venv"

# --- pick an interpreter ----------------------------------------------------
pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        echo "$candidate"; return
      fi
    fi
  done
  echo ""
}

PY="$(pick_python)"
if [ -z "$PY" ]; then
  echo "error: Python 3.10 or newer is required but was not found." >&2
  echo "       Install it, or set PYTHON=/path/to/python3 and re-run." >&2
  exit 1
fi

# --- venv + dependencies ----------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating virtualenv with $PY …"
  "$PY" -m venv "$VENV"
fi

# Reinstall only when requirements.txt is newer than the last successful install.
STAMP="$VENV/.deps-installed"
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "Installing dependencies …"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -r requirements.txt
  touch "$STAMP"
fi

# --- warn about missing external tools --------------------------------------
for bin in yt-dlp ffmpeg ffprobe; do
  command -v "$bin" >/dev/null 2>&1 || \
    echo "warning: '$bin' is not on PATH — set an explicit path on the Settings tab." >&2
done

URL="http://${HOST}:${PORT}/"
echo
echo "  yt-dlp-extended  →  ${URL}"
echo "  Ctrl-C to stop."
echo

# Open a browser once the server is actually accepting connections.
if [ "${YTX_NO_BROWSER:-0}" != "1" ]; then
  (
    for _ in $(seq 1 50); do
      if curl -fsS -o /dev/null "${URL}api/health" 2>/dev/null; then
        command -v open >/dev/null 2>&1 && open "$URL"
        break
      fi
      sleep 0.2
    done
  ) &
fi

exec "$VENV/bin/python" -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
