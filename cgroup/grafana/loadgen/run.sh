#!/usr/bin/env bash
# run.sh — run a load generator inside its own cgroup so it shows up on the
# Grafana "Host Detail" triage dashboard, with limits and automatic cleanup.
#
# Usage:
#   ./run.sh hog                  # anon memory hog   -> Culprits panels
#   ./run.sh thrash               # cache thrashing   -> Victims panels
#   ./run.sh burn                 # cpu under quota   -> CPU rate / throttle / PSI
#   ./run.sh all                  # all three at once
#   ./run.sh status               # live values of loadgen-* units
#   ./run.sh clean                # stop and remove every loadgen-* unit
#
# Options:
#   --duration N   run for N seconds, then auto-stop           (default 300)
#   --system       system.slice units, one service row each (needs sudo)
#                  default is --user: no root, charged to your user slice
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX="loadgen"
THRASH_FILE="/var/tmp/${PREFIX}-thrash.dat"
DURATION=300
MODE=user

CMD="${1:-}"; shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --duration) DURATION="$2"; shift 2 ;;
    --system)   MODE=system; shift ;;
    --user)     MODE=user; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

# systemctl / systemd-run wrappers that switch between --user and sudo (system)
sc()  { if [ "$MODE" = system ]; then sudo systemctl "$@"; else systemctl --user "$@"; fi; }
units() { sc list-units --all --plain --no-legend "${PREFIX}-*.service" 2>/dev/null | awk '{print $1}'; }

run_unit() {  # run_unit <name> <limit-property...> -- <cmd...>
  local name="$1"; shift
  local props=(); while [ "$1" != "--" ]; do props+=(-p "$1"); shift; done; shift
  local cmd=(systemd-run --quiet)
  if [ "$MODE" = system ]; then cmd=(sudo systemd-run --quiet); else cmd+=(--user); fi
  cmd+=(--unit="${PREFIX}-${name}" --collect --property=RuntimeMaxSec="$DURATION"
        --service-type=exec "${props[@]}" -- "$@")
  "${cmd[@]}"
  echo "  started ${PREFIX}-${name}  (${MODE}, auto-stops in ${DURATION}s)"
}

clean() {
  for u in $(units); do
    sc stop "$u" 2>/dev/null || true
    sc reset-failed "$u" 2>/dev/null || true
  done
  rm -f "$THRASH_FILE"
}

start_hog()    { run_unit hog    MemoryMax=2G                                 -- python3 "$DIR/hog.py"    --size-mb 800 --grow-mb 40 --hold "$DURATION"; }
start_thrash() { run_unit thrash MemoryHigh=64M MemoryMax=96M MemorySwapMax=0 -- python3 "$DIR/thrash.py" --path "$THRASH_FILE" --size-mb 1024 --duration "$DURATION"; }
start_burn()   { run_unit burn   CPUQuota=25%                                 -- python3 "$DIR/burn.py"   --workers 2 --duration "$DURATION"; }

case "$CMD" in
  status)
    for u in $(units); do
      printf "  %-22s %s\n" "$u" "$(sc show "$u" -p MemoryCurrent --value 2>/dev/null) bytes"
    done
    exit 0 ;;
  clean)
    clean; echo "cleaned all ${PREFIX}-* units (${MODE})."; exit 0 ;;
  hog|thrash|burn|all)
    clean  # clear any previous run first
    case "$CMD" in
      hog)    start_hog ;;
      thrash) start_thrash ;;
      burn)   start_burn ;;
      all)    start_hog; start_thrash; start_burn ;;
    esac ;;
  *)
    sed -n '2,20p' "$0"; exit 1 ;;
esac

echo
echo "watch: http://127.0.0.1:3000 -> cgroup -> Host Detail   ('./run.sh clean' to stop now)"
