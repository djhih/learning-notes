#!/bin/bash
# monitor.sh — interactive baseline monitor.
# Password held in process memory only (env var SSHPASS).
# Recommended: run inside tmux so polling survives terminal close.
#
# Usage:
#   tmux new -s monitor
#   bash scripts/monitor.sh
#   # enter password once
#   # Ctrl-B D to detach
#   # tmux attach -t monitor   <- come back later
#
# Stop:
#   Ctrl-C in foreground, or `tmux kill-session -t monitor`

set -u
cd "$(dirname "$0")/.."

SERVER="${SERVER:-alice@192.168.0.1}"
DB_PATH="${DB_PATH:-/var/lib/cgroup-baseline/samples.db}"
DATA_DIR="${DATA_DIR:-$HOME/cgroup-data}"
LOG_DIR="${LOG_DIR:-$DATA_DIR/monitor}"
POLL_INTERVAL="${POLL_INTERVAL:-300}"      # 5 min between health checks
FETCH_INTERVAL="${FETCH_INTERVAL:-3600}"   # 1 hour between DB snapshots
LOG="$LOG_DIR/poll.log"

mkdir -p "$DATA_DIR" "$LOG_DIR"

# Prereqs
if ! command -v sshpass &>/dev/null; then
  echo "ERROR: sshpass not installed."
  echo "  Ubuntu/Debian: sudo apt install sshpass"
  echo "  Fedora/RHEL:   sudo dnf install sshpass"
  exit 1
fi

# Prompt for password (no echo, won't enter bash history)
read -rs -p "Password for $SERVER: " SSHPASS
echo
export SSHPASS

# Quick connection test
echo -n "Testing SSH... "
if ! sshpass -e ssh -o StrictHostKeyChecking=accept-new \
       -o ConnectTimeout=10 -o BatchMode=no \
       "$SERVER" 'echo ok' >/dev/null 2>&1; then
  echo "FAILED (wrong password / unreachable)"
  unset SSHPASS
  exit 1
fi
echo "OK"

echo ""
echo "Polling every ${POLL_INTERVAL}s, fetch DB every ${FETCH_INTERVAL}s."
echo "Log: $LOG"
echo "DB snapshots: $DATA_DIR/samples-*.db"
echo "Detach with Ctrl-B D (in tmux). Stop with Ctrl-C."
echo ""

# State for delta tracking
last_rows=""
last_fetch=0

# Trap to clean up env var on exit
trap 'unset SSHPASS; echo "Exiting, env var cleared."' EXIT

# Main loop
while true; do
  ts=$(date -Iseconds)

  # Health check via Python on remote (single-shot, no persistent session)
  output=$(sshpass -e ssh -o StrictHostKeyChecking=accept-new \
    "$SERVER" "python3 -c '
import sqlite3, os, sys
p = \"$DB_PATH\"
if not os.path.exists(p):
    print(\"NO_DB\")
    sys.exit(1)
try:
    db = sqlite3.connect(p)
    rows = db.execute(\"SELECT COUNT(*) FROM samples\").fetchone()[0]
    latest = db.execute(\"SELECT MAX(ts) FROM samples\").fetchone()[0] or 0
    age = int(__import__(\"time\").time()) - latest
    size = os.path.getsize(p)
    print(f\"rows={rows} latest_ts={latest} age_sec={age} size={size}\")
except Exception as e:
    print(f\"ERR {e}\")
    sys.exit(2)
'" 2>&1)
  rc=$?

  if [ $rc -eq 0 ]; then
    rows=$(echo "$output" | grep -oP 'rows=\K\d+' || echo "")
    age=$(echo "$output" | grep -oP 'age_sec=\K\d+' || echo "")
    delta=""
    if [ -n "$last_rows" ] && [ -n "$rows" ]; then
      diff=$((rows - last_rows))
      delta=" Δ=+${diff}"
      if [ "$diff" -eq 0 ]; then
        delta=" Δ=0 (WARN: no growth)"
      fi
    fi
    flag="OK"
    if [ -n "$age" ] && [ "$age" -gt 180 ]; then
      flag="STALE"   # latest sample > 3 min old = something wrong
    fi
    line="$ts  $flag  $output$delta"
    last_rows="$rows"
  else
    line="$ts  FAIL rc=$rc  $output"
  fi
  echo "$line" | tee -a "$LOG"

  # Periodic DB fetch
  now=$(date +%s)
  if [ $((now - last_fetch)) -ge "$FETCH_INTERVAL" ]; then
    snap="$DATA_DIR/samples-$(date +%Y%m%d-%H%M).db"
    if sshpass -e scp -q -o StrictHostKeyChecking=accept-new \
         "$SERVER:$DB_PATH" "$snap" 2>>"$LOG"; then
      size=$(stat -c '%s' "$snap" 2>/dev/null)
      echo "$ts  FETCH  -> $snap ($size bytes)" | tee -a "$LOG"
      last_fetch=$now
    else
      echo "$ts  FETCH_FAIL" | tee -a "$LOG"
    fi
  fi

  sleep "$POLL_INTERVAL"
done
