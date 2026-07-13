#!/bin/bash
# monitor.sh — interactive baseline monitor.
#
# No sshpass / no SSH key / no sudo required: uses SSH connection multiplexing
# (ControlMaster). One master connection is opened up front — you type the
# password once — and every poll/fetch in the loop reuses that socket without
# re-authenticating. If the master drops, the loop re-establishes it (prompts
# again). Run inside tmux so polling survives terminal close.
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

# --- SSH connection multiplexing ------------------------------------------
# CTL is the control socket. Clients reuse the master via ControlPath and
# never authenticate themselves (ControlMaster=no), so no per-call password.
CTL="${SSH_CTL:-$LOG_DIR/ssh-ctl}"
# Options for reusing the master (poll/fetch calls).
SSH_REUSE=(-o "ControlPath=$CTL" -o ControlMaster=no -o BatchMode=yes \
           -o StrictHostKeyChecking=accept-new)

establish_master() {
  # Opens the master connection. Prompts for the password (foreground), then
  # backgrounds (-f). ControlPersist keeps it alive for the loop's reuse.
  echo -n "Connecting to $SERVER (enter password once)... "
  if ssh -fNMT \
       -o "ControlPath=$CTL" \
       -o ControlPersist=yes \
       -o StrictHostKeyChecking=accept-new \
       -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
       -o ConnectTimeout=10 \
       "$SERVER"; then
    echo "OK"
    return 0
  fi
  echo "FAILED (wrong password / unreachable)"
  return 1
}

master_alive() { ssh -O check -o "ControlPath=$CTL" "$SERVER" 2>/dev/null; }

ensure_master() {
  # Re-open the master if it has died. Returns non-zero only if it can't.
  master_alive || establish_master
}

close_master() {
  ssh -O exit -o "ControlPath=$CTL" "$SERVER" 2>/dev/null || true
  rm -f "$CTL"
}

# Clean up the master connection on exit.
trap 'close_master; echo "Exiting, SSH master closed."' EXIT

# Open master + sanity check (reuses it, so no second password).
establish_master || exit 1
echo -n "Testing reused connection... "
if ! ssh "${SSH_REUSE[@]}" "$SERVER" 'echo ok' >/dev/null 2>&1; then
  echo "FAILED (master not usable)"
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

# Main loop
while true; do
  ts=$(date -Iseconds)

  # Re-establish the master if it dropped (prompts only when truly dead).
  if ! ensure_master; then
    echo "$ts  FAIL  ssh master down, retrying next poll" | tee -a "$LOG"
    sleep "$POLL_INTERVAL"
    continue
  fi

  # Health check via Python on remote (single-shot, reuses master session)
  output=$(ssh "${SSH_REUSE[@]}" \
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

  # Periodic DB fetch (scp reuses the same master connection)
  now=$(date +%s)
  if [ $((now - last_fetch)) -ge "$FETCH_INTERVAL" ]; then
    snap="$DATA_DIR/samples-$(date +%Y%m%d-%H%M).db"
    if scp -q "${SSH_REUSE[@]}" \
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
