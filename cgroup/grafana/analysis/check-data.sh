#!/usr/bin/env bash
# check-data.sh — 前置檢查(複製出來的 Prometheus TSDB 資料夾)
#
# 你的資料是「直接複製 docker 容器內 /prometheus 資料夾」的原始 TSDB,
# 不能直接查。這支腳本會:
#   1) 用 docker 把這份資料掛成一個「本機分析用 Prometheus」(port 9091,retention 設超大不刪資料)
#   2) 確認分析需要的資料是否齊全:時間範圍、cgroup metric、user 粒度、mem/cpu/io
#   3) 印出可直接複製的 analyze.py 指令
#
# 用法:
#   bash check-data.sh <複製出來的 prometheus 資料夾>
# 停掉分析用 Prometheus:
#   docker rm -f prom-analyze
set -euo pipefail

DATA="${1:?用法: bash check-data.sh <複製出來的 prometheus 資料夾>}"
DATA="$(cd "$DATA" && pwd)"
PORT="${PORT:-9091}"
IMAGE="${IMAGE:-prom/prometheus:v2.55.1}"
NAME="${NAME:-prom-analyze}"
BASE="http://localhost:$PORT"

echo "==> 資料夾: $DATA"

# 0) 看起來像 TSDB 嗎?(該層應直接有 wal/ 或 chunks_head/ 或 01xxx block)
if ! { ls "$DATA"/wal >/dev/null 2>&1 || ls "$DATA"/chunks_head >/dev/null 2>&1 || ls -d "$DATA"/01* >/dev/null 2>&1; }; then
  echo "!! 這不像 Prometheus TSDB。該層應直接有 wal/、chunks_head/ 或 01xxx 目錄。"
  echo "   你要複製的是容器內 --storage.tsdb.path(通常就是 /prometheus)那一層。"
  exit 1
fi

# 1) 啟動分析用 Prometheus
if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "==> 已在跑: $NAME(要換資料夾先 docker rm -f $NAME)"
else
  echo "==> 啟動 $NAME @ $BASE(retention 設超大,避免把你複製來的舊資料刪掉)"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" \
    -p "127.0.0.1:$PORT:9090" \
    -v "$DATA:/prometheus" \
    "$IMAGE" \
    --config.file=/etc/prometheus/prometheus.yml \
    --storage.tsdb.path=/prometheus \
    --storage.tsdb.retention.time=100y \
    --storage.tsdb.retention.size=0 >/dev/null
fi

# 2) 等就緒
echo -n "==> 等待就緒"
ok=0
for _ in $(seq 1 40); do
  curl -sf "$BASE/-/ready" >/dev/null 2>&1 && { ok=1; break; }
  echo -n "."; sleep 1
done
echo
if [ "$ok" != 1 ]; then
  echo "!! 沒起來,log 尾巴:"
  docker logs --tail 30 "$NAME" 2>&1 | sed 's/^/   /'
  echo "   最常見是掛載資料夾權限(container 內 Prometheus 是 uid 65534)。"
  echo "   試: sudo chown -R 65534:65534 \"$DATA\"  後重跑。"
  exit 1
fi

# 3) 檢查內容
BASE="$BASE" LABEL_HINT="${LABEL:-}" python3 - <<'PY'
import json, os, urllib.parse, urllib.request as U
base = os.environ["BASE"]
def api(path, **q):
    url = base + path + ("?" + urllib.parse.urlencode(q) if q else "")
    return json.load(U.urlopen(url, timeout=30))

print("\n===== 1. 時間範圍(head)=====")
maxt = 0
try:
    hs = api("/api/v1/status/tsdb")["data"].get("headStats", {})
    import datetime as dt
    f = lambda ms: (dt.datetime.utcfromtimestamp(ms/1000).isoformat()+"Z") if ms and ms > 0 else "?"
    print("  series 數:", hs.get("numSeries"), " 最早:", f(hs.get("minTime",0)), " 最新:", f(hs.get("maxTime",0)))
    if hs.get("maxTime", 0) > 0: maxt = int(hs["maxTime"]) // 1000
except Exception as e:
    print("  (讀 status/tsdb 失敗:", e, ")")

print("\n===== 2. cgroup metric 清單 =====")
names = [n for n in api("/api/v1/label/__name__/values")["data"] if n.startswith("cgroup_")]
for n in names: print("   ", n)
if not names:
    print("   (沒有任何 cgroup_* metric — 確認你掛對資料夾/資料裡真的有 cgroup 指標)")

print("\n===== 3. 分析所需資料是否齊全 =====")
need = {
    "記憶體用量 (cgroup_memory_current_bytes)": "cgroup_memory_current_bytes" in names,
    "CPU 用量 (cgroup_cpu_usage_usec_total)":   "cgroup_cpu_usage_usec_total" in names,
    "CPU 節流 (cgroup_cpu_throttled_usec_total)": "cgroup_cpu_throttled_usec_total" in names,
    "OOM 事件 (cgroup_memory_events_oom_kill_total)": "cgroup_memory_events_oom_kill_total" in names,
    "memory PSI": any("psi_memory" in n for n in names),
    "IO 用量 (io bytes)": any(("io_" in n and "bytes" in n) for n in names),
    "IO PSI": any("psi_io" in n for n in names),
}
for k, v in need.items():
    print(f"   [{'OK ' if v else '缺 '}] {k}")

print("\n===== 4. user 粒度(label)=====")
cand = None
if "cgroup_memory_current_bytes" in names:
    s = api("/api/v1/series", **{"match[]": "cgroup_memory_current_bytes"})["data"]
    labels = sorted({k for m in s for k in m if k != "__name__"})
    print("   可用 label:", labels)
    for c in (os.environ.get("LABEL_HINT") or "", "service", "id", "cgroup", "name", "unit"):
        if c and c in labels: cand = c; break
    if cand:
        vals = api(f"/api/v1/label/{cand}/values")["data"]
        print(f"   推測 user label = '{cand}',共 {len(vals)} 個值,例:", vals[:8])
        us = any("user" in str(v) for v in vals)
        print("   含 user.slice 粒度:", "有 ✔" if us else "沒有(只有服務/其他;要限『登入使用者』需 exporter 掃 user.slice)")
    else:
        print("   找不到明顯的 user label,跑 analyze.py 時用 --label 指定")

print("\n===== 下一步:直接複製這行跑 =====")
cmd = f"python3 analyze.py --prom {base} --window 15d"
if cand: cmd += f" --label {cand}"
if maxt: cmd += f" --at {maxt}"
print("  ", cmd)
print("   (資料跨越超過 window 就把 --window 調大;--at 已鎖定資料最新時間,因為快照的『現在』通常沒資料)")
PY

echo
echo "==> 完成。分析結束後停掉: docker rm -f $NAME"
