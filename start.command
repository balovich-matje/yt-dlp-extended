#!/usr/bin/env bash
# Double-clickable launcher for macOS Finder.
#
# Stops any server this app already has running, starts a fresh one, and opens
# the web UI. Safe to double-click repeatedly — that is the point of it.
set -uo pipefail

# Finder launches this from an arbitrary working directory.
cd "$(dirname "$0")" || exit 1

PORT="${YTX_PORT:-8723}"
PATTERN="uvicorn app.main:app"

# Keep the Terminal window readable if we bail out early.
die() {
  echo
  echo "error: $*" >&2
  echo
  read -r -n 1 -p "Press any key to close…" _ || true
  echo
  exit 1
}

echo "yt-dlp-extended launcher"
echo "────────────────────────"

# --- stop the previous instance ---------------------------------------------
# Match our own uvicorn invocation rather than whatever holds the port, so we
# never kill an unrelated process that happens to be listening.
OLD_PIDS="$(pgrep -f "$PATTERN" || true)"

if [ -n "$OLD_PIDS" ]; then
  echo "Stopping running server (pid $(echo "$OLD_PIDS" | tr '\n' ' '))…"
  # shellcheck disable=SC2086
  kill $OLD_PIDS 2>/dev/null || true

  for _ in $(seq 1 40); do
    pgrep -f "$PATTERN" >/dev/null 2>&1 || break
    sleep 0.25
  done

  if pgrep -f "$PATTERN" >/dev/null 2>&1; then
    echo "Still alive after SIGTERM — forcing."
    pkill -9 -f "$PATTERN" 2>/dev/null || true
    sleep 1
  fi
else
  echo "No server currently running."
fi

# --- make sure the port is actually free ------------------------------------
# If something else owns it, say so instead of killing a stranger's process.
if command -v lsof >/dev/null 2>&1; then
  for _ in $(seq 1 20); do
    HOLDER="$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
    [ -z "$HOLDER" ] && break
    sleep 0.25
  done
  HOLDER="$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$HOLDER" ]; then
    NAME="$(ps -p "$(echo "$HOLDER" | head -1)" -o comm= 2>/dev/null || echo unknown)"
    die "port ${PORT} is held by another process (pid $(echo "$HOLDER" | tr '\n' ' '), $NAME).
       Quit it, or start on a different port:  YTX_PORT=8724 ./start.command"
  fi
fi

# --- start fresh ------------------------------------------------------------
# run.sh sets up the venv if needed, serves, and opens the browser once the
# server answers.
echo "Starting server on port ${PORT}…"
echo
[ -x ./run.sh ] || chmod +x ./run.sh 2>/dev/null

./run.sh || die "the server exited unexpectedly (see the output above)."
