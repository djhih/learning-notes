#!/usr/bin/env bash
# load-test.sh — 在會被 cgroup-exporter 抓到的 cgroup 裡製造異常負載，
# 用來驗證 Grafana dashboard 的每一張 chart 都能正常顯示異常流量。
#
# 為什麼用 systemd-run + 命名 service：
#   exporter 只抓 system.slice/*.service|*.scope（且排除 run-*/session-*）。
#   `systemd-run --scope` 會產生 run-*.scope → 被排除抓不到。
#   所以這裡用 `systemd-run --unit=cgtest-<x>` 產生 system.slice/cgtest-<x>.service。
#
# stressor 全用 coreutils/python3 stdlib，不依賴 stress-ng（符合專案最小依賴原則）。
# 所有壓力都 scope 在各自 cgroup 內（MemoryMax / CPUQuota），不會拖垮整台機。
#
# 用法：
#   sudo bash load-test.sh all                 # 全部情境，預設跑 180s 後自動清掉
#   sudo bash load-test.sh oom                 # 只測 OOM killer
#   sudo bash load-test.sh cpu --duration 300  # 指定秒數
#   sudo bash load-test.sh all --keep          # 跑著不自動清，要自己 clean
#   sudo bash load-test.sh clean               # 停掉並移除所有 cgtest-* 單元
#   sudo bash load-test.sh status              # 看目前 test cgroup 的即時值
#
# 情境 → 會點亮哪張 chart：
#   mem  → Top10 memory / memory.current / Memory pressure
#   oom  → OOM kills（+ Memory pressure 尖峰）
#   cpu  → CPU usage rate / CPU pressure（throttle）
#   io   → IO pressure
#   all  → 以上全部同時
set -euo pipefail

DURATION=180
KEEP=0
IO_PATH="${IO_PATH:-/var/tmp/cgtest-io.tmp}"
PREFIX="cgtest"
SUDO=""; [[ $(id -u) -ne 0 ]] && SUDO="sudo"

# ---- 共用 ----
units() { systemctl list-units --all --plain --no-legend "${PREFIX}-*.service" 2>/dev/null | awk '{print $1}'; }

clean() {
  echo "==> 清理 ${PREFIX}-* 單元..."
  local found=0
  for u in $(units); do
    found=1; echo "    stop $u"; $SUDO systemctl stop "$u" 2>/dev/null || true
    $SUDO systemctl reset-failed "$u" 2>/dev/null || true
  done
  [[ $found -eq 0 ]] && echo "    (沒有殘留單元)"
  $SUDO rm -f "$IO_PATH"
  echo "==> 清理完成。"
}

run_unit() {  # run_unit <name-suffix> <prop1> <prop2> ... -- <cmd...>
  local suffix="$1"; shift
  local unit="${PREFIX}-${suffix}"
  local props=(); while [[ "$1" != "--" ]]; do props+=("-p" "$1"); shift; done; shift
  $SUDO systemctl stop "${unit}.service" 2>/dev/null || true
  $SUDO systemctl reset-failed "${unit}.service" 2>/dev/null || true
  echo "    起 system.slice/${unit}.service  [${props[*]}]"
  $SUDO systemd-run --quiet --unit="$unit" --service-type=exec \
    --collect "${props[@]}" -- "$@"
}

# ---- 各情境 ----
scen_mem() {
  echo "==> [mem] 記憶體佔用 + memory pressure（不殺）"
  # MemoryHigh 以上會觸發回收 → memory.pressure 上升；不到 MemoryMax 不會 OOM。
  run_unit mem MemoryHigh=200M MemoryMax=320M -- \
    /usr/bin/python3 -c 'import time; b=bytearray(260*1024*1024); time.sleep(10**9)'
}

scen_oom() {
  echo "==> [oom] 超過 MemoryMax → cgroup OOM killer 反覆觸發"
  # 無 swap + 不斷成長 → 撞 128M 上限被殺；Restart=always 讓 oom_kill rate 持續可見。
  run_unit oom MemoryMax=128M MemorySwapMax=0 Restart=always RestartSec=3 StartLimitIntervalSec=0 -- \
    /usr/bin/python3 -c 'b=[]
import time
while True:
    b.append(bytearray(16*1024*1024)); time.sleep(0.1)'
}

scen_cpu() {
  echo "==> [cpu] CPUQuota throttle → CPU usage + CPU pressure"
  # 2 條燒 CPU 的工作擠進 20% quota → 大量 throttle、cpu.pressure(some) 上升。
  run_unit cpu CPUQuota=20% -- \
    /bin/bash -c 'for i in 1 2; do sha256sum /dev/zero & done; wait'
}

scen_io() {
  echo "==> [io] 大量 direct IO + fsync → IO pressure"
  run_unit io IOWeight=10 -- \
    /bin/bash -c "while :; do dd if=/dev/zero of='$IO_PATH' bs=1M count=256 oflag=direct conv=fsync status=none 2>/dev/null || dd if=/dev/zero of='$IO_PATH' bs=1M count=256 conv=fsync status=none; sync; done"
}

status() {
  echo "==> 目前 ${PREFIX}-* cgroup 即時值："
  local root=/sys/fs/cgroup/system.slice
  for u in $(units); do
    local d="$root/$u"
    [[ -d "$d" ]] || { printf "    %-22s (cgroup 不存在/已停)\n" "$u"; continue; }
    local mem oom cput cpu_throt
    mem=$(cat "$d/memory.current" 2>/dev/null || echo -)
    oom=$(awk '/oom_kill /{print $2}' "$d/memory.events" 2>/dev/null || echo -)
    cpu_throt=$(awk '/nr_throttled/{print $2}' "$d/cpu.stat" 2>/dev/null || echo -)
    printf "    %-22s mem=%-12s oom_kill=%-5s nr_throttled=%-6s\n" \
      "$u" "$mem" "${oom:--}" "${cpu_throt:--}"
  done
}

precheck() {
  command -v systemd-run >/dev/null || { echo "ERROR: 找不到 systemd-run"; exit 1; }
  [[ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" == "cgroup2fs" ]] || \
    { echo "ERROR: 不是 cgroup v2"; exit 1; }
  # 提醒 exporter 在不在
  if ! curl -sf --max-time 2 http://127.0.0.1:9753/metrics >/dev/null 2>&1; then
    echo "WARN: 127.0.0.1:9753 沒有 exporter 在跑 — chart 不會有資料。"
    echo "      先在另一個 terminal: CGROUP_EXPORTER_LISTEN=0.0.0.0:9753 python3 ../exporter/exporter.py"
  fi
}

usage() { sed -n '2,30p' "$0"; exit "${1:-0}"; }

# ---- 參數 ----
[[ $# -eq 0 ]] && usage 1
CMD="$1"; shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration) DURATION="$2"; shift 2;;
    --keep) KEEP=1; shift;;
    -h|--help) usage 0;;
    *) echo "未知參數: $1"; usage 1;;
  esac
done

case "$CMD" in
  clean) clean; exit 0;;
  status) status; exit 0;;
  -h|--help) usage 0;;
esac

precheck

# 跑情境前先清乾淨，避免殘留
clean >/dev/null 2>&1 || true

case "$CMD" in
  mem) scen_mem;;
  oom) scen_oom;;
  cpu) scen_cpu;;
  io)  scen_io;;
  all) scen_mem; scen_oom; scen_cpu; scen_io;;
  *)   echo "未知情境: $CMD"; usage 1;;
esac

echo
echo "==> 已啟動。去 Grafana 看 cgroup overview，service 篩選 ${PREFIX}-* 應出現異常。"
echo "    對照：mem→memory/PSI, oom→OOM kills, cpu→CPU usage+pressure, io→IO pressure"

if [[ $KEEP -eq 1 ]]; then
  echo "==> --keep：不自動清。看完後執行：sudo bash $0 clean"
  exit 0
fi

echo "==> 跑 ${DURATION}s 後自動清理（Ctrl-C 也會清）。中途看即時值：sudo bash $0 status"
trap 'echo; clean; exit 0' INT TERM
sleep "$DURATION"
clean
